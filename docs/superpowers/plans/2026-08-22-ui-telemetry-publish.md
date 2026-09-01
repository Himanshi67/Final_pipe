# Run Telemetry & Publish Legibility (UI Refit Phase 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The enricher panel answers "how far, how long, how much" (per-status tally, ETA, Serper-credit and cache counters), and Step 5 turns wc_publish output into a create/update diff table before commit and a per-SKU results table (with store links and failed-row retry) after.

**Architecture:** Two pure parsers in `gui_helpers.py` (`parse_enricher_log`, `parse_publish_output`) plus a pure `eta_text`; `classify`/`BADGE` move from `gui.py` into `gui_helpers.py` (they are pure and the parser needs them) with `gui.py` importing them back — no call-site changes. `gui.py` wiring only: the fragment panel and `step_publish`. The publish two-gate flow, chunked `--sku` pinning, and dry-run-unlocks-commit logic are untouched.

**Tech Stack:** Python 3.13 (`.venv\Scripts\python.exe` ONLY), Streamlit, pandas, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-22-ui-refit.md` (Phase 3). Prerequisite: Phase 1 merged. Independent of Phase 2, but if executed after it, expect its extra tests in the suite — verify "all pass + N new", not absolute totals.

## Global Constraints

- Python is ALWAYS `.venv\Scripts\python.exe` — never bare `python`, never "activate". Shell is PowerShell.
- Run tests as: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
- Git: stage by explicit path only. NEVER `git add -A`, `git add .`, or `git commit -a`. Never touch `_push_export\`.
- This plan touches `gui.py` and `gui_helpers.py` only — neither is mirrored, no /sync-copies step. Do NOT edit `sku_enricher.py` or `wc_publish.py`: their output formats are the contract; the GUI adapts to them, never the reverse.
- Tests use synthetic strings/DataFrames only. `gui_helpers.py` stays free of streamlit imports.
- Line anchors are as of commit `8697d2d`; Phase 2 (`b01968d`…`074ca60`) and the `width="stretch"` fix (`d6db572`) have since shifted gui.py by roughly +90 lines — ALWAYS locate the anchor by its quoted content, never by number.
- End commit messages with a `Co-Authored-By` trailer naming the model doing the work (the task blocks show `Claude Fable 5` — substitute your own model name).

Log/output grammar this plan parses (verified in code — do not "improve" the regexes beyond it):

- Enricher per-SKU header: `[3] Row 5: WM-HW100` (sku_enricher.py:3690; no `/total` — total comes from `run_meta.json["total_rows"]`). Status result: 4-space indent, `    -> FOUND (croma.com)` etc. (3737). Cache markers, 6-space indent: `      (cached search)` (1037), `      (cached page, 214 KB)` (1406). Serper counter every 10 rows: `    [37 serper.dev call(s) this run so far - the free tier is 2,500/month]` (3742). Final: `Done. Processed 41 SKUs: 33 found, 8 not found / errors.` (3760).
- wc_publish per SKU (same in dry-run and commit, wc_publish.py:696-700): `  CREATE <sku>` / `  UPDATE <sku>`, then an indented describe line. Commit-only success: `    -> id 123  https://…` (723). Failure: `    !! <exception text>` (727). Summary (commit only): `Done: 2 created, 9 updated, 1 failed.` (737). Dry run ends with `Dry run complete - nothing was written. …` (739). Taxonomy lines start `    ~ would create` / `    + created` — they must NOT be parsed as products.
- `run_meta.json` has `enricher_started` (epoch float) and `total_rows` (gui.py:229-231).

---

### Task 1: Move `classify`/`BADGE`; add `parse_enricher_log` + `eta_text`

**Files:**

- Modify: `gui_helpers.py` (append), `gui.py` (delete `classify`/`BADGE` ~lines 303–322, extend the helper import)
- Test: `tests\test_gui_helpers.py` (append)

**Interfaces:**

- Consumes: nothing new.
- Produces (all in `gui_helpers`):
  - `classify(status) -> str` and `BADGE: dict` — moved VERBATIM from gui.py (buckets: `found`/`review`/`failed`/`skipped`/`pending`). gui.py re-imports them; every existing call site keeps working unchanged.
  - `parse_enricher_log(text: str) -> dict` — keys: `done` (int, last `[N] Row` counter), `buckets` (dict bucket→count from `-> status` lines), `serper_calls` (int, last reported, 0 if none yet), `cached_searches` (int), `cached_pages` (int).
  - `eta_text(done, total, started_epoch, now_epoch) -> str` — `"~7 min left"` / `"under a minute left"` / `""` when unknowable (no rows yet, no total, finished).

