"""wc_checkpoint.py - snapshot the store's products, and put them back.

    .venv\\Scripts\\python.exe wc_checkpoint.py create --label before-seo
    .venv\\Scripts\\python.exe wc_checkpoint.py list
    .venv\\Scripts\\python.exe wc_checkpoint.py diff latest
    .venv\\Scripts\\python.exe wc_checkpoint.py restore latest          # dry run
    .venv\\Scripts\\python.exe wc_checkpoint.py restore latest --commit

A checkpoint is a gzipped JSON copy of every product exactly as the WooCommerce
API returned it. Restoring writes the captured fields back. NOTHING is written
without --commit, and a dry run prints a per-product diff first.

WHAT THIS COVERS
    Products: name, slug, status, catalogue visibility, both descriptions,
    both prices, SKU, categories, tags, attributes, image list, and the full
    meta_data set (which is where every SEO plugin keeps its fields).

WHAT IT DOES NOT COVER - read this before relying on it
    * Media LIBRARY FILES. Image URLs are restored; the uploaded files are not.
      Deleting a file from the Media Library is not undone by a restore.
    * Orders, customers, coupons, reviews.
    * Pages, blog posts, menus, widgets.
    * Plugin and theme settings, and the plugin set itself.
    * Anything a plugin stores outside product meta.
    * Products CREATED after the checkpoint. They are reported, never deleted -
      deleting a product a human added is not a thing a rollback should decide
      to do on its own.
    * Products DELETED after the checkpoint. Reported; recreating them would
      mint new IDs and break every link, so it is deliberately not attempted.

So this is a product-state checkpoint, not a site backup. For a site backup use
the host's own snapshot feature. The two answer different questions: this one
answers "undo what the pipeline just did to my products".
"""

import argparse
import gzip
import json
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

import wc_publish as W                       # noqa: E402  session, auth, challenge

CHECKPOINT_DIR = Path(__file__).resolve().parent / "checkpoints"
PAGE_SIZE = 100
# Below this a capture is almost certainly truncated rather than a small store.
PARTIAL_THRESHOLD = 25

# The fields a restore puts back. Everything else the API returns is either
# read-only (permalink, date_created), derived (price_html), or a relationship
# better left alone (related_ids, variations).
RESTORABLE = ("name", "slug", "status", "catalog_visibility", "description",
              "short_description", "regular_price", "sale_price", "sku",
              "categories", "tags", "attributes", "images", "meta_data")

# Meta keys that are bookkeeping rather than content. Restoring these fights
# the plugins that own them and achieves nothing.
META_SKIP_PREFIXES = ("_edit_lock", "_edit_last", "_wp_old_slug",
                      "rank_math_internal_links_processed",
                      "_elementor_", "_wc_gla_", "_oembed_")


def is_restorable_meta(key):
    return bool(key) and not str(key).startswith(META_SKIP_PREFIXES)


# ------------------------------- storage ---------------------------------

def checkpoint_path(name):
    return CHECKPOINT_DIR / f"{name}.json.gz"


def save_checkpoint(name, payload):
    CHECKPOINT_DIR.mkdir(exist_ok=True)
    path = checkpoint_path(name)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


def load_checkpoint(name):
    if name == "latest":
        found = sorted(CHECKPOINT_DIR.glob("*.json.gz"))
        if not found:
            sys.exit(f"No checkpoints in {CHECKPOINT_DIR}. Make one with: "
                     f"wc_checkpoint.py create")
        path = found[-1]
    else:
        path = checkpoint_path(name)
        if not path.exists():
            sys.exit(f"No such checkpoint: {path}")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return path, json.load(fh)


def list_checkpoints():
    if not CHECKPOINT_DIR.exists():
        print(f"No checkpoints yet ({CHECKPOINT_DIR} does not exist).")
        return
    found = sorted(CHECKPOINT_DIR.glob("*.json.gz"))
    if not found:
        print(f"No checkpoints in {CHECKPOINT_DIR}.")
        return
    print(f"{len(found)} checkpoint(s) in {CHECKPOINT_DIR}:\n")
    for path in found:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                data = json.load(fh)
            n = len(data.get("products", []))
            when = data.get("created_at", "?")
            label = data.get("label", "")
            size = path.stat().st_size / 1_048_576
            flag = "  <-- PARTIAL, not a full restore point" if n < PARTIAL_THRESHOLD else ""
            print(f"  {path.stem}{flag}")
            print(f"    {n} product(s), {when}, {size:.1f} MB"
                  + (f", label: {label}" if label else ""))
        except Exception as exc:
            print(f"  {path.stem}  (unreadable: {exc})")
    print("\n'latest' always refers to the newest of these.")


# ------------------------------- capture ---------------------------------

