"""build_csv.py - run the app.py formatter without the browser.

Same code path as the Streamlit app (it imports app.py and calls
process_inventory), so the CSV this writes is byte-identical to the one you
would get by clicking "Generate file & apply changes". Images are downloaded
into product_images_for_upload\\ exactly as they are in the app.

    .venv\\Scripts\\python.exe build_csv.py "LED_filled.xlsx"
    .venv\\Scripts\\python.exe build_csv.py "LED_filled.xlsx" -o out.csv --month 2026/08
    .venv\\Scripts\\python.exe build_csv.py "LED_filled.xlsx" --all-rows

Exists so the pipeline can run unattended and so the formatter is testable -
the Streamlit-only version could not be asserted on by any script.
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
import app                                     # noqa: E402  (after the env var)


def read_inventory(path):
    if path.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(path)
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin1"):
        try:
            return pd.read_csv(path, encoding=encoding, sep=None, engine="python")
        except UnicodeDecodeError:
            continue
        except Exception:
            continue
    sys.exit(f"Unable to read {path.name}. Save it as UTF-8 CSV or .xlsx.")


def main():
    ap = argparse.ArgumentParser(
        description="Headless WooCommerce CSV build (the app.py formatter, "
                    "without Streamlit).")
    ap.add_argument("input", help="A *_filled.xlsx from sku_enricher.py (or a CSV)")
    ap.add_argument("-o", "--out", help="Output CSV "
                    "(default: wordpress_ready_clean_repaired.csv)")
    ap.add_argument("--month", default=f"{datetime.now():%Y/%m}", metavar="YYYY/MM",
                    help="WordPress uploads folder the images will land in "
                         "(default: this month)")
    ap.add_argument("--all-rows", action="store_true",
                    help="Include unverified rows (CHECK MANUALLY / CLOSE MATCH "
                         "/ NOT FOUND). Default is FOUND rows only.")
    ap.add_argument("--estimate-mrp", action="store_true",
                    help="Fill a missing regular price with sale price x 1.25 "
                         "(off by default: an invented MRP is not a real MRP)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N rows (use for smoke tests)")
    ap.add_argument("--seo", action="store_true",
                    help="Add SEO meta columns: meta title, meta description, "
                         "focus keyword and slug. OFF by default; nothing else "
                         "in the output changes when it is off.")
    ap.add_argument("--seo-plugin", default="yoast",
                    choices=["yoast", "rankmath", "both"],
                    help="Which plugin's meta keys to write (default: yoast)")
    ap.add_argument("--no-validate", action="store_true",
                    help="Skip the QA pass that normally runs on the file this "
                         "writes")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"File not found: {src}")
    out = Path(args.out) if args.out else Path("wordpress_ready_clean_repaired.csv")

    df = read_inventory(src)
    total = len(df)
    df = app.filter_active_inventory_rows(df)
    print(f"Loaded {len(df)} active rows from {total} total rows")

    if "status" in df.columns and not args.all_rows:
        keep = df["status"].map(app.compact).str.upper().str.startswith("FOUND")
        excluded = int((~keep).sum())
        df = df[keep].copy()
        if excluded:
            print(f"Excluded {excluded} unverified row(s); --all-rows keeps them")

    if args.limit:
        df = df.head(args.limit).copy()
        print(f"--limit {args.limit}: building {len(df)} row(s)")

    if df.empty:
        sys.exit("Nothing to build: no rows left after filtering.")

    folder = args.month.strip().strip("/") or f"{datetime.now():%Y/%m}"
    media_base = f"https://shreejeeelectronics.in/wp-content/uploads/{folder}"

    result = app.process_inventory(df, media_base=media_base,
                                   estimate_mrp=args.estimate_mrp,
                                   seo=args.seo, seo_plugin=args.seo_plugin)
    result.to_csv(out, index=False, quoting=1)

    images = result["Images"].map(app.compact)
    with_images = int((images != "").sum())
    print(f"\nWrote {out}  ({len(result)} product(s), "
          f"{with_images} with images, media base {media_base})")
    if with_images < len(result):
        print(f"[warn] {len(result) - with_images} product(s) have NO images - "
              f"check image_url in the input")
    if args.seo:
        print(f"SEO columns written for '{args.seo_plugin}': "
              + ", ".join(app.seo_columns(args.seo_plugin)))
        print("  note: the WooCommerce CSV importer has no slug field - the "
              "'slug' column is used on the wc_publish.py (API) route only.")

    # The QA gate is part of generating the file, not a step to remember.
    if not args.no_validate:
        print("\n" + "-" * 60)
        try:
            import validate_csv
            issues = validate_csv.validate_export(
                pd.read_csv(out, dtype=str).fillna(""), out.name)
            if issues:
                print(f"\n[warn] {len(issues)} row(s) need a look before "
                      f"publishing - see validation_issues.csv "
                      f"(--no-validate skips this pass)")
        except Exception as exc:
            # a QA pass that fails must not invalidate a CSV already on disk
            print(f"[warn] validation pass failed ({type(exc).__name__}: {exc}). "
                  f"The CSV was written; run validate_csv.py by hand.")


if __name__ == "__main__":
    main()
