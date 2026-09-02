"""P3-52: a clean validation run must delete the previous validation_issues.csv.

Before the fix `validate_export`/`validate_filled` only ever WROTE the report,
so after the operator fixed the flagged rows and rebuilt, the stale file
survived and gui.validator_flags kept showing "Validator flagged this SKU last
run". Offline - no network, no spreadsheet outside a temp directory.

    .venv\\Scripts\\python.exe tests\\test_validate_issues.py
"""

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd                               # noqa: E402

import validate_csv as V                          # noqa: E402

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        raise AssertionError(f"{label}: got {got!r}, want {want!r}")


def clean_category():
    """A category string the map actually allows (falls back when it is absent)."""
    valid = V.load_valid_categories()
    for wanted in ("Air Conditioner", "Air Conditioner > Split AC"):
        if wanted in valid:
            return wanted
    return sorted(valid)[0] if valid else "Air Conditioner"


def sample_row(**over):
    row = {
        "sku": "AC-SAMPLE-15TC5",
        "title": "Sample Brand 1.5 Ton 5 Star Inverter Split AC",
        "categories": clean_category(),
        "description": "A long enough product description for the QA gate.",
        "Images": ("https://x.test/AC-SAMPLE-15TC5_1.webp,"
                   "https://x.test/AC-SAMPLE-15TC5_2.webp,"
                   "https://x.test/AC-SAMPLE-15TC5_3.webp"),
        "regular price": 45990,
        "sale price": 38990,
    }
    row.update(over)
    return row


@contextlib.contextmanager
def no_local_images():
    """IMAGE_DIR is the client's image folder; point it at nothing so the
    local-file check is skipped and the fixture needs no .webp files on disk."""
    saved = V.IMAGE_DIR
    V.IMAGE_DIR = os.path.join(tempfile.gettempdir(), "no_such_image_root__p3_52")
    try:
        yield
    finally:
        V.IMAGE_DIR = saved


def run_export(df, csv_path):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        issues = V.validate_export(df, str(csv_path), check_images=False)
    return issues, buf.getvalue()


def test_01_clean_export_removes_the_stale_report():
    print("\n#1  a clean validate_export deletes last run's validation_issues.csv")
    with no_local_images(), tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "wordpress_ready.csv"
        report = Path(V.issues_report_path(csv_path))

        dirty = pd.DataFrame([sample_row(**{"regular price": "", "sale price": ""})])
        issues, _ = run_export(dirty, csv_path)
        check("the dirty run flags the priceless row", bool(issues), True)
        check("the dirty run writes the report", report.exists(), True)

        clean, _ = run_export(pd.DataFrame([sample_row()]), csv_path)
        check("the clean run flags nothing", clean, [])
        check("the stale report is gone", report.exists(), False)


def test_02_clean_first_run_without_a_report_is_fine():
    print("\n#2  a clean FIRST run (no report on disk) does not raise")
    with no_local_images(), tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "wordpress_ready.csv"
        report = Path(V.issues_report_path(csv_path))
        check("no report to start with", report.exists(), False)
        issues, _ = run_export(pd.DataFrame([sample_row()]), csv_path)
        check("still clean", issues, [])
        check("and none was created", report.exists(), False)


def test_03_write_issues_false_leaves_the_file_alone():
    print("\n#3  write_issues=False neither writes nor deletes")
    with no_local_images(), tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "wordpress_ready.csv"
        report = Path(V.issues_report_path(csv_path))
        report.write_text("row,sku,reasons\n2,AC-OLD,images=0\n", encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            V.validate_export(pd.DataFrame([sample_row()]), str(csv_path),
                              check_images=False, write_issues=False)
        check("a caller that opted out keeps its file", report.exists(), True)


def test_04_remove_stale_report_survives_a_locked_file():
    print("\n#4  an unremovable report prints a line instead of crashing")
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "wordpress_ready.csv"
        report = Path(V.issues_report_path(csv_path))
        report.write_text("row,sku,reasons\n", encoding="utf-8")

        real_remove = os.remove

        def boom(path, *a, **kw):
            if os.path.abspath(path) == os.path.abspath(str(report)):
                raise OSError(13, "Permission denied (open in Excel)")
            return real_remove(path, *a, **kw)

        os.remove = boom
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                removed = V.remove_stale_report(csv_path)
            out = buf.getvalue()
        finally:
            os.remove = real_remove

        check("it reports failure rather than raising", removed, False)
        check("and says so on stdout", "Could not remove" in out, True)
        check("the file is untouched", report.exists(), True)


def test_05_clean_filled_sheet_removes_the_stale_report():
    print("\n#5  validate_filled's xlsx branch clears it too")
    with tempfile.TemporaryDirectory() as tmp:
        xlsx_path = Path(tmp) / "LIST_filled.xlsx"
        report = Path(V.issues_report_path(xlsx_path))
        report.write_text("row,sku,reasons\n2,AC-OLD,missing_price\n", encoding="utf-8")

        empty = pd.DataFrame(columns=["sku", "title", "status", "url_link",
                                      "image_url", "price", "mrp"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            issues = V.validate_filled(empty, str(xlsx_path))
        check("nothing to flag in an empty sheet", issues, [])
        check("the stale report is gone", report.exists(), False)


def main():
    for fn in (test_01_clean_export_removes_the_stale_report,
               test_02_clean_first_run_without_a_report_is_fine,
               test_03_write_issues_false_leaves_the_file_alone,
               test_04_remove_stale_report_survives_a_locked_file,
               test_05_clean_filled_sheet_removes_the_stale_report):
        try:
            fn()
        except AssertionError as exc:
            print(f"  ! {exc}")
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