def fetch_all_products(store):
    products, page = [], 1
    while True:
        batch = store.call("GET", "products",
                           params={"per_page": PAGE_SIZE, "page": page,
                                   "status": "any", "orderby": "id",
                                   "order": "asc"})
        if not batch:
            break
        products.extend(batch)
        print(f"  captured {len(products)} product(s)...")
        if len(batch) < PAGE_SIZE:
            break
        page += 1
    return products


def create(store, label):
    print(f"Capturing products from {store.base}")
    products = fetch_all_products(store)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{label}" if label else stamp
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "store": store.base,
        "label": label,
        "product_count": len(products),
        "products": products,
    }
    path = save_checkpoint(name, payload)
    print(f"\nCheckpoint '{name}'")
    print(f"  {len(products)} product(s) -> {path} "
          f"({path.stat().st_size / 1_048_576:.1f} MB)")
    if len(products) < PARTIAL_THRESHOLD:
        # A capture that stops early - a dropped connection, a filtered key -
        # still writes a perfectly valid file, and a file named 'before-seo'
        # looks like a full restore point whatever is inside it. Say so loudly:
        # restoring a 3-product checkpoint believing it covers the catalogue is
        # a mistake you only find out about afterwards.
        print(f"\n  !! WARNING: only {len(products)} product(s) were captured.")
        print(f"     That is below {PARTIAL_THRESHOLD}, so this is very likely "
              f"a PARTIAL capture, not a full one.")
        print(f"     Restoring it would only touch those {len(products)} "
              f"product(s). Check the store's product count and recapture.")
    print(f"\nRestore it later with:")
    print(f"  .venv\\Scripts\\python.exe wc_checkpoint.py restore {name}")
    print(f"  (add --commit to actually write; without it you get a diff)")
    return name


# -------------------------------- compare --------------------------------

def meta_map(product):
    return {m.get("key"): m.get("value")
            for m in product.get("meta_data", []) or []
            if is_restorable_meta(m.get("key"))}


def scalar(value):
    """Comparable form. Lists of dicts (categories, tags, attributes, images)
    are reduced to the identity that matters, so a reordered-but-equivalent
    list does not read as a change."""
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, dict):
                key = item.get("id", item.get("name", item.get("src", "")))
                out.append(str(key))
            else:
                out.append(str(item))
        return sorted(out)
    return value


def diff_product(old, live):
    """{field: (old, live)} for the fields a restore would change."""
    changes = {}
    for field in RESTORABLE:
        if field == "meta_data":
            continue
        before, after = scalar(old.get(field)), scalar(live.get(field))
        if before != after:
            changes[field] = (old.get(field), live.get(field))

    old_meta, live_meta = meta_map(old), meta_map(live)
    changed_meta = {}
    for key in set(old_meta) | set(live_meta):
        if old_meta.get(key) != live_meta.get(key):
            changed_meta[key] = (old_meta.get(key), live_meta.get(key))
    if changed_meta:
        changes["meta_data"] = changed_meta
    return changes


def show_diff(checkpoint, live_products, limit=25):
    """What has changed on the store since the checkpoint."""
    old_by_id = {p["id"]: p for p in checkpoint["products"]}
    live_by_id = {p["id"]: p for p in live_products}

    added = [i for i in live_by_id if i not in old_by_id]
    removed = [i for i in old_by_id if i not in live_by_id]
    changed = []
    for pid, old in old_by_id.items():
        live = live_by_id.get(pid)
        if live:
            d = diff_product(old, live)
            if d:
                changed.append((pid, old, d))

    print(f"\nAgainst checkpoint of {checkpoint.get('created_at', '?')}:")
    print(f"  {len(changed)} product(s) changed")
    print(f"  {len(added)} created since (a restore leaves these alone)")
    print(f"  {len(removed)} deleted since (a restore cannot bring these back)")

    for pid, old, d in changed[:limit]:
        print(f"\n  id {pid}  {str(old.get('name'))[:60]}")
        for field, value in d.items():
            if field == "meta_data":
                for key, (before, after) in list(value.items())[:6]:
                    print(f"    meta {key}")
                    print(f"      now : {str(after)[:80]}")
                    print(f"      was : {str(before)[:80]}")
                if len(value) > 6:
                    print(f"    ...and {len(value) - 6} more meta key(s)")
            else:
                print(f"    {field}")
                print(f"      now : {str(value[1])[:80]}")
                print(f"      was : {str(value[0])[:80]}")
    if len(changed) > limit:
        print(f"\n  ...and {len(changed) - limit} more changed product(s)")
    return changed, added, removed


# -------------------------------- restore --------------------------------

