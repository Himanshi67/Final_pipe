"""wc_seo.py - back-fill SEO meta on products ALREADY LIVE in WooCommerce.

`build_csv.py --seo` covers products this pipeline is about to publish. This
covers the ones already on the storefront, including any that predate the
pipeline: it reads each product back over the API, recomputes the meta from the
product's OWN live data (name, categories, attributes, prices) with the same
deterministic functions app.py uses, and writes back only what is missing.

    .venv\\Scripts\\python.exe wc_seo.py                       # DRY RUN, all products
    .venv\\Scripts\\python.exe wc_seo.py --sku AP-XYZ          # one product
    .venv\\Scripts\\python.exe wc_seo.py --commit              # writes
    .venv\\Scripts\\python.exe wc_seo.py --overwrite --commit  # replace existing meta too

NOTHING is written without --commit.

Two deliberate safety defaults, both of which protect work a human did:

  * only products whose meta is EMPTY are touched. Meta somebody wrote by hand
    is better than meta a script derived, so `--overwrite` is opt-in.
  * the SLUG IS NEVER CHANGED unless you pass --slugs. Changing the slug of a
    live product changes its URL: existing links break, and whatever search
    ranking that URL had is thrown away. On a new product it is free; on a live
    one it is a decision, not a detail.

Credentials and the CDN-challenge cookie work exactly as in wc_publish.py -
this imports that module rather than reimplementing any of it.
"""

import argparse
import html
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

import app                                  # noqa: E402  deterministic SEO text
import wc_publish as W                      # noqa: E402  session, auth, challenge

PAGE_SIZE = 100


def unescape_text(value):
    """WordPress returns names HTML-escaped, so a category really does arrive
    as 'Geyser &amp; Accessories'. Left alone that lands in a focus keyword as
    'geyser &amp; accessories', which is not a phrase anyone types."""
    return app.compact(html.unescape(str(value or "")))


def leaf_category(product):
    """WooCommerce returns every category a product is in, flat. The most
    specific one is what the copy should name, and the deepest slug is the best
    available proxy for that when the API does not say which is a child."""
    names = [unescape_text(c.get("name", ""))
             for c in product.get("categories", []) if c.get("name")]
    names = [n for n in names if n]
    if not names:
        return ""
    return max(names, key=len)


def spec_rows_from(product):
    """Live attributes in the (label, value) shape app.seo_description wants."""
    rows = []
    for attr in product.get("attributes", []):
        name = app.compact(attr.get("name"))
        options = [app.compact(o) for o in attr.get("options", []) if app.compact(o)]
        if name and options:
            rows.append((name, ", ".join(options)))
    return rows


def meta_text(value):
    """A meta value as text, or '' if it is not text at all.

    WooCommerce meta values are arbitrary JSON: other plugins store lists and
    dicts in there (`_elementor_page_assets` is an empty list,
    `_wc_gla_errors` is an object). app.compact() runs pd.isna() on whatever it
    is given, and pd.isna([]) returns an empty ARRAY rather than a bool, so
    `if pd.isna(value)` raises 'truth value of an empty array is ambiguous'.
    That took out 335 of 1,279 live products - 26% - before this guard.

    Nothing that is not a scalar can be an SEO title anyway, so a non-scalar is
    simply not text."""
    if isinstance(value, (list, dict, tuple, set)):
        return ""
    return app.compact(value)


def brand_of(product):
    for meta in product.get("meta_data", []):
        if meta.get("key") == "_brand" and meta_text(meta.get("value")):
            return meta_text(meta.get("value"))
    for attr in product.get("attributes", []):
        if app.compact(attr.get("name")).lower() == "brand":
            options = [app.compact(o) for o in attr.get("options", []) if app.compact(o)]
            if options:
                return options[0]
    return app.infer_brand(product.get("name", ""), product.get("sku", ""))


def existing_meta(product):
    return {m.get("key"): meta_text(m.get("value"))
            for m in product.get("meta_data", []) if m.get("key")}


# A model code inside a product name. The trailing (?!\.\d) stops it biting a
# decimal in half: 'VECTRA PRISM1.25 Ton' must not yield the model 'PRISM1'.
MODEL_IN_NAME_RE = re.compile(r"\b(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])"
                              r"[A-Za-z][A-Za-z0-9-]{3,}\b(?!\.\d)")


