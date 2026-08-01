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
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import blog_brief as B          # noqa: E402
import blog_render as R         # noqa: E402
import blog_verify as V         # noqa: E402

OUT_DIR = HERE / "out"


def build_brief(args):
    products = B.read_products(args.source)
    print(f"Loaded {len(products)} product(s) from {Path(args.source).name}")

    chosen = B.pick(
        products,
        category=args.category,
        max_price=args.max_price,
        min_price=args.min_price,
        brand=args.brand,
        limit=args.limit,
        skus=args.sku,
    )
    if not chosen:
        sys.exit("No products matched. Loosen --category / --max-price / --sku.")
    if args.kind == "comparison" and len(chosen) < 2:
        sys.exit(f"A comparison needs at least 2 products; matched {len(chosen)}. "
                 f"Pass more --sku values.")

    print(f"Selected {len(chosen)} product(s): "
          + ", ".join(p["sku"] for p in chosen[:8])
          + (" ..." if len(chosen) > 8 else ""))

    return B.build(args.kind, chosen, category=args.category,
                   max_price=args.max_price, brand=args.brand)


def make_prose(brief, args):
    """(prose or None, ok_to_publish). None means the deterministic renderer
    is used, which is always publishable because it only restates the brief."""
    if not args.llm:
        print("Prose: deterministic templates (--llm turns on generated prose)")
        return None, True

    import blog_write as Wr
    if not Wr.available():
        print("[warn] ANTHROPIC_API_KEY is not set - falling back to the "
              "deterministic renderer.")
        return None, True

    print(f"Prose: generating with {Wr.MODEL} (effort {args.effort})...")
    try:
        prose = Wr.write(brief, effort=args.effort)
    except RuntimeError as exc:
        print(f"[warn] generation failed: {exc}")
        print("[warn] falling back to the deterministic renderer.")
        return None, True

    usage = prose.pop("_usage", {})
    model = prose.pop("_model", Wr.MODEL)
    print(f"  {model}: {usage.get('input_tokens', 0)} in / "
          f"{usage.get('output_tokens', 0)} out"
          + (f", {usage.get('cache_read', 0)} cached" if usage.get("cache_read") else ""))

    ok, problems, warnings = V.verify(prose, brief)
    print("\n" + V.report(ok, problems, warnings) + "\n")

    if not ok and not args.allow_unverified:
        print("The generated prose was discarded. The deterministic renderer "
              "will be used instead, so the post is still usable.")
        return None, True
    if not ok:
        # --allow-unverified is for looking at a failure, never for shipping it
        print("[warn] --allow-unverified: keeping prose that FAILED "
              "verification. This post cannot be published.")
        return prose, False
    return prose, True


def main():
    ap = argparse.ArgumentParser(
        description="Generate a catalogue-driven blog post. Facts come from "
                    "the data; prose is optional and always fact-checked.")
    ap.add_argument("kind", choices=["roundup", "comparison", "guide"])
    ap.add_argument("--source", required=True,
                    help="The WooCommerce CSV from build_csv.py")
    ap.add_argument("--category", default="",
                    help="Match products whose category/tags/name contain this")
    ap.add_argument("--brand", default="", help="Restrict to one brand")
    ap.add_argument("--sku", action="append", default=[],
                    help="Exact SKU (repeatable). Required for a comparison.")
    ap.add_argument("--max-price", type=int, default=None,
                    help="Only products at or below this price")
    ap.add_argument("--min-price", type=int, default=None,
                    help="Only products at or above this price")
    ap.add_argument("--limit", type=int, default=8,
                    help="At most N products in the post (default: 8)")

    ap.add_argument("--llm", action="store_true",
                    help="Have Claude write the prose. OFF by default: without "
                         "it the post is fully deterministic.")
    ap.add_argument("--effort", default="medium",
                    choices=["low", "medium", "high", "xhigh", "max"],
                    help="Reasoning effort for the prose pass (default: medium)")
    ap.add_argument("--allow-unverified", action="store_true",
                    help="Keep prose that failed the fact check, for "
                         "inspection. Such a post can never be published.")

    ap.add_argument("--publish", action="store_true",
                    help="Send the post to WordPress")
    ap.add_argument("--commit", action="store_true",
                    help="Actually write. Without this nothing is sent.")
    ap.add_argument("--status", default="draft",
                    choices=["draft", "publish", "pending", "private"],
                    help="Status for posts this run creates (default: draft)")
    ap.add_argument("--post-category", default="Buying Guides",
                    help="WordPress post category (default: Buying Guides)")
    ap.add_argument("--site-url", default="",
                    help="Store URL for product links (default: $WC_URL)")
    ap.add_argument("--cookie-file", metavar="PATH",
                    help="Browser Cookie header, for a store behind a CDN "
                         "challenge")
    ap.add_argument("--user-agent", metavar="UA",
                    help="Must match the browser the cookie came from")
    args = ap.parse_args()

    brief = build_brief(args)
    prose, publishable = make_prose(brief, args)

    import os
    site_url = args.site_url or os.environ.get("WC_URL", "")
    payload = R.post_payload(brief, prose, site_url, args.status)
    slug = B.slugify(payload["title"])

    OUT_DIR.mkdir(exist_ok=True)
    stem = f"{args.kind}-{slug}"[:80]
    B.save(brief, OUT_DIR / f"{stem}.brief.json")
    (OUT_DIR / f"{stem}.html").write_text(
        f"<h1>{payload['title']}</h1>\n{payload['content']}", encoding="utf-8")
    (OUT_DIR / f"{stem}.json").write_text(
        json.dumps({**payload, "slug": slug}, indent=2), encoding="utf-8")

    print(f"\nTitle : {payload['title']}")
    print(f"Slug  : {slug}")
    print(f"Body  : {len(payload['content'])} chars")
    print(f"Files : {OUT_DIR / (stem + '.html')}")
    print(f"        {OUT_DIR / (stem + '.brief.json')}")

    if not args.publish:
        print("\nNot publishing (--publish sends it to WordPress).")
        return

    if not publishable:
        sys.exit("\nRefusing to publish: the prose failed verification. "
                 "Re-run without --allow-unverified to use the deterministic "
                 "renderer instead.")

    import blog_publish as P
    P.configure(args.cookie_file or "", "", args.user_agent or "")
    print(f"\n{'PUBLISHING' if args.commit else 'DRY RUN'} -> "
          f"{os.environ.get('WC_URL', '(WC_URL not set)')}")
    try:
        P.publish(payload, slug=slug, category=args.post_category,
                  commit=args.commit, status=args.status)
    except Exception as exc:
        sys.exit(f"\n{exc}")


if __name__ == "__main__":
    main()
