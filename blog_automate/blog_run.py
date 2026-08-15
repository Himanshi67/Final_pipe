"""blog_run.py - the CLI that chains brief -> prose -> verify -> publish.

    python blog_run.py roundup    --source out.csv --category LED --limit 5
    python blog_run.py roundup    --source out.csv --category LED --llm
    python blog_run.py comparison --source out.csv --sku AP-A --sku AP-B --llm
    python blog_run.py guide      --source out.csv --category "Washing Machines"

Add --publish to send it to WordPress; nothing is written without --commit,
and posts are created as drafts unless --status publish.

Every run writes the brief, the post and the verification result into out\\ so
a human can read them before anything goes live.
"""

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import blog_brief as B          # noqa: E402
import blog_render as R         # noqa: E402
import blog_verify as V         # noqa: E402

OUT_DIR = HERE / "out"


class Held(SystemExit):
    """Something is wrong enough that publishing would be a mistake."""


def build_brief(args):
    products, malformed = B.read_products(args.source)
    products, duplicates = B.dedupe(products)
    print(f"Loaded {len(products)} product(s) from {Path(args.source).name}")
    if malformed:
        print(f"  [warn] skipped {len(malformed)} row(s) with more fields than "
              f"headers - an unquoted comma shifts every column after it, so "
              f"the price would not be the price: "
              + ", ".join(malformed[:5])
              + (" ..." if len(malformed) > 5 else ""))
    if duplicates:
        print(f"  [warn] ignored {len(duplicates)} duplicate SKU row(s): "
              + ", ".join(duplicates[:5])
              + (" ..." if len(duplicates) > 5 else ""))

    chosen, notes = B.pick(
        products,
        category=args.category,
        max_price=args.max_price,
        min_price=args.min_price,
        brand=args.brand,
        limit=args.limit,
        skus=args.sku,
    )

    # every product that was asked for and not used says so. A silently
    # dropped --sku used to look identical to a typo'd one.
    missing = [note for note in notes if note.startswith("--sku ")]
    for note in notes:
        print(f"  - skipped {note}" if not note.startswith("--sku ")
              else f"  [warn] {note}")
    if missing:
        raise Held(f"{len(missing)} --sku value(s) are not in "
                   f"{Path(args.source).name}. Fix the SKU or the source file.")

    if not chosen:
        raise Held("No products matched. Loosen --category / --max-price / --sku.")
    if args.kind == "comparison" and len(chosen) < 2:
        raise Held(f"A comparison needs at least 2 products; matched "
                   f"{len(chosen)}. Pass more --sku values.")

    print(f"Selected {len(chosen)} product(s): "
          + ", ".join(p["sku"] for p in chosen[:8])
          + (" ..." if len(chosen) > 8 else ""))

    return B.build(args.kind, chosen, category=args.category,
                   max_price=args.max_price, brand=args.brand)


def make_prose(brief, args):
    """(prose or None, ok_to_publish). None means the deterministic renderer
    is used, which is always publishable because it only restates the brief.

    With --llm the fallbacks no longer publish silently: asking for generated
    prose and getting the template instead is a different post from the one
    that was asked for, so it needs --allow-fallback to go out."""
    if not args.llm:
        print("Prose: deterministic templates (--llm turns on generated prose)")
        return None, True

    fallback_ok = args.allow_fallback

    import blog_write as Wr
    if not Wr.available():
        print("[warn] ANTHROPIC_API_KEY is not set - falling back to the "
              "deterministic renderer.")
        return None, fallback_ok

    print(f"Prose: generating with {args.model} (effort {args.effort})...")
    try:
        prose = Wr.write(brief, effort=args.effort, model=args.model)
    except RuntimeError as exc:
        print(f"[warn] generation failed: {exc}")
        print("[warn] falling back to the deterministic renderer.")
        return None, fallback_ok

    usage = prose.pop("_usage", {})
    model = prose.pop("_model", args.model)
    print(f"  {model}: {usage.get('input_tokens', 0)} in / "
          f"{usage.get('output_tokens', 0)} out"
          + (f", {usage.get('cache_read', 0)} cached" if usage.get("cache_read") else ""))

    ok, problems, warnings = V.verify(prose, brief)
    print("\n" + V.report(ok, problems, warnings) + "\n")

    if not ok and not args.allow_unverified:
        print("The generated prose was discarded. The deterministic renderer "
              "will be used instead, so the post is still usable.")
        return None, fallback_ok
    if not ok:
        # --allow-unverified is for looking at a failure, never for shipping it
        print("[warn] --allow-unverified: keeping prose that FAILED "
              "verification. This post cannot be published.")
        return prose, False
    return prose, True


