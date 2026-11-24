"""UI tests for gui.py - the wizard - via streamlit.testing.v1.AppTest.

Headless: no browser, no subprocesses, no network. Each test runs gui.main()
inside AppTest with gui.RUNS_DIR patched to a scratch directory, so real
runs\\ batches are never read or written.

    .venv\\Scripts\\python.exe tests\\test_gui_ui.py

Standalone script like the rest of tests\\, and collected by pytest too. What
AppTest cannot drive (file_uploader, data_editor cell edits, real
subprocesses) is covered by the enricher/CSV end-to-end tests instead; here we
assert the wizard's structure: boot, run creation, step gating, and that each
step renders against seeded artifacts without raising.
"""

import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

# the seeded rows carry example.com image URLs: never download them for the
# card review's final-image preview (conftest.py sets the same under pytest)
os.environ.setdefault("PIPELINE_REVIEW_IMAGE_PREVIEW", "off")

FILLED_HEADERS = ["sku", "title", "regular price", "sale price", "url_link",
                  "image_url", "specs", "status", "categories", "Brands"]

FILLED_ROWS = [
    ["SKU-A", "Samsung 7 kg Washer", 25000, "22,599",
     "https://example.com/a",
     "https://example.com/a1.jpg,https://example.com/a2.jpg",
     "Type: Front Load", "FOUND (amazon)", "", "Samsung"],
    ["SKU-B", "LG 190 L Fridge", "", 18000,
     "https://example.com/b", "https://example.com/b1.jpg",
     "", "FOUND (flipkart) - only 1 image(s)", "", "LG"],
    ["SKU-C", "", "", "", "", "", "", "NOT FOUND", "", ""],
]


