"""gui.py - one GUI for the whole pipeline, as a step-by-step wizard.

    .venv\\Scripts\\python.exe -m streamlit run gui.py

Steps:  1 Upload & Enrich   (sku_enricher.py as a subprocess, live log)
        2 Review & Confirm  (edit price/title/URLs inline, confirm rows)
        3 Build CSV         (app.process_inventory - same path as build_csv.py)
        4 Validate          (validate_csv.validate_export)
        5 Publish           (wc_publish.py; dry-run first, gated --commit)

Everything for one batch lives in runs\\<name>\\: input.xlsx, input_filled.xlsx,
enricher.log, wordpress_ready.csv, validation_issues.csv, review_state.json,
run_meta.json. State is re-derived from those files, so a browser refresh (or
closing the tab mid-enrichment) loses nothing - the enricher checkpoints after
every row and a relaunch resumes where it stopped.

Existing scripts are not modified; app.py remains usable on its own.
"""

import contextlib
import ctypes
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import openpyxl
import pandas as pd
import streamlit as st

from gui_helpers import (BADGE, batch_summary, card_queue_index, classify,
                         eligible_rows, eta_text,
                         filter_review_df, flag_value, found_status,
                         image_basenames_from_csv, merge_confirm_flags,
                         parse_enricher_log,
                         parse_image_urls, parse_publish_output,
                         parse_specs_cell, raw_sku_for_csv_sku,
                         run_stage, sanity_issues, select_build_rows,
                         sheet_combos)

REPO_ROOT = Path(__file__).parent
VENV_PY = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
RUNS_DIR = REPO_ROOT / "runs"

# What the Step-1 uploader accepts. Legacy .xls is deliberately NOT here:
# openpyxl cannot open it, so count_skus() reads 0 rows and the enricher dies
# on the first row. run_paths() still SCANS for input.xls, because an older run
# folder may already hold one - such a run lands on the "Could not read any
# SKUs" error and its "Replace input file" button, which is the way out.
UPLOAD_TYPES = ["xlsx", "csv"]

def _sku_header_match(value) -> bool:
    """Does this header name the SKU column?

    Delegated to the enricher rather than kept as a parallel list here. The
    local copy compared raw lowercase text ('item details') against a hand
    written set, while the enricher normalises to alphanumerics
    ('itemdetails'), so the two disagreed on real headers - the GUI then fell
    back to column 1 and reported the wrong SKU total in the progress bar."""
    global _SKU_HEADER_LOOKUP
    try:
        if _SKU_HEADER_LOOKUP is None:
            import sku_enricher
            _SKU_HEADER_LOOKUP = (sku_enricher.norm_header,
                                  frozenset(sku_enricher.SKU_HEADERS))
        norm, headers = _SKU_HEADER_LOOKUP
        return norm(value) in headers
    except Exception:
        return str(value or "").strip().lower() in {"sku", "skus", "model"}


_SKU_HEADER_LOOKUP = None

STILL_ACTIVE = 259


# ---------------------------------------------------------------- run files --

def run_paths(run_dir: Path) -> dict:
    inp = None
    for ext in (".xlsx", ".xls", ".csv"):
        cand = run_dir / f"input{ext}"
        if cand.exists():
            inp = cand
            break
    return {
        "run": run_dir,
        "input": inp or run_dir / "input.xlsx",
        "filled": run_dir / "input_filled.xlsx",
        "report": run_dir / "input_filled_report.md",
        "log": run_dir / "enricher.log",
        "publish_log": run_dir / "publish.log",
        "csv": run_dir / "wordpress_ready.csv",
        "issues": run_dir / "validation_issues.csv",
        "review": run_dir / "review_state.json",
        "meta": run_dir / "run_meta.json",
    }


def load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8")


def load_meta(P): return load_json(P["meta"])


def update_meta(P, **kv):
    meta = load_meta(P)
    meta.update(kv)
    save_json(P["meta"], meta)
    return meta


def confirmed_set(P) -> set:
    state = load_json(P["review"])
    return {sku for sku, ok in state.get("confirmed", {}).items() if ok}


def save_confirmed(P, skus: dict):
    state = load_json(P["review"])
    state["confirmed"] = skus
    state["updated"] = datetime.now().isoformat(timespec="seconds")
    save_json(P["review"], state)


def count_skus(path: Path) -> int:
    """Rows the enricher will consider: non-empty cells in the SKU column
    (a header that looks SKU-ish, else column 1 - same fallback the enricher
    uses)."""
    try:
        wb = openpyxl.load_workbook(path, read_only=True)
        ws = wb.active
        col = 1
        for c, cell in enumerate(next(ws.iter_rows(min_row=1, max_row=1)), 1):
            if _sku_header_match(cell.value):
                col = c
                break
        n = 0
        for row in ws.iter_rows(min_row=2, min_col=col, max_col=col):
            if row and str(row[0].value or "").strip():
                n += 1
        wb.close()
        return n
    except Exception:
        return 0


# --------------------------------------------------------------- subprocess --

_PID_IS_PYTHON = {}          # pid -> tasklist verdict, one subprocess per pid