def identifier_for(product):
    """The SKU, or a model code recovered from the product name.

    Plenty of live products carry no SKU at all. app.seo_focus_keyword falls
    back to brand + category for those, which on this catalogue produced the
    focus keyword 'air conditioner' for EVERY air conditioner - hundreds of
    products competing with each other for one phrase, which is worse than no
    keyword. Pulling the model code out of the name ('BLUESTAR BI IC318DNU
    (INV)1.5 Ton 3 Star AC' -> 'IC318DNU') gives each one a keyword a shopper
    would actually type."""
    sku = app.compact(product.get("sku"))
    if sku:
        return sku
    name = app.compact(product.get("name"))
    # skip tokens that are a measurement rather than a model code
    for token in MODEL_IN_NAME_RE.findall(name):
        if re.fullmatch(r"\d+(?:kg|ltr?|w|v|hz|ton|star|inch|cm|mm)", token, re.I):
            continue
        return token
    return ""


# the measurements that actually distinguish two otherwise identical listings
SPEC_TOKEN_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ton|kg|kgs|l|ltr|litre|liters?|star|inch|inches|"
    r"cm|mm|w|watt|watts|v)\b", re.I)


def distinct_focus_keyword(brand, sku, leaf, name):
    """A focus keyword that is not shared with 66 other products.

    app.seo_focus_keyword gives brand + model when a model code exists, and
    brand + category when it does not. The second case is fine for one product
    and useless for a catalogue: measured on the live store, the plain fallback
    produced 'voltas air conditioner' for 67 products and 'usha electric iron'
    for 32. Products competing with each other for one phrase is the thing a
    focus keyword is supposed to prevent.

    So when the fallback would fire, the distinguishing measurements from the
    product's own name are folded in - 'voltas 1.5 ton 3 star air conditioner'
    rather than 'voltas air conditioner'."""
    base = app.seo_focus_keyword(brand, sku, leaf)
    if sku:                       # a real model code is already distinct
        return base
    specs = []
    for token in SPEC_TOKEN_RE.findall(name):
        token = app.compact(token).lower()
        if token and token not in specs:
            specs.append(token)
    if not specs:
        return base
    lead = app.compact(brand).lower()
    if lead == "authorized brand":
        lead = ""
    parts = [p for p in ([lead] + specs[:2] + [app.compact(leaf).lower()]) if p]
    return " ".join(parts)[:80]


def desired_meta(product, plugin):
    """What this product's SEO meta should be, derived from its own live data."""
    name = app.compact(product.get("name"))
    sku = identifier_for(product)
    brand = brand_of(product)
    leaf = leaf_category(product)
    specs = spec_rows_from(product)
    regular = product.get("regular_price") or ""
    sale = product.get("sale_price") or ""
    # WooCommerce keeps the struck-through price in regular_price and the
    # charged one in sale_price; with no sale, regular IS what you pay
    charged = sale or regular
    struck = regular if sale else ""

    title = app.seo_title(name, leaf)
    description = app.seo_description(name, brand, leaf, specs, struck, charged)
    focus = distinct_focus_keyword(brand, sku, leaf, name)

    out = {}
    for keys in app.seo_plugin_keys(plugin):
        out[keys["title"]] = title
        out[keys["description"]] = description
        out[keys["focus"]] = focus
    # app.seo_columns() names them with the CSV importer's 'Meta: ' prefix;
    # the API wants the bare key
    return {k[len("Meta: "):] if k.startswith("Meta: ") else k: v
            for k, v in out.items()}


def plan_for(product, plugin, overwrite, want_slug):
    """(meta entries to write, slug or None, notes). Empty means nothing to do."""
    have = existing_meta(product)
    want = desired_meta(product, plugin)

    entries, kept = [], []
    for key, value in want.items():
        if not value:
            continue
        current = have.get(key, "")
        if current and not overwrite:
            kept.append(key)
            continue
        if current == value:
            continue                        # already exactly right
        entries.append({"key": key, "value": value})

    slug = None
    if want_slug:
        candidate = app.seo_slug(app.compact(product.get("sku")),
                                 app.compact(product.get("name")))
        if candidate and candidate != app.compact(product.get("slug")):
            slug = candidate

    notes = []
    if kept:
        notes.append(f"kept {len(kept)} hand-written field(s) - --overwrite replaces them")
    return entries, slug, notes