def make_filled(path: Path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(FILLED_HEADERS)
    for row in FILLED_ROWS:
        ws.append(row)
    wb.save(path)


def wizard(runs_dir):
    """App entry for AppTest: gui.main() with RUNS_DIR redirected."""
    import gui
    gui.RUNS_DIR = runs_dir
    gui.main()


def boot(runs_dir, timeout=60):
    at = AppTest.from_function(wizard, args=(runs_dir,))
    at.run(timeout=timeout)
    return at


def assert_no_exception(at):
    assert not at.exception, f"page raised: {[e.value for e in at.exception]}"


def run_picker(at):
    """The sidebar's 'Batch / run' selectbox, BY KEY - the Client selector
    sits above it, so index 0 is no longer the run list."""
    return at.sidebar.selectbox(key="run_picker")


def client_picker(at):
    return at.sidebar.selectbox(key="client_picker")


def seeded_run(base: Path, name: str, with_csv=False) -> Path:
    run = base / name
    run.mkdir(parents=True)
    (run / "input.xlsx").write_bytes(b"")      # placeholder input artifact
    make_filled(run / "input_filled.xlsx")
    if with_csv:
        (run / "wordpress_ready.csv").write_text(
            'sku,title\n"WM-SKU-A","Samsung 7 kg Washer"\n', encoding="utf-8")
    return run


# ----------------------------------------------------------------- tests --


def test_boot_empty_runs(tmp):
    at = boot(tmp / "runs_empty")
    assert_no_exception(at)
    assert any("Create a run" in str(b.value) for b in at.info), \
        "expected the create-a-run hint on an empty install"


def test_create_run(tmp):
    runs = tmp / "runs"
    at = boot(runs)
    at.sidebar.text_input[0].set_value("uitest").run(timeout=60)
    create = [b for b in at.sidebar.button if "Create run" in str(b.label)]
    assert create, "Create run button missing"
    create[0].click().run(timeout=60)
    assert_no_exception(at)
    assert (runs / "uitest").is_dir(), "run folder was not created on disk"


def test_step_gating_locked(tmp):
    runs = tmp / "runs"
    (runs / "empty_run").mkdir(parents=True)
    at = boot(runs)
    radio = at.sidebar.radio[0]
    labels = [str(o) for o in radio.options]
    assert len(labels) == 5, labels
    assert "🔒" not in labels[0]
    assert all("🔒" in lbl for lbl in labels[1:]), labels
    at.sidebar.radio[0].set_value(labels[2]).run(timeout=60)
    assert_no_exception(at)
    assert any("Locked" in str(i.value) for i in at.info)


def test_step1_asks_for_upload(tmp):
    runs = tmp / "runs"
    (runs / "fresh").mkdir(parents=True)
    at = boot(runs)
    assert_no_exception(at)
    assert any("Upload the bare SKU list" in str(i.value) for i in at.info)


def test_step2_renders_buckets(tmp):
    runs = tmp / "runs"
    seeded_run(runs, "seeded")
    at = boot(runs)
    labels = [str(o) for o in at.sidebar.radio[0].options]
    assert "🔒" not in labels[1], "step 2 should unlock once _filled exists"
    at.sidebar.radio[0].set_value(labels[1]).run(timeout=60)
    assert_no_exception(at)
    metric_labels = [str(m.label) for m in at.metric]
    assert any("Found" in t for t in metric_labels), metric_labels
    assert any("Needs review" in t for t in metric_labels), metric_labels
    # seeded statuses: exactly 1 clean FOUND, 1 review, 1 failed
    by_label = {str(m.label): str(m.value) for m in at.metric}
    found = next(v for k, v in by_label.items() if "Found" in k)
    review = next(v for k, v in by_label.items() if "Needs review" in k)
    assert found == "1" and review == "1", by_label


def test_step3_scope_options(tmp):
    runs = tmp / "runs"
    seeded_run(runs, "seeded3")
    at = boot(runs)
    labels = [str(o) for o in at.sidebar.radio[0].options]
    at.sidebar.radio[0].set_value(labels[2]).run(timeout=60)
    assert_no_exception(at)
    page_radios = [r for r in at.radio if "Rows to include" in str(r.label)]
    assert page_radios, "scope radio missing on step 3"
    # AppTest's .options carries the DISPLAYED labels (format_func output);
    # what must stay stable is the returned .value - a raw key, never the
    # wording - so built_scope can't be flipped by a label rewrite
    opts = [str(o) for o in page_radios[0].options]
    assert len(opts) == 2 and "Confirmed rows only" in opts[0], opts
    assert page_radios[0].value in ("confirmed", "all_found"), \
        page_radios[0].value


def test_steps_4_5_locked_without_csv(tmp):
    runs = tmp / "runs"
    seeded_run(runs, "seeded4")
    at = boot(runs)
    labels = [str(o) for o in at.sidebar.radio[0].options]
    assert "🔒" in labels[3] and "🔒" in labels[4], \
        f"steps 4-5 must stay locked until a CSV exists: {labels}"


def test_step5_requires_validation(tmp):
    runs = tmp / "runs"
    seeded_run(runs, "seeded5", with_csv=True)
    at = boot(runs)
    labels = [str(o) for o in at.sidebar.radio[0].options]
    assert "🔒" not in labels[4], labels
    at.sidebar.radio[0].set_value(labels[4]).run(timeout=60)
    assert_no_exception(at)
    assert any("Validate" in str(w.value) for w in at.warning), \
        "step 5 must demand a validation pass before anything else"


def test_price_comma_parsing(tmp):
    """load_filled_df must survive Indian-formatted prices ('22,599')."""
    import gui
    run = tmp / "prices"
    run.mkdir()
    make_filled(run / "input_filled.xlsx")
    P = gui.run_paths(run)
    df = gui.load_filled_df(P)
    row = df[df["sku"] == "SKU-A"].iloc[0]
    assert row["sale price"] == 22599, row["sale price"]


def test_classify_buckets():
    import gui
    cases = {"FOUND (amazon)": "found",
             "FOUND (amazon) - no price shown": "review",
             "FOUND (croma) - price from amazon": "review",
             "CLOSE MATCH - CHECK": "review",
             "CHECK MANUALLY": "review",
             "NOT FOUND": "failed",
             "ERROR: Timeout": "failed",
             "": "failed",
             "SKIPPED - same unit as row 3": "skipped"}
    for status, want in cases.items():
        got = gui.classify(status)
        assert got == want, f"{status!r}: {got} != {want}"


def make_sku_list(path: Path):
    """A minimal but REAL input sheet: the SKU header plus one row."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["sku"])
    ws.append(["WM-1234"])
    wb.save(path)


def test_uploader_rejects_xls():
    """.xls is not offered: openpyxl cannot open it, so count_skus() would
    read 0 rows and the enricher would exit on the first row."""
    import gui
    assert "xls" not in gui.UPLOAD_TYPES, gui.UPLOAD_TYPES
    assert "xlsx" in gui.UPLOAD_TYPES and "csv" in gui.UPLOAD_TYPES, \
        gui.UPLOAD_TYPES


def test_step1_offers_replace_before_enrichment(tmp):
    """The uploader hides once input.* exists, so an un-enriched run must
    still offer a way to swap the file out."""
    runs = tmp / "runs"
    run = runs / "replaceable"
    run.mkdir(parents=True)
    make_sku_list(run / "input.xlsx")
    at = boot(runs)
    assert_no_exception(at)
    assert any("Replace input" in str(b.label) for b in at.button), \
        [str(b.label) for b in at.button]
    assert not at.error, [str(e.value) for e in at.error]


def test_step1_flags_unreadable_input(tmp):
    """A corrupt / legacy sheet counts 0 SKUs - say so instead of letting the
    operator start a run that dies on row 1."""
    runs = tmp / "runs"
    run = runs / "unreadable"
    run.mkdir(parents=True)
    (run / "input.xlsx").write_bytes(b"this is not a spreadsheet")
    at = boot(runs)
    assert_no_exception(at)
    assert any("Could not read" in str(e.value) for e in at.error), \
        [str(e.value) for e in at.error]
    assert any("Replace input" in str(b.label) for b in at.button), \
        [str(b.label) for b in at.button]


def test_step3_seo_preview_matches_the_build(tmp):
    """P1-16: ticking SEO on step 3 previews what the BUILD would write.

    The preview reads the sheet the way the build does (read_excel ->
    filter_active_inventory_rows -> select_build_rows) and derives every
    field through app.row_context, so the NOT FOUND row must be absent and
    the SKUs must be the prefixed, CSV-bound ones - not the raw sheet cells.
    """
    import app
    runs = tmp / "runs"
    seeded_run(runs, "seeded_seo")
    at = boot(runs)
    labels = [str(o) for o in at.sidebar.radio[0].options]
    at.sidebar.radio[0].set_value(labels[2]).run(timeout=60)
    # nothing is approved, and the default scope is now always 'Confirmed
    # rows only' (which previews nothing) - pick the FOUND scope explicitly
    next(r for r in at.radio if "Rows to include" in str(r.label)) \
        .set_value("all_found").run(timeout=60)
    boxes = [c for c in at.checkbox if "SEO meta" in str(c.label)]
    assert boxes, [str(c.label) for c in at.checkbox]
    boxes[0].check().run(timeout=60)
    assert_no_exception(at)
    assert at.dataframe, "SEO preview table did not render"
    shown = at.dataframe[0].value
    assert "slug" in shown.columns, list(shown.columns)

    expected = [app.row_context(r)["sku"] for r in
                (dict(zip(FILLED_HEADERS, row)) for row in FILLED_ROWS)
                if str(r["status"]).startswith("FOUND")]
    assert list(shown["sku"]) == expected, (list(shown["sku"]), expected)
    assert "SKU-C" not in set(shown["sku"]), "NOT FOUND row leaked into the "\
        "preview - select_build_rows' FOUND gate is not being applied"


def test_preview_drops_the_rows_the_build_drops():
    """P1-16 (refutation): scoping alone is not fidelity. process_inventory
    drops a blank-title row and skips a repeated prefixed sku BEFORE it
    writes, so a .head(5) preview showed products the CSV never contains -
    and each of them ate a slot belonging to a row that IS built."""
    import pandas as pd
    import app
    import gui

    rows = [
        # blank title (whitespace only): process_inventory drops it outright
        {"sku": "BT-000", "title": "   ", "status": "FOUND (amazon)"},
        {"sku": "WM-101", "title": "LG 7 kg Front Load Washing Machine FHM1207",
         "status": "FOUND (amazon)"},
        # same raw sku -> same prefixed sku: the build skips the second
        {"sku": "WM-101", "title": "LG 7 kg Front Load Washing Machine FHM1207 "
                                   "duplicate", "status": "FOUND (amazon)"},
        {"sku": "MW-202", "title": "IFB 20 L Solo Microwave Oven 20PM2S",
         "status": "FOUND (amazon)"},
    ]
    for r in rows:
        r.setdefault("image_url", "")          # never download anything here
        for col in ("regular price", "sale price", "specs", "categories",
                    "Brands", "url_link"):
            r.setdefault(col, "")
    pf = pd.DataFrame(rows)

    got = gui.preview_build_rows(pf, app, limit=5)
    skus = [ctx["sku"] for _, ctx in got]
    assert "BT-000" not in " ".join(skus), skus
    assert len(skus) == len(set(skus)), skus

    built = app.process_inventory(pf.copy(), media_base="https://x/up/2026/09")
    assert skus == list(built["sku"]), (skus, list(built["sku"]))

    # the window is still 5 rows of what the build writes
    assert len(gui.preview_build_rows(pf, app, limit=1)) == 1


# ------------------------------------------------------------------ P2-25 --

def home(runs_dir):
    """The '🏠 All runs' page, where the run manager lives (the sidebar
    lands on the newest run, so it has to be selected)."""
    at = boot(runs_dir)
    run_picker(at).set_value("🏠 All runs").run(timeout=60)
    return at


def manage_buttons(at):
    return {"archive": next(b for b in at.button
                            if "Archive" in str(b.label)),
            "delete": next(b for b in at.button
                           if "Delete run" in str(b.label))}


def test_manage_buttons_are_dead_while_the_enricher_owns_the_run(tmp):
    """P2-25: Archive and Delete must both be disabled, with a caption
    saying so, while that run's enricher is alive - rmtree would hit
    enricher.log, which the child holds open for write."""
    import gui
    runs = tmp / "runs"
    run = seeded_run(runs, "busy")
    (run / "run_meta.json").write_text(
        json.dumps({"enricher_pid": 4242, "enricher_started": 1.0,
                    "total_rows": 3}), encoding="utf-8")
    real = gui.pid_alive
    gui.pid_alive = lambda pid, started=None: bool(pid)   # no real syscall
    try:
        at = home(runs)
    finally:
        gui.pid_alive = real
    assert_no_exception(at)
    btns = manage_buttons(at)
    assert btns["archive"].disabled, "Archive stayed clickable mid-run"
    assert btns["delete"].disabled, "Delete stayed clickable mid-run"
    assert any("stop the enricher first" in str(c.value)
               for c in at.caption), [str(c.value) for c in at.caption]


def test_manage_buttons_are_live_when_no_enricher_owns_the_run(tmp):
    """P2-25 control: with no live PID only the delete confirmation gates
    Delete, Archive is clickable, and the warning caption is absent."""
    runs = tmp / "runs"
    seeded_run(runs, "idle")
    at = home(runs)
    assert_no_exception(at)
    btns = manage_buttons(at)
    assert not btns["archive"].disabled
    assert btns["delete"].disabled, "delete needs the confirmation checkbox"
    assert not any("stop the enricher first" in str(c.value)
                   for c in at.caption)


# --------------------------------------------- P2-25 (second half) --


def mgr_select(at):
    """The run manager's own 'Run' selectbox (the sidebar has one too)."""
    return next(s for s in at.selectbox if str(s.label) == "Run")


def delete_tick(at):
    return next(c for c in at.checkbox if "permanently delete" in str(c.label))


def test_delete_confirmation_does_not_carry_to_another_run(tmp):
    """P2-25 (missed on the first pass): the confirmation checkbox used a
    static key while its label named the selected run, so ticking it for one
    run left it ticked when the selectbox moved to another - and the next
    run was one unconfirmed click from deletion."""
    runs = tmp / "runs"
    seeded_run(runs, "aaa")
    seeded_run(runs, "bbb")
    at = home(runs)
    assert_no_exception(at)
    first = str(mgr_select(at).value)
    other = "aaa" if first == "bbb" else "bbb"

    delete_tick(at).set_value(True).run(timeout=60)
    assert not manage_buttons(at)["delete"].disabled,         "confirming the selected run should arm Delete"

    mgr_select(at).set_value(other).run(timeout=60)
    assert_no_exception(at)
    tick = delete_tick(at)
    assert ("runs\\" + other) in str(tick.label)
    assert tick.value is False,         "the confirmation carried over to a run that was never confirmed"
    assert manage_buttons(at)["delete"].disabled,         "Delete was armed on a run the operator never confirmed"
    assert (runs / "aaa").exists() and (runs / "bbb").exists()


def test_delete_reports_any_os_error_instead_of_a_traceback(tmp):
    """P2-25: the guard caught PermissionError only. A folder removed in
    Explorer (FileNotFoundError) or a bare OSError from a second browser tab
    between render and click still put a red traceback on the page."""
    import shutil as sh
    for exc in (PermissionError(13, "in use"),
                FileNotFoundError(2, "no such directory"),
                OSError(39, "directory not empty")):
        runs = tmp / f"runs_{exc.__class__.__name__}"
        seeded_run(runs, "doomed")
        at = home(runs)
        delete_tick(at).set_value(True).run(timeout=60)

        def boom(*a, **k):
            raise exc
        real = sh.rmtree
        sh.rmtree = boom
        try:
            manage_buttons(at)["delete"].click().run(timeout=60)
        finally:
            sh.rmtree = real
        assert_no_exception(at)
        assert any("Could not delete" in str(e.value) for e in at.error),             f"{exc.__class__.__name__} produced no friendly error"


def test_archive_reports_a_copy_failure_instead_of_a_traceback(tmp):
    """The archive_run copy loop was unguarded: a sheet locked in Excel or an
    image deleted under the copy raised straight through st.button."""
    import gui
    runs = tmp / "runs"
    seeded_run(runs, "arch")

    def boom(run_dir, dest):
        raise OSError(13, "Permission denied")

    real_root, real_arch = gui.REPO_ROOT, gui.archive_run
    gui.REPO_ROOT = tmp / "repo"   # never the real batches folder
    gui.archive_run = boom
    try:
        at = home(runs)
        manage_buttons(at)["archive"].click().run(timeout=60)
    finally:
        gui.REPO_ROOT, gui.archive_run = real_root, real_arch
    assert_no_exception(at)
    assert any("Could not finish archiving" in str(e.value)
               for e in at.error), [str(e.value) for e in at.error]


def test_archive_success_points_at_the_zip_to_hand_over(tmp):
    """Item 6: the deliverable is ONE file. The success message has to say
    which one, or the operator hands over the folder path again."""
    import gui
    runs = tmp / "runs"
    seeded_run(runs, "arch2")
    made = {}

    def fake_archive(run_dir, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "README.md").write_text("# Batch arch2", encoding="utf-8")
        zip_path = dest.with_suffix(".zip")
        zip_path.write_text("stub zip", encoding="utf-8")
        gui.LAST_ARCHIVE_ZIP = made["zip"] = zip_path
        return 4

    real_root, real_arch = gui.REPO_ROOT, gui.archive_run
    gui.REPO_ROOT = tmp / "repo"          # never the real batches folder
    gui.archive_run = fake_archive
    try:
        at = home(runs)
        manage_buttons(at)["archive"].click().run(timeout=60)
    finally:
        gui.REPO_ROOT, gui.archive_run = real_root, real_arch
        gui.LAST_ARCHIVE_ZIP = None
    assert_no_exception(at)
    captions = [str(c.value) for c in at.caption]
    assert any("arch2.zip" in c and "one file to hand over" in c
               for c in captions), captions
    assert made["zip"].exists()


def test_archive_success_says_so_when_there_is_no_zip(tmp):
    """A zip that could not be written must not leave the operator hunting
    for a file that is not there."""
    import gui
    runs = tmp / "runs"
    seeded_run(runs, "arch3")

    def fake_archive(run_dir, dest):
        dest.mkdir(parents=True, exist_ok=True)
        gui.LAST_ARCHIVE_ZIP = None
        return 4

    real_root, real_arch = gui.REPO_ROOT, gui.archive_run
    gui.REPO_ROOT = tmp / "repo"
    gui.archive_run = fake_archive
    try:
        at = home(runs)
        manage_buttons(at)["archive"].click().run(timeout=60)
    finally:
        gui.REPO_ROOT, gui.archive_run = real_root, real_arch
    assert_no_exception(at)
    captions = [str(c.value) for c in at.caption]
    assert any("could not be written" in c for c in captions), captions


# ------------------------------------------------------------------ P2-28 --


def launch_enricher(run_dir_str, flags):
    """AppTest entry: gui.start_enricher with Popen stubbed (no subprocess)."""
    import subprocess
    from pathlib import Path as _P
    import gui

    class _Proc:
        pid = 9999

    real = subprocess.Popen
    subprocess.Popen = lambda *a, **k: _Proc()
    try:
        gui.start_enricher(gui.run_paths(_P(run_dir_str)), list(flags), 40)
    finally:
        subprocess.Popen = real


def test_retry_launch_keeps_total_rows_as_the_sheet_count(tmp):
    """P2-28 regression guard: the ETA denominator must live in its own key.
    `total_rows` means 'rows in the sheet' to the Home dashboard, to
    archive_run's README and to the fallback step 2 hands the NEXT retry, so
    a retry launch may not overwrite it with the eligible count."""
    import openpyxl

    run = tmp / "runs" / "r"
    run.mkdir(parents=True)
    (run / "input.xlsx").write_bytes(b"")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(FILLED_HEADERS)
    for i in range(40):                       # 5 of 40 rows are incomplete
        incomplete = i < 5
        ws.append([f"SKU-{i}", "Some Washer", 25000,
                   "" if incomplete else 22599,
                   "https://example.com/x",
                   "" if incomplete else "https://example.com/x1.jpg",
                   "", "FOUND (amazon)" + (" - no price shown" if incomplete
                                           else ""), "", "Samsung"])
    wb.save(run / "input_filled.xlsx")

    at = AppTest.from_function(launch_enricher,
                               args=(str(run), ["--retry-incomplete"]))
    at.run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]

    meta = json.loads((run / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["total_rows"] == 40,         f"total_rows must stay the sheet count, got {meta['total_rows']}"
    assert meta["enricher_total"] == 5,         f"enricher_total must be the eligible count, got {meta}"


def launch_enricher_total(run_dir_str, flags, total):
    """launch_enricher, but with the sheet row count passed in - a FIRST
    launch hands start_enricher the count of the INPUT sheet."""
    import subprocess
    from pathlib import Path as _P
    import gui

    class _Proc:
        pid = 9999

    real = subprocess.Popen
    subprocess.Popen = lambda *a, **k: _Proc()
    try:
        gui.start_enricher(gui.run_paths(_P(run_dir_str)), list(flags),
                           int(total))
    finally:
        subprocess.Popen = real


def _first_launch_total(tmp, flags, sheet_rows=300):
    """enricher_total after a launch with NO <stem>_filled.xlsx on disk."""
    run = tmp / "runs" / f"fresh{len(flags)}"
    run.mkdir(parents=True)
    (run / "input.xlsx").write_bytes(b"")
    assert not (run / "input_filled.xlsx").exists()   # the first-launch case
    at = AppTest.from_function(launch_enricher_total,
                               args=(str(run), flags, sheet_rows))
    at.run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    meta = json.loads((run / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["total_rows"] == sheet_rows          # still the sheet count
    return meta["enricher_total"]


def test_first_launch_with_limit_caps_the_eta_denominator(tmp):
    """P2-28 on the most common path of all: the documented `--limit 5` smoke
    run over a 300-row list. eligible_total used to bail to the full count
    because input_filled.xlsx does not exist yet, so the panel showed
    '3 / 300' and an ETA ~50x too long. --limit needs no sheet to be read."""
    assert _first_launch_total(tmp, ["--limit", "5"]) == 5


def test_first_launch_without_limit_still_counts_the_whole_sheet(tmp):
    """The cap is the ONLY thing knowable without the sheet: an uncapped
    first run must keep falling back to every row in the list."""
    assert _first_launch_total(tmp, []) == 300


# ------------------------------------------------- step 5 dry-run gate --


def _write_publish_state(run, rows, scope):
    """Write the CSV plus the run_meta Step 5 demands before it will show a
    Dry run button - i.e. exactly what a Step 3 build + Step 4 validate
    leave behind for that scope."""
    (run / "wordpress_ready.csv").write_text(
        "sku,title\n" + "".join(f'"{s}","{t}"\n' for s, t in rows),
        encoding="utf-8")
    (run / "run_meta.json").write_text(
        json.dumps({"validated": True, "issue_count": 0,
                    "built_scope": scope,
                    "built_skus": [s for s, _ in rows]}), encoding="utf-8")


def publish_wizard(runs_dir, calls):
    """AppTest entry: the wizard with gui.run_publish stubbed out. No
    subprocess, no store, no network - every invocation is just recorded so
    the test can assert what would have been sent."""
    import gui
    gui.RUNS_DIR = runs_dir

    def _fake_run_publish(P, extra):
        calls.append(list(extra))
        return 0, "DRY RUN\n"

    gui.run_publish = _fake_run_publish
    gui.main()


def _open_step5(runs, calls, timeout=60, preflight=True):
    at = AppTest.from_function(publish_wizard, args=(runs, calls))
    at.run(timeout=timeout)
    labels = [str(o) for o in at.sidebar.radio[0].options]
    assert "🔒" not in labels[4], labels
    at.sidebar.radio[0].set_value(labels[4]).run(timeout=timeout)
    if preflight:
        # the dry run is disabled until preflight passes in this session
        _click(at, "Preflight", timeout=timeout)
    return at


# what the client's store URL has to be for Step 5 to render at all: a
# WC_URL naming another host is refused before any button (item 3.2)
STORE_URL = "https://shreejeeelectronics.in"


def _click(at, needle, timeout=60):
    hits = [b for b in at.button if needle in str(b.label)]
    assert hits, f"{needle!r} missing: {[str(b.label) for b in at.button]}"
    hits[0].click().run(timeout=timeout)
    return at


def _commit_armed(at) -> bool:
    """The commit section only renders past the dry-run gate."""
    return any("I reviewed the dry-run output" in str(c.label)
               for c in at.checkbox)


def _page_text(at) -> str:
    els = list(getattr(at, "caption", [])) + list(getattr(at, "markdown", []))
    return " ".join(str(e.value) for e in els)


def test_dry_run_unlock_does_not_survive_a_rebuild(tmp):
    """Step 3 re-stamps built_scope/built_skus on EVERY rebuild, so Step 5's
    two scope guards pass again on the newly built CSV. While the dry-run
    flag was a bare bool it was still sitting at True, and Commit stayed
    armed for rows nobody had dry-run:

        build 'Confirmed rows only' (2 rows) -> dry run -> back to Step 3,
        rebuild 'All FOUND rows' (3 rows) -> Commit armed, 3 SKUs commit,
        one of them never dry-run.

    The flag is now the csv_signature of the file that was dry-run, compared
    against the file on disk at render time - which also re-locks a second
    tab, where this session's state was never visible at all."""
    import os
    import gui

    runs = tmp / "runs"
    run = runs / "pubgate"
    run.mkdir(parents=True)
    (run / "input.xlsx").write_bytes(b"")
    make_filled(run / "input_filled.xlsx")
    confirmed_rows = [("WM-SKU-A", "Samsung 7 kg Washer"),
                      ("RF-SKU-B", "LG 190 L Fridge")]
    _write_publish_state(run, confirmed_rows, "confirmed")

    calls = []
    real_run_publish = gui.run_publish
    saved = {k: os.environ.get(k) for k in ("WC_URL", "WC_KEY", "WC_SECRET")}
    os.environ.update({"WC_URL": STORE_URL,
                       "WC_KEY": "ck_test", "WC_SECRET": "cs_test"})
    try:
        at = _open_step5(runs, calls)
        assert_no_exception(at)
        assert not _commit_armed(at), "Commit armed before any dry run"

        _click(at, "Dry run")
        assert_no_exception(at)
        assert _commit_armed(at), "a clean dry run must unlock Commit"

        # --- the replay: Step 3 rebuilt, wider scope, same session ---
        _write_publish_state(run, confirmed_rows + [("AC-SKU-C",
                                                     "Voltas 1.5 T AC")],
                             "all_found")
        at.run(timeout=60)
        assert_no_exception(at)
        assert not _commit_armed(at), \
            "Commit stayed armed on a CSV that was never dry-run"
        assert "rebuilt since the last dry run" in _page_text(at), \
            _page_text(at)
        assert all("--commit" not in c for c in calls), calls

        # --- and dry-running the NEW csv re-arms it ---
        _click(at, "Dry run")
        assert_no_exception(at)
        assert _commit_armed(at), "dry-running the rebuilt CSV must re-unlock"
        assert sum("AC-SKU-C" in c for c in calls) == 1, calls
    finally:
        gui.run_publish = real_run_publish
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.update({k: v})


# ------------------------------------------------------------------ P3-50 --


def _lock_share_read(path: Path):
    """Open `path` the way Excel does - readable by others, NOT writable.

    Plain open() would not do: Python opens with FILE_SHARE_WRITE, so
    to_csv succeeds and the bug cannot be reproduced."""
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = ctypes.c_void_p
    h = k32.CreateFileW(str(path), ctypes.c_uint(0x80000000),   # GENERIC_READ
                        ctypes.c_uint(0x00000001),              # SHARE_READ
                        None, ctypes.c_uint(3),                 # OPEN_EXISTING
                        ctypes.c_uint(0x80), None)
    assert h and h != 2 ** 64 - 1, "could not lock the scratch CSV"
    return h


def _unlock(h):
    import ctypes
    ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))


def build_wizard(runs_dir):
    """AppTest entry: the wizard with app.process_inventory stubbed, so Build
    writes a tiny frame and downloads nothing."""
    import os
    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    import pandas as pd
    import app
    import gui
    gui.RUNS_DIR = runs_dir
    real = app.process_inventory
    app.process_inventory = lambda df, **kw: pd.DataFrame(
        [{"sku": "WM-SKU-A", "post_title": "Samsung 7 kg Washer",
          "Images": "", "_spec_gaps": "", "regular price": "25000",
          "sale price": "22599", "Brands": "Samsung", "categories": ""}])
    try:
        gui.main()
    finally:
        app.process_inventory = real


def _step3(runs, timeout=90):
    at = AppTest.from_function(build_wizard, args=(runs,))
    at.run(timeout=timeout)
    labels = [str(o) for o in at.sidebar.radio[0].options]
    at.sidebar.radio[0].set_value(labels[2]).run(timeout=timeout)
    return at


def test_build_says_so_when_the_csv_is_open_elsewhere(tmp):
    """P3-50: wordpress_ready.csv open in Excel made to_csv raise a raw
    PermissionError traceback AFTER every image had been downloaded, and
    update_meta never ran - so the run silently stayed at its old stage with
    no explanation on the page."""
    import gui
    runs = tmp / "runs"
    run = seeded_run(runs, "locked", with_csv=True)
    # the build is approved-only by default: approve one row first
    _tick(gui.run_paths(run), "SKU-A")
    at = _step3(runs)
    assert_no_exception(at)

    h = _lock_share_read(run / "wordpress_ready.csv")
    try:
        build = next(b for b in at.button if "Build CSV" in str(b.label))
        at = build.click().run(timeout=90)
    finally:
        _unlock(h)

    assert_no_exception(at)
    assert any("Could not write wordpress_ready.csv" in str(e.value)
               and "Excel" in str(e.value) for e in at.error), \
        [str(e.value) for e in at.error]
    meta_path = run / "run_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) \
        if meta_path.exists() else {}
    assert "built_skus" not in meta and not meta.get("validated"), meta


def test_build_writes_the_csv_when_nothing_holds_it(tmp):
    """The other half of the guard: the same path with no lock must still
    build, validate and stamp the meta."""
    import gui
    runs = tmp / "runs"
    run = seeded_run(runs, "unlocked")
    # the build is approved-only by default: approve one row first
    _tick(gui.run_paths(run), "SKU-A")
    at = _step3(runs)
    build = next(b for b in at.button if "Build CSV" in str(b.label))
    at = build.click().run(timeout=90)
    assert_no_exception(at)
    assert not [str(e.value) for e in at.error], [str(e.value)
                                                  for e in at.error]
    meta = json.loads((run / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["built_skus"] == ["WM-SKU-A"] and meta["validated"] is True


# --------------------------------------- approved-only build + skip state --


def spy_build_wizard(runs_dir, calls):
    """build_wizard, but process_inventory RECORDS the raw skus it was handed
    - that frame is exactly what the build turns into CSV rows and images."""
    import os
    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    import pandas as pd
    import app
    import gui
    gui.RUNS_DIR = runs_dir
    real = app.process_inventory

    def _spy(df, **kw):
        calls.append([str(s) for s in df["sku"].tolist()])
        return pd.DataFrame(
            [{"sku": "WM-" + str(s), "post_title": "x", "Images": "",
              "_spec_gaps": "", "regular price": "25000",
              "sale price": "22599", "Brands": "Samsung", "categories": ""}
             for s in df["sku"]])

    app.process_inventory = _spy
    try:
        gui.main()
    finally:
        app.process_inventory = real


def _spy_step3(runs, calls, timeout=90):
    at = AppTest.from_function(spy_build_wizard, args=(runs, calls))
    at.run(timeout=timeout)
    labels = [str(o) for o in at.sidebar.radio[0].options]
    at.sidebar.radio[0].set_value(labels[2]).run(timeout=timeout)
    return at


def _scope_radio(at):
    return next(r for r in at.radio if "Rows to include" in str(r.label))


def _build_button(at):
    return next(b for b in at.button if "Build CSV" in str(b.label))


def test_zero_approvals_defaults_to_confirmed_and_blocks_the_build(tmp):
    """The owner's design: only approved rows go on. With nothing approved
    the default used to flip to 'All FOUND rows' and build every FOUND row
    unseen; now the default stays 'Confirmed rows only' and Build is off."""
    runs = tmp / "runs"
    seeded_run(runs, "noapprovals")
    calls = []
    at = _spy_step3(runs, calls)
    assert_no_exception(at)
    assert _scope_radio(at).value == "confirmed"
    assert any("Approve at least one row in step 2 - Review" in str(e.value)
               for e in at.error), [str(e.value) for e in at.error]
    assert _build_button(at).disabled, "Build must be off with 0 approvals"
    assert any("Approved 0 · Skipped 0 · Not reviewed 2 · Not FOUND 1"
               in str(i.value) for i in at.info), [str(i.value)
                                                   for i in at.info]
    assert calls == []


def test_default_build_sends_only_the_approved_row(tmp):
    import gui
    runs = tmp / "runs"
    run = seeded_run(runs, "approvedonly")
    P = gui.run_paths(run)
    gui.save_confirmed(P, {"SKU-A": True}, gui.load_filled_df(P),
                       {"SKU-B": "duplicate photos"})
    calls = []
    at = _spy_step3(runs, calls)
    assert_no_exception(at)
    assert _scope_radio(at).value == "confirmed"
    assert any("Approved 1 · Skipped 1 · Not reviewed 0 · Not FOUND 1"
               in str(i.value) for i in at.info), [str(i.value)
                                                   for i in at.info]
    assert any("Skipped rows (1)" in str(e.label) for e in at.expander)
    at = _build_button(at).click().run(timeout=90)
    assert_no_exception(at)
    assert calls == [["SKU-A"]], calls
    assert any("from the 1 approved row(s) only" in str(i.value)
               for i in at.info), [str(i.value) for i in at.info]
    meta = json.loads((run / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["built_scope"] == "confirmed"


def test_all_found_scope_still_excludes_the_skipped_row(tmp):
    import gui
    runs = tmp / "runs"
    run = seeded_run(runs, "allfound")
    P = gui.run_paths(run)
    gui.save_confirmed(P, {"SKU-A": True}, gui.load_filled_df(P),
                       {"SKU-B": ""})
    calls = []
    at = _spy_step3(runs, calls)
    _scope_radio(at).set_value("all_found").run(timeout=90)
    assert_no_exception(at)
    assert any("includes rows nobody approved" in str(w.value)
               for w in at.warning), [str(w.value) for w in at.warning]
    assert not _build_button(at).disabled
    at = _build_button(at).click().run(timeout=90)
    assert_no_exception(at)
    assert calls == [["SKU-A"]], calls
    assert any("all FOUND rows minus 1 skipped" in str(i.value)
               for i in at.info), [str(i.value) for i in at.info]


def test_card_skip_stores_the_skip_with_its_reason(tmp):
    """Card mode: Skip is a stored decision, not just 'next card'."""
    import gui
    runs = tmp / "runs"
    run = seeded_run(runs, "cardskip")
    P = gui.run_paths(run)
    at = boot(runs)
    step2(at)
    next(r for r in at.radio if "View" in str(r.label)).set_value(
        "Card review").run(timeout=60)
    assert_no_exception(at)
    # first card is SKU-B (review bucket sorts first)
    reason = next(t for t in at.text_input if "Reason to skip" in str(t.label))
    reason.set_value("gallery is another model").run(timeout=60)
    next(b for b in at.button if "Skip" in str(b.label)).click().run(
        timeout=60)
    assert_no_exception(at)
    assert gui.skipped_map(P) == {"SKU-B": "gallery is another model"}
    assert gui.confirmed_set(P) == set()
    # approving it later clears the skip (three-state rule)
    gui.save_confirmed(P, {"SKU-B": True}, None, {})
    assert gui.skipped_map(P) == {} and gui.confirmed_set(P) == {"SKU-B"}


# ------------------------------------------------------------------ P3-51 --


def cancel_entry(run_dir_str, calls, pid, started):
    """AppTest entry: gui.cancel_enricher with os.kill, subprocess.run and
    time.sleep stubbed - nothing is ever signalled for real."""
    import os
    import subprocess
    import time
    from pathlib import Path as _P
    import gui

    P = gui.run_paths(_P(run_dir_str))
    gui.update_meta(P, enricher_pid=pid, enricher_started=started)
    real = (os.kill, subprocess.run, time.sleep)
    os.kill = lambda *a, **k: calls.append(("kill",) + a)
    subprocess.run = lambda *a, **k: calls.append(("run",) + a)
    time.sleep = lambda *a, **k: None
    try:
        gui.cancel_enricher(P)
    finally:
        os.kill, subprocess.run, time.sleep = real


def _cancel(tmp, name, started_delta=None):
    """Run Stop against THIS process's pid. started_delta None => the true
    creation time (we are 'our own' enricher); a number => that many seconds
    off, i.e. a recycled pid."""
    import os
    import gui
    run = tmp / "runs" / name
    run.mkdir(parents=True)
    created = gui._pid_created(os.getpid())
    assert created, "GetProcessTimes unavailable - cannot test the identity check"
    started = created if started_delta is None else created - started_delta
    calls = []
    at = AppTest.from_function(cancel_entry,
                               args=(str(run), calls, os.getpid(), started))
    at.run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    meta = json.loads((run / "run_meta.json").read_text(encoding="utf-8"))
    return calls, meta


def test_stop_never_signals_a_recycled_pid(tmp):
    """P3-51: cancel_enricher checked pid_alive WITHOUT enricher_started, so
    a stale pid in run_meta.json (GUI reloaded, second tab, restarted
    mid-batch) that Windows had handed to some other python - another
    Streamlit, a build_csv.py run - was CTRL_BREAKed and then taskkill /T /F
    with its whole tree."""
    calls, meta = _cancel(tmp, "recycled", started_delta=100000)
    assert calls == [], calls
    assert meta["enricher_pid"] is None, meta


def test_stop_still_breaks_our_own_enricher(tmp):
    """The identity check must not disarm Stop on the real child."""
    import os
    import signal
    calls, meta = _cancel(tmp, "ours")
    assert calls and calls[0] == ("kill", os.getpid(),
                                  signal.CTRL_BREAK_EVENT), calls
    # the stubbed CTRL_BREAK cannot end this process, so the 15 s grace
    # expires and the hard kill follows - both are recorded, never run
    assert any(c[0] == "run" and "taskkill" in c[1] for c in calls), calls
    assert meta["enricher_pid"] is None, meta


# ------------------------------------------------------------------ P3-53 --


def _tick(P, sku):
    """Confirm one SKU exactly as Step 2's save path does - flags merged from
    the sheet as it stands, fingerprints hashed from that same frame."""
    import gui
    flags = {s: True for s in gui.confirmed_set(P)}
    flags[str(sku)] = True
    gui.save_confirmed(P, flags, gui.load_filled_df(P))


def _re_enrich_row(path: Path, sku: str):
    """What --retry-incomplete does to a 'FOUND - only 1 image(s)' row: fills
    the price and replaces the gallery, in place."""
    wb = openpyxl.load_workbook(path)
    ws = wb.active
    head = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    for row in ws.iter_rows(min_row=2):
        if str(row[head.index("sku")].value) == sku:
            row[head.index("sale price")].value = 17499
            row[head.index("image_url")].value = \
                "https://example.com/new1.jpg,https://example.com/new2.jpg"
    wb.save(path)


def test_a_re_enriched_row_loses_its_tick(tmp):
    """P3-53: a tick on a suffixed-FOUND row survived 'Re-enrich incomplete
    rows', which rewrites exactly that bucket in place - so 'Confirmed rows
    only' built re-scraped data nobody reviewed, rejected images included."""
    import gui
    runs = tmp / "runs"
    run = seeded_run(runs, "retried")
    P = gui.run_paths(run)
    _tick(P, "SKU-B")                      # FOUND (flipkart) - only 1 image(s)
    assert gui.confirmed_set(P) == {"SKU-B"}
    _re_enrich_row(P["filled"], "SKU-B")

    at = boot(runs)
    labels = [str(o) for o in at.sidebar.radio[0].options]
    at.sidebar.radio[0].set_value(labels[1]).run(timeout=60)   # Step 2
    assert_no_exception(at)
    assert any("confirmation(s) dropped" in str(w.value) for w in at.warning), \
        [str(w.value) for w in at.warning]
    assert gui.confirmed_set(P) == set(), \
        json.loads(P["review"].read_text(encoding="utf-8"))

    at.sidebar.radio[0].set_value(labels[2]).run(timeout=60)   # Step 3
    assert_no_exception(at)
    scope = next(r for r in at.radio
                 if any("Confirmed rows only" in str(o) for o in r.options))
    assert any("Confirmed rows only (0)" in str(o) for o in scope.options), \
        [str(o) for o in scope.options]


def test_an_untouched_tick_survives_a_reload(tmp):
    """The guard may not fire on a sheet nobody re-enriched."""
    import gui
    runs = tmp / "runs"
    run = seeded_run(runs, "stable")
    P = gui.run_paths(run)
    _tick(P, "SKU-B")
    at = boot(runs)
    labels = [str(o) for o in at.sidebar.radio[0].options]
    at.sidebar.radio[0].set_value(labels[1]).run(timeout=60)
    assert_no_exception(at)
    assert not any("confirmation(s) dropped" in str(w.value)
                   for w in at.warning), [str(w.value) for w in at.warning]
    assert gui.confirmed_set(P) == {"SKU-B"}


def test_step_3_reader_agrees_with_step_2_on_the_fingerprint(tmp):
    """Step 2 reads the sheet dtype=str + to_numeric, Step 3 with a plain
    read_excel, and the reconcile runs on BOTH - so a price cell must hash
    the same either way, or every tick would be dropped at build time."""
    import pandas as pd
    import gui
    run = seeded_run(tmp / "runs", "readers")
    P = gui.run_paths(run)
    _tick(P, "SKU-B")
    # the step-3 reader, unchanged sheet: nothing may be dropped
    assert gui.reconcile_confirmations(P, pd.read_excel(P["filled"])) == 0
    assert gui.confirmed_set(P) == {"SKU-B"}
    _re_enrich_row(P["filled"], "SKU-B")
    assert gui.reconcile_confirmations(P, pd.read_excel(P["filled"])) == 1
    assert gui.confirmed_set(P) == set()


def test_legacy_bool_review_state_still_counts_and_is_never_stale(tmp):
    """review_state.json written before P3-53 holds bare bools. They keep
    their tick (there is no fingerprint to compare) and must not be dropped
    on the next render."""
    import gui
    runs = tmp / "runs"
    run = seeded_run(runs, "legacy")
    P = gui.run_paths(run)
    P["review"].write_text(json.dumps({"confirmed": {"SKU-B": True,
                                                     "SKU-A": False}}),
                           encoding="utf-8")
    assert gui.confirmed_set(P) == {"SKU-B"}
    _re_enrich_row(P["filled"], "SKU-B")
    at = boot(runs)
    labels = [str(o) for o in at.sidebar.radio[0].options]
    at.sidebar.radio[0].set_value(labels[1]).run(timeout=60)
    assert_no_exception(at)
    assert gui.confirmed_set(P) == {"SKU-B"}


# ------------------------------------- Phase 1 item 5: review telemetry --

def wizard_edited(runs_dir, sku=None, edits=None, confirm=None):
    """AppTest cannot type into a data_editor, so the editors are wrapped:
    every frame they hand back gets the edit/tick applied to one SKU, which
    is exactly the state the Save button reads."""
    import gui
    gui.RUNS_DIR = runs_dir
    real = gui.review_editor

    def patched(df, key, cat_options=None, allow_confirm=True):
        out = real(df, key, cat_options, allow_confirm)
        if sku is None or out is None or "sku" not in out.columns:
            return out
        out = out.copy()
        hit = out["sku"].astype(str) == str(sku)
        for col, val in (edits or {}).items():
            if col in out.columns:
                out.loc[hit, col] = val
        if confirm is not None and allow_confirm:
            out.loc[hit, "confirmed"] = bool(confirm)
        return out

    gui.review_editor = patched
    try:
        gui.main()
    finally:
        gui.review_editor = real


def step2(at, timeout=60):
    labels = [str(o) for o in at.sidebar.radio[0].options]
    at.sidebar.radio[0].set_value(labels[1]).run(timeout=timeout)
    return at


def events_of(P):
    return json.loads(P["review"].read_text(encoding="utf-8")).get("events", [])


def test_table_save_logs_an_edit_and_a_tick(tmp):
    """One save that changes a title and ticks the row logs two table
    events and lands review_metrics.json beside review_state.json."""
    import gui
    runs = tmp / "runs"
    run = seeded_run(runs, "telemetry")
    P = gui.run_paths(run)
    at = AppTest.from_function(
        wizard_edited,
        args=(runs, "SKU-A", {"title": "Samsung 7 kg Washer (edited)"}, True))
    at.run(timeout=60)
    step2(at)
    assert_no_exception(at)
    save = next(b for b in at.button
                if "Save edits & confirmations" in str(b.label))
    save.click().run(timeout=60)
    assert_no_exception(at)

    evs = events_of(P)
    assert len(evs) == 2, evs
    assert {e["action"] for e in evs} == {"edit", "approve"}, evs
    assert all(e["mode"] == "table" and e["sku"] == "SKU-A" for e in evs), evs
    assert all(e["secs"] >= 0 for e in evs), evs
    assert gui.confirmed_set(P) == {"SKU-A"}
    assert P["metrics"].exists(), "review_metrics.json was not written"
    m = json.loads(P["metrics"].read_text(encoding="utf-8"))
    assert m["rows_touched"] == 1 and m["actions"]["edit"] == 1


def test_a_pasted_url_is_logged_as_url_pasted(tmp):
    """A url_link filled in on a failed row is the NEEDS LINK remediation,
    not a cosmetic edit - the metrics have to tell them apart."""
    import gui
    runs = tmp / "runs"
    run = seeded_run(runs, "pasted")
    P = gui.run_paths(run)
    at = AppTest.from_function(
        wizard_edited,
        args=(runs, "SKU-C", {"url_link": "https://example.com/c"}, None))
    at.run(timeout=60)
    step2(at)
    next(b for b in at.button
         if "Save edits & confirmations" in str(b.label)).click().run(
        timeout=60)
    assert_no_exception(at)
    evs = events_of(P)
    assert [e["action"] for e in evs] == ["url_pasted"], evs
    assert evs[0]["sku"] == "SKU-C" and evs[0]["mode"] == "table"


def test_card_approve_and_skip_log_card_events(tmp):
    """Card mode times each card on its own: Approve books the seconds the
    card was on screen, Skip still costs the reviewer that time."""
    import gui
    runs = tmp / "runs"
    run = seeded_run(runs, "cards")
    P = gui.run_paths(run)
    at = boot(runs)
    step2(at)
    view = next(r for r in at.radio if "View" in str(r.label))
    view.set_value("Card review").run(timeout=60)
    assert_no_exception(at)

    approve = next(b for b in at.button if "Approve" in str(b.label))
    assert not approve.disabled, "the first card is a FOUND row"
    approve.click().run(timeout=60)
    assert_no_exception(at)
    evs = events_of(P)
    assert evs and evs[0]["action"] == "approve", evs
    assert evs[0]["mode"] == "card" and evs[0]["sku"] == "SKU-B", evs
    assert evs[0]["secs"] >= 0, evs             # review bucket sorts first
    # a card that also rewrote a field logs the extra action at 0 s: the
    # card's time is billed once, to the primary event
    assert all(e["secs"] == 0 for e in evs[1:]), evs
    assert {e["sku"] for e in evs} == {"SKU-B"}, evs
    assert gui.confirmed_set(P) == {"SKU-B"}

    n_before = len(evs)
    next(b for b in at.button if "Skip" in str(b.label)).click().run(
        timeout=60)
    assert_no_exception(at)
    evs = events_of(P)
    assert len(evs) == n_before + 1, evs
    assert evs[-1]["action"] == "skip" and evs[-1]["mode"] == "card", evs
    assert evs[-1]["secs"] >= 0, evs
    m = json.loads(P["metrics"].read_text(encoding="utf-8"))
    assert m["actions"]["approve"] == 1 and m["actions"]["skip"] == 1, m


def test_step2_shows_the_review_caption_on_reload(tmp):
    """The caption is read back from review_metrics.json, so a fresh browser
    session (or another operator) sees the time already spent."""
    import gui
    runs = tmp / "runs"
    run = seeded_run(runs, "caption")
    P = gui.run_paths(run)
    state = {"confirmed": {}, "events": [
        {"sku": "SKU-A", "action": "edit", "mode": "table",
         "at": "2026-09-02T10:00:00", "secs": 120.0},
        {"sku": "SKU-B", "action": "approve", "mode": "card",
         "at": "2026-09-02T10:02:00", "secs": 60.0}]}
    P["review"].write_text(json.dumps(state), encoding="utf-8")
    gui.save_json(P["metrics"],
                  gui.review_metrics(state, gui.load_filled_df(P)))
    at = boot(runs)
    step2(at)
    assert_no_exception(at)
    caps = [str(c.value) for c in at.caption]
    assert any("Review so far:" in c and "min/SKU" in c and "needs-human" in c
               for c in caps), caps


def test_a_legacy_review_state_still_renders_step_2(tmp):
    """review_state.json from before item 5 has no events key and no metrics
    file; step 2 must render, and the first save must grow one."""
    import gui
    runs = tmp / "runs"
    run = seeded_run(runs, "legacyevents")
    P = gui.run_paths(run)
    P["review"].write_text(json.dumps({"confirmed": {"SKU-A": True}}),
                           encoding="utf-8")
    at = AppTest.from_function(
        wizard_edited, args=(runs, "SKU-A", {"title": "Retitled"}, None))
    at.run(timeout=60)
    step2(at)
    assert_no_exception(at)
    assert not any("Review so far:" in str(c.value) for c in at.caption)
    next(b for b in at.button
         if "Save edits & confirmations" in str(b.label)).click().run(
        timeout=60)
    assert_no_exception(at)
    assert [e["action"] for e in events_of(P)] == ["edit"], events_of(P)
    # the legacy tick survived the new key landing beside it
    assert gui.confirmed_set(P) == {"SKU-A"}


# ------------------------------------------------- per-client wiring (P1) --
# docs/CLIENT_CONFIG.md items 1-4. Everything here is offline: no subprocess
# (Popen / gui.run_publish are stubbed), no store, no network, and every
# global that is rebound is restored - the rest of the suite runs as
# 'shreejee' and would fail against another client's prefixes or taxonomy.

ACME_TOML = """\
[client]
slug = "acme"
name = "Acme Appliances"

[store]
url = "https://acme.example"
media_base = "{url}/wp-content/uploads/{yyyy}/{mm}"
seo_site_name = "Acme Appliances"

[taxonomy]
category_map = "category_map.xlsx"

[sku]
fallback_prefix = "AP"
never_rename = ["WM"]
strip_codes = ["DW"]

[sku.prefix_by_parent]
"Washing Machines" = "WM"
"Kitchen Appliances" = "KA"

[sku.input_codes]
WM = ["Washing Machines", ""]
"""


def make_clients_dir(tmp: Path) -> Path:
    """A scratch clients\\ holding the real shreejee config plus a synthetic
    second client, so a two-client test never touches the repo's own
    clients\\ directory.

    Both clients get a REAL category_map.xlsx - shreejee's, copied - because
    load_client() refuses a client whose taxonomy file is missing and
    app.configure() reads it for real."""
    src = ROOT / "clients" / "shreejee"
    dest = tmp / "clients"
    (dest / "shreejee").mkdir(parents=True)
    shutil.copy2(src / "client.toml", dest / "shreejee" / "client.toml")
    shutil.copy2(src / "category_map.xlsx",
                 dest / "shreejee" / "category_map.xlsx")
    (dest / "acme").mkdir()
    (dest / "acme" / "client.toml").write_text(ACME_TOML, encoding="utf-8")
    shutil.copy2(src / "category_map.xlsx",
                 dest / "acme" / "category_map.xlsx")
    return dest


def seed_run_for(runs: Path, name: str, slug: str) -> Path:
    run = runs / name
    run.mkdir(parents=True)
    (run / "run_meta.json").write_text(json.dumps({"client": slug}),
                                       encoding="utf-8")
    return run


def client_wizard(runs_dir, clients_dir):
    import client_config
    import gui
    gui.RUNS_DIR = runs_dir
    client_config.CLIENTS_DIR = clients_dir
    gui.main()


def guard_wizard(runs_dir, clients_dir, run_name):
    """Open ONE named run under whatever client the selector resolves to -
    the code path the run list deliberately hides, so it still needs a
    test of its own."""
    import client_config
    import gui
    gui.RUNS_DIR = runs_dir
    client_config.CLIENTS_DIR = clients_dir
    cfg = gui.current_client()
    gui.guard_run_client(runs_dir / run_name, cfg)


def _errors(at) -> str:
    return " ".join(str(e.value) for e in at.error)


def test_client_selector_defaults_to_shreejee_and_titles_the_page(tmp):
    """Item 1: the selector is a sidebar control, starts on the default
    client, and its NAME (not a hard-coded 'Shree Jee') is the heading."""
    runs = tmp / "runs"
    runs.mkdir()
    at = boot(runs)
    assert_no_exception(at)
    picker = client_picker(at)
    assert str(picker.label) == "Client"
    assert picker.value == "shreejee", picker.value
    assert "shreejee" in [str(o) for o in picker.options]
    assert any("Shree Jee Electronics" in str(t.value) for t in at.title), \
        [str(t.value) for t in at.title]


def test_a_run_of_another_client_is_hidden_from_the_list(tmp):
    """Item 1: the run list is scoped to the selected client. A legacy run
    with no `client` key counts as the default client and stays visible."""
    import client_config

    runs = tmp / "runs"
    clients = make_clients_dir(tmp)
    seed_run_for(runs, "acme_batch", "acme")
    seed_run_for(runs, "sj_batch", "shreejee")
    (runs / "legacy_batch").mkdir()            # no run_meta.json at all

    old_dir = client_config.CLIENTS_DIR
    try:
        at = AppTest.from_function(client_wizard, args=(runs, clients))
        at.run(timeout=60)
        assert_no_exception(at)
        options = [str(o) for o in run_picker(at).options]
        assert "sj_batch" in options and "legacy_batch" in options, options
        assert "acme_batch" not in options, options
    finally:
        client_config.CLIENTS_DIR = old_dir


def test_opening_another_clients_run_is_refused_naming_both_slugs(tmp):
    """Item 1: and if one is reached anyway, the refusal names BOTH slugs -
    the run's owner and the session's - because the fix is to switch back,
    never to re-home the run."""
    import client_config

    runs = tmp / "runs"
    clients = make_clients_dir(tmp)
    seed_run_for(runs, "acme_batch", "acme")

    old_dir = client_config.CLIENTS_DIR
    try:
        at = AppTest.from_function(guard_wizard,
                                   args=(runs, clients, "acme_batch"))
        at.run(timeout=60)
        assert_no_exception(at)
        text = _errors(at)
        assert "acme" in text and "shreejee" in text, text
    finally:
        client_config.CLIENTS_DIR = old_dir


def test_switching_client_retitles_the_page_and_rebinds_the_sku_codes(tmp):
    """Item 1 + item 2: moving the selector reconfigures the whole process -
    the heading, app.py's taxonomy and media base, and gui_helpers' SKU
    prefixes - not just a label. Everything is restored: the rest of the
    suite runs as shreejee."""
    import client_config
    import gui_helpers

    runs = tmp / "runs"
    runs.mkdir()
    clients = make_clients_dir(tmp)

    import os

    # both of the modules switch_client() reconfigures, imported here so the
    # restore below is unconditional - leaving validate_csv on 'acme' breaks
    # tests/test_client_config.py two files later
    import app                                  # noqa: E402 (lazy on purpose)
    import validate_csv                         # noqa: E402
    old_dir = client_config.CLIENTS_DIR
    old_codes = tuple(gui_helpers.SKU_CODES)
    # switch_client mirrors the slug into the process env (that is how a
    # lazily-imported app / a subprocess is told) - restore it too, or the
    # NEXT test boots as 'acme'
    old_env = os.environ.get(client_config.ENV_VAR)
    try:
        at = AppTest.from_function(client_wizard, args=(runs, clients))
        at.run(timeout=60)
        assert_no_exception(at)
        client_picker(at).set_value("acme").run(timeout=60)
        assert_no_exception(at)
        assert any("Acme Appliances" in str(t.value) for t in at.title), \
            [str(t.value) for t in at.title]
        assert app.CLIENT.slug == "acme"
        assert validate_csv.CLIENT.slug == "acme"
        assert app.MEDIA_BASE.startswith("https://acme.example"), \
            app.MEDIA_BASE
        # acme's [sku] table has three codes, not shreejee's ten
        assert tuple(gui_helpers.SKU_CODES) == ("WM", "KA", "AP"), \
            gui_helpers.SKU_CODES
    finally:
        client_config.CLIENTS_DIR = old_dir
        if old_env is None:
            os.environ.pop(client_config.ENV_VAR, None)
        else:
            os.environ[client_config.ENV_VAR] = old_env
        app.configure("shreejee")
        validate_csv.configure("shreejee")
        gui_helpers.configure_sku_codes(old_codes)


def test_a_legacy_run_is_stamped_with_the_default_client_on_first_open(tmp):
    """Item 1: a run created before the selector existed is claimed ONCE, so
    the next operator cannot adopt it by moving the selector."""
    import client_config
    import gui

    runs = tmp / "runs"
    clients = make_clients_dir(tmp)
    (runs / "legacy_batch").mkdir(parents=True)

    old_dir = client_config.CLIENTS_DIR
    try:
        at = AppTest.from_function(guard_wizard,
                                   args=(runs, clients, "legacy_batch"))
        at.run(timeout=60)
        assert_no_exception(at)
        assert not _errors(at), _errors(at)
        meta = json.loads((runs / "legacy_batch" / "run_meta.json")
                          .read_text(encoding="utf-8"))
        assert meta["client"] == gui.DEFAULT_CLIENT, meta
    finally:
        client_config.CLIENTS_DIR = old_dir


# --------------------------------------------------------- item 4: budget --

def enrich_wizard(runs_dir, calls):
    """AppTest entry with gui.subprocess replaced by a recorder - nothing is
    launched, the argv and the child's env are just captured.

    AppTest.from_function re-executes only THIS function's source, so the
    stand-in has to be built inside it; module-level names are not in scope
    there."""
    import subprocess as _sp
    import types as _types

    import gui
    gui.RUNS_DIR = runs_dir

    class _FakeSubprocess:
        STDOUT = _sp.STDOUT
        CREATE_NEW_PROCESS_GROUP = getattr(
            _sp, "CREATE_NEW_PROCESS_GROUP", 0)

        @staticmethod
        def Popen(cmd, **kw):
            calls.append({"cmd": list(cmd),
                          "env": dict(kw.get("env") or {})})
            # pid 0 and a poll() that reports "exited": the panel then
            # renders the finished state instead of polling a live child
            return _types.SimpleNamespace(pid=0, poll=lambda: 0)

    gui.subprocess = _FakeSubprocess
    gui.main()


def test_enricher_launch_carries_the_client_and_the_budget(tmp):
    """Items 1 + 4: --client on the argv, PIPELINE_CLIENT in the child's
    env, and the Step-1 budget box becomes `--budget N` - stored in
    enricher_flags, so the Step-2 retry buttons inherit the same cap."""
    import gui

    runs = tmp / "runs"
    run = seeded_run(runs, "enrich")
    calls = []
    real_subprocess = gui.subprocess
    try:
        at = AppTest.from_function(enrich_wizard, args=(runs, calls))
        at.run(timeout=60)
        assert_no_exception(at)
        box = next(n for n in at.number_input
                   if "Serper credit budget" in str(n.label))
        box.set_value(5)
        next(b for b in at.button
             if "▶" in str(b.label)).click().run(timeout=60)
        assert_no_exception(at)
    finally:
        gui.subprocess = real_subprocess

    assert len(calls) == 1, calls
    cmd, env = calls[0]["cmd"], calls[0]["env"]
    assert "--client" in cmd and cmd[cmd.index("--client") + 1] == "shreejee"
    assert "--budget" in cmd and cmd[cmd.index("--budget") + 1] == "5", cmd
    assert env.get("PIPELINE_CLIENT") == "shreejee", env.get("PIPELINE_CLIENT")
    meta = json.loads((run / "run_meta.json").read_text(encoding="utf-8"))
    assert "--budget" in meta["enricher_flags"], meta["enricher_flags"]


# ------------------------------------------------- item 3: publish guards --

def publish_wizard_codes(runs_dir, calls, codes):
    """publish_wizard whose stub returns the exit codes in `codes`, one per
    wc_publish call, then 0.

    A list, not one code, and the wizard body owns the patch: AppTest
    re-executes this function on EVERY rerun, so a gui.run_publish swapped in
    from the test would be overwritten again on the next click."""
    import gui
    gui.RUNS_DIR = runs_dir

    def _fake(P, extra):
        calls.append(list(extra))
        code = codes.pop(0) if codes else 0
        return code, ("REFUSED\n" if code else "DRY RUN\n")

    gui.run_publish = _fake
    gui.main()


def _open_step5_codes(runs, calls, codes, timeout=60):
    at = AppTest.from_function(publish_wizard_codes, args=(runs, calls, codes))
    at.run(timeout=timeout)
    labels = [str(o) for o in at.sidebar.radio[0].options]
    at.sidebar.radio[0].set_value(labels[4]).run(timeout=timeout)
    return at


def _publish_run(tmp, name="pub", scope="confirmed", confirm=True):
    runs = tmp / "runs"
    run = seeded_run(runs, name)
    rows = [("WM-SKU-A", "Samsung 7 kg Washer"), ("RF-SKU-B", "LG 190 L")]
    _write_publish_state(run, rows, scope)
    if confirm:
        (run / "review_state.json").write_text(json.dumps({"confirmed": {
            "SKU-A": {"confirmed": True}, "SKU-B": {"confirmed": True}}}),
            encoding="utf-8")
    return runs, run


def _with_creds(fn, url=None):
    """Run fn() with WC_* set (and restored afterwards) - Step 5 renders
    nothing at all without them."""
    import os
    keys = ("WC_URL", "WC_KEY", "WC_SECRET")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update({"WC_URL": url or STORE_URL,
                       "WC_KEY": "ck_test", "WC_SECRET": "cs_test"})
    try:
        return fn()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_publish_refuses_a_wc_url_on_another_host(tmp):
    """Item 3.2: WC_URL and the client's own store must name one host. The
    refusal names BOTH and lands BEFORE any button - a stale shell export is
    exactly how one client's catalogue reaches another's store."""
    import gui

    runs, _ = _publish_run(tmp)
    calls = []
    real = gui.run_publish
    try:
        at = _with_creds(lambda: _open_step5(runs, calls, preflight=False),
                         url="https://someone-elses-store.example")
    finally:
        gui.run_publish = real
    assert_no_exception(at)
    text = _errors(at)
    assert "someone-elses-store.example" in text, text
    assert "shreejeeelectronics.in" in text, text
    assert not [b for b in at.button if "Dry run" in str(b.label)]
    assert not calls, calls


def test_preflight_must_pass_before_the_dry_run(tmp):
    """Item 3.6: the dry-run button is locked until preflight passes in this
    session, and preflight itself is a bare --preflight (no --sku)."""
    import gui

    runs, _ = _publish_run(tmp)
    calls = []
    real = gui.run_publish
    try:
        at = _with_creds(lambda: _open_step5(runs, calls, preflight=False))
        assert_no_exception(at)
        dry = next(b for b in at.button if "Dry run" in str(b.label))
        assert dry.disabled, "dry run must be locked before preflight"
        assert any("Run Preflight" in str(c.value) for c in at.caption)

        _click(at, "Preflight")
        assert_no_exception(at)
        assert calls == [["--preflight"]], calls
        _click(at, "Dry run")
        assert_no_exception(at)
    finally:
        gui.run_publish = real
    assert len(calls) == 2, calls
    assert "--preflight" not in calls[1] and "--commit" not in calls[1], calls


def test_exit_4_reports_that_nothing_was_written(tmp):
    """Item 3: exit 4 means 'a guard tripped and NOTHING was written',
    distinct from a run that failed part-way - and the page must say so."""
    import gui

    runs, _ = _publish_run(tmp)
    calls = []
    real = gui.run_publish
    try:
        at = _with_creds(lambda: _open_step5_codes(runs, calls, [4]))
        assert_no_exception(at)
        _click(at, "Preflight")
        assert any("Preflight FAILED" in str(e.value) for e in at.error), \
            _errors(at)
        # preflight refused -> the dry run stays locked, nothing is offered
        assert next(b for b in at.button
                    if "Dry run" in str(b.label)).disabled
    finally:
        gui.run_publish = real


def test_a_refused_dry_run_says_nothing_was_written(tmp):
    """The same exit code, on the button that has a three-way outcome:
    finished / refused / failed."""
    import gui

    runs, _ = _publish_run(tmp)
    calls = []
    real = gui.run_publish
    try:
        # preflight passes; the dry run behind it is refused
        at = _with_creds(lambda: _open_step5_codes(runs, calls, [0, 4]))
        assert_no_exception(at)
        _click(at, "Preflight")
        _click(at, "Dry run")
        assert_no_exception(at)
        assert any("refused" in str(e.value) and "nothing was written"
                   in str(e.value) for e in at.error), _errors(at)
    finally:
        gui.run_publish = real


def test_go_live_commit_writes_the_signoff_and_caps_the_blast_radius(tmp):
    """Items 3.3 + 3.4: a go-live commit passes --max-commit for exactly the
    scope the page showed, and leaves review_signoff.csv beside the CSV
    naming every SKU a human ticked."""
    import gui

    runs, run = _publish_run(tmp)
    calls = []
    real = gui.run_publish
    try:
        at = _with_creds(lambda: _open_step5(runs, calls))
        assert_no_exception(at)
        _click(at, "Dry run")
        next(c for c in at.checkbox
             if "I reviewed the dry-run output" in str(c.label)) \
            .check().run(timeout=60)
        at.text_input[0].set_value("pub").run(timeout=60)
        next(c for c in at.checkbox
             if "status publish" in str(c.label)).check().run(timeout=60)
        assert_no_exception(at)
        commit = next(b for b in at.button
                      if "Commit 2 products" in str(b.label))
        assert "LIVE" in str(commit.label), commit.label
        commit.click().run(timeout=60)
        assert_no_exception(at)
    finally:
        gui.run_publish = real

    commits = [c for c in calls if "--commit" in c]
    assert commits, calls
    assert "--max-commit" in commits[0], commits[0]
    assert commits[0][commits[0].index("--max-commit") + 1] == "2", commits[0]
    assert "--status" in commits[0] and "publish" in commits[0], commits[0]

    signoff = run / "review_signoff.csv"
    assert signoff.exists(), "go-live commit wrote no review_signoff.csv"
    lines = [ln for ln in signoff.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    assert lines[0].startswith("sku,reviewer,signed_at"), lines[0]
    assert sorted(ln.split(",")[0] for ln in lines[1:]) == ["RF-SKU-B",
                                                            "WM-SKU-A"], lines


def test_go_live_is_refused_while_any_row_is_unconfirmed(tmp):
    """Item 3.4: LIVE is the irreversible step, so an unticked row blocks it
    and is NAMED - and what remains offered is a DRAFT commit."""
    import gui

    runs, run = _publish_run(tmp, scope="all_found", confirm=False)
    calls = []
    real = gui.run_publish
    try:
        at = _with_creds(lambda: _open_step5(runs, calls))
        _click(at, "Dry run")
        next(c for c in at.checkbox
             if "I reviewed the dry-run output" in str(c.label)) \
            .check().run(timeout=60)
        at.text_input[0].set_value("pub").run(timeout=60)
        next(c for c in at.checkbox
             if "status publish" in str(c.label)).check().run(timeout=60)
        assert_no_exception(at)
        text = _errors(at)
        assert "not confirmed in Step 2" in text, text
        assert "RF-SKU-B" in text, text
        assert any("DRAFTS" in str(b.label) for b in at.button), \
            [str(b.label) for b in at.button]
    finally:
        gui.run_publish = real
    assert not (run / "review_signoff.csv").exists()


# ------------------------------------------------------------------ main --



# ------------------------------- item 1: an unusable client is never silent

def texts(at, kind):
    """Element text from the WHOLE page - these live in the sidebar, and the
    root getters and the sidebar getters do not overlap in every version."""
    seen, out = set(), []
    for block in (at, at.sidebar):
        for el in getattr(block, kind):
            v = str(el.value)
            if v not in seen:
                seen.add(v)
                out.append(v)
    return out


def add_broken_client(clients_dir, slug="nosku"):
    bad = Path(clients_dir) / slug
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "client.toml").write_text("this is not = = toml\n",
                                     encoding="utf-8")
    return bad


def bad_env_wizard(runs_dir, clients_dir):
    """The GUI booted with a TYPO in $PIPELINE_CLIENT."""
    import os
    import client_config
    import gui
    gui.RUNS_DIR = runs_dir
    client_config.CLIENTS_DIR = clients_dir
    os.environ[client_config.ENV_VAR] = "shreejeee"
    gui.main()


def failing_switch_wizard(runs_dir, clients_dir):
    """Selecting 'acme' fails to load - the config broke after the scan."""
    import client_config
    import gui
    gui.RUNS_DIR = runs_dir
    client_config.CLIENTS_DIR = clients_dir
    real = client_config.load_client

    def fake(slug=None, argv=None):
        if slug == "acme":
            raise client_config.ClientConfigError(
                "clients\\acme\\client.toml: [store] url is empty")
        return real(slug, argv)

    gui.load_client = fake              # gui imported it by name
    try:
        gui.main()
    finally:
        gui.load_client = real


def test_an_unknown_pipeline_client_falls_back_to_default_with_a_warning(tmp):
    """Item 1: a typo in $PIPELINE_CLIENT used to select whichever client
    sorted FIRST ('acme') and switch to it in silence - i.e. build for
    somebody else's store. It falls back to the DEFAULT client and says so."""
    import os
    import client_config

    runs = tmp / "runs"
    runs.mkdir()
    clients = make_clients_dir(tmp)
    old_dir = client_config.CLIENTS_DIR
    old_env = os.environ.get(client_config.ENV_VAR)
    try:
        at = AppTest.from_function(bad_env_wizard, args=(runs, clients))
        at.run(timeout=60)
        assert_no_exception(at)
        assert client_picker(at).value == gui_default_client(), \
            client_picker(at).value
        warned = " ".join(texts(at, "warning"))
        assert "'shreejeee'" in warned, warned
    finally:
        client_config.CLIENTS_DIR = old_dir
        if old_env is None:
            os.environ.pop(client_config.ENV_VAR, None)
        else:
            os.environ[client_config.ENV_VAR] = old_env


def test_a_broken_client_toml_is_not_offered_and_is_named_in_a_caption(tmp):
    """Item 1: an unloadable client.toml is not a choice - offering it just
    moves the failure to after the click. It is reported under the picker."""
    import client_config

    runs = tmp / "runs"
    runs.mkdir()
    clients = make_clients_dir(tmp)
    add_broken_client(clients)
    old_dir = client_config.CLIENTS_DIR
    try:
        at = AppTest.from_function(client_wizard, args=(runs, clients))
        at.run(timeout=60)
        assert_no_exception(at)
        options = [str(o) for o in client_picker(at).options]
        assert "nosku" not in options, options
        caption = " ".join(texts(at, "caption"))
        assert "nosku" in caption, caption
    finally:
        client_config.CLIENTS_DIR = old_dir


def test_a_failed_switch_names_the_client_being_switched_to(tmp):
    """Item 1: switch_client() raises BEFORE it moves CLIENT_KEY, so the
    page used to send the operator to the PREVIOUS client's client.toml -
    a file that is perfectly fine."""
    import client_config

    runs = tmp / "runs"
    runs.mkdir()
    clients = make_clients_dir(tmp)
    old_dir = client_config.CLIENTS_DIR
    try:
        at = AppTest.from_function(failing_switch_wizard,
                                   args=(runs, clients))
        at.run(timeout=60)
        assert_no_exception(at)
        client_picker(at).set_value("acme").run(timeout=60)
        assert_no_exception(at)
        text = _errors(at)
        assert "clients\\acme\\client.toml" in text, text
        assert "clients\\shreejee\\client.toml" not in text, text
    finally:
        client_config.CLIENTS_DIR = old_dir


def gui_default_client():
    import gui
    return gui.DEFAULT_CLIENT


# ------------------------- item 5: the metrics file cannot go stale in place

def set_status(path, sku, status):
    wb = openpyxl.load_workbook(path)
    ws = wb.active
    headers = [str(c.value) for c in ws[1]]
    si, ti = headers.index("sku"), headers.index("status")
    for row in ws.iter_rows(min_row=2):
        if str(row[si].value) == sku:
            row[ti].value = status
    wb.save(path)


def test_step_2_refreshes_stale_review_metrics_on_load(tmp):
    """Item 5: review_metrics.json used to be written ONLY on a review save,
    so a re-enrichment that rewrote statuses left the README and the enricher
    report quoting a needs-human percentage for a sheet that no longer
    existed. Opening step 2 recomputes it."""
    import gui
    runs = tmp / "runs"
    run = seeded_run(runs, "stalemetrics")
    P = gui.run_paths(run)
    P["review"].write_text(json.dumps({"confirmed": {}, "events": [
        {"sku": "SKU-A", "action": "edit", "mode": "table",
         "at": "2026-09-01T10:00:00", "secs": 30}]}), encoding="utf-8")
    # true when that edit was saved: A hands-on, B in the review bucket,
    # C failed - 3 of 3 rows needed a human
    P["metrics"].write_text(json.dumps({
        "review_minutes": 0.5, "rows_touched": 1, "min_per_sku": 0.5,
        "needs_human_pct": 100.0, "actions": {"edit": 1}}), encoding="utf-8")
    # ... then a re-enrichment completed SKU-B
    set_status(P["filled"], "SKU-B", "FOUND (flipkart)")

    at = boot(runs)
    step2(at)
    assert_no_exception(at)
    m = json.loads(P["metrics"].read_text(encoding="utf-8"))
    assert m["needs_human_pct"] == 66.7, m
    # the event log is untouched, so the time half must not move
    assert m["review_minutes"] == 0.5 and m["rows_touched"] == 1, m


def test_a_run_with_no_review_events_gets_no_metrics_file(tmp):
    """The refresh must not CREATE the file: a zeroed metrics file would make
    the caption print '0 min' where today it prints nothing at all."""
    import gui
    runs = tmp / "runs"
    run = seeded_run(runs, "nometrics")
    P = gui.run_paths(run)
    at = boot(runs)
    step2(at)
    assert_no_exception(at)
    assert not P["metrics"].exists(), "metrics file created for an unreviewed run"
    assert not any("Review so far:" in c for c in texts(at, "caption"))


# ------------- pass 2 reachable from a run whose rows are all NEEDS LINK --

NEEDS_LINK_ROWS = [
    ["SKU-X", "", "", "", "", "", "",
     "NEEDS LINK - NOT FOUND - 6 page(s) checked, none matched", "", ""],
    ["SKU-Y", "", "", "", "", "", "",
     "NEEDS LINK - NOT FOUND - no usable search results", "", ""],
]


def needs_link_run(base: Path, name: str) -> Path:
    """A --discover run in which every row came back NEEDS LINK."""
    run = base / name
    run.mkdir(parents=True)
    (run / "input.xlsx").write_bytes(b"")
    wb = openpyxl.Workbook()
    wb.active.append(FILLED_HEADERS)
    for row in NEEDS_LINK_ROWS:
        wb.active.append(row)
    wb.save(run / "input_filled.xlsx")
    return run


def crawl_btn(at):
    return [b for b in at.button if "Crawl all info" in str(b.label)]


def card_mode(at):
    next(r for r in at.radio if "View" in str(r.label)).set_value(
        "Card review").run(timeout=60)
    assert_no_exception(at)
    return at


def test_crawl_is_hidden_while_no_needs_link_row_has_a_link(tmp):
    """Nothing to crawl yet: the button would start a run that crawls 0
    rows, so it stays hidden in both views."""
    runs = tmp / "runs"
    needs_link_run(runs, "nolinks")
    at = boot(runs)
    step2(at)
    assert_no_exception(at)
    assert not crawl_btn(at)
    card_mode(at)
    assert not crawl_btn(at)


def test_a_url_saved_in_the_table_unlocks_the_crawl(tmp):
    """The failed tab says 'paste a URL, save, then press Crawl all info':
    after that save the button must exist, in the table AND card view."""
    import gui
    runs = tmp / "runs"
    run = needs_link_run(runs, "tablepaste")
    P = gui.run_paths(run)
    at = AppTest.from_function(
        wizard_edited,
        args=(runs, "SKU-X", {"url_link": "https://example.com/x"}, None))
    at.run(timeout=60)
    step2(at)
    assert not crawl_btn(at)
    next(b for b in at.button
         if "Save edits & confirmations" in str(b.label)).click().run(
        timeout=60)
    assert_no_exception(at)
    df = gui.load_filled_df(P)
    assert df.set_index("sku").loc["SKU-X", "url_link"] == \
        "https://example.com/x"
    assert crawl_btn(at), "crawl button missing after a url_link was saved"
    assert any("1 row(s) still have no link" in c
               for c in texts(at, "caption")), texts(at, "caption")
    card_mode(at)
    assert crawl_btn(at), "crawl button missing in card review"


def test_a_url_saved_on_a_card_unlocks_the_crawl(tmp):
    import gui
    runs = tmp / "runs"
    run = needs_link_run(runs, "cardpaste")
    P = gui.run_paths(run)
    at = boot(runs)
    step2(at)
    card_mode(at)
    assert not crawl_btn(at)
    sku = next(s for s in ("SKU-X", "SKU-Y")
               if any(sku_h.value == f"🔴 {s}" for sku_h in at.subheader))
    next(t for t in at.text_input if "Product URL" in str(t.label)) \
        .set_value("https://example.com/card").run(timeout=60)
    next(b for b in at.button if "Save & next" in str(b.label)).click().run(
        timeout=60)
    assert_no_exception(at)
    df = gui.load_filled_df(P).set_index("sku")
    assert df.loc[sku, "url_link"] == "https://example.com/card"
    # still NEEDS LINK, so still not approvable - the import gate holds
    assert str(df.loc[sku, "status"]).startswith("NEEDS LINK")
    assert crawl_btn(at), "crawl button missing after a card save"


def main():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    passed = failed = 0
    for name, fn in tests:
        try:
            if fn.__code__.co_argcount:
                with tempfile.TemporaryDirectory(prefix="gui_ui_") as td:
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