def _pid_created(pid: int):
    """Process creation time as a unix epoch, or None if unreadable."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
    if not h:
        return None
    times = [ctypes.c_ulonglong() for _ in range(4)]
    ok = k32.GetProcessTimes(h, *[ctypes.byref(t) for t in times])
    k32.CloseHandle(h)
    if not ok:
        return None
    return times[0].value / 1e7 - 11644473600   # FILETIME -> epoch


def pid_alive(pid: int, started: float = None) -> bool:
    """Is this PID still the process we launched?

    Windows recycles PIDs aggressively, and a stale `enricher_pid` left in
    run_meta.json by a GUI closed mid-batch would sooner or later match some
    unrelated process, locking Step 2 permanently. The exact identity check
    is the process CREATION TIME against the launch timestamp we stored - a
    syscall, safe to call from the 2-second progress fragment. The tasklist
    image-name check remains only as a fallback for metas that predate the
    timestamp, memoized so it spawns at most once per PID."""
    if not pid:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    if not (bool(ok) and code.value == STILL_ACTIVE):
        _PID_IS_PYTHON.pop(pid, None)
        return False
    if started:
        created = _pid_created(pid)
        if created is not None:
            return abs(created - started) < 300
    if pid not in _PID_IS_PYTHON:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=10).stdout
            _PID_IS_PYTHON[pid] = "python" in out.lower()
        except Exception:
            return True             # cannot tell -> trust the handle check
    return _PID_IS_PYTHON[pid]


def enricher_running(P) -> bool:
    proc = st.session_state.get(f"proc_{P['run'].name}")
    if proc is not None and proc.poll() is None:
        return True
    meta = load_meta(P)
    return pid_alive(meta.get("enricher_pid"), meta.get("enricher_started"))


def eligible_total(P, extra_flags: list, fallback: int) -> int:
    """Rows THIS invocation will really process, for the progress bar and ETA.

    P2-28: a --retry-incomplete / --retry-missing / --crawl-urls / resumed
    run touches a handful of rows, but the panel was handed the whole
    sheet's row count, so '3 / 40' and '~37 min left' described a run that
    ended at 5/40. gui_helpers.eligible_rows mirrors the enricher's own skip
    rules. Falls back to the full count whenever the sheet cannot be read
    (not enriched yet, open in Excel, missing columns)."""
    if not P["filled"].exists():
        return fallback
    try:
        df = pd.read_excel(P["filled"], dtype=str)
        n = eligible_rows(df, extra_flags)
    except Exception:
        return fallback
    return n or fallback


def start_enricher(P, extra_flags: list, total: int):
    """Launch sku_enricher.py detached, stdout -> enricher.log (a file, never a
    pipe: a full pipe buffer would deadlock the child)."""
    # Two different numbers, two different keys. `total_rows` means "rows in
    # the sheet" everywhere else (Home dashboard, archive README, the fallback
    # this file hands the NEXT retry) and must keep meaning that;
    # `enricher_total` is how many rows THIS launch will actually touch and is
    # the only value the progress bar and the ETA may divide by.
    eligible = eligible_total(P, extra_flags, total)
    cmd = [str(VENV_PY), "sku_enricher.py", str(P["input"])] + extra_flags
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    with open(P["log"], "w", encoding="utf-8") as log_f:
        proc = subprocess.Popen(
            cmd, stdout=log_f, stderr=subprocess.STDOUT,
            cwd=str(REPO_ROOT), env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    st.session_state[f"proc_{P['run'].name}"] = proc
    st.session_state[f"running_{P['run'].name}"] = True
    update_meta(P, enricher_pid=proc.pid, enricher_started=time.time(),
                enricher_flags=extra_flags,
                total_rows=total, enricher_total=eligible,
                enricher_cmd=" ".join(cmd))


def cancel_enricher(P):
    """CTRL_BREAK first - it reaches only this child's process group, and
    sku_enricher.install_break_handler() maps it to KeyboardInterrupt so the
    run checkpoints ('Stopped by user - progress saved'). Delivery waits for
    the row's current sleep/request to return, hence the 15 s grace before the
    hard kill (safe either way: saves are atomic)."""
    proc = st.session_state.get(f"proc_{P['run'].name}")
    pid = proc.pid if proc is not None else load_meta(P).get("enricher_pid")
    if not pid:
        return
    try:
        os.kill(pid, signal.CTRL_BREAK_EVENT)
    except OSError:
        pass
    deadline = 15
    if proc is not None:
        try:
            proc.wait(timeout=deadline)
        except subprocess.TimeoutExpired:
            pass
    else:
        while deadline > 0 and pid_alive(pid):
            time.sleep(1)
            deadline -= 1
    if pid_alive(pid):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
    update_meta(P, enricher_pid=None)
    st.session_state[f"running_{P['run'].name}"] = False


def tail_text(path: Path, max_bytes=20000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def enricher_panel(P):
    """Live progress while the enricher subprocess runs. A fragment so only
    this panel re-renders every 2 s, not the whole page."""
    run_key = f"running_{P['run'].name}"

    @st.fragment(run_every=2.0)
    def _panel():
        # stats want the WHOLE log (a 20 KB tail would under-count the
        # tallies); these files are small enough to re-read every 2 s
        full = (P["log"].read_text(encoding="utf-8", errors="replace")
                if P["log"].exists() else "")
        stats = parse_enricher_log(full)
        meta = load_meta(P)
        # eligible count for THIS launch (P2-28); total_rows is the whole
        # sheet and is only the fallback for meta written before this key
        total = meta.get("enricher_total") or meta.get("total_rows") or 0
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

    _panel()
    if st.button("⏹ Stop enrichment (progress is saved)", key=f"cancel_{P['run'].name}"):
        cancel_enricher(P)
        st.rerun()


# ------------------------------------------------------------------- step 1 --

def step_enrich(P):
    st.header("1 · Upload & Enrich")

    if not P["input"].exists():
        up = st.file_uploader("SKU list (.xlsx / .csv) - just a SKU "
                              "column is enough", type=UPLOAD_TYPES)
        if up is None:
            st.info("Upload the bare SKU list to start this run.")
            return
        dest = P["run"] / ("input" + Path(up.name).suffix.lower())
        dest.write_bytes(up.getvalue())
        update_meta(P, original_name=up.name, uploaded=datetime.now().isoformat(
            timespec="seconds"))
        st.rerun()

    total = count_skus(P["input"]) if P["input"].suffix != ".csv" else 0
    meta = load_meta(P)
    st.success(f"Input: **{meta.get('original_name', P['input'].name)}**"
               + (f" — {total} SKUs" if total else ""))

    if enricher_running(P) or st.session_state.get(f"running_{P['run'].name}"):
        st.subheader("Enrichment running…")
        enricher_panel(P)
        return

    if not P["filled"].exists():
        # Nothing has been enriched yet, so the input file is still replaceable
        # - and it HAS to be, because the uploader above hides itself the
        # moment any input.* exists. Without this the run wedges: an unreadable
        # sheet (a legacy .xls, a corrupt upload, a sheet with no SKU column)
        # counts 0 rows, the enricher exits immediately, and there is no way
        # back to the uploader. .csv is exempt from the count because
        # count_skus() reads xlsx only.
        if P["input"].suffix.lower() != ".csv" and total == 0:
            st.error(f"Could not read any SKUs from {P['input'].name} - is it "
                     "a real .xlsx/.csv with a sku column?")
        if st.button("🔁 Replace input file",
                     key=f"replace_input_{P['run'].name}"):
            try:
                P["input"].unlink(missing_ok=True)
            except OSError:
                # the operator's most likely state is the sheet still open in
                # Excel, which holds the file without FILE_SHARE_DELETE - the
                # same friendly message the rest of this file uses, not a red
                # traceback. Nothing was changed; the button works next click.
                st.error(f"Could not delete {P['input'].name} — it looks like "
                         "the file is open in Excel. Close it and click "
                         "Replace again.")
                return
            fresh = load_meta(P)
            fresh.pop("original_name", None)
            fresh.pop("uploaded", None)
            # the row count belonged to the sheet just deleted; leaving it
            # made the Home dashboard's 'rows' column report a file that is
            # no longer there
            fresh.pop("total_rows", None)
            fresh.pop("enricher_total", None)
            save_json(P["meta"], fresh)
            st.rerun()

    if P["filled"].exists():
        text = tail_text(P["log"])
        done_line = next((ln for ln in reversed(text.splitlines())
                          if ln.startswith("Done.")), "")
        st.info("This run already has enriched output"
                + (f" — last run: *{done_line}*" if done_line else "")
                + ". Starting again resumes: rows already filled are skipped.")
        if P["report"].exists():
            with st.expander("Batch report"):
                st.markdown(P["report"].read_text(encoding="utf-8",
                                                  errors="replace"))

    two_pass = st.radio(
        "How do you want to run this?",
        ["Two passes — find links & photos first, review them, then crawl",
         "One pass — find and extract everything now"],
        index=0,
        help="Two passes costs the same crawling but puts your review BEFORE "
             "the full extraction: you fix wrong or missing links, and pass 2 "
             "then replays the cached pages for free, fetching only what you "
             "changed. It also runs no searches at all, so it spends no "
             "Serper credits.")
    discover = two_pass.startswith("Two passes")

    with st.form("enrich_opts"):
        c1, c2, c3 = st.columns(3)
        limit = c1.number_input("Limit (0 = all rows)", 0, 10000, 0,
                                help="Smoke-test with 2-3 first")
        min_price = c2.number_input("Min price ₹ (0 = off)", 0, 10_000_000, 0,
                                    help="Rejects accessories like covers/stands")
        max_price = c3.number_input("Max price ₹ (0 = off)", 0, 10_000_000, 0)
        d1, d2 = st.columns(2)
        delay_min = d1.number_input("Delay min (s)", 0.0, 60.0, 3.0)
        delay_max = d2.number_input("Delay max (s)", 0.0, 60.0, 7.0)
        with st.expander("Advanced"):
            js = st.checkbox("--js-fallback (headless browser for blocked "
                             "pages; slow)")
            min_images = st.number_input("Min images per product", 1, 10, 3)
            force = st.checkbox("--force — re-process EVERY row (re-pays "
                                "Serper credits when the cache is cold; only "
                                "for applying parser fixes on a warm cache)")
        started = st.form_submit_button(
            "▶ Find links & photos" if discover else "▶ Start enrichment",
            type="primary")

    if started:
        flags = ["--discover"] if discover else []
        if limit:
            flags += ["--limit", str(int(limit))]
        if min_price:
            flags += ["--min-price", str(int(min_price))]
        if max_price:
            flags += ["--max-price", str(int(max_price))]
        flags += ["--delay", str(delay_min), str(max(delay_max, delay_min))]
        if js:
            flags += ["--js-fallback"]
        if min_images != 3:
            flags += ["--min-images", str(int(min_images))]
        if force:
            flags += ["--force"]
        start_enricher(P, flags, total)
        st.rerun()

    if not os.environ.get("SERPER_API_KEY"):
        st.warning("SERPER_API_KEY is not set — search falls back to "
                   "DuckDuckGo (worse results).")


# ------------------------------------------------------------------- step 2 --

EDITABLE_COLS = ["title", "regular price", "sale price", "url_link",
                 "image_url", "categories", "Brands", "dropped_images"]


def load_filled_df(P) -> pd.DataFrame:
    df = pd.read_excel(P["filled"], dtype=str).fillna("")
    df.columns = [str(c).strip() for c in df.columns]
    for col in ["sku", "status"] + EDITABLE_COLS:
        if col not in df.columns:
            df[col] = ""
    for col in ("regular price", "sale price"):
        # The enricher writes prices Indian-formatted ('22,599') because a
        # human reads this sheet. pd.to_numeric chokes on the comma and returns
        # NaN, which rendered EVERY price as an empty cell in the review
        # editor - measured on a real batch: 39 of 41 blanked, while 22 rows
        # genuinely had a price. Strip the formatting before parsing.
        df[col] = pd.to_numeric(
            df[col].astype(str)
                   .str.replace(",", "", regex=False)
                   .str.replace("₹", "", regex=False)
                   .str.strip(),
            errors="coerce")
    return df


@st.cache_resource
def category_options() -> list:
    """Exact category strings from category_map.xlsx, via the same loader
    app.py snaps against - so anything picked here is a legal snap target."""
    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    import app
    return sorted(set(app.CATEGORY_COMBOS.values()))


def review_editor(df: pd.DataFrame, key: str,
                  cat_options: list | None = None,
                  allow_confirm: bool = True) -> pd.DataFrame:
    """One review tab. allow_confirm=False locks the ✔ column: the rows in
    that tab are not FOUND, so a tick there would be refused on save
    anyway (see gui_helpers.found_status)."""
    view = df.copy()
    view.insert(0, "confirmed", view.pop("confirmed"))
    view.insert(1, "state", view["status"].map(
        lambda s: f"{BADGE[classify(s)]} {s}"))
    view.insert(2, "thumb", view["image_url"].map(
        lambda v: str(v).split(",")[0].strip() if str(v).strip() else None))
    show = ["confirmed", "state", "sku", "title", "regular price",
            "sale price", "thumb", "image_url", "url_link", "categories",
            "Brands", "specs"]
    show = [c for c in show if c in view.columns]
    return st.data_editor(
        view[show], key=key, num_rows="fixed", height=420,
        disabled=(["state", "sku", "thumb", "specs"]
                  + ([] if allow_confirm else ["confirmed"])),
        column_config={
            "confirmed": st.column_config.CheckboxColumn(
                "✔", default=False, width="small",
                help=None if allow_confirm else
                "Not confirmable — paste a URL and press Crawl all info "
                "first"),
            "state": st.column_config.TextColumn("status", width="medium"),
            "thumb": st.column_config.ImageColumn("img", width="small"),
            "url_link": st.column_config.LinkColumn("url", width="medium"),
            "image_url": st.column_config.TextColumn("image URLs (comma-sep)"),
            "regular price": st.column_config.NumberColumn("MRP ₹",
                                                           format="%.0f"),
            "sale price": st.column_config.NumberColumn("price ₹",
                                                        format="%.0f"),
            **({"categories": st.column_config.SelectboxColumn(
                    "categories", options=cat_options, required=False,
                    width="medium")} if cat_options else {}),
        })


def write_edits_back(P, original: pd.DataFrame, edited_parts: list) -> int:
    """Write ONLY changed editable cells back into input_filled.xlsx, matched
    by SKU, preserving everything else the enricher wrote."""
    edits = {}
    orig_by_sku = original.set_index(original["sku"].astype(str))
    orig_by_sku = orig_by_sku[~orig_by_sku.index.duplicated(keep="first")]
    for part in edited_parts:
        for _, row in part.iterrows():
            sku = str(row["sku"])
            if sku not in orig_by_sku.index:
                continue
            before = orig_by_sku.loc[sku]
            for col in EDITABLE_COLS:
                if col not in row.index:
                    continue
                new, old = row[col], before[col]
                if pd.isna(new) and pd.isna(old):
                    continue
                if (pd.isna(new) != pd.isna(old)) or str(new) != str(old):
                    edits.setdefault(sku, {})[col] = None if pd.isna(new) else new
    if not edits:
        return 0

    wb = openpyxl.load_workbook(P["filled"])
    ws = wb.active
    headers = {str(ws.cell(row=1, column=c).value or "").strip(): c
               for c in range(1, ws.max_column + 1)}
    sku_c = headers.get("sku")
    if not sku_c:
        raise RuntimeError("no 'sku' column in the filled sheet")
    # the review GUI can introduce columns the enricher never wrote
    # (dropped_images) - create the header on demand, else the
    # `col in headers` guard below silently discards the edit
    needed = {c for edit in edits.values() for c in edit}
    for col in sorted(needed - set(headers)):
        new_c = ws.max_column + 1
        ws.cell(row=1, column=new_c, value=col)
        headers[col] = new_c
    changed = 0
    for r in range(2, ws.max_row + 1):
        sku = str(ws.cell(row=r, column=sku_c).value or "").strip()
        if sku in edits:
            for col, val in edits[sku].items():
                if col in headers:
                    ws.cell(row=r, column=headers[col], value=val)
                    changed += 1
    wb.save(P["filled"])
    return changed


def validator_flags(P, raw_sku: str, all_raws=None) -> str:
    """Reasons the last validation run flagged this review row for, if any.
    The issues CSV carries the PREFIXED sku, so map back by suffix.

    all_raws is every sku on the sheet. Resolving the CSV sku against the
    WHOLE list and then checking the winner is this row is what keeps a
    re-coded-prefix match honest: asked in isolation, 'HA-X200' also matches
    the raw 'AC-X200' (same sku, different code), so a card would have shown
    another product's validation reasons. With the full list the true owner
    ('X200') wins and this card correctly shows nothing."""
    if not P["issues"].exists():
        return ""
    try:
        idf = pd.read_csv(P["issues"], dtype=str).fillna("")
    except Exception:
        return ""
    candidates = [str(s) for s in (all_raws or [raw_sku])]
    for _, r in idf.iterrows():
        if raw_sku_for_csv_sku(str(r.get("sku", "")), candidates) == raw_sku:
            return str(r.get("reasons", ""))
    return ""


def card_review(P, df, cat_opts, all_raws=None):
    """One SKU per screen: gallery with keep/drop, editable fields, an
    approve/skip queue. df is the (possibly filtered) review dataframe -
    prices numeric, confirmed bool, _bucket set."""
    run = P["run"].name
    if df.empty:
        st.info("No rows in the current filter.")
        return
    order = {"review": 0, "failed": 1, "pending": 2, "found": 3, "skipped": 4}
    queue = df.sort_values(
        by=["confirmed", "_bucket"],
        key=lambda s: s.map(order) if s.name == "_bucket" else s,
    ).reset_index(drop=True)

    ikey = f"card_i_{run}"
    # never rest on a confirmed card while unconfirmed ones remain: approving
    # the LAST unconfirmed card leaves it at position i, and i + 1 clamps
    # right back onto it, so Approve/Skip would dead-loop on an already
    # approved card (P1-9 tail) - card_queue_index snaps forward instead
    i = card_queue_index(queue["confirmed"], st.session_state.get(ikey, 0))
    st.session_state[ikey] = i
    row = queue.iloc[i]
    sku = str(row["sku"])

    def goto(j):
        st.session_state[ikey] = max(0, min(j, len(queue) - 1))
        st.rerun()

    st.progress((i + 1) / len(queue),
                text=f"Card {i + 1} / {len(queue)} — "
                     f"{int(queue['confirmed'].sum())} confirmed")
    if bool(queue["confirmed"].all()):
        st.success(f"All {len(queue)} cards confirmed.")
    st.subheader(f"{BADGE[row['_bucket']]} {sku}")
    st.caption(str(row["status"]))
    src = str(row.get("url_link", "") or "").strip()
    if src:
        st.markdown(f"🔗 [Open source page]({src})")
    flagged = validator_flags(P, sku, all_raws)
    if flagged:
        st.warning(f"Validator flagged this SKU last run: {flagged}")

    urls = parse_image_urls(row.get("image_url", ""))
    dropped_now = set(parse_image_urls(row.get("dropped_images", "")))
    keeps = {}
    if urls:
        cols = st.columns(min(4, len(urls)))
        for j, u in enumerate(urls):
            with cols[j % len(cols)]:
                st.image(u, width="stretch")
                keeps[u] = st.checkbox("keep", value=u not in dropped_now,
                                       key=f"keep_{run}_{sku}_{j}")
    else:
        st.info("No images on this row.")

    e1, e2, e3 = st.columns([2, 1, 1])
    title = e1.text_input("Title", str(row.get("title", "")),
                          key=f"ct_{run}_{sku}")
    mrp_s = e2.text_input(
        "MRP ₹", "" if pd.isna(row["regular price"])
        else f"{row['regular price']:.0f}", key=f"cm_{run}_{sku}")
    sale_s = e3.text_input(
        "Sale ₹", "" if pd.isna(row["sale price"])
        else f"{row['sale price']:.0f}", key=f"cs_{run}_{sku}")
    url = st.text_input("Product URL", src, key=f"cu_{run}_{sku}")
    cat_now = str(row.get("categories", "") or "").strip()
    cat_list = [""] + list(cat_opts)
    cat = st.selectbox("Category", cat_list,
                       index=cat_list.index(cat_now) if cat_now in cat_list
                       else 0, key=f"cc_{run}_{sku}")

    pairs = parse_specs_cell(row.get("specs", ""))
    if pairs:
        with st.expander(f"Scraped specs ({len(pairs)})"):
            st.table(pd.DataFrame(pairs, columns=["spec", "value"]))

    def _num(s):
        return pd.to_numeric(str(s).replace(",", "").replace("₹", "")
                             .strip(), errors="coerce")

    def save(confirm):
        part = pd.DataFrame([{
            "sku": sku,
            "confirmed": bool(row["confirmed"]) if confirm is None
            else bool(confirm),
            "title": title.strip(),
            "regular price": _num(mrp_s),
            "sale price": _num(sale_s),
            "url_link": url.strip(),
            "image_url": str(row.get("image_url", "") or ""),
            "categories": cat if cat else cat_now,
            "Brands": str(row.get("Brands", "") or ""),
            "dropped_images": ", ".join(
                u for u in urls if not keeps.get(u, True)),
        }])
        try:
            write_edits_back(P, df, [part])
        except PermissionError:
            st.error("The filled .xlsx is open in Excel — close it and "
                     "retry.")
            return False
        if confirm is not None:
            flags = {s: True for s in confirmed_set(P)}
            flags[sku] = bool(confirm)
            save_confirmed(P, flags)
        return True

    b1, b2, b3, b4 = st.columns(4)
    # only a FOUND row is importable - approving anything else could pull an
    # unvetted row into a confirmed-scope build (the save would refuse it
    # anyway, see gui_helpers.found_status)
    can_approve = found_status(row["status"])
    if b1.button("✅ Approve & next", type="primary", disabled=not can_approve,
                 help=None if can_approve else "Approve needs a FOUND row — "
                 "paste or fix the URL and press Crawl all info first.",
                 key=f"ap_{run}_{sku}"):
        if save(True):
            # the queue sorts unconfirmed rows first, so an approved row
            # leaves position i and the next unconfirmed card slides into
            # it - advancing here would skip that card (P1-9). A row that
            # was already confirmed does not move, so advance. The one
            # position where an approved row does NOT leave i is the last
            # unconfirmed one; card_queue_index catches that on the next
            # run and snaps to the first card still unconfirmed.
            goto(i if not bool(row["confirmed"]) else i + 1)
    if b2.button("💾 Save & next", key=f"sv_{run}_{sku}"):
        if save(None):
            goto(i + 1)
    if b3.button("→ Skip", key=f"sk_{run}_{sku}"):
        goto(i + 1)
    if b4.button("← Back", disabled=i == 0, key=f"bk_{run}_{sku}"):
        goto(i - 1)


def step_review(P):
    st.header("2 · Review & Confirm")
    if not P["filled"].exists():
        st.info("No enriched output yet — run Step 1 first.")
        return
    if enricher_running(P) or st.session_state.get(f"running_{P['run'].name}"):
        st.subheader("Enrichment running — review unlocks when it finishes")
        enricher_panel(P)
        return

    df = load_filled_df(P)
    conf = confirmed_set(P)
    df["confirmed"] = df["sku"].astype(str).isin(conf)
    df["_bucket"] = df["status"].map(classify)

    n = df["_bucket"].value_counts()
    pending = int(n.get("pending", 0))
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("🔵 Link found, not crawled", pending)
    m2.metric("🟢 Found (clean)", int(n.get("found", 0)))
    m3.metric("🟡 Needs review", int(n.get("review", 0)))
    m4.metric("🔴 No link / failed", int(n.get("failed", 0)))
    m5.metric("✔ Confirmed", int(df["confirmed"].sum()))

    # the Save handler reruns immediately, so its outcome is reported here
    for kind, text in st.session_state.pop(
            f"review_msg_{P['run'].name}", []):
        (st.warning if kind == "warning" else st.success)(text)

    probs = sanity_issues(
        df, min_price=flag_value(load_meta(P).get("enricher_flags", []),
                                 "--min-price"))
    if probs:
        st.warning(f"⚠ {len(probs)} data problem(s) in this sheet — fix them "
                   "here before building the CSV.")
        with st.expander("Show problems", expanded=len(probs) <= 10):
            st.dataframe(pd.DataFrame(probs), hide_index=True,
                         width="stretch")

    fc1, fc2 = st.columns([2, 1])
    query = fc1.text_input("🔍 Search SKU / title",
                           key=f"flt_q_{P['run'].name}")
    brand_opts = sorted(b for b in df["Brands"].astype(str).str.strip()
                        .unique() if b)
    brands = fc2.multiselect("Brand", brand_opts,
                             key=f"flt_b_{P['run'].name}")
    fdf = filter_review_df(df, query, brands)
    # a SelectboxColumn renders a cell as blank if its current value is not
    # in options - union in whatever the sheet already holds so nothing
    # displays empty
    cat_opts = category_options()
    cat_opts = cat_opts + sorted(
        set(df["categories"].astype(str).str.strip()) - set(cat_opts) - {""})
    if len(fdf) < len(df):
        st.caption(f"Showing **{len(fdf)} of {len(df)}** rows. Save edits "
                   "before changing the filter — unsaved edits in hidden "
                   "rows are lost on rerender.")
    # a new filter must reset the editors, else stale edit state maps onto
    # different rows; hashing the filter into the key does that. The save
    # counter is in the hash for the same reason: streamlit keeps each
    # editor's edited_rows under an UNCHANGED key, so after a save every tab
    # would keep replaying the edit that was just written to disk. Once the
    # sheet holds it, that replay is no longer an edit but a stale opinion —
    # and two tabs replaying opposite stale opinions made the flag flip on
    # every Save press (P1-8). A new key after each save reloads the editors
    # from the saved sheet, so a merge only ever sees fresh edits.
    egen = st.session_state.get(f"ed_gen_{P['run'].name}", 0)
    fsig = hashlib.md5(
        f"{query}|{'|'.join(brands)}|{egen}".encode()).hexdigest()[:6]

    mode = st.radio("View", ["Table", "Card review"], horizontal=True,
                    key=f"mode_{P['run'].name}",
                    help="Card review walks the rows one SKU at a time with "
                         "the full image gallery. Bulk actions (confirm all, "
                         "crawl, retries) live in the Table view.")
    if mode == "Card review":
        # the UNFILTERED sku column: validator_flags has to resolve a CSV sku
        # against every raw on the sheet, not just the visible ones
        card_review(P, fdf, cat_opts, df["sku"].astype(str).tolist())
        return

    st.caption("Edit title, prices, URLs and image URLs directly in the "
               "table; tick ✔ to confirm a row for the CSV. Then press "
               "**Save edits & confirmations**.")

    tabs = st.tabs([f"🟡 Needs review ({int(n.get('review', 0))})",
                    f"🔴 No link / failed ({int(n.get('failed', 0))})",
                    f"🔵 Awaiting crawl ({pending})",
                    f"🟢 Found ({int(n.get('found', 0))})",
                    f"All rows ({len(df)})"])
    parts = []
    with tabs[0]:
        parts.append(review_editor(fdf[fdf["_bucket"] == "review"],
                                   f"ed_review_{fsig}", cat_opts))
    with tabs[1]:
        if pending or int(n.get("failed", 0)):
            st.caption("Paste a product URL into **url_link** for each row "
                       "here, save, then press **Crawl all info**.")
        parts.append(review_editor(fdf[fdf["_bucket"] == "failed"],
                                   f"ed_failed_{fsig}", cat_opts,
                                   allow_confirm=False))
    with tabs[2]:
        parts.append(review_editor(fdf[fdf["_bucket"] == "pending"],
                                   f"ed_pending_{fsig}", cat_opts,
                                   allow_confirm=False))
    with tabs[3]:
        parts.append(review_editor(fdf[fdf["_bucket"] == "found"],
                                   f"ed_found_{fsig}", cat_opts))
    with tabs[4]:
        parts.append(review_editor(fdf, f"ed_all_{fsig}", cat_opts))

    # the All tab duplicates the bucket rows; parts is in TAB order - review,
    # failed, pending, found, all - so All is the last one and goes first.
    # merge_confirm_flags applies a part's value only when it CHANGES that
    # sku's persisted flag, so an untouched copy of a row can never overwrite
    # a tick made in the other tab. Order therefore decides one case only: the
    # same sku changed in BOTH its bucket tab and the All tab during this
    # render - the bucket tab, being later, wins.
    ordered = [parts[-1]] + parts[:-1]

    def persist_flags(flags, rejected, changed=None):
        """Save the merged ✔ map, then stash the outcome and rerun. The rerun
        throws the page away, so anything printed here would never be seen -
        step_review pops these messages at the top of the next run."""
        save_confirmed(P, flags)
        head = "" if changed is None else f"Saved — {changed} cell(s) " \
                                          "updated, "
        msgs = [("success", f"{head}{sum(flags.values())} row(s) confirmed.")]
        if rejected:
            msgs.append(("warning",
                         f"{len(rejected)} tick(s) ignored — a tick only "
                         "counts on a FOUND row. Paste a URL and press Crawl "
                         "all info first: " + ", ".join(rejected)))
        st.session_state[f"review_msg_{P['run'].name}"] = msgs
        # the sheet now holds these edits; retire the editors' edit state
        st.session_state[f"ed_gen_{P['run'].name}"] = egen + 1
        st.rerun()

    b1, b2, b3, b4 = st.columns([1.4, 1.4, 1.6, 1.6])
    if b1.button("💾 Save edits & confirmations", type="primary"):
        try:
            changed = write_edits_back(P, df, ordered)
        except PermissionError:
            st.error("The filled .xlsx is open in Excel — close it and save "
                     "again.")
            return
        flags, rejected = merge_confirm_flags(confirmed_set(P), df, ordered)
        persist_flags(flags, rejected, changed)

    if b2.button("✅ Confirm all clean FOUND"):
        # merge the tabs FIRST: a tick made in any editor and not yet saved is
        # an edit like any other, and rebuilding the map from `df` alone would
        # silently drop it (the P1-8 data loss). The merge is also what applies
        # the FOUND gate to ticks already sitting on disk.
        flags, rejected = merge_confirm_flags(confirmed_set(P), df, ordered)
        for _, r in df.iterrows():
            if r["_bucket"] == "found" and found_status(r["status"]):
                flags[str(r["sku"])] = True
        persist_flags(flags, rejected)

    meta = load_meta(P)
    # --discover MUST be stripped: passing it alongside --crawl-urls is the one
    # combination the enricher refuses outright
    base_flags = [f for f in meta.get("enricher_flags", [])
                  if f not in ("--force", "--retry-incomplete",
                               "--retry-missing", "--discover", "--crawl-urls")]
    total = meta.get("total_rows", 0)

    if pending or int(n.get("review", 0)):
        no_link = int((df["url_link"].astype(str).str.strip() == "").sum()) \
            if "url_link" in df.columns else 0
        st.divider()
        st.subheader("Pass 2 · crawl the reviewed links")
        st.caption(f"Crawls the url_link on every row and pulls in the full "
                   f"specifications. Runs **no searches**, so it spends no "
                   f"Serper credits — links already crawled replay from cache "
                   f"for free."
                   + (f" {no_link} row(s) still have no link and will be "
                      f"skipped." if no_link else ""))
        if st.button("🔎 Crawl all info from these links", type="primary",
                     key="crawl_urls_btn"):
            start_enricher(P, base_flags + ["--crawl-urls"], total)
            st.rerun()
        st.divider()
    if b3.button("🔁 Re-enrich incomplete rows",
                 help="--retry-incomplete: rows FOUND but missing a price or "
                      "enough images; good cells are kept"):
        start_enricher(P, base_flags + ["--retry-incomplete"], total)
        st.rerun()
    if b4.button("🔁 Retry failed rows",
                 help="--retry-missing: re-attempt NOT FOUND / ERROR rows"):
        start_enricher(P, base_flags + ["--retry-missing"], total)
        st.rerun()


# ------------------------------------------------------------------- step 3 --

def preview_build_rows(pf, app_mod, estimate_mrp=False, limit=5):
    """The first `limit` rows the build would REALLY write, as (row, ctx).

    Scoping alone is not enough for a faithful preview (P1-16): before it
    writes anything, process_inventory drops rows whose title is blank and
    skips a row whose prefixed sku it has already written. A preview that
    only did .head(limit) showed products the CSV never contains - a blank
    title row previews under its own sku with full SEO columns, and a
    duplicate previews a second slug and meta description that are never
    written - and each of them consumed a slot belonging to a row that IS
    built. `app_mod` is passed in because gui.py imports app lazily.
    """
    out, seen = [], set()
    for _, row in pf.iterrows():
        if len(out) >= limit:
            break
        if not app_mod.compact(row.get("title")):
            continue
        ctx = app_mod.row_context(row, estimate_mrp=estimate_mrp)
        if ctx["sku"] in seen:
            continue
        seen.add(ctx["sku"])
        out.append((row, ctx))
    return out


def step_build(P):
    st.header("3 · Build WooCommerce CSV")
    if not P["filled"].exists():
        st.info("No enriched output yet — run Steps 1–2 first.")
        return

    conf = confirmed_set(P)
    # the VALUE is a stable key; only the rendering carries the wording, so a
    # label rewrite can never silently flip what built_scope records
    scope = st.radio(
        "Rows to include", ["confirmed", "all_found"],
        format_func=lambda v: (f"Confirmed rows only ({len(conf)})"
                               if v == "confirmed" else
                               "All FOUND rows (skip unverified, like "
                               "build_csv.py)"),
        index=0 if conf else 1)
    month = st.text_input("WordPress uploads folder (YYYY/MM)",
                          value=f"{datetime.now():%Y/%m}")
    c1, c2 = st.columns(2)
    estimate_mrp = c1.checkbox("Estimate missing MRP (sale price × 1.25)")
    seo = c2.checkbox("SEO meta columns (opt-in)")
    seo_plugin = st.selectbox("SEO plugin", ["yoast", "rankmath", "both"],
                              disabled=not seo)

    if seo:
        with st.expander("👁 SEO preview — first 5 rows in scope",
                         expanded=False):
            os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
            import app as _app
            # read and scope the sheet EXACTLY as the build below does -
            # same reader, same active-row filter, same select_build_rows
            # (FOUND gate included), and every field through row_context, so
            # the preview cannot drift from the CSV it is previewing (P1-16)
            pf = _app.filter_active_inventory_rows(pd.read_excel(P["filled"]))
            pf, _ = select_build_rows(pf, scope, conf)

            # process_inventory's own entry check: a sheet missing a required
            # column renders a full preview and then fails at build time
            preview_error = ""
            try:
                _app.require_input_columns(pf)
            except Exception as exc:
                preview_error = str(exc)
                pf = pf.iloc[0:0]
            if preview_error:
                st.error(f"This sheet cannot be built yet: {preview_error}")

            sample = []
            for _, ctx in preview_build_rows(pf, _app,
                                             estimate_mrp=estimate_mrp):
                sample.append({"sku": ctx["sku"],
                               **_app.seo_fields(
                                   ctx["sku"],
                                   ctx["title"],
                                   ctx["brand"],
                                   ctx["category_hierarchy"],
                                   ctx["dynamic_attrs"],
                                   ctx["regular price"],
                                   ctx["sale price"],
                                   plugin=seo_plugin)})
            if sample:
                st.dataframe(pd.DataFrame(sample), hide_index=True,
                             width="stretch")
                st.caption("These exact columns are appended to the CSV "
                           "when you build with SEO on. Untick the checkbox "
                           "to build without them.")
            else:
                st.info("No rows in scope to preview.")

    if not st.button("🛠 Build CSV", type="primary"):
        if P["csv"].exists():
            st.success(f"CSV already built: {P['csv'].name} — rebuild any "
                       "time, or continue to Step 4.")
        return

    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    import app                      # same code path as build_csv.py

    df = pd.read_excel(P["filled"])
    df = app.filter_active_inventory_rows(df)
    df, skipped = select_build_rows(df, scope, conf)
    if skipped:
        st.warning(f"{len(skipped)} confirmed row(s) skipped — not FOUND "
                   "(NEEDS CRAWL / NOT FOUND / CLOSE MATCH …). Fix them in "
                   "Step 2 and crawl: " + ", ".join(skipped))
    if df.empty:
        st.error("Nothing to build — confirm rows in Step 2 first.")
        return

    folder = month.strip().strip("/") or f"{datetime.now():%Y/%m}"
    media_base = f"https://shreejeeelectronics.in/wp-content/uploads/{folder}"

    buf = io.StringIO()
    with st.spinner(f"Building CSV for {len(df)} product(s) — images are "
                    "downloaded and converted, this can take a while…"):
        with contextlib.redirect_stdout(buf):
            result = app.process_inventory(df, media_base=media_base,
                                           estimate_mrp=estimate_mrp,
                                           seo=seo, seo_plugin=seo_plugin)
        result.to_csv(P["csv"], index=False, quoting=1)

    with_images = int((result["Images"].map(app.compact) != "").sum())
    st.success(f"Wrote **{P['csv'].name}** — {len(result)} product(s), "
               f"{with_images} with images, media base `{media_base}`")
    if with_images < len(result):
        st.warning(f"{len(result) - with_images} product(s) have NO images — "
                   "check image_url in Step 2.")

    s = batch_summary(result)
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Avg images/SKU", s["avg_images"])
    k2.metric("Zero-image rows", s["zero_image"])
    k3.metric("Rows w/ spec gaps", s["spec_gap_rows"])
    k4.metric("Missing brand", s["missing_brand"])
    k5.metric("Price range ₹", f"{s['price_min']:,}–{s['price_max']:,}")
    if s["category_counts"]:
        with st.expander("Products per category", expanded=True):
            st.bar_chart(pd.Series(s["category_counts"], name="products"))

    with st.expander("Build log"):
        st.code(buf.getvalue() or "(no output)", language=None)
    st.dataframe(result.head(50))
    st.download_button("⬇ Download CSV", P["csv"].read_bytes(),
                       file_name=P["csv"].name, mime="text/csv")
    # Record WHAT went into the CSV. Step 5 used to intersect the confirmed
    # SKUs (raw, as the enricher wrote them) with the CSV's SKUs (prefixed by
    # app.prefixed_sku), and those two sets barely overlap - measured on a
    # real batch, 2 of 14 - so a "publish confirmed rows" run silently sent a
    # fraction of them. The CSV IS the chosen scope; built_skus lets Step 5
    # verify the file on disk is still the one this build wrote.
    # validate the moment the file is written - step 4 stays as a re-check
    # (e.g. with image inspection toggled) but problems surface here first
    import validate_csv
    vdf = pd.read_csv(P["csv"], dtype=str).fillna("")
    vbuf = io.StringIO()
    with st.spinner("Validating the new CSV…"):
        with contextlib.redirect_stdout(vbuf):
            v_issues = validate_csv.validate_export(
                vdf, str(P["csv"]), check_images=True)
    update_meta(P, validated=True, issue_count=len(v_issues),
                built_scope=scope,
                built_skus=[str(s) for s in result["sku"].tolist()])
    st.session_state[f"val_out_{P['run'].name}"] = vbuf.getvalue()
    if v_issues:
        st.warning(f"Validation found **{len(v_issues)} issue(s)** — fix the "
                   "rows in Step 2 and rebuild, or review details in Step 4.")
        if P["issues"].exists():
            st.dataframe(pd.read_csv(P["issues"], dtype=str).fillna(""),
                         hide_index=True, width="stretch")
            fix_in_review_controls(
                P, pd.read_csv(P["issues"], dtype=str).fillna(""), "build")
    else:
        st.success("Validation passed — continue to **Step 5 · Publish**.")


# ------------------------------------------------------------------- step 4 --

def fix_in_review_controls(P, idf, key: str):
    """Pick a flagged SKU and jump to Step 2 with the search prefilled.
    The issues CSV holds prefixed SKUs; map back to the raw sheet sku so
    the review filter actually matches."""
    skus = [s for s in dict.fromkeys(idf["sku"].astype(str)) if s.strip()]
    if not skus:
        return
    raw = load_filled_df(P)["sku"].astype(str).tolist()
    c1, c2 = st.columns([2, 1])
    pick = c1.selectbox("Flagged SKU", skus, key=f"fix_pick_{key}")
    if c2.button("🔎 Fix in review", key=f"fix_btn_{key}"):
        st.session_state[f"flt_q_{P['run'].name}"] = (
            raw_sku_for_csv_sku(pick, raw) or pick)
        st.session_state["jump_step"] = 1        # steps[1] = Review
        st.rerun()


def step_validate(P):
    st.header("4 · Validate")
    if not P["csv"].exists():
        st.info("No CSV yet — run Step 3 first.")
        return

    # This flag gates validate_export's LOCAL image inspection (Pillow reads
    # each downloaded .webp header for size and shape). It never fetched a URL,
    # and defaulting it off made the GUI's validation weaker than the one
    # build_csv.py runs automatically.
    check_images = st.checkbox(
        "Inspect downloaded image files (size, shape, duplicate galleries)",
        value=True,
        help="Reads each .webp header from product_images_for_upload. This is "
             "what catches thumbnails, share banners and two SKUs sharing one "
             "gallery. Untick only if it is slow on a very large batch.")
    if st.button("🔍 Run validation", type="primary"):
        import validate_csv
        df = pd.read_csv(P["csv"], dtype=str).fillna("")
        buf = io.StringIO()
        with st.spinner("Validating…"):
            with contextlib.redirect_stdout(buf):
                issues = validate_csv.validate_export(
                    df, str(P["csv"]), check_images=check_images)
        update_meta(P, validated=True, issue_count=len(issues))
        st.session_state[f"val_out_{P['run'].name}"] = buf.getvalue()

    meta = load_meta(P)
    if not meta.get("validated"):
        return
    issues = meta.get("issue_count", 0)
    c1, c2 = st.columns(2)
    c1.metric("Rows in CSV", len(pd.read_csv(P["csv"], dtype=str)))
    c2.metric("Issues", issues)
    out = st.session_state.get(f"val_out_{P['run'].name}")
    if out:
        with st.expander("Validation output", expanded=issues > 0):
            st.code(out, language=None)
    if issues and P["issues"].exists():
        st.dataframe(pd.read_csv(P["issues"], dtype=str).fillna(""))
        fix_in_review_controls(
            P, pd.read_csv(P["issues"], dtype=str).fillna(""), "val")
        st.warning(f"{issues} issue(s) — fix them in Step 2 and rebuild, or "
                   "tick 'proceed anyway' in Step 5.")
    if not issues:
        st.success("No issues — ready to publish.")


# ------------------------------------------------------------------- step 5 --

def run_publish(P, extra: list) -> tuple:
    cmd = [str(VENV_PY), "wc_publish.py", str(P["csv"])] + extra
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    res = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env,
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=3600)
    out = (res.stdout or "") + (res.stderr or "")
    P["publish_log"].write_text(out, encoding="utf-8")
    return res.returncode, out


def step_publish(P):
    st.header("5 · Publish to WooCommerce")
    if not P["csv"].exists():
        st.info("No CSV yet — run Step 3 first.")
        return
    meta = load_meta(P)
    if not meta.get("validated"):
        st.warning("Run **Step 4 · Validate** first.")
        return
    if meta.get("issue_count", 0) and not st.checkbox(
            f"Proceed despite {meta['issue_count']} validation issue(s)"):
        st.stop()

    missing = [v for v in ("WC_URL", "WC_KEY", "WC_SECRET")
               if not os.environ.get(v)]
    if missing:
        st.error(f"Missing credentials in environment: {', '.join(missing)}. "
                 "Set them, then restart the app from that shell.")
        return
    if st.button("🔑 Test credentials (--check)"):
        code, out = run_publish(P, ["--check"])
        (st.success if code == 0 else st.error)("Credentials "
                                               + ("OK" if code == 0 else "FAILED"))
        st.code(out[-3000:], language=None)

    csv_df = pd.read_csv(P["csv"], dtype=str).fillna("")
    sku_col = next((c for c in csv_df.columns if c.strip().lower() == "sku"),
                   None)
    csv_skus = set(csv_df[sku_col].astype(str)) if sku_col else set()
    # The CSV is the scope: Step 3 already applied the confirmed/all-FOUND
    # filter when it built the file (re-filtering against the confirmed set
    # cannot work - Step 2 confirms the enricher's RAW sku while the CSV
    # carries app.prefixed_sku's form). But that trust holds only while the
    # file on disk IS the one Step 3 wrote - so verify it against built_skus
    # and refuse anything unrecorded or changed underneath.
    built_scope = meta.get("built_scope")
    built_skus = set(meta.get("built_skus") or [])
    if not built_scope:
        st.error("This CSV predates scope tracking — rebuild it in Step 3 so "
                 "the GUI knows exactly which rows it holds, then validate "
                 "and come back.")
        return
    if built_skus != csv_skus:
        st.error(f"The CSV on disk ({len(csv_skus)} row(s)) no longer matches "
                 f"what Step 3 built ({len(built_skus)} recorded) — it was "
                 "rebuilt or edited outside this run. Rebuild in Step 3 and "
                 "validate again.")
        return
    publish_skus = sorted(csv_skus)
    if built_scope == "confirmed":
        st.info(f"Scope: **{len(publish_skus)}** product(s) — the CSV was "
                "built from your confirmed rows only.")
    else:
        st.warning(f"Scope: all **{len(publish_skus)}** FOUND row(s) — this "
                   "CSV was NOT limited to confirmed rows. Rebuild in Step 3 "
                   "with 'Confirmed rows only' if that is what you want.")
    if not publish_skus:
        st.error("Nothing to publish.")
        return
    if len(publish_skus) > 100:
        st.warning(f"{len(publish_skus)} products is a large batch — it will "
                   "be sent in chunks of 150 SKUs per wc_publish call.")

    # Mirrors wc_publish.EXIT_BLOCKED. Duplicated rather than imported so this
    # module never has to import wc_publish (which reads store credentials at
    # import time). If that constant moves, this must follow.
    PUBLISH_EXIT_BLOCKED = 3

    def run_publish_pinned(extra):
        """Every invocation pins its scope with explicit --sku flags. Past
        ~200 the flags would hit the Windows command-line limit, so the set
        is CHUNKED across several wc_publish calls rather than ever falling
        back to publishing the CSV blind."""
        outs, worst = [], 0
        for i in range(0, len(publish_skus), 150):
            flags = []
            for s in publish_skus[i:i + 150]:
                flags += ["--sku", s]
            code, out = run_publish(P, flags + extra)
            outs.append(out)
            # Worst-of, never last-of: wc_publish exits 2 when ANY row failed, so
            # returning the final chunk's code would let a clean chunk 2 mask a
            # failed chunk 1 and unlock the commit gate after a bad dry run.
            worst = max(worst, code)
            # Only an edge challenge (EXIT_BLOCKED) makes the later chunks
            # pointless - every one would be blocked the same way. A row-level
            # failure must NOT stop them, or the retry panel never sees the SKUs
            # that come after the first bad row.
            if code == PUBLISH_EXIT_BLOCKED:
                break
        return worst, "\n".join(outs)

    dry_key = f"dryrun_{P['run'].name}"
    st.subheader("Ⅰ · Dry run (nothing is written)")
    if st.button("🧪 Dry run", type="primary"):
        with st.spinner("Dry run…"):
            code, out = run_publish_pinned([])
        st.session_state[dry_key] = (code == 0)
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
        (st.success if code == 0 else st.error)(
            "Dry run finished" if code == 0 else "Dry run FAILED — fix before "
            "committing")

    if not st.session_state.get(dry_key):
        st.caption("Commit unlocks after a successful dry run in this session.")
        return

    st.subheader("Ⅱ · Commit")
    reviewed = st.checkbox("I reviewed the dry-run output above")
    typed = st.text_input(f"Type the run name (`{P['run'].name}`) to enable "
                          "the commit button")
    armed = reviewed and typed == P["run"].name
    go_live = False
    if armed:
        go_live = st.checkbox("Also `--status publish` — make products LIVE "
                              "immediately (default: drafts)", value=False)
    label = ("🚀 Commit — create/update as LIVE products" if go_live
             else "📝 Commit — create/update as DRAFTS")
    if st.button(label, type="primary", disabled=not armed):
        extra = ["--commit"]
        if go_live:
            extra += ["--status", "publish"]
        with st.spinner(f"Publishing {len(publish_skus)} product(s)…"):
            code, out = run_publish_pinned(extra)
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
        if code == 0:
            st.success(f"Done — {len(publish_skus)} product(s) upserted as "
                       + ("LIVE products." if go_live else "drafts."))
            update_meta(P, published=datetime.now().isoformat(timespec="seconds"),
                        published_live=go_live,
                        published_count=len(publish_skus))
            st.session_state[dry_key] = False
        else:
            st.error("Publish FAILED — see output above; full log in "
                     f"{P['publish_log'].name}")

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


# --------------------------------------------------------------------- main --

def archive_run(run_dir: Path, dest: Path) -> int:
    """Copy a run's reviewable outputs into batches\\<name>\\ - filled sheet,
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


