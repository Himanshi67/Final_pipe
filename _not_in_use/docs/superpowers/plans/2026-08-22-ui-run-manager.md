# Run Manager, Taxonomy Editor & SEO Preview (UI Refit Phase 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A home dashboard listing all runs with stage/archive/delete actions, an in-app `category_map.xlsx` editor with removal warnings and automatic backups, and a build-time SEO preview so the opt-in checkbox shows what it will write before it writes it.

**Architecture:** Pure helpers (`run_stage`, `image_basenames_from_csv`, `sheet_combos`) in `gui_helpers.py` with tests; `gui.py` gains `archive_run()`, a home view in `main()` when no run is selected, a `category_map_editor()` view toggled from the sidebar, and an SEO-preview expander in `step_build()` calling `app.seo_fields` directly (pure per-row, app.py:1437 — no image downloads). No pipeline script changes at all.

**Tech Stack:** Python 3.13 (`.venv\Scripts\python.exe` ONLY), Streamlit, pandas, openpyxl, shutil, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-22-ui-refit.md` (Phase 4). Prerequisites: Phase 1 merged; Phase 2 merged (`parse_specs_cell` from its Task 1 is consumed here). Independent of Phase 3.

## Global Constraints

- Python is ALWAYS `.venv\Scripts\python.exe` — never bare `python`, never "activate". Shell is PowerShell.
- Run tests as: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
- Git: stage by explicit path only. NEVER `git add -A`, `git add .`, or `git commit -a`. Never touch `_push_export\`.
- This plan touches `gui.py` and `gui_helpers.py` only — not mirrored, no /sync-copies.
- `category_map.xlsx` is LIVE configuration: the editor must back it up to `backups\` before every save, and tests never touch the real file — synthetic DataFrames and `tmp_path` only.
- Deleting a run is destructive: the UI must require an explicit confirmation checkbox, and archive must be offered alongside. Never auto-delete.
- SEO stays opt-in: the preview renders ONLY when the existing `seo` checkbox is ticked, and changes nothing about what the build writes.
- Line anchors are as of commit `8697d2d`; Phases 2–3 have since shifted gui.py — ALWAYS locate the anchor by its quoted content, never by number.
- End commit messages with a `Co-Authored-By` trailer naming the model doing the work (the task blocks show `Claude Fable 5` — substitute your own model name).

Facts this plan is built on (verified in code):

- `run_paths()` keys: `run,input,filled,report,log,publish_log,csv,issues,review,meta` (gui.py:72–90). `run_meta.json` fields seen anywhere: `enricher_*`, `original_name`, `uploaded`, `total_rows`, `validated`, `issue_count`, `built_scope`, `built_skus`, `published`, `published_live`, `published_count`.
- The CSV's `Images` column is `{media_base}/{prefixed_sku}_{n}.webp` comma-joined (app.py:1736) — basenames of that column are exactly the files this run's build produced, so archive matches images by basename in `product_images_for_upload\` (flat, app.py:84).
- `app.seo_fields(sku, title, brand, category_hierarchy, spec_rows, regular_price, sale_price, plugin='yoast')` (app.py:1437) is pure and standalone-callable; `spec_rows` is a list of `(name, value)` pairs — `parse_specs_cell` produces that shape from the sheet's `specs` cell.
- `app.CATEGORY_COMBOS` is loaded once at app import (app.py:339); after saving an edited map, `importlib.reload(app)` + `category_options.clear()` refresh both the build path and the review dropdown without restarting Streamlit.
- `category_map.xlsx` shape (app.load_category_map, app.py:308–336): column 0 = parent category, column 1 = subcategories (multi-valued cell), header row detected by the literal `parent category`.

---

### Task 1: Helpers — `run_stage`, `image_basenames_from_csv`, `sheet_combos`

**Files:**

- Modify: `gui_helpers.py` (append)
- Test: `tests\test_gui_helpers.py` (append)

**Interfaces:**

- Consumes: nothing new.
- Produces:
  - `run_stage(meta: dict, files: dict) -> str` — `files` has bools `input`/`filled`/`csv`; returns one of `created, uploaded, enriched, built, validated, published` (highest stage wins).
  - `image_basenames_from_csv(csv_df) -> list[str]` — basenames of every URL in the `Images` column, duplicates preserved in order; `[]` when the column is absent.
  - `sheet_combos(df) -> set[str]` — `"Parent > Sub"` combos from a category-map dataframe (col 0 parent, col 1 subs split on `,`/`;`/`|`/newline). A light mirror of `app.load_category_map` used ONLY for diffing edits, never for snapping.

- [ ] **Step 1: Write the failing tests**

Append to `tests\test_gui_helpers.py`:

```python
from gui_helpers import image_basenames_from_csv, run_stage, sheet_combos