- [ ] **Step 1: Write the failing tests**

Append to `tests\test_gui_helpers.py`:

```python
from gui_helpers import classify, BADGE, parse_enricher_log, eta_text

SAMPLE_LOG = """
[1] Row 2: WM-HW100
      (cached search)
      (cached page, 214 KB)
    -> FOUND (croma.com)

[2] Row 3: X200
    -> FOUND (amazon.in) - no price shown

[3] Row 4: Y300
    [7 serper.dev call(s) this run so far - the free tier is 2,500/month]
    -> NOT FOUND - nothing matched

[4] Row 5: Z400
    -> NEEDS CRAWL (flipkart.com)
"""


def test_classify_moved_intact():
    assert classify("FOUND (croma.com)") == "found"
    assert classify("FOUND (x) - no price shown") == "review"
    assert classify("NEEDS CRAWL (x)") == "pending"
    assert classify("NEEDS CRAWL - CHECK LINK (x)") == "review"
    assert classify("NEEDS LINK - no url_link in this row") == "failed"
    assert classify("CLOSE MATCH - CHECK (x)") == "review"
    assert classify("SKIPPED - same unit as row 3") == "skipped"
    assert classify("") == "failed"
    assert set(BADGE) == {"found", "review", "failed", "skipped", "pending"}


def test_parse_enricher_log_counts():
    s = parse_enricher_log(SAMPLE_LOG)
    assert s["done"] == 4
    assert s["buckets"] == {"found": 1, "review": 1, "failed": 1,
                            "pending": 1}
    assert s["serper_calls"] == 7
    assert s["cached_searches"] == 1
    assert s["cached_pages"] == 1


def test_parse_enricher_log_empty():
    s = parse_enricher_log("")
    assert s == {"done": 0, "buckets": {}, "serper_calls": 0,
                 "cached_searches": 0, "cached_pages": 0}


def test_eta_text():
    # 10 of 40 done in 600 s -> 60 s/SKU -> 30 left -> 1800 s
    assert eta_text(10, 40, 1000.0, 1600.0) == "~30 min left"
    assert eta_text(39, 40, 1000.0, 1030.0) == "under a minute left"
    assert eta_text(0, 40, 1000.0, 1600.0) == ""      # nothing done yet
    assert eta_text(40, 40, 1000.0, 1600.0) == ""     # finished
    assert eta_text(10, 0, 1000.0, 1600.0) == ""      # no total known
    assert eta_text(10, 40, None, 1600.0) == ""       # no start time
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
Expected: prior tests pass; the 4 new FAIL with `ImportError: cannot import name 'classify'`

- [ ] **Step 3: Implement**

3a. CUT `classify()` and `BADGE` out of `gui.py` (the block from `def classify(status) -> str:` through the `BADGE = {...}` dict, ~lines 303–322) and paste them UNCHANGED into `gui_helpers.py`, then append below them:

```python
_ROW_RE = re.compile(r"^\[(\d+)\] Row \d+", re.M)
_STATUS_RE = re.compile(r"^    -> (.+)$", re.M)
_SERPER_RE = re.compile(r"^\s*\[(\d+) serper\.dev call\(s\) this run", re.M)


def parse_enricher_log(text):
    """Progress facts from the enricher's log. Grammar pinned by
    sku_enricher.py's log() calls - see the plan header for line formats."""
    done_hits = _ROW_RE.findall(text or "")
    buckets = {}
    for status in _STATUS_RE.findall(text or ""):
        b = classify(status)
        buckets[b] = buckets.get(b, 0) + 1
    serper = _SERPER_RE.findall(text or "")
    return {
        "done": int(done_hits[-1]) if done_hits else 0,
        "buckets": buckets,
        "serper_calls": int(serper[-1]) if serper else 0,
        "cached_searches": (text or "").count("(cached search)"),
        "cached_pages": (text or "").count("(cached page"),
    }


