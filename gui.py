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
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import openpyxl
import pandas as pd
import streamlit as st

from gui_helpers import (batch_summary, filter_review_df, flag_value,
                         sanity_issues)

REPO_ROOT = Path(__file__).parent
VENV_PY = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
RUNS_DIR = REPO_ROOT / "runs"

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


def start_enricher(P, extra_flags: list, total: int):
    """Launch sku_enricher.py detached, stdout -> enricher.log (a file, never a
    pipe: a full pipe buffer would deadlock the child)."""
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
                total_rows=total, enricher_cmd=" ".join(cmd))


def cancel_enricher(P):
    """CTRL_BREAK first - the enricher treats it as KeyboardInterrupt and
    checkpoints ('Stopped by user - progress saved') - then hard-kill."""
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


def rows_done(log_text: str) -> int:
    hits = re.findall(r"^\[(\d+)\] Row \d+", log_text, flags=re.M)
    return int(hits[-1]) if hits else 0


def enricher_panel(P):
    """Live progress while the enricher subprocess runs. A fragment so only
    this panel re-renders every 2 s, not the whole page."""
    run_key = f"running_{P['run'].name}"

    @st.fragment(run_every=2.0)
    def _panel():
        text = tail_text(P["log"])
        done = rows_done(text)
        total = load_meta(P).get("total_rows") or 0
        running = enricher_running(P)
        if total:
            frac = min(done / total, 0.97 if running else 1.0)
            st.progress(frac, text=f"{done} / {total} SKUs processed this run")
        lines = [ln for ln in text.splitlines() if ln.strip()][-25:]
        st.code("\n".join(lines) or "waiting for first output…", language=None)
        if not running and st.session_state.get(run_key):
            st.session_state[run_key] = False
            update_meta(P, enricher_pid=None)
            st.rerun(scope="app")

    _panel()
    if st.button("⏹ Stop enrichment (progress is saved)", key=f"cancel_{P['run'].name}"):
        cancel_enricher(P)
        st.rerun()


# ----------------------------------------------------------- status buckets --

def classify(status) -> str:
    s = str(status or "").strip()
    u = s.upper()
    # two-pass flow (--discover): a link is waiting to be looked at, and the
    # row is deliberately NOT importable yet. These must not read as failures.
    if u.startswith("NEEDS CRAWL"):
        return "review" if "CHECK LINK" in u else "pending"
    if u.startswith("NEEDS LINK"):
        return "failed"      # nothing found - a human has to paste a URL
    if u.startswith("FOUND"):
        return "review" if " - " in s else "found"
    if u.startswith(("CLOSE", "CHECK")):
        return "review"
    if u.startswith("SKIPPED"):
        return "skipped"
    return "failed"          # NOT FOUND / ERROR / blank (not processed)


BADGE = {"found": "🟢", "review": "🟡", "failed": "🔴", "skipped": "⚪",
         "pending": "🔵"}


# ------------------------------------------------------------------- step 1 --