def fetch_products(store, skus, limit):
    """Every product, or just the named SKUs."""
    if skus:
        found = []
        for sku in skus:
            hit = store.by_sku(sku)
            if hit:
                found.append(hit)
            else:
                print(f"  [skip] no product with SKU {sku}")
        return found

    products, page = [], 1
    while True:
        batch = store.call("GET", "products",
                           params={"per_page": PAGE_SIZE, "page": page,
                                   "status": "any", "orderby": "id",
                                   "order": "asc"})
        if not batch:
            break
        products.extend(batch)
        print(f"  read {len(products)} product(s)...")
        if len(batch) < PAGE_SIZE or (limit and len(products) >= limit):
            break
        page += 1
    return products[:limit] if limit else products


# A focus keyword this many products share is worse than none: they compete
# with each other for the phrase instead of ranking for it.
MAX_SHARED_FOCUS = 3


def crowded_focus_keywords(plans):
    """Focus keywords claimed by more than MAX_SHARED_FOCUS products.

    Some of this catalogue has no SKU and near-identical names, so even after
    folding in the distinguishing measurements a phrase can still land on
    dozens of products. Setting it on all of them is actively harmful, and no
    amount of name-parsing fixes data that genuinely does not distinguish the
    products - so those keywords are dropped and the title and description,
    which are per-product and correct, are written on their own."""
    counts = {}
    for _product, entries, _slug, _notes in plans:
        for entry in entries:
            if entry["key"].endswith(("focuskw", "focus_keyword")) and entry["value"]:
                counts[entry["value"]] = counts.get(entry["value"], 0) + 1
    return {kw for kw, n in counts.items() if n > MAX_SHARED_FOCUS}



