"""Run-manager and category-map-editor coverage for gui.py.

The two Home-page actions that touch files outside the run folder had no
test at all (the 2026-09-01 audit's Unverified section): archive_run copies
a run into batches\\ and the category-map editor rewrites the taxonomy
workbook in place. Both are exercised here against SCRATCH directories -
gui.REPO_ROOT is redirected at every call site, so the real batches\\,
backups\\, product_images_for_upload\\ and category_map.xlsx are never read
or written, and no Streamlit session is started (archive_run and
backup_and_save_category_map are plain functions; the surrounding UI is
covered by tests\\test_gui_ui.py).

    .venv\\Scripts\\python.exe tests\\test_gui_runmgr.py

Standalone script like the rest of tests\\, and collected by pytest too.
"""

import json
import os
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path

import openpyxl
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

import gui                                                    # noqa: E402
from gui_helpers import image_basenames_from_csv              # noqa: E402

# the Images cell is a comma-separated LIST, so it is quoted exactly as
# app.py writes it - unquoted, pandas would read it as extra columns
CSV_TEXT = (
    "sku,title,Images\n"
    'WM-A,Washer A,"https://cdn.test/WM-A_1.webp,'
    'https://cdn.test/WM-A_2.webp"\n'
    "WM-B,Washer B,https://cdn.test/WM-B_1.webp\n"
)