def test_run_stage_ladder():
    f0 = {"input": False, "filled": False, "csv": False}
    assert run_stage({}, f0) == "created"
    assert run_stage({}, {**f0, "input": True}) == "uploaded"
    assert run_stage({}, {**f0, "input": True, "filled": True}) == "enriched"
    assert run_stage({}, {"input": True, "filled": True, "csv": True}) \
        == "built"
    assert run_stage({"validated": True},
                     {"input": True, "filled": True, "csv": True}) \
        == "validated"
    assert run_stage({"validated": True, "published": "2026-08-22T10:00:00"},
                     {"input": True, "filled": True, "csv": True}) \
        == "published"


def test_image_basenames_from_csv():
    df = pd.DataFrame([
        {"Images": "https://s.in/up/2026/08/WM-HW100_1.webp, "
                   "https://s.in/up/2026/08/WM-HW100_2.webp"},
        {"Images": ""},
        {"Images": "https://s.in/up/2026/08/AP-X200_1.webp"},
    ])
    assert image_basenames_from_csv(df) == [
        "WM-HW100_1.webp", "WM-HW100_2.webp", "AP-X200_1.webp"]
    assert image_basenames_from_csv(pd.DataFrame([{"sku": "x"}])) == []


def test_sheet_combos_split_and_header_skip():
    df = pd.DataFrame([
        ["Parent Category", "Sub Categories"],
        ["Home Appliances", "Washing Machines, Refrigerators"],
        ["Kitchen", "Mixer Grinders; Kettles"],
        ["", "orphan sub"],
    ])
    assert sheet_combos(df) == {
        "Home Appliances > Washing Machines",
        "Home Appliances > Refrigerators",
        "Kitchen > Mixer Grinders", "Kitchen > Kettles"}
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
Expected: prior pass; 3 new FAIL with `ImportError: cannot import name 'image_basenames_from_csv'`

- [ ] **Step 3: Implement**

Append to `gui_helpers.py`:

```python
def run_stage(meta, files):
    """Highest pipeline stage a run has reached, from its meta dict and
    {'input','filled','csv'} existence flags."""
    if meta.get("published"):
        return "published"
    if meta.get("validated"):
        return "validated"
    if files.get("csv"):
        return "built"
    if files.get("filled"):
        return "enriched"
    if files.get("input"):
        return "uploaded"
    return "created"


def image_basenames_from_csv(csv_df):
    """Basenames of every image URL the CSV references. The Images column
    lists exactly the files the build downloaded, so these names are safe
    to copy out of product_images_for_upload when archiving a run."""
    names = []
    if "Images" not in csv_df.columns:
        return names
    for cell in csv_df["Images"].astype(str):
        for u in cell.split(","):
            u = u.strip()
            if u:
                names.append(u.rsplit("/", 1)[-1])
    return names


def sheet_combos(df):
    """'Parent > Sub' combos from a category-map dataframe - a light
    mirror of app.load_category_map for DIFFING edits only (the build
    still snaps via app.py). Col 0 parent, col 1 subcategories."""
    combos = set()
    for _, row in df.iterrows():
        vals = row.tolist()
        parent = str(vals[0] if vals else "").strip()
        if not parent or parent.lower() == "parent category":
            continue
        for sub in re.split(r"[,;|\n]", str(vals[1] if len(vals) > 1
                                            else "")):
            sub = sub.strip()
            if sub:
                combos.add(f"{parent} > {sub}")
    return combos
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
Expected: all pass (3 new)

- [ ] **Step 5: Commit**

```powershell
git add gui_helpers.py tests\test_gui_helpers.py
git commit -m @'
feat: run-stage ladder, CSV image basenames, category-map combo diff

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 2: Home dashboard with archive / delete

**Files:**

- Modify: `gui.py` — add `import shutil` to the stdlib imports; extend the helper import with `image_basenames_from_csv, run_stage`; new `archive_run()` above `pick_run()` (~line 978); replace the no-run branch in `main()` (~lines 1003–1008)