def eta_text(done, total, started_epoch, now_epoch):
    """Remaining-time estimate from this run's average pace; '' when it
    cannot be estimated honestly."""
    if not done or not total or not started_epoch or done >= total:
        return ""
    per = (now_epoch - started_epoch) / done
    if per <= 0:
        return ""
    minutes = int(per * (total - done) // 60)
    return f"~{minutes} min left" if minutes >= 1 else "under a minute left"
```

3b. In `gui.py`, extend the helper import so every existing `classify`/`BADGE` call site resolves again (Phase-1 form shown; if Phase 2 landed first, just add the four new names):

```python
from gui_helpers import (BADGE, batch_summary, classify, eta_text,
                         filter_review_df, flag_value, parse_enricher_log,
                         sanity_issues)
```

- [ ] **Step 4: Run tests + syntax check**

Run: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v; .venv\Scripts\python.exe -m py_compile gui.py`
Expected: all pass (4 new); exit 0

- [ ] **Step 5: Commit**

```powershell
git add gui.py gui_helpers.py tests\test_gui_helpers.py
git commit -m @'
feat: enricher log parser, ETA helper; classify/BADGE move to gui_helpers

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 2: Telemetry line in the enricher panel

**Files:**

- Modify: `gui.py` — `rows_done()` (~line 273, delete) and `enricher_panel()`'s inner `_panel()` (~lines 278–299)

**Interfaces:**

- Consumes: `parse_enricher_log`, `eta_text`, `BADGE` (Task 1); `run_meta.json` fields `total_rows`, `enricher_started`.
- Produces: UI only. `rows_done()` is deleted (the panel was its only caller; the parser supersedes it).

- [ ] **Step 1: Rewrite the panel body**

Delete `def rows_done(...)` (~lines 273–275). Replace the inner `_panel()` body with:

```python
    @st.fragment(run_every=2.0)
    def _panel():
        # stats want the WHOLE log (a 20 KB tail would under-count the
        # tallies); these files are small enough to re-read every 2 s
        full = (P["log"].read_text(encoding="utf-8", errors="replace")
                if P["log"].exists() else "")
        stats = parse_enricher_log(full)
        meta = load_meta(P)
        total = meta.get("total_rows") or 0
        done = stats["done"]
        running = enricher_running(P)
        if total:
            frac = min(done / total, 0.97 if running else 1.0)
            st.progress(frac, text=f"{done} / {total} SKUs processed this run")
        bits = []
        if stats["buckets"]:
            bits.append("  ".join(f"{BADGE[b]} {n}" for b, n in
                                  sorted(stats["buckets"].items())))
        if stats["serper_calls"]:
            bits.append(f"{stats['serper_calls']} serper call(s) — free "
                        "tier 2,500/mo")
        replays = stats["cached_searches"] + stats["cached_pages"]
        if replays:
            bits.append(f"{replays} cache replay(s)")
        eta = eta_text(done, total, meta.get("enricher_started"),
                       time.time())
        if running and eta:
            bits.append(eta)
        if bits:
            st.caption("  ·  ".join(bits))
        lines = [ln for ln in full.splitlines() if ln.strip()][-25:]
        st.code("\n".join(lines) or "waiting for first output…",
                language=None)
        if not running and st.session_state.get(run_key):
            st.session_state[run_key] = False
            update_meta(P, enricher_pid=None)
            st.rerun(scope="app")
```

(`tail_text` stays — `step_enrich`'s "last run: Done…" line still uses it.)

- [ ] **Step 2: Syntax check + suite**

Run: `.venv\Scripts\python.exe -m py_compile gui.py; .venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -q`
Expected: exit 0; all pass

- [ ] **Step 3: Commit**

```powershell
git add gui.py
git commit -m @'
feat: live status tally, credit meter and ETA on the enricher panel

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 3: `parse_publish_output`

**Files:**

- Modify: `gui_helpers.py` (append)
- Test: `tests\test_gui_helpers.py` (append)

**Interfaces:**

- Consumes: nothing new.
- Produces: `parse_publish_output(text: str) -> tuple[list[dict], dict]` — rows `{"sku", "action" ("create"|"update"), "result" ("ok"|"failed"|""), "link", "error"}` in output order (`result` stays `""` on a dry run, which prints no outcome lines); totals `{"created", "updated", "failed"}` from the commit summary, `{}` on a dry run.

- [ ] **Step 1: Write the failing tests**

Append to `tests\test_gui_helpers.py`:

```python
from gui_helpers import parse_publish_output

DRY_OUT = """DRY RUN: 2 product(s) from wordpress_ready.csv -> https://s.in  (images: url, status: draft for new, unchanged for existing)
  CREATE WM-HW100
    would send 14 fields, 3 image(s)
    ~ would create brand: Haier
  UPDATE AP-X200
    would send 12 fields, 2 image(s)

Dry run complete - nothing was written. Re-run with --commit to apply.
"""

COMMIT_OUT = """PUBLISHING: 2 product(s) from wordpress_ready.csv -> https://s.in  (images: url, status: draft for new, unchanged for existing)
  CREATE WM-HW100
    sending 14 fields, 3 image(s)
    -> id 4211  https://shreejeeelectronics.in/product/haier-hw100
  UPDATE AP-X200
    sending 12 fields, 2 image(s)
    !! HTTP 500 from media endpoint

Done: 1 created, 0 updated, 1 failed.
"""


def test_parse_publish_dry_run():
    rows, totals = parse_publish_output(DRY_OUT)
    assert [(r["action"], r["sku"], r["result"]) for r in rows] == [
        ("create", "WM-HW100", ""), ("update", "AP-X200", "")]
    assert totals == {}


def test_parse_publish_commit():
    rows, totals = parse_publish_output(COMMIT_OUT)
    assert rows[0]["result"] == "ok"
    assert rows[0]["link"] == \
        "https://shreejeeelectronics.in/product/haier-hw100"
    assert rows[1]["result"] == "failed"
    assert rows[1]["error"] == "HTTP 500 from media endpoint"
    assert totals == {"created": 1, "updated": 0, "failed": 1}


def test_parse_publish_ignores_taxonomy_and_noise():
    rows, _ = parse_publish_output(DRY_OUT)
    assert all(r["sku"] not in ("brand:", "Haier") for r in rows)
    assert parse_publish_output("") == ([], {})
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
Expected: prior pass; 3 new FAIL with `ImportError: cannot import name 'parse_publish_output'`

- [ ] **Step 3: Implement**

Append to `gui_helpers.py`:

```python
_PUB_ACTION_RE = re.compile(r"^  (CREATE|UPDATE) (\S+)$")
_PUB_ID_RE = re.compile(r"^    -> id (\d+)\s+(\S+)")
_PUB_FAIL_RE = re.compile(r"^    !! (.+)")
_PUB_SUMMARY_RE = re.compile(
    r"^Done: (\d+) created, (\d+) updated, (\d+) failed\.")


def parse_publish_output(text):
    """wc_publish.py stdout -> (rows, totals). Grammar pinned by
    wc_publish.py's print calls; dry runs emit action lines but no
    outcome, so result stays ''. Taxonomy side-effect lines ('~ would
    create …') are deeper-indented and never match the action regex."""
    rows, totals, current = [], {}, None
    for ln in (text or "").splitlines():
        m = _PUB_ACTION_RE.match(ln)
        if m:
            current = {"sku": m.group(2), "action": m.group(1).lower(),
                       "result": "", "link": "", "error": ""}
            rows.append(current)
            continue
        if current is not None:
            m = _PUB_ID_RE.match(ln)
            if m:
                current["result"], current["link"] = "ok", m.group(2)
                continue
            m = _PUB_FAIL_RE.match(ln)
            if m:
                current["result"], current["error"] = "failed", m.group(1)
                continue
        m = _PUB_SUMMARY_RE.match(ln)
        if m:
            totals = {"created": int(m.group(1)), "updated": int(m.group(2)),
                      "failed": int(m.group(3))}
    return rows, totals
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
Expected: all pass (3 new)

- [ ] **Step 5: Commit**

```powershell
git add gui_helpers.py tests\test_gui_helpers.py
git commit -m @'
feat: parser for wc_publish dry-run and commit output

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 4: Diff table before commit, results + retry after

**Files:**

- Modify: `gui.py` — helper import, `step_publish()` dry-run block (~lines 933–940), commit block (~lines 957–973), and a new retry section after the commit block

**Interfaces:**

- Consumes: `parse_publish_output` (Task 3); existing `run_publish_pinned`, `run_publish`, `dry_key`.
- Produces: UI only; session key `pub_failed_<run>` = `{"skus": [...], "extra": [...]}` carrying failures + the exact flags of the commit that produced them (so a retry publishes with the same draft/live status).

- [ ] **Step 1: Extend the helper import**

Add `parse_publish_output` to the `from gui_helpers import (...)` list in gui.py.

- [ ] **Step 2: Dry-run block — replace the raw dump with a diff table**

Inside `if st.button("🧪 Dry run", ...)`, replace `st.code(out[-8000:], language=None)` with:

```python
        rows, _ = parse_publish_output(out)
        if rows:
            creates = sum(r["action"] == "create" for r in rows)
            c1, c2 = st.columns(2)
            c1.metric("Would create", creates)
            c2.metric("Would update", len(rows) - creates)
            st.dataframe(pd.DataFrame(rows)[["action", "sku"]],
                         hide_index=True, width="stretch")
        with st.expander("Raw dry-run output"):
            st.code(out[-8000:], language=None)
```

(The `st.session_state[dry_key] = (code == 0)` line and the success/error line stay exactly as they are.)

- [ ] **Step 3: Commit block — results table + failure capture**

Inside the commit `if st.button(label, ...)` block, replace `st.code(out[-8000:], language=None)` with:

```python
        rows, totals = parse_publish_output(out)
        if rows:
            rdf = pd.DataFrame(rows)[["action", "sku", "result", "link",
                                      "error"]]
            st.dataframe(rdf, hide_index=True, width="stretch",
                         column_config={"link": st.column_config.LinkColumn(
                             "store link")})
        if totals:
            t1, t2, t3 = st.columns(3)
            t1.metric("Created", totals["created"])
            t2.metric("Updated", totals["updated"])
            t3.metric("Failed", totals["failed"])
        failed = [r["sku"] for r in rows if r["result"] == "failed"]
        st.session_state[f"pub_failed_{P['run'].name}"] = \
            {"skus": failed, "extra": extra} if failed else None
        with st.expander("Raw publish output"):
            st.code(out[-8000:], language=None)
```

(Everything after it — the `if code == 0:` success/meta block — stays unchanged.)

- [ ] **Step 4: Retry section**

At the very end of `step_publish()` (after the commit block), append:

```python
    pf = st.session_state.get(f"pub_failed_{P['run'].name}")
    if pf and pf["skus"]:
        st.subheader("Ⅲ · Retry failures")
        st.caption(f"{len(pf['skus'])} SKU(s) failed on the last commit: "
                   + ", ".join(pf["skus"][:10])
                   + ("…" if len(pf["skus"]) > 10 else ""))
        if st.button(f"🔁 Retry {len(pf['skus'])} failed SKU(s)"):
            flags = []
            for s in pf["skus"]:
                flags += ["--sku", s]
            with st.spinner("Retrying…"):
                code, out = run_publish(P, flags + pf["extra"])
            rows, _ = parse_publish_output(out)
            still = [r["sku"] for r in rows if r["result"] == "failed"]
            st.session_state[f"pub_failed_{P['run'].name}"] = \
                {"skus": still, "extra": pf["extra"]} if still else None
            st.code(out[-4000:], language=None)
            (st.success if not still else st.error)(
                "All retried SKUs published." if not still
                else f"{len(still)} still failing — see output above.")
```

(Retry reuses `pf["extra"]` — the same `--commit`/`--status` flags as the original commit, so a drafts commit retries as drafts. The failed set is ≤150 SKUs by construction, well under the command-line limit.)

- [ ] **Step 5: Syntax check + suite + commit**

Run: `.venv\Scripts\python.exe -m py_compile gui.py; .venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -q`
Expected: exit 0; all pass

```powershell
git add gui.py
git commit -m @'
feat: publish diff table, per-SKU results with store links, failed-row retry

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
$p = Start-Process ".venv\Scripts\python.exe" -ArgumentList '-m','streamlit','run','gui.py','--server.headless','true','--server.port','8599' -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
Start-Sleep 15
(Invoke-WebRequest "http://localhost:8599" -UseBasicParsing).StatusCode
Stop-Process $p.Id -Force
Select-String -Path $err -Pattern "Traceback" -SimpleMatch
```

Expected: `200`; no Traceback. Delete the temp files afterwards.

- [ ] **Step 3: Manual spot-check (report only)** — if a run with an `enricher.log` exists, open Step 1 and confirm the tally/caption renders under the progress area (a finished run shows the counts without an ETA). Publish flow can only be spot-checked with credentials and a dry run — do NOT run one unasked; report it as unexercised instead.