def seed_run(run_dir: Path) -> None:
    """A finished run: filled sheet, CSV, issues, report, meta, plus a log
    (which archive_run must NOT copy)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    P = gui.run_paths(run_dir)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["sku", "status"])
    ws.append(["WM-A", "FOUND (amazon.in)"])
    wb.save(P["filled"])
    P["csv"].write_text(CSV_TEXT, encoding="utf-8")
    P["issues"].write_text("sku,issue\n", encoding="utf-8")
    P["report"].write_text("# report\n", encoding="utf-8")
    P["log"].write_text("[1] Row 2: WM-A\n", encoding="utf-8")
    P["meta"].write_text(json.dumps({"original_name": "LIST.xlsx",
                                     "total_rows": 2, "issue_count": 0,
                                     "built_scope": "confirmed",
                                     "built_skus": ["WM-A", "WM-B"]}),
                         encoding="utf-8")


def seed_images(repo_root: Path, names) -> None:
    img_dir = repo_root / "product_images_for_upload"
    img_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (img_dir / name).write_bytes(b"RIFF----WEBPVP8 ")


# ---------------------------------------------------------- archive_run --

def test_archive_copies_outputs_images_and_readme(tmp: Path):
    """Audit 'Unverified' item (the run manager, alongside P2-25): archive_run
    copies the outputs + only the images the CSV names, and moves nothing."""
    repo = tmp / "repo"
    run_dir = repo / "runs" / "batch-1"
    seed_run(run_dir)
    wanted = list(dict.fromkeys(image_basenames_from_csv(
        pd.read_csv(gui.run_paths(run_dir)["csv"], dtype=str).fillna(""))))
    assert wanted == ["WM-A_1.webp", "WM-A_2.webp", "WM-B_1.webp"]
    # one image the CSV does NOT reference: archiving must leave it behind
    seed_images(repo, wanted + ["STRAY_9.webp"])

    dest = repo / "batches" / "batch-1"
    old_root = gui.REPO_ROOT
    gui.REPO_ROOT = repo
    try:
        copied = gui.archive_run(run_dir, dest)
    finally:
        gui.REPO_ROOT = old_root

    names = sorted(p.name for p in dest.iterdir())
    assert names == ["README.md", "images", "input_filled.xlsx",
                     "input_filled_report.md", "validation_issues.csv",
                     "wordpress_ready.csv"]
    assert sorted(p.name for p in (dest / "images").iterdir()) == wanted
    # 4 output files + 3 images (the README is written, not copied)
    assert copied == 7
    readme = (dest / "README.md").read_text(encoding="utf-8")
    assert "LIST.xlsx" in readme and "2 SKUs" in readme
    # copies only - the run folder is untouched, log included
    assert gui.run_paths(run_dir)["log"].exists()
    assert gui.run_paths(run_dir)["csv"].exists()


def test_archive_run_without_a_csv_skips_the_images_folder(tmp: Path):
    """Audit 'Unverified' item: archiving a half-done run must not raise on
    the missing CSV, and must not create an empty images\\ folder."""
    repo = tmp / "repo"
    run_dir = repo / "runs" / "half"
    run_dir.mkdir(parents=True)
    wb = openpyxl.Workbook()
    wb.active.append(["sku"])
    wb.save(gui.run_paths(run_dir)["filled"])
    dest = repo / "batches" / "half"
    old_root = gui.REPO_ROOT
    gui.REPO_ROOT = repo
    try:
        copied = gui.archive_run(run_dir, dest)
    finally:
        gui.REPO_ROOT = old_root
    assert copied == 1
    assert not (dest / "images").exists()
    assert (dest / "README.md").exists()


def test_archive_run_survives_a_truncated_csv(tmp: Path):
    """P2-25: a build killed mid-to_csv leaves a 0-byte wordpress_ready.csv.
    pandas raises EmptyDataError - a ValueError, NOT an OSError - so the
    Home-page `except OSError` missed it and the operator got a red
    traceback. archive_run must now archive everything else, skip the images
    folder, and say so in a README that names the run."""
    repo = tmp / "repo"
    run_dir = repo / "runs" / "truncated"
    seed_run(run_dir)
    gui.run_paths(run_dir)["csv"].write_bytes(b"")      # killed mid-write
    seed_images(repo, ["WM-A_1.webp"])

    dest = repo / "batches" / "truncated"
    old_root = gui.REPO_ROOT
    gui.REPO_ROOT = repo
    try:
        copied = gui.archive_run(run_dir, dest)         # must NOT raise
    finally:
        gui.REPO_ROOT = old_root

    assert copied == 4                                 # the 4 output files
    assert not (dest / "images").exists()
    readme = (dest / "README.md").read_text(encoding="utf-8")
    assert "# Batch truncated" in readme               # names the run
    assert "could not be read" in readme and "EmptyDataError" in readme


def test_archive_run_survives_a_ragged_csv(tmp: Path):
    """The other truncation shape: a row with more fields than the header,
    which pandas rejects with ParserError (also a ValueError)."""
    repo = tmp / "repo"
    run_dir = repo / "runs" / "ragged"
    seed_run(run_dir)
    gui.run_paths(run_dir)["csv"].write_text(
        "sku,title\nWM-A,Washer A\nWM-B,Washer B,stray,fields\n",
        encoding="utf-8")
    dest = repo / "batches" / "ragged"
    old_root = gui.REPO_ROOT
    gui.REPO_ROOT = repo
    try:
        copied = gui.archive_run(run_dir, dest)
    finally:
        gui.REPO_ROOT = old_root
    assert copied == 4
    assert not (dest / "images").exists()
    assert "could not be read" in (
        dest / "README.md").read_text(encoding="utf-8")


def _archive_with_publish_meta(tmp: Path, name: str, pub_csv) -> str:
    """Archive a published run and return its README. `pub_csv` is what the
    publish stamped as published_csv: the run's own signature, a foreign one,
    or None for a meta written before the key existed."""
    from gui_helpers import csv_signature
    repo = tmp / name
    run_dir = repo / "runs" / name
    seed_run(run_dir)
    P = gui.run_paths(run_dir)
    meta = json.loads(P["meta"].read_text(encoding="utf-8"))
    meta.update(published="2026-08-30T09:00:00", published_live=True,
                published_count=2)
    if pub_csv == "same":
        meta["published_csv"] = csv_signature(P["csv"])
    elif pub_csv is not None:
        meta["published_csv"] = pub_csv
    P["meta"].write_text(json.dumps(meta), encoding="utf-8")

    dest = repo / "batches" / name
    old_root = gui.REPO_ROOT
    gui.REPO_ROOT = repo
    try:
        gui.archive_run(run_dir, dest)
    finally:
        gui.REPO_ROOT = old_root
    return (dest / "README.md").read_text(encoding="utf-8")


REBUILT_NOTE = ("for a PREVIOUS build; the current CSV was rebuilt "
                "after publishing")


def test_archive_readme_flags_a_publish_from_an_earlier_build(tmp: Path):
    """P3-54: 'Built scope: confirmed (2 SKUs)' next to 'Published: <date>
    (LIVE)' read as those 2 SKUs being live, even when the CSV was rebuilt
    after the publish."""
    readme = _archive_with_publish_meta(tmp, "rebuilt", "0:0")
    assert "- Published: 2026-08-30T09:00:00 (LIVE)" in readme, readme
    assert REBUILT_NOTE in readme, readme


def test_archive_readme_stays_quiet_when_the_csv_is_the_published_one(
        tmp: Path):
    same = _archive_with_publish_meta(tmp, "matching", "same")
    assert "- Published: 2026-08-30T09:00:00 (LIVE)\n" in same, same
    assert REBUILT_NOTE not in same, same
    # a meta from before published_csv existed has nothing to compare
    legacy = _archive_with_publish_meta(tmp, "legacy", None)
    assert REBUILT_NOTE not in legacy, legacy


# ------------------------------------------------- category-map editor --

def make_two_sheet_map(path: Path):
    """A scratch stand-in for category_map.xlsx: the editable first sheet
    plus the 'Sub-Category Attributes' sheet app.load_subcategory_attributes
    reads (losing it blanks _spec_gaps for every later build)."""
    wb = openpyxl.Workbook()
    first = wb.active
    first.title = "Category Map"
    first.append(["Parent Category", "Sub Categories"])
    first.append(["Kitchen Appliances",
                  "Mixer Grinders, Juicers, Mixers & Grinders"])
    attrs = wb.create_sheet("Sub-Category Attributes")
    attrs.append(["Sub-Category", "Attributes"])
    attrs.append(["Mixer Grinders", "Wattage|Jars"])
    filters = wb.create_sheet("Filters")
    filters.append(["Filter", "Values"])
    wb.save(path)


def test_save_keeps_every_other_sheet_and_writes_a_backup(tmp: Path):
    """Audit 'Unverified' item: the map editor's save must keep the
    'Sub-Category Attributes' sheet and leave a pre-edit backup behind."""
    repo = tmp / "repo"
    repo.mkdir()
    path = repo / "category_map.xlsx"
    make_two_sheet_map(path)

    edited = pd.DataFrame([
        ["Parent Category", "Sub Categories"],
        ["Kitchen Appliances", "Mixer Grinders, Kettles"],
        ["Home Appliances", "Washing Machines"],
    ])

    old_root = gui.REPO_ROOT
    gui.REPO_ROOT = repo
    try:
        backup = gui.backup_and_save_category_map(path, edited)
    finally:
        gui.REPO_ROOT = old_root

    wb = openpyxl.load_workbook(path)
    # the other sheets survive, in order and with their contents
    assert wb.sheetnames == ["Category Map", "Sub-Category Attributes",
                             "Filters"]
    attrs = wb["Sub-Category Attributes"]
    assert [c.value for c in attrs[2]] == ["Mixer Grinders", "Wattage|Jars"]
    # the first sheet holds the edit
    rows = [[c.value for c in r] for r in wb["Category Map"].iter_rows()]
    assert ["Home Appliances", "Washing Machines"] in rows

    # the backup is the workbook as it was BEFORE the edit
    assert backup.exists() and backup.parent == repo / "backups"
    old = openpyxl.load_workbook(backup)
    assert old.sheetnames == ["Category Map", "Sub-Category Attributes",
                             "Filters"]
    old_rows = [[c.value for c in r] for r in old["Category Map"].iter_rows()]
    assert ["Kitchen Appliances",
            "Mixer Grinders, Juicers, Mixers & Grinders"] in old_rows
    assert ["Home Appliances", "Washing Machines"] not in old_rows


def test_editor_diff_names_the_real_combo_that_disappears(tmp: Path):
    """The warning the operator reads must name strings app.py actually
    snaps to - 'Juicers, Mixers & Grinders' is ONE category (P2-29)."""
    from gui_helpers import sheet_combos
    before = pd.DataFrame([
        ["Kitchen Appliances",
         "Mixer Grinders, Juicers, Mixers & Grinders"]])
    after = pd.DataFrame([["Kitchen Appliances", "Mixer Grinders"]])
    assert sorted(sheet_combos(before) - sheet_combos(after)) == [
        "Kitchen Appliances > Juicers, Mixers & Grinders"]



# ------------------------------------- item 6: the deliverable pack --

def seed_pack_run(run_dir: Path) -> None:
    """seed_run plus the two things a hand-over pack adds: the client's
    ORIGINAL list and the review metrics Step 2 wrote."""
    seed_run(run_dir)
    P = gui.run_paths(run_dir)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["sku"])
    ws.append(["WM-A"])
    wb.save(P["input"])                        # runs\<name>\input.xlsx
    P["metrics"].write_text(json.dumps(
        {"review_minutes": 8.0, "rows_touched": 4, "min_per_sku": 2.0,
         "needs_human_pct": 25.0, "actions": {"approve": 4}}),
        encoding="utf-8")
    # a sheet with more status shapes than seed_run's single FOUND row, so
    # the Coverage table has something to break down
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["sku", "status"])
    for sku, status in (("WM-A", "FOUND (amazon.in)"),
                        ("WM-B", "FOUND (croma.com) - only 2 image(s)"),
                        ("WM-C", "NOT FOUND - 3 page(s) checked, none matched"),
                        ("WM-D", "NEEDS LINK - no usable search results")):
        ws.append([sku, status])
    wb.save(P["filled"])


def test_archive_writes_a_coverage_review_readme_and_a_zip(tmp: Path):
    """Item 6: one folder AND one .zip to hand over, with the coverage the
    client is paying for and the review time it cost spelled out."""
    repo = tmp / "repo"
    run_dir = repo / "runs" / "pack-1"
    seed_pack_run(run_dir)
    seed_images(repo, ["WM-A_1.webp", "WM-A_2.webp"])   # WM-B_1 is MISSING

    dest = repo / "batches" / "pack-1"
    old_root = gui.REPO_ROOT
    gui.REPO_ROOT = repo
    try:
        copied = gui.archive_run(run_dir, dest)
        zip_path = gui.LAST_ARCHIVE_ZIP
    finally:
        gui.REPO_ROOT = old_root

    # 4 outputs + the original list + 2 of the 3 referenced images
    assert copied == 7
    # the client's list under the name they sent it, not "input.xlsx"
    assert (dest / "LIST.xlsx").exists()
    assert not (dest / "input.xlsx").exists()

    readme = (dest / "README.md").read_text(encoding="utf-8")
    assert "## Coverage" in readme and "## Review" in readme
    assert "## Files" in readme
    assert "| **Rows** | **4** |" in readme
    assert "| FOUND (clean) | 1 |" in readme
    assert "↳ only N image(s) | 1 |" in readme
    assert "↳ N page(s) checked, none matched | 1 |" in readme
    assert "| NEEDS LINK (two-pass) | 1 |" in readme
    assert "Review so far: 8.0 min · 2.0 min/SKU · needs-human 25.0%" in readme
    assert "- Images: 2 file(s) copied, 1 referenced but MISSING" in readme
    assert "- `LIST.xlsx`" in readme                  # the Files section
    assert "- Source list: LIST.xlsx" in readme       # the pre-item-6 line

    # one file to hand over, and it holds the README we just wrote
    assert zip_path is not None and zip_path.exists()
    assert zip_path == repo / "batches" / "pack-1.zip"
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert "README.md" in names and "LIST.xlsx" in names
        assert "images/WM-A_1.webp" in names
        assert "## Coverage" in zf.read("README.md").decode("utf-8")


def test_archive_survives_a_zip_that_cannot_be_written(tmp: Path):
    """A full disk or the previous .zip open in Explorer must cost a README
    line, not the folder we just built (and never a red traceback)."""
    import shutil as _shutil
    repo = tmp / "repo"
    run_dir = repo / "runs" / "nozip"
    seed_pack_run(run_dir)
    dest = repo / "batches" / "nozip"

    def boom(*a, **k):
        raise OSError("[Errno 28] No space left on device")

    old_root, old_make = gui.REPO_ROOT, _shutil.make_archive
    gui.REPO_ROOT = repo
    gui.shutil.make_archive = boom
    try:
        copied = gui.archive_run(run_dir, dest)        # must NOT raise
    finally:
        gui.REPO_ROOT, gui.shutil.make_archive = old_root, old_make

    assert copied >= 4 and gui.LAST_ARCHIVE_ZIP is None
    readme = (dest / "README.md").read_text(encoding="utf-8")
    assert "The .zip could not be written" in readme
    assert "No space left on device" in readme
    assert "## Coverage" in readme                    # the rest still landed


def test_archive_coverage_survives_an_unreadable_filled_sheet(tmp: Path):
    """A sheet open in Excel or truncated mid-save costs the Coverage
    numbers, never the archive."""
    repo = tmp / "repo"
    run_dir = repo / "runs" / "badsheet"
    seed_pack_run(run_dir)
    gui.run_paths(run_dir)["filled"].write_bytes(b"not an xlsx at all")
    dest = repo / "batches" / "badsheet"
    old_root = gui.REPO_ROOT
    gui.REPO_ROOT = repo
    try:
        gui.archive_run(run_dir, dest)                # must NOT raise
    finally:
        gui.REPO_ROOT = old_root
    readme = (dest / "README.md").read_text(encoding="utf-8")
    assert "## Coverage" in readme and "| **Rows** | **0** |" in readme

# ------------------------------------------------ credit ledger (item 4) --

def _cfg(tmp: Path, monthly=2500):
    """A stand-in ClientConfig: serper_caption only reads credits_file and
    serper.monthly_budget, so a real client dir would only slow this down."""
    import types
    return types.SimpleNamespace(
        slug="acme", credits_file=tmp / "credits.json",
        serper=types.SimpleNamespace(monthly_budget=monthly))


def test_serper_caption_without_a_ledger_says_so(tmp: Path):
    """A fresh client has no credits.json until its first real run - saying
    '0/2500 this month' there would claim a measurement nobody made."""
    text = gui.serper_caption(_cfg(tmp), 12)
    assert "12 this run" in text and "no ledger yet" in text, text


def test_serper_caption_reads_the_month_to_date_total(tmp: Path):
    (tmp / "credits.json").write_text(json.dumps(
        {"month": "2026-09", "serper_calls": 412, "runs": [], "history": {}}),
        encoding="utf-8")
    text = gui.serper_caption(_cfg(tmp), 37)
    assert "37 this run" in text and "412/2500 this month" in text, text


def test_serper_caption_survives_a_half_written_ledger(tmp: Path):
    """The ledger is written tmp + os.replace, but a truncated or hand-edited
    file must still not turn the progress panel into a traceback."""
    (tmp / "credits.json").write_text("{not json", encoding="utf-8")
    assert "no ledger yet" in gui.serper_caption(_cfg(tmp), 0)


# ------------------------------------------------------------------ main --

def main():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    passed = failed = 0
    for name, fn in tests:
        try:
            if fn.__code__.co_argcount:
                with tempfile.TemporaryDirectory(prefix="gui_runmgr_") as td:
                    fn(Path(td))
            else:
                fn()
            print(f"PASS {name}")
            passed += 1
        except Exception:
            print(f"FAIL {name}")
            traceback.print_exc(limit=4)
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