**Interfaces:**

- Consumes: `run_stage`, `image_basenames_from_csv` (Task 1); `run_paths`, `load_json`, `RUNS_DIR`, `REPO_ROOT`.
- Produces: `archive_run(run_dir: Path, dest: Path) -> int` (files copied) in gui.py; `batches\<name>\` layout: filled xlsx, CSV, issues CSV, report md, `images\*.webp`, generated `README.md` — matching the curated-batch convention.

- [ ] **Step 1: Add `archive_run`**

```python
def archive_run(run_dir: Path, dest: Path) -> int:
    """Copy a run's reviewable outputs into batches\<name>\ - filled sheet,
    CSV, issues, report, plus exactly the images its CSV references - and
    write a README. Returns the number of files copied. Copies only;
    the run folder is left untouched."""
    Pr = run_paths(run_dir)
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for key in ("filled", "csv", "issues", "report"):
        f = Pr[key]
        if f.exists():
            shutil.copy2(f, dest / f.name)
            copied += 1
    if Pr["csv"].exists():
        img_dest = dest / "images"
        img_dest.mkdir(exist_ok=True)
        csv_df = pd.read_csv(Pr["csv"], dtype=str).fillna("")
        for name in dict.fromkeys(image_basenames_from_csv(csv_df)):
            src = REPO_ROOT / "product_images_for_upload" / name
            if src.exists():
                shutil.copy2(src, img_dest / name)
                copied += 1
    meta = load_json(Pr["meta"])
    (dest / "README.md").write_text(
        f"# Batch {run_dir.name}\n\n"
        f"Archived {datetime.now():%Y-%m-%d %H:%M} from "
        f"runs\\{run_dir.name}.\n\n"
        f"- Source list: {meta.get('original_name', '?')}\n"
        f"- Rows enriched: {meta.get('total_rows', '?')}\n"
        f"- Built scope: {meta.get('built_scope', '?')} "
        f"({len(meta.get('built_skus') or [])} SKUs)\n"
        f"- Validation issues: {meta.get('issue_count', '?')}\n"
        f"- Published: {meta.get('published', 'never')}"
        + (" (LIVE)" if meta.get("published_live") else "") + "\n",
        encoding="utf-8")
    return copied
```

- [ ] **Step 1b: Make the dashboard reachable from the sidebar**

`pick_run()` defaults to the most recent run (`index=1 if runs else 0`), so today the no-run branch is only reached via "➕ New run…". Add an explicit Home entry. In `pick_run()` replace:

```python
    options = ["➕ New run…"] + [d.name for d in runs]
    sel = st.sidebar.selectbox("Batch / run", options,
                               index=1 if runs else 0)
    if sel != "➕ New run…":
        return RUNS_DIR / sel
```

with:

```python
    HOME, NEW = "🏠 All runs", "➕ New run…"
    options = [HOME, NEW] + [d.name for d in runs]
    # land on the latest run as before; Home is one click away
    sel = st.sidebar.selectbox("Batch / run", options,
                               index=2 if runs else 0)
    if sel == HOME:
        return None
    if sel != NEW:
        return RUNS_DIR / sel
```

(Both `HOME` and `NEW` return `None` to `main()`, which renders the dashboard; the new-run name box only appears for `NEW`, exactly as now.)

- [ ] **Step 2: Replace the empty-state branch in `main()`**

Replace:

```python
    if run_dir is None:
        st.title("Shree Jee catalogue pipeline")
        st.info("Create a run in the sidebar to begin — each run keeps its "
                "input, enriched sheet, CSV and logs together under "
                "`runs\\<name>\\`.")
        return