def restore_payload(old, live, fields):
    """What to PUT to make `live` look like `old` again."""
    payload = {}
    for field in fields:
        if field == "meta_data":
            continue
        if field in old:
            payload[field] = old[field]

    if "meta_data" in fields:
        old_meta, live_meta = meta_map(old), meta_map(live)
        entries = []
        for key, value in old_meta.items():
            if live_meta.get(key) != value:
                entries.append({"key": key, "value": value})
        # keys that did not exist at checkpoint time but do now: blank them.
        # Blanking rather than deleting because an SEO plugin treats an empty
        # field as "not set" and falls back to its default, which is exactly
        # the pre-checkpoint behaviour - and because a delete needs the meta's
        # id, which is not stable across a restore.
        for key in live_meta:
            if key not in old_meta:
                entries.append({"key": key, "value": ""})
        if entries:
            payload["meta_data"] = entries
    return payload


def restore(store, checkpoint, live_products, fields, only_skus, commit, limit=0):
    old_by_id = {p["id"]: p for p in checkpoint["products"]}
    live_by_id = {p["id"]: p for p in live_products}

    targets = []
    for pid, old in old_by_id.items():
        live = live_by_id.get(pid)
        if not live:
            continue
        if only_skus:
            sku = str(old.get("sku") or "").strip().upper()
            if sku not in only_skus:
                continue
        if diff_product(old, live):
            targets.append((pid, old, live))
    if limit:
        targets = targets[:limit]

    print(f"\n{'RESTORING' if commit else 'DRY RUN'}: {len(targets)} product(s) "
          f"differ from the checkpoint")
    print(f"  fields: {', '.join(fields)}")
    if not commit:
        print("  nothing will be written\n")

    done = failed = 0
    for pid, old, live in targets:
        name = str(old.get("name"))[:56]
        payload = restore_payload(old, live, fields)
        if not payload:
            continue
        print(f"  id {pid}  {name}")
        for field in payload:
            if field == "meta_data":
                print(f"    meta_data: {len(payload['meta_data'])} key(s) reset")
            else:
                print(f"    {field} -> {str(payload[field])[:70]}")
        if not commit:
            continue
        try:
            store.call("PUT", f"products/{pid}", json=payload)
            done += 1
            print("    -> restored")
        except W.ChallengeError:
            raise
        except Exception as exc:
            failed += 1
            print(f"    !! {exc}")

    if commit:
        print(f"\nDone: {done} restored, {failed} failed.")
    else:
        print(f"\nDry run complete - nothing was written. "
              f"Re-run with --commit to apply.")


# ---------------------------------- cli ----------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Snapshot WooCommerce products and roll them back. "
                    "Restores are dry runs unless --commit.")
    ap.add_argument("action", choices=["create", "list", "diff", "restore"])
    ap.add_argument("name", nargs="?", default="latest",
                    help="Checkpoint name, or 'latest' (default)")
    ap.add_argument("--label", default="",
                    help="Short label for a new checkpoint, e.g. before-seo")
    ap.add_argument("--commit", action="store_true",
                    help="Actually write during a restore")
    ap.add_argument("--fields", default="meta_data",
                    help="Comma-separated fields to restore. Default "
                         "'meta_data' - the SEO fields and nothing else. "
                         "Use 'all' for every restorable field.")
    ap.add_argument("--sku", action="append", default=[],
                    help="Restore only this SKU (repeatable)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Touch at most N products (use to rehearse)")
    ap.add_argument("--cookie-file", metavar="PATH")
    ap.add_argument("--cookie", metavar="VALUE")
    ap.add_argument("--user-agent", metavar="UA")
    args = ap.parse_args()

    if args.action == "list":
        list_checkpoints()
        return

    using, dropped = W.configure_session(
        W.read_cookie_arg(args.cookie, args.cookie_file), args.user_agent or "")
    if using:
        print("Using a borrowed browser cookie for the edge challenge.")
    store = W.Store(allow_writes=args.commit)

    if args.action == "create":
        create(store, args.label)
        return

    path, checkpoint = load_checkpoint(args.name)
    print(f"Checkpoint: {path.stem}  "
          f"({checkpoint.get('product_count', '?')} products, "
          f"{checkpoint.get('created_at', '?')})")
    print("Reading current store state...")
    live = fetch_all_products(store)

    if args.action == "diff":
        show_diff(checkpoint, live)
        return

    fields = (list(RESTORABLE) if args.fields.strip().lower() == "all"
              else [f.strip() for f in args.fields.split(",") if f.strip()])
    unknown = [f for f in fields if f not in RESTORABLE]
    if unknown:
        sys.exit(f"Not restorable: {', '.join(unknown)}\n"
                 f"Choose from: {', '.join(RESTORABLE)}")
    restore(store, checkpoint, live, fields,
            {s.strip().upper() for s in args.sku}, args.commit, args.limit)


if __name__ == "__main__":
    try:
        main()
    except W.ChallengeError as exc:
        sys.exit(str(exc))
    except RuntimeError as exc:
        sys.exit(str(exc))