def pick_run() -> Path | None:
    RUNS_DIR.mkdir(exist_ok=True)
    runs = sorted((d for d in RUNS_DIR.iterdir() if d.is_dir()),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    HOME, NEW = "🏠 All runs", "➕ New run…"
    options = [HOME, NEW] + [d.name for d in runs]
    # land on the latest run as before; Home is one click away. With NO
    # runs yet the default must be NEW, not HOME - Home's empty state tells
    # you to create a run in the sidebar, but the name box only renders for
    # NEW, so defaulting to Home would leave a fresh install unable to make
    # its first run (tests	est_gui_ui.py::test_create_run pins this).
    sel = st.sidebar.selectbox("Batch / run", options,
                               index=2 if runs else 1)
    if sel == HOME:
        return None
    if sel != NEW:
        return RUNS_DIR / sel
    name = st.sidebar.text_input("New run name",
                                 value=f"run_{datetime.now():%Y%m%d}")
    if st.sidebar.button("Create run"):
        safe = re.sub(r"[^\w\-]+", "_", name.strip()).strip("_")
        if safe:
            (RUNS_DIR / safe).mkdir(exist_ok=True)
            st.rerun()
    return None


def save_first_sheet(path, edited):
    """Write `edited` back into the FIRST worksheet of an existing workbook,
    keeping that sheet's NAME and position and every OTHER sheet untouched.

    `edited.to_excel(path)` would rewrite category_map.xlsx as a single-sheet
    'Sheet1' workbook, silently destroying the 'Sub-Category Attributes' sheet
    (which app.load_subcategory_attributes reads, and whose loss makes
    `_spec_gaps` blank for every product in every later build) and 'Filters'.
    """
    wb = openpyxl.load_workbook(path)
    name = wb.sheetnames[0]
    index = 0
    del wb[name]
    ws = wb.create_sheet(name, index)
    ws.append([str(c) for c in edited.columns])
    for _, row in edited.iterrows():
        ws.append(["" if pd.isna(v) else str(v) for v in row.tolist()])
    wb.save(path)


def backup_and_save_category_map(path, edited):
    """Back the taxonomy up, then write the edited first sheet back.

    The two steps belong together and are the whole of the editor's save
    path, so they live in one plain function tests\\test_gui_runmgr.py can
    call without a Streamlit session. Returns the backup path; the backup
    is taken BEFORE the write, so a botched edit is always one file copy
    away from being undone."""
    backup = (REPO_ROOT / "backups" /
              f"category_map_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
    backup.parent.mkdir(exist_ok=True)
    shutil.copy2(path, backup)
    save_first_sheet(path, edited)
    return backup


def category_map_editor():
    """Edit category_map.xlsx in place. app.py snaps categories to these
    EXACT strings, so removals are loud: they are listed and must be
    acknowledged before the save button arms. Every save backs up the
    current file to backups\\ first.

    Only the FIRST sheet is editable here; the other sheets of the workbook
    ('Sub-Category Attributes', 'Filters') are preserved verbatim."""
    st.header("⚙ Category map — category_map.xlsx")
    path = REPO_ROOT / "category_map.xlsx"
    if not path.exists():
        st.error("category_map.xlsx not found next to app.py.")
        return
    df = pd.read_excel(path, sheet_name=0, dtype=str).fillna("")
    other = openpyxl.load_workbook(path, read_only=True).sheetnames[1:]
    st.caption("Column 1: parent category. Column 2: subcategories "
               "(comma-separated). Builds snap to `Parent > Sub` exactly "
               "as written here."
               + (f" Other sheets in the workbook ({', '.join(other)}) are "
                  "left untouched by a save." if other else ""))
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
    if st.button("💾 Save category map", type="primary",
                 disabled=not ok):
        try:
            backup = backup_and_save_category_map(path, edited)
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


def main():
    st.set_page_config(page_title="Shree Jee pipeline", page_icon="🛒",
                       layout="wide")
    st.sidebar.title("🛒 Shree Jee pipeline")
    if st.sidebar.checkbox("⚙ Category map editor",
                           help="Edit the taxonomy the builds snap to. "
                                "Closes the wizard while open."):
        category_map_editor()
        return

    run_dir = pick_run()
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
            # P2-25: both actions are unsafe while the enricher owns the
            # folder - rmtree hits enricher.log, which the child holds open
            # for write, and Windows raises a sharing violation mid-delete;
            # archive would copy a sheet still being rewritten.
            busy = enricher_running(run_paths(tdir))
            if busy:
                st.caption(f"Enrichment is running in runs\\{target} — stop "
                           "the enricher first (open the run, then Cancel).")
            if c1.button("📦 Archive to batches\\", key="mgr_archive",
                         disabled=busy):
                dest = REPO_ROOT / "batches" / target
                if dest.exists():
                    st.error(f"batches\\{target} already exists — rename or "
                             "remove it first; nothing was copied.")
                else:
                    try:
                        n = archive_run(tdir, dest)
                    except OSError as e:
                        # a sheet open in Excel, an image deleted under us, a
                        # full disk: a friendly message, never a red traceback
                        st.error(f"Could not finish archiving runs\\{target}: "
                                 f"{e.__class__.__name__} — {e}. "
                                 f"batches\\{target} may be partly written; "
                                 "remove it and retry.")
                    else:
                        st.success(f"Archived {n} file(s) to batches\\{target}."
                                   " The run folder itself is untouched.")
            # key MUST carry the target: a static key keeps the tick alive
            # when the selectbox moves to another run, so the next run could
            # be deleted with one click and no confirmation (P2-25)
            sure = c2.checkbox(f"Yes, permanently delete runs\\{target}",
                               key=f"mgr_sure_{target}")
            if c2.button("🗑 Delete run", disabled=not sure or busy,
                         key="mgr_del"):
                try:
                    shutil.rmtree(tdir)
                except OSError as e:
                    # OSError, not PermissionError: the folder can also be
                    # gone already (Explorer, a second browser tab) or fail
                    # with a bare OSError - all of them must stay a message
                    st.error(f"Could not delete runs\\{target} "
                             f"({e.__class__.__name__}): a file in it is in "
                             "use (open in Excel or still being written), or "
                             "the folder changed underneath — refresh and "
                             "retry. Some files may already have been "
                             "removed.")
                else:
                    st.rerun()
        return
    P = run_paths(run_dir)

    steps = [("1 · Upload & Enrich", True, step_enrich),
             ("2 · Review & Confirm", P["filled"].exists(), step_review),
             ("3 · Build CSV", P["filled"].exists(), step_build),
             ("4 · Validate", P["csv"].exists(), step_validate),
             ("5 · Publish", P["csv"].exists(), step_publish)]
    labels = [f"{name}  {'' if ok else '🔒'}" for name, ok, _ in steps]
    # a step can request a jump (e.g. "fix this SKU in review") by setting
    # jump_step before st.rerun(); labels are rebuilt every run, so resolve
    # the index here, never store a label string
    if "jump_step" in st.session_state:
        st.session_state["step_radio"] = labels[
            st.session_state.pop("jump_step")]
    choice = st.sidebar.radio("Steps", labels, key="step_radio")
    idx = labels.index(choice)

    st.sidebar.divider()
    st.sidebar.caption(f"Run folder: `runs\\{run_dir.name}`")
    if enricher_running(P):
        st.sidebar.warning("Enricher running…")

    name, ok, fn = steps[idx]
    if not ok:
        st.header(name)
        st.info("🔒 Locked — complete the earlier steps first.")
        return
    fn(P)


if __name__ == "__main__":
    main()
