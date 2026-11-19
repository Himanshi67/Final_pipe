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
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

# --client has to be resolved BEFORE `import app`: app.py reads its taxonomy,
# media base and SEO site name at import time. argparse has not run yet, so
# scan argv here and hand the answer to app through the environment.
import client_config                            # noqa: E402
os.environ["PIPELINE_CLIENT"] = client_config.resolve_slug()

import app                                      # noqa: E402  (after the env vars)


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
    ap.add_argument("--pack", action="store_true",
                    help="Also write <out stem>_pack\\ (CSV, validation "
                         "issues, the images the CSV references and a "
                         "README - never the _filled.xlsx, the enricher "
                         "report or a log) and zip it - one file to hand "
                         "to the client. Same client pack the GUI's Archive "
                         "button zips.")
    # Declared so --client shows in --help and is not an "unrecognized
    # argument"; it was already applied above, before `import app`.
    client_config.cli_client(ap)
    args = ap.parse_args()
    print(f"Client: {app.CLIENT.name} ({app.CLIENT.slug})")

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"File not found: {src}")
    out = Path(args.out) if args.out else Path("wordpress_ready_clean_repaired.csv")

    df = read_inventory(src)
    sheet_df = df                    # every row as delivered - the pack's
    total = len(df)                  # Coverage table counts the WHOLE list
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
    media_base = app.CLIENT.media_base(folder)

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
    issue_count = None
    if not args.no_validate:
        print("\n" + "-" * 60)
        try:
            import validate_csv
            issues = validate_csv.validate_export(
                pd.read_csv(out, dtype=str).fillna(""), str(out))
            issue_count = len(issues)
            if issues:
                print(f"\n[warn] {len(issues)} row(s) need a look before "
                      f"publishing - see validation_issues.csv "
                      f"(--no-validate skips this pass)")
        except Exception as exc:
            # a QA pass that fails must not invalidate a CSV already on disk
            print(f"[warn] validation pass failed ({type(exc).__name__}: {exc}). "
                  f"The CSV was written; run validate_csv.py by hand.")

    if args.pack:
        write_pack(out, src, sheet_df, df, args, issue_count)


def write_pack(out: Path, src: Path, sheet_df, built_df, args, issue_count):
    """--pack: the same deliverable the GUI's Archive button builds, from the
    headless route. gui_helpers owns BOTH the copy routine (assemble_pack)
    and the README renderer, so the two entry points cannot drift."""
    import gui_helpers

    # Owner rule 2026-11-19: the pack goes to the CLIENT, so it holds only the
    # import CSV, the images the CSV references and the README (since the
    # fifth round of 2026-11-19 validation_issues.csv is INTERNAL: it stays
    # next to the CSV and is not packed). NEVER the source list: it is the _filled.xlsx (source page URLs,
    # maker image URLs, statuses naming the source site). assemble_pack's
    # guard refuses such members a second time.
    dest = out.parent / f"{out.stem}_pack"
    hosts = gui_helpers.client_hosts(app.CLIENT)
    files = {out.name: out}

    pack = gui_helpers.assemble_pack(dest, files, app.IMAGE_DIR, out,
                                     allowed_hosts=hosts)
    coverage = gui_helpers.coverage_summary(sheet_df)
    meta = {"total_rows": len(sheet_df),
            "built_scope": "all rows" if args.all_rows else "FOUND rows only",
            "built_skus": list(built_df["sku"]) if "sku" in built_df.columns
            else [],
            "issue_count": "not run" if issue_count is None else issue_count,
            "files": sorted({p.name for p in dest.iterdir()} | {"README.md"})}
    notes = []
    if pack["csv_error"]:
        notes.append(f"\n- **The CSV could not be read** "
                     f"({pack['csv_error']}), so NO images were packed.\n")
    for item in pack["refused"]:
        notes.append(f"not packed (internal or links to a source site): "
                     f"{item.split(': ', 1)[0]}")
    readme = dest / "README.md"
    # the client's list name, not the enriched sheet's ('LIST_filled.xlsx')
    source_name = re.sub(r"_filled(?=\.[^.]+$)", "", src.name)
    readme.write_text(
        gui_helpers.render_batch_readme(
            out.stem, meta, coverage,
            images={"count": pack["images"], "missing": pack["missing"]},
            source_name=source_name, notes=notes),
        encoding="utf-8")
    # last check on the finished folder (README included) before the zip
    gui_helpers.remove_refused(dest, hosts)
    print(f"\nPack: {dest}  ({pack['copied']} file(s), "
          f"{pack['images']} image(s)"
          + (f", {pack['missing']} image(s) MISSING" if pack["missing"]
             else "") + ")")

    # a zip that cannot be written must not lose the folder just built
    try:
        zip_path = shutil.make_archive(str(dest), "zip", root_dir=str(dest))
        print(f"Zip:  {zip_path}")
    except (OSError, ValueError) as exc:
        with readme.open("a", encoding="utf-8") as fh:
            fh.write(f"\n- **The .zip could not be written** "
                     f"({type(exc).__name__}: {exc}) - the folder above is "
                     f"complete; zip it by hand.\n")
        print(f"[warn] could not zip the pack ({type(exc).__name__}: {exc}). "
              f"The folder is complete: {dest}")


if __name__ == "__main__":
    main()