def preflight(brief, args, site_url):
    """Checks that only matter because the post is about to go live.

    Returns a list of reasons publishing would be wrong. Everything here is a
    warning on a build-only run - you may well be generating the post before
    the products are imported - and a hold on a publish run."""
    reasons = []

    unlinked = R.missing_links(brief, site_url)
    if unlinked:
        why = ("WC_URL is not set" if not site_url
               else "the CSV has no slug column - re-run build_csv.py --seo")
        print(f"[warn] {len(unlinked)}/{brief['product_count']} product(s) will "
              f"not be linked: {why}.")
        if not args.allow_no_links:
            reasons.append(f"{len(unlinked)} product(s) have no storefront link "
                           f"({why}). Pass --allow-no-links to publish anyway.")

    if args.check_images:
        broken = check_images(brief)
        if broken:
            print(f"[warn] {len(broken)} image(s) are not reachable:")
            for sku, url, status in broken[:10]:
                print(f"         {sku}: HTTP {status} {url}")
            if not args.allow_missing_images:
                reasons.append(
                    f"{len(broken)} image(s) 404 - the post hotlinks the "
                    f"store's uploads folder, which only fills up after the "
                    f"WooCommerce import runs. Import the products first, or "
                    f"pass --allow-missing-images.")
    elif R.external_images(brief) and args.publish:
        print("[warn] images are hotlinked to the store's uploads folder and "
              "were not checked (--check-images verifies they exist).")
    return reasons


def check_images(brief):
    """[(sku, url, status)] for every image the post would hotlink and the
    site does not serve."""
    try:
        import requests
    except ImportError:
        print("[warn] --check-images needs the requests package; skipping.")
        return []
    broken = []
    for sku, url in R.external_images(brief):
        try:
            response = requests.head(url, timeout=20, allow_redirects=True)
            if response.status_code >= 400:
                response = requests.get(url, timeout=20, stream=True)
            if response.status_code >= 400:
                broken.append((sku, url, response.status_code))
        except Exception as exc:                    # network is not the point
            broken.append((sku, url, type(exc).__name__))
    return broken


