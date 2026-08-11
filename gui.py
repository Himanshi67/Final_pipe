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
import io
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import openpyxl
import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).parent
VENV_PY = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
RUNS_DIR = REPO_ROOT / "runs"

SKU_HEADER_HINTS = {"sku", "skus", "sku code", "item code", "itemcode",
                    "model", "model no", "part no", "item"}

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
            h = str(cell.value or "").strip().lower()
            if h in SKU_HEADER_HINTS:
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

def pid_alive(pid: int) -> bool:
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
    return bool(ok) and code.value == STILL_ACTIVE


def enricher_running(P) -> bool:
    proc = st.session_state.get(f"proc_{P['run'].name}")
    if proc is not None and proc.poll() is None:
        return True
    return pid_alive(load_meta(P).get("enricher_pid"))


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
    update_meta(P, enricher_pid=proc.pid, enricher_flags=extra_flags,
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
        import time
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
    if u.startswith("FOUND"):
        return "review" if " - " in s else "found"
    if u.startswith(("CLOSE", "CHECK")):
        return "review"
    if u.startswith("SKIPPED"):
        return "skipped"
    return "failed"          # NOT FOUND / ERROR / blank (not processed)


BADGE = {"found": "🟢", "review": "🟡", "failed": "🔴", "skipped": "⚪"}


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
        started = st.form_submit_button("▶ Start enrichment", type="primary")

    if started:
        flags = []
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
                 "image_url", "categories", "Brands"]


def load_filled_df(P) -> pd.DataFrame:
    df = pd.read_excel(P["filled"], dtype=str).fillna("")
    df.columns = [str(c).strip() for c in df.columns]
    for col in ["sku", "status"] + EDITABLE_COLS:
        if col not in df.columns:
            df[col] = ""
    for col in ("regular price", "sale price"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def review_editor(df: pd.DataFrame, key: str) -> pd.DataFrame:
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
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🟢 Found (clean)", int(n.get("found", 0)))
    m2.metric("🟡 Needs review", int(n.get("review", 0)))
    m3.metric("🔴 Failed / not processed", int(n.get("failed", 0)))
    m4.metric("✔ Confirmed", int(df["confirmed"].sum()))

    st.caption("Edit title, prices, URLs and image URLs directly in the "
               "table; tick ✔ to confirm a row for the CSV. Then press "
               "**Save edits & confirmations**.")

    tabs = st.tabs([f"🟡 Needs review ({int(n.get('review', 0))})",
                    f"🔴 Failed ({int(n.get('failed', 0))})",
                    f"🟢 Found ({int(n.get('found', 0))})",
                    f"All rows ({len(df)})"])
    parts = []
    with tabs[0]:
        parts.append(review_editor(df[df["_bucket"] == "review"], "ed_review"))
    with tabs[1]:
        parts.append(review_editor(df[df["_bucket"] == "failed"], "ed_failed"))
    with tabs[2]:
        parts.append(review_editor(df[df["_bucket"] == "found"], "ed_found"))
    with tabs[3]:
        parts.append(review_editor(df, "ed_all"))

    b1, b2, b3, b4 = st.columns([1.4, 1.4, 1.6, 1.6])
    if b1.button("💾 Save edits & confirmations", type="primary"):
        # the All tab duplicates the bucket rows; later parts win, so pass it
        # first and let bucket edits override it
        ordered = [parts[3], parts[0], parts[1], parts[2]]
        try:
            changed = write_edits_back(P, df, ordered)
        except PermissionError:
            st.error("The filled .xlsx is open in Excel — close it and save "
                     "again.")
            return
        flags = {}
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
    base_flags = [f for f in meta.get("enricher_flags", [])
                  if f not in ("--force", "--retry-incomplete",
                               "--retry-missing")]
    total = meta.get("total_rows", 0)
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
    scope = st.radio("Rows to include",
                     [f"Confirmed rows only ({len(conf)})",
                      "All FOUND rows (skip unverified, like build_csv.py)"],
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
    if scope.startswith("Confirmed"):
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
    with st.expander("Build log"):
        st.code(buf.getvalue() or "(no output)", language=None)
    st.dataframe(result.head(50))
    st.download_button("⬇ Download CSV", P["csv"].read_bytes(),
                       file_name=P["csv"].name, mime="text/csv")
    update_meta(P, validated=False)     # a new CSV must be re-validated
    st.info("Continue to **Step 4 · Validate**.")


# ------------------------------------------------------------------- step 4 --

def step_validate(P):
    st.header("4 · Validate")
    if not P["csv"].exists():
        st.info("No CSV yet — run Step 3 first.")
        return

    check_images = st.checkbox("Also verify image URLs respond", value=False,
                               help="Slower: fetches every image header")
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

    conf = confirmed_set(P)
    csv_df = pd.read_csv(P["csv"], dtype=str).fillna("")
    sku_col = next((c for c in csv_df.columns if c.strip().lower() == "sku"),
                   None)
    csv_skus = set(csv_df[sku_col].astype(str)) if sku_col else set()
    publish_skus = sorted(conf & csv_skus) if conf else sorted(csv_skus)
    if conf:
        st.info(f"Scope: **{len(publish_skus)} confirmed** SKU(s) present in "
                "the CSV (only confirmed rows are ever sent).")
    else:
        st.warning(f"No rows confirmed in Step 2 — scope falls back to all "
                   f"{len(publish_skus)} CSV rows. Confirming first is safer.")
    if not publish_skus:
        st.error("Nothing to publish.")
        return
    if len(publish_skus) > 100:
        st.warning(f"{len(publish_skus)} products is a large batch — consider "
                   "publishing in chunks.")

    sku_flags = []
    if conf:                      # pin scope explicitly; never the CSV blind
        for s in publish_skus:
            sku_flags += ["--sku", s]

    dry_key = f"dryrun_{P['run'].name}"
    st.subheader("Ⅰ · Dry run (nothing is written)")
    if st.button("🧪 Dry run", type="primary"):
        with st.spinner("Dry run…"):
            code, out = run_publish(P, sku_flags)
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
        extra = sku_flags + ["--commit"]
        if go_live:
            extra += ["--status", "publish"]
        with st.spinner(f"Publishing {len(publish_skus)} product(s)…"):
            code, out = run_publish(P, extra)
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