def step_enrich(P):
    st.header("1 · Upload & Enrich")

    if not P["input"].exists():
        up = st.file_uploader("SKU list (.xlsx / .xls / .csv) - just a SKU "
                              "column is enough", type=["xlsx", "xls", "csv"])
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
                  cat_options: list | None = None) -> pd.DataFrame:
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
        disabled=["state", "sku", "thumb", "specs"],
        column_config={
            "confirmed": st.column_config.CheckboxColumn("✔", default=False,
                                                         width="small"),
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

    probs = sanity_issues(
        df, min_price=flag_value(load_meta(P).get("enricher_flags", []),
                                 "--min-price"))
    if probs:
        st.warning(f"⚠ {len(probs)} data problem(s) in this sheet — fix them "
                   "here before building the CSV.")
        with st.expander("Show problems", expanded=len(probs) <= 10):
            st.dataframe(pd.DataFrame(probs), hide_index=True,
                         use_container_width=True)

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
    # different rows; hashing the filter into the key does that
    fsig = hashlib.md5(f"{query}|{'|'.join(brands)}".encode()).hexdigest()[:6]

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
                                   f"ed_failed_{fsig}", cat_opts))
    with tabs[2]:
        parts.append(review_editor(fdf[fdf["_bucket"] == "pending"],
                                   f"ed_pending_{fsig}", cat_opts))
    with tabs[3]:
        parts.append(review_editor(fdf[fdf["_bucket"] == "found"],
                                   f"ed_found_{fsig}", cat_opts))
    with tabs[4]:
        parts.append(review_editor(fdf, f"ed_all_{fsig}", cat_opts))

    b1, b2, b3, b4 = st.columns([1.4, 1.4, 1.6, 1.6])
    if b1.button("💾 Save edits & confirmations", type="primary"):
        # the All tab duplicates the bucket rows; later parts win, so pass it
        # first and let bucket edits override it. parts is in TAB order -
        # review, failed, pending, found, all - so All is the last one.
        ordered = [parts[-1]] + parts[:-1]
        try:
            changed = write_edits_back(P, df, ordered)
        except PermissionError:
            st.error("The filled .xlsx is open in Excel — close it and save "
                     "again.")
            return
        flags = {sku: True for sku in confirmed_set(P)}   # hidden rows keep ✔
        for part in ordered:
            for _, row in part.iterrows():
                flags[str(row["sku"])] = bool(row["confirmed"])
        save_confirmed(P, flags)
        st.success(f"Saved — {changed} cell(s) updated, "
                   f"{sum(flags.values())} row(s) confirmed.")
        st.rerun()

    if b2.button("✅ Confirm all clean FOUND"):
        flags = {str(r["sku"]): (bool(r["confirmed"]) or r["_bucket"] == "found")
                 for _, r in df.iterrows()}
        save_confirmed(P, flags)
        st.rerun()

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

    if not st.button("🛠 Build CSV", type="primary"):
        if P["csv"].exists():
            st.success(f"CSV already built: {P['csv'].name} — rebuild any "
                       "time, or continue to Step 4.")
        return

    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    import app                      # same code path as build_csv.py

    df = pd.read_excel(P["filled"])
    df = app.filter_active_inventory_rows(df)
    if scope == "confirmed":
        df = df[df["sku"].astype(str).isin(conf)].copy()
    elif "status" in df.columns:
        keep = (df["status"].map(app.compact).str.upper()
                .str.startswith("FOUND"))
        df = df[keep].copy()
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
                         hide_index=True, use_container_width=True)
    else:
        st.success("Validation passed — continue to **Step 5 · Publish**.")


# ------------------------------------------------------------------- step 4 --

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

    def run_publish_pinned(extra):
        """Every invocation pins its scope with explicit --sku flags. Past
        ~200 the flags would hit the Windows command-line limit, so the set
        is CHUNKED across several wc_publish calls rather than ever falling
        back to publishing the CSV blind."""
        outs, code = [], 0
        for i in range(0, len(publish_skus), 150):
            flags = []
            for s in publish_skus[i:i + 150]:
                flags += ["--sku", s]
            code, out = run_publish(P, flags + extra)
            outs.append(out)
            if code != 0:
                break
        return code, "\n".join(outs)

    dry_key = f"dryrun_{P['run'].name}"
    st.subheader("Ⅰ · Dry run (nothing is written)")
    if st.button("🧪 Dry run", type="primary"):
        with st.spinner("Dry run…"):
            code, out = run_publish_pinned([])
        st.session_state[dry_key] = (code == 0)
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


# --------------------------------------------------------------------- main --

def pick_run() -> Path | None:
    RUNS_DIR.mkdir(exist_ok=True)
    runs = sorted((d for d in RUNS_DIR.iterdir() if d.is_dir()),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    options = ["➕ New run…"] + [d.name for d in runs]
    sel = st.sidebar.selectbox("Batch / run", options,
                               index=1 if runs else 0)
    if sel != "➕ New run…":
        return RUNS_DIR / sel
    name = st.sidebar.text_input("New run name",
                                 value=f"run_{datetime.now():%Y%m%d}")
    if st.sidebar.button("Create run"):
        safe = re.sub(r"[^\w\-]+", "_", name.strip()).strip("_")
        if safe:
            (RUNS_DIR / safe).mkdir(exist_ok=True)
            st.rerun()
    return None


def main():
    st.set_page_config(page_title="Shree Jee pipeline", page_icon="🛒",
                       layout="wide")
    st.sidebar.title("🛒 Shree Jee pipeline")

    run_dir = pick_run()
    if run_dir is None:
        st.title("Shree Jee catalogue pipeline")
        st.info("Create a run in the sidebar to begin — each run keeps its "
                "input, enriched sheet, CSV and logs together under "
                "`runs\\<name>\\`.")
        return
    P = run_paths(run_dir)

    steps = [("1 · Upload & Enrich", True, step_enrich),
             ("2 · Review & Confirm", P["filled"].exists(), step_review),
             ("3 · Build CSV", P["filled"].exists(), step_build),
             ("4 · Validate", P["csv"].exists(), step_validate),
             ("5 · Publish", P["csv"].exists(), step_publish)]
    labels = [f"{name}  {'' if ok else '🔒'}" for name, ok, _ in steps]
    choice = st.sidebar.radio("Steps", labels)
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
