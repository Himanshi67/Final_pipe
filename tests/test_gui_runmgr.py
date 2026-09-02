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