def use_utf8_stdout():
    """A Windows console is cp1252, and scraped titles/SKUs are not. Without
    this a single unmappable character aborts the run with a UnicodeEncodeError
    traceback - mid-batch, after work has already been done. sku_enricher.py
    has always done this; the rest of the entry points had not."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

def main():
    use_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="Back-fill SEO meta on live WooCommerce products. "
                    "Dry run unless --commit.")
    ap.add_argument("--commit", action="store_true",
                    help="Actually write. Without this nothing is sent.")
    ap.add_argument("--sku", action="append", default=[], metavar="SKU",
                    help="Only this SKU (repeatable)")
    ap.add_argument("--limit", type=int, default=0,
                    help="At most N products (use for a first look)")
    ap.add_argument("--plugin", default="yoast",
                    choices=["yoast", "rankmath", "both"],
                    help="Which plugin's meta keys to write (default: yoast)")
    ap.add_argument("--overwrite", action="store_true",
                    help="Replace meta that already has a value. Off by "
                         "default: hand-written meta beats derived meta.")
    ap.add_argument("--slugs", action="store_true",
                    help="Also rewrite product slugs. OFF by default because "
                         "this CHANGES LIVE URLS - old links break and the "
                         "ranking that URL had is lost.")
    ap.add_argument("--report", metavar="PATH", default="seo_changes.csv",
                    help="Where to write the per-product diff "
                         "(default: seo_changes.csv)")
    ap.add_argument("--no-checkpoint", action="store_true",
                    help="Skip the automatic pre-write checkpoint. Only do "
                         "this if you have another way back.")
    ap.add_argument("--checkpoint-label", default="",
                    help="Label for the automatic checkpoint "
                         "(default: before-seo)")
    ap.add_argument("--cookie-file", metavar="PATH",
                    help="File holding your browser's Cookie header, for a "
                         "store behind a CDN JS challenge")
    ap.add_argument("--cookie", metavar="VALUE", help="The Cookie header itself")
    ap.add_argument("--user-agent", metavar="UA",
                    help="Must match the browser the cookie came from")
    args = ap.parse_args()

    using_cookie, dropped = W.configure_session(
        W.read_cookie_arg(args.cookie, args.cookie_file), args.user_agent or "")
    if using_cookie:
        print("Using a borrowed browser cookie for the edge challenge.")
        if dropped:
            print(f"  Dropped {len(dropped)} WordPress session cookie(s) so the "
                  f"API key is what authenticates.")

    store = W.Store(allow_writes=args.commit)
    print(f"{'WRITING' if args.commit else 'DRY RUN'}: SEO meta ({args.plugin}) "
          f"-> {store.base}")
    print(f"  existing meta: {'REPLACED' if args.overwrite else 'kept as-is'}"
          f"   slugs: {'REWRITTEN (live URLs change)' if args.slugs else 'left alone'}\n")

    products = fetch_products(store, args.sku, args.limit)
    print(f"\n{len(products)} product(s) to consider\n")

    # A checkpoint BEFORE the first write, so there is always a way back.
    # Learned the hard way: the first live run of this script modified 1,279
    # products with no rollback, because taking one was a separate command
    # nobody had a reason to run first. A safety net you have to remember is
    # not a safety net.
    if args.commit and not args.no_checkpoint:
        try:
            import wc_checkpoint
            name = wc_checkpoint.create(store, args.checkpoint_label or "before-seo")
            print(f"\n  Roll back with:  .venv\\Scripts\\python.exe "
                  f"wc_checkpoint.py restore {name} --commit\n")
        except Exception as exc:
            sys.exit(f"Could not take a checkpoint ({type(exc).__name__}: {exc}).\n"
                     f"Refusing to write without one. Use --no-checkpoint to "
                     f"override, but then nothing can undo this run.")

    # Pass 1: work out what every product wants, so duplicate focus keywords
    # can be spotted before anything is written.
    plans, failed = [], 0
    for product in products:
        try:
            plans.append((product,) + plan_for(product, args.plugin,
                                               args.overwrite, args.slugs))
        except W.ChallengeError:
            raise
        except Exception as exc:
            failed += 1
            print(f"  !! {app.compact(product.get('name'))[:50]}: {exc}")
    drop_focus = crowded_focus_keywords(plans)
    if drop_focus:
        print(f"  {len(drop_focus)} focus keyword(s) were shared by too many "
              f"products and have been dropped rather than set - a keyword "
              f"{MAX_SHARED_FOCUS + 1}+ products compete for helps none of them.\n")

    rows = []
    changed = skipped = 0
    for product, entries, slug, notes in plans:
        sku = app.compact(product.get("sku")) or f"id:{product.get('id')}"
        name = app.compact(product.get("name"))
        try:
            entries = [e for e in entries
                       if not (e["key"].endswith(("focuskw", "focus_keyword"))
                               and e["value"] in drop_focus)]
            if not entries and not slug:
                skipped += 1
                continue
            changed += 1
            print(f"  {sku}  {name[:58]}")
            for entry in entries:
                print(f"    {entry['key']}")
                print(f"      -> {entry['value'][:120]}")
            if slug:
                print(f"    slug: {app.compact(product.get('slug'))} -> {slug}")
            for note in notes:
                print(f"    [note] {note}")

            rows.append({
                "sku": sku, "id": product.get("id"), "name": name,
                "fields": ", ".join(e["key"] for e in entries),
                "meta_title": next((e["value"] for e in entries
                                    if "title" in e["key"]), ""),
                "meta_description": next((e["value"] for e in entries
                                          if "desc" in e["key"]), ""),
                "focus_keyword": next((e["value"] for e in entries
                                       if "focus" in e["key"]), ""),
                "slug_change": slug or "",
            })

            if not args.commit:
                continue
            payload = {}
            if entries:
                payload["meta_data"] = entries
            if slug:
                payload["slug"] = slug
            # meta_data is upserted by key and every other field is omitted, so
            # this touches nothing else about the product
            store.call("PUT", f"products/{product['id']}", json=payload)
            print("    -> written")
        except W.ChallengeError:
            raise                           # no request will succeed; stop now
        except Exception as exc:
            failed += 1
            print(f"    !! {exc}")
            continue

    if rows:
        try:
            import pandas as pd
            pd.DataFrame(rows).to_csv(args.report, index=False)
            print(f"\nWrote {args.report} ({len(rows)} product(s))")
        except Exception as exc:
            print(f"\n[warn] could not write {args.report}: {exc}")

    print(f"\n{'Done' if args.commit else 'Dry run complete'}: "
          f"{changed} product(s) to update, {skipped} already fine, {failed} failed.")
    if not args.commit and changed:
        print("Nothing was written. Re-run with --commit to apply.")


if __name__ == "__main__":
    try:
        main()
    except W.ChallengeError as exc:
        sys.exit(str(exc))
    except RuntimeError as exc:
        sys.exit(str(exc))