def resolve_slug(brief, title, out_dir):
    """A slug that cannot collide with a DIFFERENT post.

    find_by_slug upserts, so two unrelated posts whose titles slugify the same
    way would overwrite each other on the site. The brief fingerprint says
    'this post, about exactly these products'; when a stored brief claims the
    slug with a different fingerprint, this one takes a suffixed slug."""
    slug = B.slugify(title)
    for existing in sorted(out_dir.glob("*.brief.json")):
        try:
            stored = json.loads(existing.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if stored.get("slug") != slug:
            continue
        if stored.get("fingerprint") == brief["fingerprint"]:
            return slug                             # the same post, re-run
        slug = f"{B.slugify(title, limit=60)}-{brief['fingerprint'][:6]}"
        print(f"[warn] slug '{B.slugify(title)}' is already used by a different "
              f"post ({existing.name}); using '{slug}' instead.")
        return slug
    return slug


def stamp_path(out_dir, stem):
    return out_dir / f"{stem}.published.json"


def main():
    ap = argparse.ArgumentParser(
        description="Generate a catalogue-driven blog post. Facts come from "
                    "the data; prose is optional and always fact-checked.")
    ap.add_argument("kind", choices=["roundup", "comparison", "guide"])
    ap.add_argument("--source", required=True,
                    help="The WooCommerce CSV from build_csv.py")
    ap.add_argument("--category", default="",
                    help="Match products whose category/tags/name contain this "
                         "word (whole words, not substrings)")
    ap.add_argument("--brand", default="", help="Restrict to one brand")
    ap.add_argument("--sku", action="append", default=[],
                    help="Exact SKU (repeatable). Required for a comparison.")
    ap.add_argument("--max-price", type=int, default=None,
                    help="Only products at or below this price")
    ap.add_argument("--min-price", type=int, default=None,
                    help="Only products at or above this price")
    ap.add_argument("--limit", type=int, default=8,
                    help="At most N products in the post (default: 8)")
    ap.add_argument("--out", default=str(OUT_DIR),
                    help="Where to write the brief/HTML/payload")

    ap.add_argument("--llm", action="store_true",
                    help="Have Claude write the prose. OFF by default: without "
                         "it the post is fully deterministic.")
    ap.add_argument("--model", default=None,
                    help="Model for the prose pass (default: blog_write.MODEL)")
    ap.add_argument("--effort", default="medium",
                    choices=["low", "medium", "high", "xhigh", "max"],
                    help="Reasoning effort for the prose pass (default: medium)")
    ap.add_argument("--allow-unverified", action="store_true",
                    help="Keep prose that failed the fact check, for "
                         "inspection. Such a post can never be published.")
    ap.add_argument("--allow-fallback", action="store_true",
                    help="With --llm, publish the template post anyway if "
                         "generation is unavailable or fails the fact check.")
    ap.add_argument("--no-schema", action="store_true",
                    help="Leave out the schema.org ItemList markup")

    ap.add_argument("--publish", action="store_true",
                    help="Send the post to WordPress")
    ap.add_argument("--commit", action="store_true",
                    help="Actually write. Without this nothing is sent.")
    ap.add_argument("--status", default=None,
                    choices=["draft", "publish", "pending", "private"],
                    help="Status to set. New posts default to draft. Passing "
                         "this explicitly ALSO applies it to a post that "
                         "already exists, which is how a draft goes live.")
    ap.add_argument("--post-category", default="Buying Guides",
                    help="WordPress post category (default: Buying Guides)")
    ap.add_argument("--site-url", default="",
                    help="Store URL for product links (default: $WC_URL)")
    ap.add_argument("--allow-no-links", action="store_true",
                    help="Publish even though products cannot be linked")
    ap.add_argument("--check-images", action="store_true",
                    help="Verify every hotlinked image actually loads")
    ap.add_argument("--allow-missing-images", action="store_true",
                    help="Publish even though images do not load yet")
    ap.add_argument("--featured-image", action="store_true",
                    help="Upload the lead product image to the media library "
                         "and set it as the featured image")
    ap.add_argument("--force-update", action="store_true",
                    help="Overwrite a post that was edited on the site since "
                         "this tool last wrote it")
    ap.add_argument("--cookie-file", metavar="PATH",
                    help="Browser Cookie header, for a store behind a CDN "
                         "challenge")
    ap.add_argument("--user-agent", metavar="UA",
                    help="Must match the browser the cookie came from")
    args = ap.parse_args()

    if args.model is None:
        import blog_write as Wr
        args.model = Wr.MODEL

    brief = build_brief(args)
    prose, publishable = make_prose(brief, args)

    site_url = (args.site_url or os.environ.get("WC_URL", "")).strip()
    payload = R.post_payload(brief, prose, site_url, args.status or "draft",
                             schema=not args.no_schema)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = resolve_slug(brief, payload["title"], out_dir)
    brief["slug"] = slug

    stem = f"{args.kind}-{slug}"[:80]
    B.save(brief, out_dir / f"{stem}.brief.json")
    (out_dir / f"{stem}.html").write_text(
        f"<h1>{R.esc(payload['title'])}</h1>\n{payload['content']}",
        encoding="utf-8")
    (out_dir / f"{stem}.json").write_text(
        json.dumps({**payload, "slug": slug}, indent=2), encoding="utf-8")

    print(f"\nTitle : {payload['title']}")
    print(f"Slug  : {slug}")
    print(f"Tags  : {', '.join(payload['tags']) or '(none)'}")
    print(f"Body  : {len(payload['content'])} chars")
    print(f"Files : {out_dir / (stem + '.html')}")
    print(f"        {out_dir / (stem + '.brief.json')}")

    blockers = preflight(brief, args, site_url)

    if not args.publish:
        if blockers:
            print("\nThese would block --publish:")
            for reason in blockers:
                print(f"  - {reason}")
        print("\nNot publishing (--publish sends it to WordPress).")
        return

    if not publishable:
        raise Held("\nRefusing to publish: the prose was not the verified, "
                   "generated prose that was asked for. Re-run without "
                   "--allow-unverified, or pass --allow-fallback to ship the "
                   "template post.")
    if blockers:
        raise Held("\nRefusing to publish:\n"
                   + "\n".join(f"  - {reason}" for reason in blockers))

    import blog_publish as P
    P.configure(args.cookie_file or "", "", args.user_agent or "")
    print(f"\n{'PUBLISHING' if args.commit else 'DRY RUN'} -> "
          f"{os.environ.get('WC_URL', '(WC_URL not set)')}")

    stamp_file = stamp_path(out_dir, stem)
    last_seen = None
    if stamp_file.exists():
        try:
            last_seen = json.loads(stamp_file.read_text(encoding="utf-8")).get(
                "modified_gmt")
        except (OSError, ValueError):
            last_seen = None

    lead = brief["products"][0]["image"] if (args.featured_image
                                             and brief["products"]) else ""
    try:
        result = P.publish(payload, slug=slug, category=args.post_category,
                           commit=args.commit, status=args.status or "draft",
                           last_seen_modified=last_seen,
                           force_update=args.force_update,
                           featured_image=lead,
                           explicit_status=args.status is not None)
    except Exception as exc:
        raise Held(f"\n{exc}") from exc

    if result:
        stamp_file.write_text(json.dumps({
            "id": result.get("id"),
            "slug": slug,
            "modified_gmt": result.get("modified_gmt", ""),
            "link": result.get("link", ""),
        }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