```

with:

```python
    if run_dir is None:
        st.title("Shree Jee catalogue pipeline")
        run_dirs = sorted((d for d in RUNS_DIR.iterdir() if d.is_dir()),
                          key=lambda d: d.stat().st_mtime, reverse=True)
        if not run_dirs:
            st.info("Create a run in the sidebar to begin — each run keeps "
                    "its input, enriched sheet, CSV and logs together under "
                    "`runs\\<name>\\`.")
            return
        rows = []
        for d in run_dirs:
            Pd = run_paths(d)
            m = load_json(Pd["meta"])
            rows.append({
                "run": d.name,
                "stage": run_stage(m, {"input": Pd["input"].exists(),
                                       "filled": Pd["filled"].exists(),
                                       "csv": Pd["csv"].exists()}),
                "rows": m.get("total_rows", ""),
                "issues": m.get("issue_count", ""),
                "published": m.get("published", ""),
                "updated": datetime.fromtimestamp(
                    d.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True,
                     width="stretch")
        st.caption("Pick a run in the sidebar to open it.")
        with st.expander("🗂 Manage a run (archive / delete)"):
            target = st.selectbox("Run", [r["run"] for r in rows],
                                  key="mgr_target")
            tdir = RUNS_DIR / target
            c1, c2 = st.columns(2)
            if c1.button("📦 Archive to batches\\", key="mgr_archive"):
                dest = REPO_ROOT / "batches" / target
                if dest.exists():
                    st.error(f"batches\\{target} already exists — rename or "
                             "remove it first; nothing was copied.")
                else:
                    n = archive_run(tdir, dest)
                    st.success(f"Archived {n} file(s) to batches\\{target}. "
                               "The run folder itself is untouched.")
            sure = c2.checkbox(f"Yes, permanently delete runs\\{target}",
                               key="mgr_sure")
            if c2.button("🗑 Delete run", disabled=not sure, key="mgr_del"):
                shutil.rmtree(tdir)
                st.rerun()
        return
```

- [ ] **Step 3: Syntax check + suite + commit**

Run: `.venv\Scripts\python.exe -m py_compile gui.py; .venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -q`
Expected: exit 0; all pass

```powershell
git add gui.py
git commit -m @'
feat: home dashboard listing runs, with archive-to-batches and gated delete

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 3: Category map editor

**Files:**

- Modify: `gui.py` — extend helper import with `sheet_combos`; new `category_map_editor()` above `main()`; sidebar toggle in `main()` directly after `st.sidebar.title(...)` (~line 1000)

**Interfaces:**

- Consumes: `sheet_combos` (Task 1); `category_options` (Phase 1 — its `.clear()` refreshes the review dropdown cache); `app` module reload semantics (CATEGORY_COMBOS bound at import).
- Produces: `category_map_editor()` in gui.py; behavioral contract — every save first copies the current file to `backups\category_map_<timestamp>.xlsx`.

- [ ] **Step 1: Add the editor view**

```python
def category_map_editor():
    """Edit category_map.xlsx in place. app.py snaps categories to these
    EXACT strings, so removals are loud: they are listed and must be
    acknowledged before the save button arms. Every save backs up the
    current file to backups\ first."""
    st.header("⚙ Category map — category_map.xlsx")
    path = REPO_ROOT / "category_map.xlsx"
    if not path.exists():
        st.error("category_map.xlsx not found next to app.py.")
        return
    df = pd.read_excel(path, dtype=str).fillna("")
    st.caption("Column 1: parent category. Column 2: subcategories "
               "(comma-separated). Builds snap to `Parent > Sub` exactly "
               "as written here.")
    edited = st.data_editor(df, num_rows="dynamic",
                            width="stretch", key="catmap_editor")
    removed = sorted(sheet_combos(df) - sheet_combos(edited))
    if removed:
        st.warning("This edit REMOVES categories existing sheets may still "
                   "use — their rows will fail category snapping on the "
                   "next build:\n\n- " + "\n- ".join(removed))
    ok = st.checkbox("I understand builds snap to these exact strings"
                     + (" and accept the removals listed above"
                        if removed else ""), key="catmap_ok")
    if st.button("💾 Save category map", type="primary", disabled=not ok):
        backup = (REPO_ROOT / "backups" /
                  f"category_map_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
        backup.parent.mkdir(exist_ok=True)
        shutil.copy2(path, backup)
        try:
            edited.to_excel(path, index=False)
        except PermissionError:
            st.error("category_map.xlsx is open in Excel — close it and "
                     "save again.")
            return
        # app.CATEGORY_COMBOS is bound at import; reload so the NEXT build
        # in this session snaps against the edited map, and clear the
        # cached dropdown options so review picks it up too
        import importlib
        os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
        import app
        importlib.reload(app)
        category_options.clear()
        st.success(f"Saved. Backup: backups\\{backup.name}. Review "
                   "dropdowns and the next build now use the edited map.")
```

- [ ] **Step 2: Sidebar toggle in `main()`**

Directly after `st.sidebar.title("🛒 Shree Jee pipeline")`, insert:

```python
    if st.sidebar.checkbox("⚙ Category map editor",
                           help="Edit the taxonomy the builds snap to. "
                                "Closes the wizard while open."):
        category_map_editor()
        return
```

- [ ] **Step 3: Syntax check + suite + commit**

Run: `.venv\Scripts\python.exe -m py_compile gui.py; .venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -q`
Expected: exit 0; all pass

```powershell
git add gui.py
git commit -m @'
feat: in-app category map editor with removal warnings and backups

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 4: SEO preview in Step 3

**Files:**

- Modify: `gui.py` — `step_build()`, directly after the `seo_plugin = st.selectbox(...)` line (~line 703)

**Interfaces:**

- Consumes: `app.seo_fields(sku, title, brand, category_hierarchy, spec_rows, regular_price, sale_price, plugin)` (app.py:1437, pure); `parse_specs_cell` (Phase 2 Task 1); `conf`/`scope` already in scope in `step_build`.
- Produces: UI only — a preview expander; the build itself is untouched.

- [ ] **Step 1: Add the preview expander**

```python
    if seo:
        with st.expander("👁 SEO preview — first 5 rows in scope",
                         expanded=False):
            os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
            import app as _app
            pf = pd.read_excel(P["filled"], dtype=str).fillna("")
            if scope == "confirmed":
                pf = pf[pf["sku"].astype(str).isin(conf)]

            def _p(v):
                return str(v or "").replace(",", "").replace("₹", "").strip()

            sample = []
            for _, r in pf.head(5).iterrows():
                sample.append({"sku": r.get("sku", ""),
                               **_app.seo_fields(
                                   str(r.get("sku", "")),
                                   str(r.get("title", "")),
                                   str(r.get("Brands", "")),
                                   str(r.get("categories", "")),
                                   parse_specs_cell(r.get("specs", "")),
                                   _p(r.get("regular price", "")),
                                   _p(r.get("sale price", "")),
                                   plugin=seo_plugin)})
            if sample:
                st.dataframe(pd.DataFrame(sample), hide_index=True,
                             width="stretch")
                st.caption("These exact columns are appended to the CSV "
                           "when you build with SEO on. Untick the checkbox "
                           "to build without them.")
            else:
                st.info("No rows in scope to preview.")
```

(If `parse_specs_cell` is not yet in gui.py's helper import — Phase 2 added it — extend the `from gui_helpers import (...)` list.)

- [ ] **Step 2: Syntax check + suite + commit**

Run: `.venv\Scripts\python.exe -m py_compile gui.py; .venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -q`
Expected: exit 0; all pass

```powershell
git add gui.py
git commit -m @'
feat: SEO meta preview before an opted-in build

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 5: Whole-suite regression + headless smoke boot

**Files:** none — verification only.

- [ ] **Step 1: Full regression, per suite** — `pytest tests -q` dies at collection: the older test files assert at import and call `sys.exit`, by design. Run `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -q`, then run every other `tests\*.py` as a script (`.venv\Scripts\python.exe tests\<file>.py`), delegating the batch to the runner agent so the log noise stays out of the main context. Expected: exit 0 everywhere; any failure in a pre-existing suite is a regression — stop and fix.

- [ ] **Step 2: Headless smoke boot**

```powershell
$out = Join-Path $env:TEMP "gui_smoke_out.txt"; $err = Join-Path $env:TEMP "gui_smoke_err.txt"
$p = Start-Process ".venv\Scripts\python.exe" -ArgumentList '-m','streamlit','run','gui.py','--server.headless','true','--server.port','8601' -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
Start-Sleep 15
(Invoke-WebRequest "http://localhost:8601" -UseBasicParsing).StatusCode
Stop-Process $p.Id -Force
Select-String -Path $err -Pattern "Traceback" -SimpleMatch
```

Expected: `200`; no Traceback. Delete the temp files afterwards. If the port is already held by a stale Streamlit from an earlier phase, Streamlit exits immediately and the request fails — that is a port collision, not a code failure: pick another free port and re-run.

- [ ] **Step 3: Manual spot-check (report only)** — open the app with NO run selected: the dashboard table should list existing runs with stages. Open the category map editor, change nothing, confirm the save button stays disabled until the checkbox is ticked, then close WITHOUT saving. In Step 3 tick SEO and confirm the preview expander renders for a run with a filled sheet. Do NOT archive or delete a real run and do NOT save the category map during the spot-check — report those paths as exercised only up to their confirmation gates.
