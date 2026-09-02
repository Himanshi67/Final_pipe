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
import sys
import tempfile
import traceback
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

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
    at.sidebar.selectbox[0].set_value("🏠 All runs").run(timeout=60)
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


# ------------------------------------------------------------------ main --


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
