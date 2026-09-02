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

import client_config                         # noqa: E402
import wc_publish as W                       # noqa: E402  session, auth, challenge

# One client's snapshots never sit next to another's: a restore is chosen by
# name from a listing, and two stores in one folder is how the wrong catalogue
# gets written back (PRODUCT_PLAN Phase 1 item 3).
CHECKPOINT_ROOT = Path(__file__).resolve().parent / "checkpoints"
# The directory the module is CURRENTLY reading and writing. main() and
# create() point it at checkpoint_dir(cfg); it starts at the legacy root so an
# importer that never configures still finds the pre-split files.
CHECKPOINT_DIR = CHECKPOINT_ROOT
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

def checkpoint_dir(cfg):
    """<repo>/checkpoints/<slug>/ for a ClientConfig (or a bare slug)."""
    slug = str(getattr(cfg, "slug", cfg) or "").strip()
    return CHECKPOINT_ROOT / slug if slug else CHECKPOINT_ROOT


def resolve_client(store=None, client=None):
    """Whose checkpoints these are: an explicit client wins, else the client
    whose store.url IS the store being captured, else whatever --client /
    $PIPELINE_CLIENT resolved to. Returns None when no config loads at all,
    which keeps the legacy unscoped folder working."""
    if client is not None:
        return client
    host = client_config.store_host(getattr(store, "base", "") or "")
    if host:
        for slug in client_config.available_clients():
            try:
                cfg = client_config.load_client(slug)
            except client_config.ClientConfigError:
                continue
            if client_config.store_host(cfg.store_url) == host:
                return cfg
    cfg = getattr(W, "CLIENT", None)
    if cfg is not None:
        return cfg
    try:
        return client_config.load_client()
    except client_config.ClientConfigError:
        return None


def configure(cfg):
    """Point the module at one client's checkpoint folder."""
    global CHECKPOINT_DIR
    CHECKPOINT_DIR = checkpoint_dir(cfg) if cfg is not None else CHECKPOINT_ROOT
    return CHECKPOINT_DIR


def checkpoint_path(name, directory=None):
    return (directory or CHECKPOINT_DIR) / f"{name}.json.gz"


def save_checkpoint(name, payload, directory=None):
    directory = directory or CHECKPOINT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = checkpoint_path(name, directory)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


def legacy_files():
    """Snapshots taken before checkpoints/ was split per client. They are
    still restorable, so they are still listed - just not silently mixed in
    with a named client's."""
    if CHECKPOINT_DIR == CHECKPOINT_ROOT or not CHECKPOINT_ROOT.exists():
        return []
    return sorted(CHECKPOINT_ROOT.glob("*.json.gz"))


def load_checkpoint(name):
    if name == "latest":
        found = sorted(CHECKPOINT_DIR.glob("*.json.gz")) or legacy_files()
        if not found:
            sys.exit(f"No checkpoints in {CHECKPOINT_DIR}. Make one with: "
                     f"wc_checkpoint.py create")
        path = found[-1]
    else:
        path = checkpoint_path(name)
        if not path.exists():
            # a name from before the per-client split still resolves
            legacy = checkpoint_path(name, CHECKPOINT_ROOT)
            if legacy.exists():
                path = legacy
            else:
                sys.exit(f"No such checkpoint: {path}")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return path, json.load(fh)


def _print_entry(path):
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


def list_checkpoints():
    found = sorted(CHECKPOINT_DIR.glob("*.json.gz")) if CHECKPOINT_DIR.exists() else []
    if found:
        print(f"{len(found)} checkpoint(s) in {CHECKPOINT_DIR}:\n")
        for path in found:
            _print_entry(path)
    else:
        print(f"No checkpoints in {CHECKPOINT_DIR}.")
    legacy = legacy_files()
    if legacy:
        print(f"\n{len(legacy)} checkpoint(s) in {CHECKPOINT_ROOT} "
              f"(unscoped - taken before checkpoints were split per client, "
              f"so which store they hold is only recorded inside the file):\n")
        for path in legacy:
            _print_entry(path)
    if found or legacy:
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


def create(store, label="", client=None):
    """Capture every product into checkpoints/<slug>/.

    `client` is how wc_publish/wc_seo say whose store this is; without one the
    client is inferred from the store URL, then from --client / the env."""
    cfg = resolve_client(store, client)
    directory = checkpoint_dir(cfg)
    print(f"Capturing products from {store.base}")
    products = fetch_all_products(store)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{label}" if label else stamp
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "store": store.base,
        "client": getattr(cfg, "slug", ""),
        "label": label,
        "product_count": len(products),
        "products": products,
    }
    path = save_checkpoint(name, payload, directory)
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


def checkpoint_store_mismatch(checkpoint, store_url, cfg=None):
    """Why this snapshot must not be written into `store_url`, else "".

    A restore is chosen by NAME from a listing, and 'latest' falls back to the
    unscoped legacy folder when a client has no snapshots of its own - so the
    file that comes back can easily be another store's catalogue. create()
    stamps every snapshot with the store URL and client slug it came from
    (PRODUCT_PLAN Phase 1 item 3); this is the check that reads them back.
    A snapshot with no stamp at all predates that, so it cannot be cleared
    either - it is refused the same way, with a different reason."""
    recorded_store = str(checkpoint.get("store") or "").strip()
    recorded_client = str(checkpoint.get("client") or "").strip()
    target = str(store_url or "").strip()
    slug = str(getattr(cfg, "slug", "") or "").strip()
    target_host = client_config.store_host(target) or target or "(no WC_URL)"

    if not recorded_store:
        return (f"this snapshot predates store stamping - it records no "
                f"'store', so nothing proves it was taken from {target_host}")
    if not client_config.same_store(recorded_store, target):
        return (f"this snapshot was taken from "
                f"{client_config.store_host(recorded_store)} but the store "
                f"being written is {target_host}")
    if recorded_client and slug and recorded_client != slug:
        return (f"this snapshot belongs to client '{recorded_client}' but the "
                f"resolved client is '{slug}'")
    return ""


def restore(store, checkpoint, live_products, fields, only_skus, commit,
            limit=0, force_store=False, client=None):
    # Belt and braces: main() checks this before it ever gets here, but
    # restore() is also called directly (wc_seo, tests, a future caller) and a
    # cross-store write is the one mistake with no way back.
    cfg = client if client is not None else getattr(W, "CLIENT", None)
    mismatch = checkpoint_store_mismatch(checkpoint, getattr(store, "base", ""),
                                         cfg)
    if mismatch and not force_store:
        print(f"\nREFUSED: {mismatch}.")
        print(f"  Nothing was written. Pass --force-store only if you really "
              f"mean to write this snapshot into that store. "
              f"Exit code {W.EXIT_REFUSED}.")
        sys.exit(W.EXIT_REFUSED)
    if mismatch:
        print(f"\n  !! --force-store: {mismatch}.")

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
    ap.add_argument("--force-store", action="store_true",
                    help="Restore a snapshot whose recorded store/client is "
                         "not the one being written (or that records none at "
                         "all, i.e. predates store stamping). Without this a "
                         "cross-store restore is REFUSED and nothing is "
                         "written.")
    ap.add_argument("--cookie-file", metavar="PATH")
    ap.add_argument("--cookie", metavar="VALUE")
    ap.add_argument("--user-agent", metavar="UA")
    client_config.cli_client(ap)
    args = ap.parse_args()

    cfg = None
    try:
        cfg = client_config.load_client(args.client)
    except client_config.ClientConfigError as exc:
        sys.exit(str(exc))
    W.CLIENT = cfg
    # the client's own credentials, overriding a stale shell export - the
    # snapshot must come from the store this client owns (item 3)
    W.apply_client_env(cfg)
    configure(cfg)
    print(f"Client: {cfg.name} ({cfg.slug})   checkpoints: {CHECKPOINT_DIR}")

    if args.action == "list":
        list_checkpoints()
        return

    mismatch = W.store_mismatch(cfg)
    if mismatch:
        # every action here either writes (restore) or is a verdict about the
        # store's contents, so a wrong WC_URL is never harmless
        print(f"REFUSED: {mismatch}")
        sys.exit(W.EXIT_REFUSED)

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

    # Which store this snapshot came from, before a single product is read
    # back: 'latest' can resolve into the unscoped legacy folder, and those
    # files' store is only recorded inside them.
    origin = checkpoint_store_mismatch(checkpoint, store.base, cfg)
    if origin and args.action == "restore" and not args.force_store:
        print(f"\nREFUSED: {origin}.")
        print(f"  Nothing was written. Pass --force-store only if you really "
              f"mean to write this snapshot into that store. "
              f"Exit code {W.EXIT_REFUSED}.")
        sys.exit(W.EXIT_REFUSED)
    if origin:
        print(f"\n  !! WARNING: {origin}.")
        if args.action == "diff":
            print("     This diff compares two different catalogues; every "
                  "product will look changed.")

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
            {s.strip().upper() for s in args.sku}, args.commit, args.limit,
            force_store=args.force_store, client=cfg)


if __name__ == "__main__":
    try:
        main()
    except W.ChallengeError as exc:
        sys.exit(str(exc))
    except RuntimeError as exc:
        sys.exit(str(exc))
