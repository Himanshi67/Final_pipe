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
import csv as csv_module
import ctypes
import getpass
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

import client_config
import gui_helpers
from client_config import (DEFAULT_CLIENT, ClientConfigError,
                           available_clients, broken_clients,
                           load_client, load_env,
                           resolve_slug, same_store, store_host)
from gui_helpers import (BADGE, append_events, assemble_pack, batch_summary,
                         card_queue_index, classify, configure_sku_codes,
                         clock_elapsed, confirmed_from_state,
                         coverage_summary, default_client_hosts,
                         remove_refused,
                         csv_signature, eligible_rows, eta_text,
                         filter_review_df, flag_value, found_status,
                         limit_from_flags,
                         merge_review_flags,
                         APPROVE as REVIEW_APPROVE, SKIP as REVIEW_SKIP,
                         CLEAR as REVIEW_CLEAR,
                         review_counts, review_counts_text, review_decision,
                         review_entries, skipped_from_state,
                         parse_enricher_log,
                         parse_image_urls, parse_publish_output,
                         parse_specs_cell, raw_sku_for_csv_sku,
                         render_batch_readme,
                         review_caption, review_event, review_metrics,
                         row_fingerprint,
                         run_stage, sanity_issues, select_build_rows,
                         sheet_combos, stale_confirmations)

REPO_ROOT = Path(__file__).parent
# The Windows venv interpreter when it exists; in the Linux container (no
# .venv) the interpreter that runs this GUI.
VENV_PY = gui_helpers.venv_python(REPO_ROOT)
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


# -------------------------------------------------------------- share mode --
# GUI_SHARE_MODE / GUI_SHARE_PASSWORD / GUI_SHARE_BUDGET are read only by the
# gui_helpers.share_* functions. Each dangerous action is guarded twice: the
# control is not rendered, AND the function that does the work refuses
# (ShareModeRefused, or exit code 4 for run_publish), so a crafted widget
# state cannot get through. tools\share_gui.ps1 starts this mode.

SHARE_AUTH_KEY = "_share_authenticated"


def share_mode() -> bool:
    return gui_helpers.share_mode_on()


def refuse_in_share_mode(action: str):
    """Raise ShareModeRefused when share mode is on. Called first thing by
    every function that publishes, deletes, archives, saves the taxonomy or
    switches the client."""
    if share_mode():
        raise gui_helpers.ShareModeRefused(
            f"{action} is off in share mode.")


def gate_on() -> bool:
    """The password gate runs: share mode, or password-only mode
    (GUI_PASSWORD). share_mode() stays False in password-only mode."""
    return gui_helpers.gate_on()


def password_gate() -> bool:
    """The ONE password gate, for share mode AND password-only mode. Runs
    right after st.set_page_config and before any other widget, sidebar or
    file access. Returns True when the page may render; otherwise it renders
    only the gate and calls st.stop().

    With neither mode on it renders nothing and returns True. With no usable
    password (unset in share mode, or < 12 characters in either mode) it
    renders only an error (fail closed). gui_helpers.gate_password() picks
    the password: GUI_SHARE_PASSWORD when share mode is on (share mode wins
    when both are set), else GUI_PASSWORD. The guess throttle is process-wide
    (gui_helpers.SHARE_THROTTLE): 8 wrong attempts in total, across all
    sessions, lock every login for 15 minutes. The password is never echoed,
    logged or kept in session_state. Only share mode shows the banner and
    hides controls; password-only mode hides nothing after login."""
    if not gate_on():
        return True
    problem = gui_helpers.gate_password_problem()
    if problem:
        st.error(problem)
        st.stop()
    if st.session_state.get(SHARE_AUTH_KEY) is True:
        if share_mode():
            st.info(gui_helpers.SHARE_BANNER, icon="🔒")
        return True

    throttle = gui_helpers.SHARE_THROTTLE
    st.title("Catalogue pipeline")
    st.caption("This page is password-protected.")
    with st.form("share_login", clear_on_submit=True):
        given = st.text_input("Password", type="password",
                              key="share_password_input")
        submitted = st.form_submit_button("Log in")
    if submitted:
        wait = throttle.locked_for()
        if wait > 0:
            st.error(f"Too many wrong passwords. Logins are locked for "
                     f"{int(wait // 60) + 1} more minute(s).")
        elif gui_helpers.check_share_password(
                given, gui_helpers.gate_password()):
            throttle.record_success()
            st.session_state[SHARE_AUTH_KEY] = True
            st.rerun()
        else:
            throttle.record_failure()
            time.sleep(gui_helpers.SHARE_FAIL_DELAY)
            if throttle.locked_for() > 0:
                st.error("Too many wrong passwords. Logins are locked for "
                         f"{gui_helpers.SHARE_LOCK_SECONDS // 60} minutes.")
            else:
                st.error("Wrong password.")
    st.stop()
    return False


share_gate = password_gate          # the old name, kept for callers/tests


# ------------------------------------------------------------------ client --
# One GUI process serves every client, so the selected slug lives in
# session_state, never in this module. It IS mirrored into os.environ,
# because that is how app.py / validate_csv.py pick their client up at import
# time and how every subprocess (sku_enricher.py, wc_publish.py) is told.
# See docs/CLIENT_CONFIG.md.

CLIENT_KEY = "client_slug"
_CFG_KEY = "_client_cfg"
# the slug the selector last OFFERED (so a load failure names the config the
# operator just picked, not the previous one) and a slug that was resolved
# but is not selectable, kept across the rerun so the warning survives it
ATTEMPT_KEY = "_client_attempt"
IGNORED_KEY = "_client_ignored"


def default_slug() -> str:
    """The slug the selector starts on: $PIPELINE_CLIENT, else DEFAULT_CLIENT.

    argv is deliberately EMPTY - under `streamlit run gui.py` sys.argv holds
    Streamlit's own arguments, and a stray `--client` there would be
    Streamlit's, not ours."""
    return resolve_slug(argv=[])


def current_client():
    """The ClientConfig for the slug the sidebar selector holds.

    Cached in session_state so a re-render does not re-parse client.toml, and
    os.environ is refreshed on every call: a lazily-imported `import app`
    deeper in a step must see the right client even if the selector moved in
    an earlier rerun.

    In share mode the slug is ALWAYS the one the process started with
    (default_slug()); session_state cannot move it."""
    if share_mode():
        slug = default_slug()
    else:
        slug = st.session_state.get(CLIENT_KEY) or default_slug()
    st.session_state[CLIENT_KEY] = slug
    os.environ[client_config.ENV_VAR] = slug
    cached = st.session_state.get(_CFG_KEY)
    if cached is not None and cached.slug == slug:
        return cached
    cfg = load_client(slug)
    st.session_state[_CFG_KEY] = cfg
    return cfg


def switch_client(slug: str):
    """Point the whole process at another client and rerun.

    Order matters: the environment goes first, so any module imported AFTER
    this point boots on the new client by itself; the two modules that may
    already be imported are reconfigured explicitly (they hold the taxonomy
    and the media base as module state); gui_helpers' SKU codes are rebound;
    and the category dropdown cache is dropped because it is keyed by slug
    but its underlying `app` module is a singleton."""
    refuse_in_share_mode("Switching the client")
    cfg = load_client(slug)                      # raises before anything moves
    os.environ[client_config.ENV_VAR] = slug
    st.session_state[CLIENT_KEY] = slug
    st.session_state[_CFG_KEY] = cfg
    configure_sku_codes(cfg.sku.prefixes)
    for name in ("app", "validate_csv"):
        module = sys.modules.get(name)
        # not imported yet = nothing to reconfigure; the env above is what
        # its import-time constants will read.
        if module is not None and hasattr(module, "configure"):
            module.configure(slug)
    with contextlib.suppress(Exception):
        category_options.clear()
    return cfg


def client_selector():
    """Sidebar Client selectbox, above the run selector. Returns the config.

    A failure here is fatal for the page (no taxonomy, no store URL), so it
    is reported as an error rather than a traceback."""
    if share_mode():
        # no selector at all: the client is the one the process started with
        cfg = current_client()
        st.sidebar.caption(f"Client: `{cfg.slug}` (fixed in share mode)")
        return cfg
    slugs = available_clients() or [DEFAULT_CLIENT]
    current = st.session_state.get(CLIENT_KEY) or default_slug()
    if current not in slugs:
        # a typo in $PIPELINE_CLIENT used to select slugs[0] in silence -
        # i.e. somebody ELSE's store, whichever one sorts first - and then
        # switch_client() to it. The CLI refuses the same typo loudly; so
        # does this, and the fallback is the DEFAULT client, never [0].
        st.session_state[IGNORED_KEY] = current
        current = DEFAULT_CLIENT if DEFAULT_CLIENT in slugs else slugs[0]
    ignored = st.session_state.get(IGNORED_KEY)
    if ignored and ignored not in slugs:
        st.sidebar.warning(
            f"Unknown client '{ignored}' ignored (${client_config.ENV_VAR} "
            f"or a stale selection) — using '{current}'.")
    else:
        st.session_state.pop(IGNORED_KEY, None)
    # a client.toml that exists but does not load is not offered at all
    # (available_clients filters it out), so say why it is missing
    broken = broken_clients()
    if broken:
        st.sidebar.caption(
            "Not selectable, client.toml does not load: "
            + "; ".join(f"{slug} ({why})" for slug, why in broken.items()))
    index = slugs.index(current)
    chosen = st.sidebar.selectbox(
        "Client", slugs, index=index, key="client_picker",
        help="Which store this session builds for — its taxonomy, media "
             "base, SKU prefixes, credentials and credit ledger all come "
             "from clients\\<slug>\\.")
    st.session_state[ATTEMPT_KEY] = chosen
    if chosen != st.session_state.get(CLIENT_KEY):
        switch_client(chosen)
        st.rerun()
    return current_client()


def client_env(cfg) -> dict:
    """clients/<slug>/.env as a dict; {} (loudly) when it is malformed.

    The GUI never injects it into its OWN os.environ - one process serves
    every client, so a stray WC_SECRET would outlive the selector. It reads
    the file for the credential presence check and the store-URL guard;
    every subprocess loads the same file itself (client_config.apply_env)."""
    try:
        return load_env(cfg)
    except ClientConfigError as exc:
        st.error(f"clients\\{cfg.slug}\\.env could not be read: {exc}")
        return {}


def run_client_slug(run_dir: Path) -> str:
    """Which client owns a run. A run created before the selector existed has
    no `client` key in run_meta.json and belongs to DEFAULT_CLIENT."""
    meta = load_json(run_paths(run_dir)["meta"])
    return str(meta.get("client") or "").strip() or DEFAULT_CLIENT


def guard_run_client(run_dir: Path, cfg) -> bool:
    """True when this run may be opened under `cfg`; else render the refusal.

    A run is never re-homed: its CSV, its downloaded images and the taxonomy
    its categories were snapped to all belong to the client that created it,
    so opening it under another one would publish one store's catalogue to
    another. A legacy run is STAMPED with its implied owner on first open, so
    the next operator cannot claim it by switching the selector."""
    owner = run_client_slug(run_dir)
    if owner != cfg.slug:
        st.error(
            f"Run `{run_dir.name}` belongs to client `{owner}`, but the "
            f"sidebar is set to `{cfg.slug}`. Switch the Client selector back "
            f"to `{owner}` to open it — a run keeps the taxonomy, images and "
            "store it was built for.")
        return False
    meta_path = run_paths(run_dir)["meta"]
    meta = load_json(meta_path)
    if not str(meta.get("client") or "").strip():
        meta["client"] = owner
        save_json(meta_path, meta)
    return True


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
        "metrics": run_dir / "review_metrics.json",
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
    return confirmed_from_state(load_json(P["review"]))


def skipped_map(P) -> dict:
    """{sku: reason} of the rows the reviewer skipped."""
    return skipped_from_state(load_json(P["review"]))


def save_confirmed(P, skus: dict, df=None, skipped=None):
    """Persist the review map as {sku: {"confirmed", "skipped",
    "skip_reason", "fp"}}.

    `df` is the sheet as it stands AFTER the edits being saved (P3-53): the
    fingerprint has to describe the row the reviewer just approved, so a
    later --retry-incomplete rewriting that row invalidates the tick. Rows
    absent from `df` keep whatever fingerprint they already had - the card
    view hands in a single-row frame, and that must not blank the rest.

    `skipped` is the COMPLETE {sku: reason} skip map to store. None keeps
    the skips already on disk - and then a skipped row stays skipped even
    if `skus` ticks it (gui_helpers.review_entries: skip wins), so a caller
    that means 'approve' must clear the skip through review_decision."""
    state = load_json(P["review"])
    old = state.get("confirmed", {})
    if skipped is None:
        skipped = skipped_from_state(state)
    fps = {}
    if df is not None and "sku" in getattr(df, "columns", []):
        for _, row in df.iterrows():
            fps.setdefault(str(row["sku"]), row_fingerprint(row))
    state["confirmed"] = review_entries(old, skus, skipped, fps)
    state["updated"] = datetime.now().isoformat(timespec="seconds")
    save_json(P["review"], state)


def reconcile_confirmations(P, df) -> int:
    """Drop tick marks whose row has been re-enriched since (P3-53).

    'Re-enrich incomplete rows' rewrites title/prices/image_url/status of
    exactly the suffixed-FOUND bucket a reviewer is most likely to have
    ticked, and dropped_images is matched by exact URL - so a stale tick
    builds re-scraped data nobody looked at, rejected images included.
    Returns how many ticks were dropped."""
    state = load_json(P["review"])
    stale = stale_confirmations(state, df)
    if not stale:
        return 0
    conf = state.get("confirmed", {})
    for sku in stale:
        conf.pop(sku, None)
    state["confirmed"] = conf
    state["updated"] = datetime.now().isoformat(timespec="seconds")
    save_json(P["review"], state)
    return len(stale)


REVIEW_CLOCK = "review_clock_{run}"
CARD_SHOWN = "card_shown_{run}"


def log_table_save(P, touched, before, flags, before_skips=None,
                   skips=None):
    """Log one save from the review TABLE.

    `touched` is write_edits_back_detail's {sku: {col: value}}: a row whose
    url_link changed counts as 'url_pasted' (that is the NEEDS LINK
    remediation, not a cosmetic edit), any other changed column as 'edit'.
    A confirmed flag that flipped adds 'approve'/'unapprove', a Skip flag
    that flipped adds 'skip'/'unskip'. One save can
    only be timed as a whole, so its elapsed seconds are split EQUALLY over
    every event it logs - guessing per-row would be fiction."""
    events = []
    for sku, cols in (touched or {}).items():
        action = "url_pasted" if "url_link" in cols else "edit"
        events.append((str(sku), action))
    for sku, ok in (flags or {}).items():
        sku = str(sku)
        was = sku in (before or set())
        if bool(ok) and not was:
            events.append((sku, "approve"))
        elif was and not bool(ok):
            events.append((sku, "unapprove"))
    if skips is not None:
        was_skipped = {str(s) for s in (before_skips or {})}
        now_skipped = {str(s) for s in skips}
        events += [(s, "skip") for s in sorted(now_skipped - was_skipped)]
        events += [(s, "unskip") for s in sorted(was_skipped - now_skipped)]
    if not events:
        return 0
    elapsed = clock_elapsed(st.session_state,
                            REVIEW_CLOCK.format(run=P["run"].name),
                            time.time())
    each = elapsed / len(events)
    return log_review_events(
        P, [review_event(sku, action, "table", each)
            for sku, action in events])


def review_clock_start(P):
    """Start Step 2's stopwatch the first time it renders in this session.

    Everything after that is measured from the LAST logged event, so the
    idle time before a reviewer opened the page is never billed to a SKU."""
    key = REVIEW_CLOCK.format(run=P["run"].name)
    if key not in st.session_state:
        st.session_state[key] = time.time()
    return key


def refresh_review_metrics(P, df) -> bool:
    """Recompute review_metrics.json from the event log + the sheet AS IT
    NOW STANDS. Returns True when the file changed.

    log_review_events() is the only other writer, so between two review
    saves the file could go stale: 'Re-enrich incomplete rows' rewrites
    statuses, which moves needs_human_pct, and the archive README and the
    enricher report both read the file. Step 2 calls this on load, so
    re-opening the review is enough to make it true again.

    It never CREATES the file for a run nobody has reviewed - a zeroed
    metrics file would make review_caption() print '0 min' where today it
    prints nothing at all."""
    try:
        state = load_json(P["review"])
        if not (state or {}).get("events") and not P["metrics"].exists():
            return False
        fresh = review_metrics(state, df)
        if load_json(P["metrics"]) == fresh:
            return False
        save_json(P["metrics"], fresh)
        return True
    except Exception:
        # instrumentation must never be able to break opening a review
        return False


def log_review_events(P, events):
    """Append review events to review_state.json and refresh review_metrics.

    The clocks restart here, on the write, so the next event measures from
    the moment this one landed (Phase 1 item 5). Metrics are recomputed from
    the WHOLE event log plus the sheet as it now stands, so the file is
    always a complete picture and never an increment that can drift."""
    events = [e for e in (events or []) if e]
    if not events:
        return 0
    state = load_json(P["review"])
    append_events(state, events)
    state["updated"] = datetime.now().isoformat(timespec="seconds")
    save_json(P["review"], state)
    now = time.time()
    st.session_state[REVIEW_CLOCK.format(run=P["run"].name)] = now
    st.session_state[CARD_SHOWN.format(run=P["run"].name)] = now
    try:
        save_json(P["metrics"], review_metrics(state, load_filled_df(P)))
    except Exception:
        # instrumentation must never be able to break a save
        pass
    return len(events)


STALE_TICK_MSG = ("{n} confirmation(s) dropped: those rows were re-enriched "
                  "since they were ticked - review them again (rejected "
                  "images may have returned).")


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
    """Is this PID still the process we launched? Windows: _pid_alive_nt.
    POSIX (the Linux container): _pid_alive_posix."""
    if os.name == "nt":
        return _pid_alive_nt(pid, started)
    return _pid_alive_posix(pid)


def _pid_alive_posix(pid: int) -> bool:
    """POSIX liveness: the pid exists, is not a zombie, and runs
    sku_enricher.py.

    The enricher is a child of THIS Streamlit process. When the session that
    held the Popen handle is gone, an exited child stays a zombie and
    os.kill(pid, 0) still succeeds - the wizard would show "running" forever.
    So reap first (WNOHANG; ChildProcessError = not our child), then read
    /proc. The cmdline check replaces the Windows creation-time check: a
    recycled pid that is not the enricher returns False, so Stop never
    signals an unrelated process."""
    if not pid:
        return False
    pid = int(pid)
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8",
                                                   errors="replace")
    except OSError:
        return False
    state = gui_helpers.posix_stat_state(stat)
    if state is None or state in ("Z", "X"):
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return gui_helpers.posix_cmdline_is_enricher(cmdline)


def _posix_hard_kill(pid: int, proc=None):
    """SIGKILL the enricher's whole process group (it is a session leader:
    start_new_session=True), then reap it so no zombie is left."""
    try:
        os.killpg(int(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    if proc is not None:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        return
    # not blocking: if the kill failed, a waitpid(pid, 0) would hang the GUI
    for _ in range(50):
        try:
            done, _status = os.waitpid(int(pid), os.WNOHANG)
        except (ChildProcessError, OSError):
            return              # not our child, or already reaped
        if done:
            return
        time.sleep(0.1)


def _pid_alive_nt(pid: int, started: float = None) -> bool:
    """Is this PID still the process we launched? (Windows)

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
    # --limit is knowable from argv alone, so cap the fallback BEFORE the
    # no-sheet bail-out: on a FIRST launch (no <stem>_filled.xlsx yet) the
    # documented `--limit 5` smoke run on a 300-row sheet otherwise stored
    # enricher_total=300 -> '3 / 300' and a ~50x ETA, the P2-28 symptom on
    # the most common launch path of all.
    limit = limit_from_flags(extra_flags)
    if limit:
        fallback = min(limit, fallback) if fallback else limit
    if not P["filled"].exists():
        return fallback
    try:
        df = pd.read_excel(P["filled"], dtype=str)
        n = eligible_rows(df, extra_flags)
    except Exception:
        return fallback
    # `n or fallback`: a genuine 0-eligible run (everything already done)
    # collapses to the whole sheet rather than showing 0/0. Known owner nit,
    # recorded in docs/NEXT_STEPS.md - deliberately left as-is.
    return n or fallback


def start_enricher(P, extra_flags: list, total: int):
    """Launch sku_enricher.py detached, stdout -> enricher.log (a file, never a
    pipe: a full pipe buffer would deadlock the child)."""
    # Two different numbers, two different keys. `total_rows` means "rows in
    # the sheet" everywhere else (Home dashboard, archive README, the fallback
    # this file hands the NEXT retry) and must keep meaning that;
    # `enricher_total` is how many rows THIS launch will actually touch and is
    # the only value the progress bar and the ETA may divide by.
    if share_mode():
        # every launch path (Step 1, Step 2 retries, pass 2) comes through
        # here: no headless browser, no --force, and a capped --budget
        extra_flags = gui_helpers.share_cap_flags(
            extra_flags, gui_helpers.share_budget())
    eligible = eligible_total(P, extra_flags, total)
    # --client on the command line AND PIPELINE_CLIENT in the env: the flag is
    # what shows up in enricher_cmd and the report header, the env var is what
    # anything the child imports lazily reads. They can never disagree - both
    # come from the run's own slug, not from this process's ambient env.
    slug = run_client_slug(P["run"])
    cmd = ([str(VENV_PY), "sku_enricher.py", str(P["input"]),
            "--client", slug] + extra_flags)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env[client_config.ENV_VAR] = slug
    # Windows: its own process group, so CTRL_BREAK reaches only the child.
    # POSIX: its own session (and process group), so SIGINT / killpg reach
    # only the child and its descendants.
    if os.name == "nt":
        group_kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        group_kw = {"start_new_session": True}
    with open(P["log"], "w", encoding="utf-8") as log_f:
        proc = subprocess.Popen(
            cmd, stdout=log_f, stderr=subprocess.STDOUT,
            cwd=str(REPO_ROOT), env=env, **group_kw)
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
    hard kill (safe either way: saves are atomic). POSIX (Linux container):
    SIGINT, the same 15 s grace, then SIGKILL to the child's process group."""
    proc = st.session_state.get(f"proc_{P['run'].name}")
    meta = load_meta(P)
    # P3-51: every liveness check here carries the launch timestamp, so
    # pid_alive compares process CREATION TIME and not just "a live pid whose
    # image name contains python". Without it a stale enricher_pid (GUI
    # reloaded, second tab, restarted mid-batch) could match a recycled pid
    # and Stop would CTRL_BREAK + taskkill /T /F an unrelated python - another
    # Streamlit, a build_csv.py run. When the identity check fails we signal
    # NOTHING and just clear the stale pid. With a live `proc` handle the
    # timestamp came from that same launch, so it stays truthful.
    started = meta.get("enricher_started")
    pid = proc.pid if proc is not None else meta.get("enricher_pid")
    if not pid:
        return
    if not pid_alive(pid, started):
        update_meta(P, enricher_pid=None)
        st.session_state[f"running_{P['run'].name}"] = False
        return
    if os.name == "nt":
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        except OSError:
            pass
    else:
        # POSIX: SIGINT -> KeyboardInterrupt in the child, the same
        # 'Stopped by user - progress saved' checkpoint path as CTRL_BREAK
        try:
            os.kill(pid, signal.SIGINT)
        except OSError:
            pass
    deadline = 15
    if proc is not None:
        try:
            proc.wait(timeout=deadline)
        except subprocess.TimeoutExpired:
            pass
    else:
        while deadline > 0 and pid_alive(pid, started):
            time.sleep(1)
            deadline -= 1
    if pid_alive(pid, started):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
        else:
            _posix_hard_kill(pid, proc)
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


def serper_caption(cfg, run_calls: int) -> str:
    """`Serper: N this run · M/BUDGET this month`, from the credit ledger.

    clients/<slug>/credits.json is written by sku_enricher.Ledger (item 4 of
    docs/CLIENT_CONFIG.md). It is absent until the first real run, which is
    the normal state of a fresh client - say so rather than showing 0/2500 as
    if the month had been measured. A corrupt/half-written file reads the same
    way: the panel is never the place a JSONDecodeError surfaces."""
    text = f"Serper: {int(run_calls or 0)} this run"
    try:
        data = json.loads(Path(cfg.credits_file).read_text(encoding="utf-8"))
        month_to_date = int(data.get("serper_calls") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return text + " · no ledger yet"
    return (text + f" · {month_to_date}/{cfg.serper.monthly_budget} "
            "this month")


def enricher_panel(P):
    """Live progress while the enricher subprocess runs. A fragment so only
    this panel re-renders every 2 s, not the whole page."""
    run_key = f"running_{P['run'].name}"
    cfg = current_client()

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
        bits.append(serper_caption(cfg, stats["serper_calls"]))
        replays = stats["cached_searches"] + stats["cached_pages"]
        if replays:
            bits.append(f"{replays} cache replay(s)")
        eta = eta_text(done, total, meta.get("enricher_started"),
                       time.time())
        if running and eta:
            bits.append(eta)
        if bits:
            st.caption("  ·  ".join(bits))
        if stats.get("stopped_by_budget"):
            # exit code 0 and a saved sheet, so nothing else says UNFINISHED
            st.warning("Stopped at the credit budget — progress saved; raise "
                       "the budget and re-run to continue.")
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
    cfg = current_client()
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
        budget = st.number_input(
            "Serper credit budget for this run (0 = unlimited)",
            0, 100000, int(cfg.serper.default_run_budget),
            help="A hard stop, not a warning: the enricher saves and exits "
                 "cleanly the moment it has spent this many search credits. "
                 "Pre-filled from this client's [serper] default_run_budget.")
        if share_mode():
            st.caption(f"Share mode: every run is capped at "
                       f"{gui_helpers.share_budget()} Serper credits; the "
                       "headless browser and --force are off.")
        with st.expander("Advanced"):
            js = force = False
            if not share_mode():
                js = st.checkbox("--js-fallback (headless browser for "
                                 "blocked pages; slow)")
            min_images = st.number_input("Min images per product", 1, 10, 3)
            if not share_mode():
                force = st.checkbox("--force — re-process EVERY row (re-pays "
                                    "Serper credits when the cache is cold; "
                                    "only for applying parser fixes on a "
                                    "warm cache)")
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
        if budget:
            # stored in enricher_flags like every other flag, so the Step-2
            # retry buttons (which reuse base_flags) inherit the same cap
            flags += ["--budget", str(int(budget))]
        start_enricher(P, flags, total)
        st.rerun()

    key_env = cfg.serper.api_key_env
    if not (os.environ.get(key_env) or client_env(cfg).get(key_env)):
        st.warning(f"{key_env} is set neither in the environment nor in "
                   f"clients\\{cfg.slug}\\.env — search falls back to "
                   "DuckDuckGo (worse results).")


# ------------------------------------------------------------------- step 2 --

EDITABLE_COLS = ["title", "regular price", "sale price", "url_link",
                 "image_url", "categories", "Brands", "dropped_images"]


def load_filled_df(P) -> pd.DataFrame:
    """The review sheet as a frame. Every Streamlit rerun (each click) asks
    for it, so the parse is cached on the file's path + mtime + size: a save,
    a crawl or a re-enrich rewrites the file and so misses the cache. The
    cache hands every caller its own copy."""
    stat = Path(P["filled"]).stat()
    return _read_filled_df(str(P["filled"]), stat.st_mtime_ns, stat.st_size)


@st.cache_data(max_entries=4, show_spinner=False)
def _read_filled_df(path: str, mtime_ns: int, size: int) -> pd.DataFrame:
    df = pd.read_excel(path, dtype=str).fillna("")
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
def category_options(slug: str) -> list:
    """Exact category strings from this client's category_map.xlsx, via the
    same loader app.py snaps against - so anything picked here is a legal
    snap target.

    Keyed by slug because the cache outlives a client switch, while `app` is
    a singleton: the entry for slug X must never be served from an `app` that
    is currently configured for Y, hence the configure() below."""
    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    os.environ[client_config.ENV_VAR] = slug
    import app
    if getattr(getattr(app, "CLIENT", None), "slug", slug) != slug:
        app.configure(slug)
    return sorted(set(app.CATEGORY_COMBOS.values()))


def review_editor(df: pd.DataFrame, key: str,
                  cat_options: list | None = None,
                  allow_confirm: bool = True) -> pd.DataFrame:
    """One review tab. allow_confirm=False locks the ✔ column: the rows in
    that tab are not FOUND, so a tick there would be refused on save
    anyway (see gui_helpers.found_status)."""
    view = df.copy()
    view.insert(0, "confirmed", view.pop("confirmed"))
    # Skip sits next to ✔: the reviewer's explicit 'no'. Allowed on every
    # tab - a skip needs no FOUND status
    view.insert(1, "skipped", view.pop("skipped") if "skipped" in view.columns
                else False)
    view.insert(2, "state", view["status"].map(
        lambda s: f"{BADGE[classify(s)]} {s}"))
    view.insert(3, "thumb", view["image_url"].map(
        lambda v: str(v).split(",")[0].strip() if str(v).strip() else None))
    # url_link sits next to sku: pasting a link on a NEEDS LINK row is the
    # main edit on the failed tab, and far right it was off-screen
    show = ["confirmed", "skipped", "state", "sku", "url_link", "title",
            "regular price", "sale price", "thumb", "image_url", "categories",
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
            "skipped": st.column_config.CheckboxColumn(
                "Skip", default=False, width="small",
                help="Skip this row: it is never built, in any scope. "
                     "Ticking ✔ later clears the skip."),
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
    by SKU, preserving everything else the enricher wrote. Returns the number
    of cells written; write_edits_back_detail also says which SKUs."""
    return write_edits_back_detail(P, original, edited_parts)[0]


def write_edits_back_detail(P, original: pd.DataFrame,
                            edited_parts: list) -> tuple:
    """write_edits_back, plus the map of what actually changed.

    Returns (cells_written, {sku: {column: new value}}) covering only the
    rows the sheet really holds - the review instrumentation needs to know
    WHICH SKUs a save touched (and whether url_link was one of the columns)
    to log an event per row, and a count cannot tell it that."""
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
        return 0, {}

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
    written = {}
    for r in range(2, ws.max_row + 1):
        sku = str(ws.cell(row=r, column=sku_c).value or "").strip()
        if sku in edits:
            for col, val in edits[sku].items():
                if col in headers:
                    ws.cell(row=r, column=headers[col], value=val)
                    written.setdefault(sku, {})[col] = val
                    changed += 1
    wb.save(P["filled"])
    return changed, written


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


def _configured_app():
    """app.py, configured for the current client (same rule as
    category_options: `app` is a singleton that may hold another client)."""
    slug = current_client().slug
    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    os.environ[client_config.ENV_VAR] = slug
    import app
    if getattr(getattr(app, "CLIENT", None), "slug", slug) != slug:
        app.configure(slug)
    return app


_FIELD_LABEL = {"short description": "Short description",
                "description": "Description",
                gui_helpers.KEY_FEATURES_FIELD: "Key features paragraph"}


def description_editor(P, row, where="card"):
    """Show and edit the three description fields of ONE row (short
    description, description, key-features paragraph = CSV column
    'configuration'), as the build will write them. Computed for this row only (app.description_fields_for_row
    - no image download), so it stays cheap on every rerun.

    The edit is saved as a per-SKU override in runs\\<name>\\
    description_overrides.json (gui_helpers); _filled.xlsx is not touched.
    Each line passes the content firewall at save time; a refused line is
    named and not saved. Empty text saves only with 'publish empty' ticked."""
    run = P["run"].name
    sku = str(row["sku"])
    app = _configured_app()
    brand = app.compact(row.get("Brands")) or app.infer_brand(
        app.compact(row.get("title")), row.get("sku"))
    try:
        generated = app.description_fields_for_row(row)
    except Exception as exc:          # a row the builder cannot read yet
        st.info(f"Descriptions cannot be generated for this row yet "
                f"({type(exc).__name__}: {exc}).")
        return
    ov_file = gui_helpers.overrides_path(P["run"])
    state = gui_helpers.load_overrides_state(ov_file)
    gen_key = f"desc_gen_{run}_{sku}"
    gen = st.session_state.get(gen_key, 0)

    msg_key = f"desc_msg_{run}_{sku}"
    for kind, text in st.session_state.pop(msg_key, []):
        getattr(st, kind)(text)

    st.caption("One line per table row, as **Label: value**. Delete a line "
               "to remove that point. This is the text the CSV will contain "
               "(content firewall and the key-point limit already applied). "
               "The key-features paragraph writes each line as one "
               "'Label: value.' sentence. Edits are saved for this run only; the scraped sheet is not "
               "changed. Title or price edits above show here after Save.")
    texts, empties = {}, {}
    for field in gui_helpers.OVERRIDE_FIELDS:
        gen_lines = app.description_lines(generated[field])
        entry = gui_helpers.get_override(state, sku, field)
        label = _FIELD_LABEL[field]
        if entry is not None:
            label += " — EDITED (overrides the generated text)"
            if gui_helpers.override_is_stale(entry, gen_lines):
                st.warning(f"{_FIELD_LABEL[field]}: the generated text has "
                           "changed since this edit was saved (the row was "
                           "re-enriched or edited). The edit is still used; "
                           "reset it to see the new text.")
        shown = entry["lines"] if entry is not None else gen_lines
        texts[field] = st.text_area(
            label, "\n".join(shown), height=34 * max(3, len(shown) + 1),
            key=f"desc_{field[:5]}_{where}_{run}_{sku}_{gen}")
        empties[field] = st.checkbox(
            f"Publish {_FIELD_LABEL[field].lower()} EMPTY",
            value=bool(entry is not None and not entry["lines"]),
            key=f"desc_empty_{field[:5]}_{where}_{run}_{sku}_{gen}",
            help="Tick only if this field must be published with no text.")
        with st.expander(f"Preview: {_FIELD_LABEL[field].lower()} as "
                         "published"):
            rows = (app.parse_description_lines(entry["lines"], brand)[0]
                    if entry is not None else generated[field])
            html = app.render_override_field(field, rows)
            if html:
                st.markdown(html, unsafe_allow_html=True)
            else:
                st.caption("(empty)")
        if entry is not None and st.button(
                f"↺ Reset {_FIELD_LABEL[field].lower()} to generated",
                key=f"desc_rst_{field[:5]}_{where}_{run}_{sku}"):
            gui_helpers.reset_override(state, sku, field)
            gui_helpers.save_overrides_state(ov_file, state)
            st.session_state[msg_key] = [("success", f"{_FIELD_LABEL[field]}"
                                          " reset to the generated text.")]
            st.session_state[gen_key] = gen + 1
            st.rerun()

    b1, b2 = st.columns(2)
    if b1.button("💾 Save description edits", key=f"desc_save_{where}_{run}_{sku}"):
        notes = []
        for field in gui_helpers.OVERRIDE_FIELDS:
            gen_lines = app.description_lines(generated[field])
            rows, refused = app.parse_description_lines(texts[field], brand)
            name = _FIELD_LABEL[field]
            for line, why in refused:
                notes.append(("error", f"{name}: NOT saved — “{line}” — {why}."))
            if not rows and not empties[field]:
                if texts[field].strip():
                    notes.append(("error", f"{name}: no line can be "
                                  "published, so nothing was saved. Fix the "
                                  "lines, or tick 'publish empty'."))
                elif gui_helpers.get_override(state, sku, field) is None:
                    notes.append(("warning", f"{name}: the text is empty. "
                                  "Tick 'publish empty' to save an empty "
                                  "field; nothing was saved."))
                continue
            lines = [] if empties[field] else app.description_lines(rows)
            done = gui_helpers.set_override(state, sku, field, lines,
                                            gen_lines)
            notes.append(("success", f"{name}: " + (
                "same as the generated text — no override kept."
                if done == "removed" else
                "saved as EMPTY." if not lines else
                f"saved ({len(lines)} line(s)).")))
        try:
            gui_helpers.save_overrides_state(ov_file, state)
        except OSError as exc:
            notes.append(("error", f"Could not write {ov_file.name}: {exc}"))
        log_review_events(P, [review_event(sku, "edit", where, 0.0)])
        st.session_state[msg_key] = notes
        st.session_state[gen_key] = gen + 1
        st.rerun()
    if b2.button("↺ Reset all three to generated",
                 key=f"desc_reset_{where}_{run}_{sku}",
                 disabled=not any(gui_helpers.get_override(state, sku, f)
                                  for f in gui_helpers.OVERRIDE_FIELDS)):
        gui_helpers.reset_override(state, sku)
        gui_helpers.save_overrides_state(ov_file, state)
        st.session_state[msg_key] = [("success", "Descriptions reset to the "
                                      "generated text.")]
        st.session_state[gen_key] = gen + 1
        st.rerun()


_SOURCE_MARK = {"specification": "specification", "description": "description",
                "title": "title", "brand": "brand", "reviewer": "reviewer ✎"}


def price_attribute_editor(P, row, where="card"):
    """Price and attributes of ONE row as the build will publish them, with
    the source of each attribute, and an editor (owner request 2026-11-19,
    late). Computed for this row only (app.review_summary - no image
    download). Edits are per-SKU overrides in runs\\<name>\\
    description_overrides.json (keys 'price' / 'attributes', gui_helpers);
    _filled.xlsx is never written. Refused entries are shown in red and not
    saved."""
    run = P["run"].name
    sku = str(row["sku"])
    app = _configured_app()
    ov_file = gui_helpers.overrides_path(P["run"])
    state = gui_helpers.load_overrides_state(ov_file)
    build_ov = gui_helpers.overrides_for_build(state).get(sku)
    try:
        info = app.review_summary(row, build_ov)
    except Exception as exc:          # a row the builder cannot read yet
        st.info(f"Price and attributes cannot be computed for this row yet "
                f"({type(exc).__name__}: {exc}).")
        return
    brand = app.compact(row.get("Brands")) or app.infer_brand(
        app.compact(row.get("title")), row.get("sku"))
    gen_key = f"pa_gen_{run}_{sku}"
    gen = st.session_state.get(gen_key, 0)
    msg_key = f"pa_msg_{run}_{sku}"
    for kind, text in st.session_state.pop(msg_key, []):
        getattr(st, kind)(text)

    # ---- price -------------------------------------------------------------
    price_entry = gui_helpers.get_price_override(state, sku)
    kind = info["kind"] or "kind not recorded"
    published = app._money(info["published"]) or "—"
    hand = " — ✎ HAND-ENTERED by the reviewer" if info["hand_price"] else ""
    st.markdown(
        f"**Price** · MRP on the page: **{app._money(info['mrp']) or '—'}** · "
        f"selling price on the page: **{app._money(info['sale']) or '—'}** "
        f"({kind}) · published: **{published}**{hand}  \n"
        f"Rule: {info['rule']}")
    c1, c2, c3 = st.columns(3)
    sheet = {"regular price": row.get("regular price"),
             "sale price": row.get("sale price")}
    shown = {k: gui_helpers._price_text(
        (price_entry or {}).get("values", {}).get(k, sheet.get(k)))
        for k in ("regular price", "sale price", "publish price")}
    mrp_in = c1.text_input("Page MRP", shown["regular price"],
                           key=f"pa_mrp_{where}_{run}_{sku}_{gen}")
    sale_in = c2.text_input("Page selling price", shown["sale price"],
                            key=f"pa_sale_{where}_{run}_{sku}_{gen}")
    pub_in = c3.text_input("Publish this exact price (empty = rule)",
                           shown["publish price"],
                           key=f"pa_pub_{where}_{run}_{sku}_{gen}")

    # ---- attributes --------------------------------------------------------
    rows = info["attributes"]
    table = pd.DataFrame(
        [{"attribute": n, "value": v,
          "source": _SOURCE_MARK.get(s, s),
          "filter": "✔" if f else ""} for n, v, s, f in rows],
        columns=["attribute", "value", "source", "filter"])
    st.markdown("**Attributes as published** (✔ = store filter). Change a "
                "value, delete a row, or add a row; then Save.")
    edited = st.data_editor(
        table, num_rows="dynamic", hide_index=True, width="stretch",
        disabled=["source", "filter"],
        key=f"pa_attr_{where}_{run}_{sku}_{gen}")
    if info["missing"]:
        st.warning("⚠ Required attribute(s) still missing: "
                   + ", ".join(info["missing"]))
    for note in info["unmatched"]:
        st.caption(f"Filter note: {note}")
    for why in info["refused"]:
        st.error(f"Stored edit not used: {why}")

    attr_entry = gui_helpers.get_attribute_override(state, sku)
    b1, b2, b3 = st.columns(3)
    if b1.button("💾 Save price & attributes",
                 key=f"pa_save_{where}_{run}_{sku}"):
        notes = []
        # price: every entry is checked; a refused one is NOT saved
        mrp_after = mrp_in
        bad = False
        for label, text in (("Page MRP", mrp_in),
                            ("Page selling price", sale_in)):
            why = app.check_price_input(text)
            if why:
                notes.append(("error", f"{label} NOT saved — “{text}” — {why}."))
                bad = True
        if not app.check_price_input(mrp_in):
            _n, why = app.check_publish_price(
                pub_in, app.number_or_blank(mrp_after))
            if why:
                notes.append(("error", f"Publish price NOT saved — “{pub_in}”"
                              f" — {why}."))
                bad = True
        if not bad:
            done = gui_helpers.set_price_override(
                state, sku, {"regular price": mrp_in, "sale price": sale_in,
                             "publish price": pub_in}, sheet)
            notes.append(("success", "Price: " + (
                "same as the page — no override kept." if done == "removed"
                else "saved.")))
        # attributes
        current = [(n, v) for n, v, _s, _f in rows]
        new_rows = [(r.get("attribute"), r.get("value"))
                    for r in edited.to_dict("records")]
        sets, removes = gui_helpers.attribute_edits(current, new_rows)
        accepted = {}
        for name, value in sets.items():
            why = app.check_attribute_edit(name, value, brand)
            if why:
                notes.append(("error", f"Attribute NOT saved — “{name}: "
                              f"{value}” — {why}."))
            else:
                accepted[name] = value
        if accepted or removes:
            m_sets, m_removes = gui_helpers.merge_attribute_edits(
                attr_entry, accepted, removes)
            gui_helpers.set_attribute_override(state, sku, m_sets, m_removes)
            notes.append(("success", f"Attributes: {len(accepted)} set, "
                          f"{len(removes)} removed."))
        try:
            gui_helpers.save_overrides_state(ov_file, state)
        except OSError as exc:
            notes.append(("error", f"Could not write {ov_file.name}: {exc}"))
        log_review_events(P, [review_event(sku, "edit", where, 0.0)])
        st.session_state[msg_key] = notes
        st.session_state[gen_key] = gen + 1
        st.rerun()
    if b2.button("↺ Reset price to the page", key=f"pa_rprice_{where}_{run}_{sku}",
                 disabled=price_entry is None):
        gui_helpers.reset_override(state, sku, gui_helpers.PRICE_FIELD)
        gui_helpers.save_overrides_state(ov_file, state)
        st.session_state[msg_key] = [("success", "Price reset to the page.")]
        st.session_state[gen_key] = gen + 1
        st.rerun()
    if b3.button("↺ Reset attributes to generated",
                 key=f"pa_rattr_{where}_{run}_{sku}",
                 disabled=attr_entry is None):
        gui_helpers.reset_override(state, sku, gui_helpers.ATTRIBUTE_FIELD)
        gui_helpers.save_overrides_state(ov_file, state)
        st.session_state[msg_key] = [("success",
                                      "Attributes reset to generated.")]
        st.session_state[gen_key] = gen + 1
        st.rerun()


# --------- card review: preview of the final 1500 x 1500 images --------------
# Owner request 2026-11-20. app.preview_images_for_row runs the build's own
# image code (fetch, 1000 px floor, white canvas, _1 = whitest photo) and
# writes nothing. Each photo is cached per URL as a small WebP, so a rerun
# does not download again. PIPELINE_REVIEW_IMAGE_PREVIEW=off sets the toggle's
# default to off (tests\conftest.py does this, so no test downloads).
REVIEW_PREVIEW_ENV = "PIPELINE_REVIEW_IMAGE_PREVIEW"


def review_preview_default() -> bool:
    return os.environ.get(REVIEW_PREVIEW_ENV, "on").strip().lower() not in (
        "0", "off", "false", "no")


@st.cache_data(max_entries=256, ttl=3600, show_spinner=False)
def _final_image_preview(url: str) -> dict:
    """One photo through app.square_image, stored compact: a ~600 px WebP
    of the canvas plus the white flag and share the build orders by. A
    failure is stored too, so a rerun does not wait on it again."""
    import app
    try:
        white, share, data = app.square_image(url)
    except app.ImageBelowFloor as exc:
        return {"ok": False, "floor": True, "size": list(exc.size)}
    except Exception as exc:              # network, decode, anything
        return {"ok": False, "floor": False,
                "reason": f"{type(exc).__name__}: {exc}"[:200]}
    return {"ok": True, "white": bool(white), "share": float(share),
            "thumb": app.preview_thumbnail(data)}


def _cached_square(url):
    """fetch_one for app.preview_images_for_row, from the cache above."""
    import app
    got = _final_image_preview(url)
    if got["ok"]:
        return got["white"], got["share"], got["thumb"]
    if got["floor"]:
        raise app.ImageBelowFloor(got["size"])
    raise app.ImageFetchFailed(got["reason"])


def final_image_gallery(urls, keeps, slots, show_final):
    """Fill each photo's slot: the final canvas with its number when the
    build keeps it, else the source photo with the reason. A failure here
    never breaks the card - it falls back to the source photos."""
    def source(j, u, caption=None):
        with slots[j]:
            st.image(u, width="stretch", caption=caption)

    if not show_final:
        for j, u in enumerate(urls):
            source(j, u)
        return
    kept = [u for u in urls if keeps.get(u)]
    try:
        app = _configured_app()
        with st.spinner("Preparing the final images…"):
            preview = app.preview_images_for_row(kept,
                                                 fetch_one=_cached_square)
    except Exception as exc:
        st.warning(f"The final-image preview failed ({type(exc).__name__}: "
                   f"{exc}); the source photos show instead.")
        for j, u in enumerate(urls):
            source(j, u)
        return
    final = {img["url"]: img for img in preview["images"]}
    refused = {r["url"]: r for r in preview["rejected"]}
    st.caption(f"Preview of the {app.CANVAS_EDGE} x {app.CANVAS_EDGE} "
               "output (shown smaller). The number is the file the build "
               "writes; _1 is the main image. Untick 'keep' to drop a photo.")
    if preview["main_not_white"]:
        st.warning("No kept photo has a white background: _1 is the first "
                   "photo, and the validator will flag it.")
    for j, u in enumerate(urls):
        if u in final:
            img = final[u]
            label = f"_{img['position']} (photo {j + 1})"
            if img["position"] == 1:
                label += " - main image"
            with slots[j]:
                st.image(img["data"], width="stretch", caption=label)
        elif u in refused:
            source(j, u, f"Photo {j + 1}: NOT built - "
                         f"{refused[u]['reason']}")
        else:
            source(j, u, f"Photo {j + 1}: dropped - not built")
    if st.button("↻ Download the preview images again",
                 key="final_prev_refresh"):
        _final_image_preview.clear()
        st.rerun()


def card_review(P, df, cat_opts, all_raws=None):
    """One SKU per screen: gallery with keep/drop, editable fields, an
    approve/skip queue. df is the (possibly filtered) review dataframe -
    prices numeric, confirmed bool, _bucket set."""
    run = P["run"].name
    if df.empty:
        st.info("No rows in the current filter.")
        return
    order = {"review": 0, "failed": 1, "pending": 2, "found": 3, "skipped": 4}
    df = df.copy()
    if "skipped" not in df.columns:
        df["skipped"] = False
    # a DECIDED card (approved or skipped) sorts after every undecided one,
    # so the queue always offers the next row nobody has judged yet
    df["_decided"] = df["confirmed"].astype(bool) | df["skipped"].astype(bool)
    queue = df.sort_values(
        by=["_decided", "_bucket"],
        key=lambda s: s.map(order) if s.name == "_bucket" else s,
    ).reset_index(drop=True)

    ikey = f"card_i_{run}"
    # never rest on a decided card while undecided ones remain: approving
    # the LAST undecided card leaves it at position i, and i + 1 clamps
    # right back onto it, so Approve/Skip would dead-loop on an already
    # approved card (P1-9 tail) - card_queue_index snaps forward instead
    i = card_queue_index(queue["_decided"], st.session_state.get(ikey, 0))
    st.session_state[ikey] = i
    row = queue.iloc[i]
    sku = str(row["sku"])

    # the per-card stopwatch: restamped whenever a different card comes up
    # (and by log_review_events after every save), so `secs` is the time
    # this card was actually on screen, never the whole session
    shown_key = CARD_SHOWN.format(run=run)
    if st.session_state.get(f"card_shown_i_{run}") != i             or shown_key not in st.session_state:
        st.session_state[shown_key] = time.time()
        st.session_state[f"card_shown_i_{run}"] = i

    def card_secs():
        return clock_elapsed(st.session_state, shown_key, time.time())

    def goto(j):
        st.session_state[ikey] = max(0, min(j, len(queue) - 1))
        st.rerun()

    st.progress((i + 1) / len(queue),
                text=f"Card {i + 1} / {len(queue)} — "
                     f"{int(queue['confirmed'].sum())} approved, "
                     f"{int(queue['skipped'].sum())} skipped")
    if bool(queue["_decided"].all()):
        st.success(f"All {len(queue)} cards decided (approved or skipped).")
    st.subheader(f"{BADGE[row['_bucket']]} {sku}")
    st.caption(str(row["status"]))
    is_skipped = bool(row["skipped"])
    skip_reasons = skipped_map(P)
    if is_skipped:
        why = skip_reasons.get(sku, "")
        st.warning("Skipped — this row is never built"
                   + (f": {why}" if why else ".")
                   + " Approve it to take it back into the build.")
    elif bool(row["confirmed"]):
        st.success("Approved — this row is in the build.")
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
        show_final = st.toggle(
            "Show the final 1500 x 1500 images (downloads the photos)",
            value=review_preview_default(), key=f"final_prev_{run}",
            help="On: each kept photo shows as the build will save it, with "
                 "its final number (_1 is the main image). Off: the source "
                 "photos only, with no download.")
        cols = st.columns(min(4, len(urls)))
        slots = []
        for j, u in enumerate(urls):
            with cols[j % len(cols)]:
                # the picture goes in ABOVE the checkbox, but is drawn after
                # every checkbox is read: the final order depends on them
                slots.append(st.container())
                keeps[u] = st.checkbox("keep", value=u not in dropped_now,
                                       key=f"keep_{run}_{sku}_{j}")
        final_image_gallery(urls, keeps, slots, show_final)
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

    with st.expander("💰 Price & attributes (as published — editable)",
                     expanded=True):
        price_attribute_editor(P, row, "card")
    with st.expander("📝 Description & short description (as published — "
                     "editable)", expanded=True):
        description_editor(P, row, "card")

    def _num(s):
        return pd.to_numeric(str(s).replace(",", "").replace("₹", "")
                             .strip(), errors="coerce")

    def save(confirm, action="edit"):
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
            _, touched = write_edits_back_detail(P, df, [part])
        except PermissionError:
            st.error("The filled .xlsx is open in Excel — close it and "
                     "retry.")
            return False
        # one primary event carries the card's time; the extras describe
        # what else the reviewer did to this row and carry 0 s, so a single
        # card can never bill its minutes twice
        cols = set(touched.get(sku, {}))
        events = [review_event(sku, action, "card", card_secs())]
        if "dropped_images" in cols:
            events.append(review_event(sku, "drop_image", "card", 0.0))
        if action != "edit" and (cols - {"dropped_images"}):
            events.append(review_event(sku, "edit", "card", 0.0))
        log_review_events(P, events)
        if confirm is not None:
            # approving clears a skip (three-state rule, gui_helpers)
            conf, skips = review_decision(
                confirmed_set(P), skipped_map(P), sku,
                REVIEW_APPROVE if confirm else REVIEW_CLEAR)
            flags = {s: True for s in conf}
            flags.setdefault(sku, False)
            # `part` holds this row exactly as it was just written back, so
            # the fingerprint describes what the reviewer approved (P3-53)
            save_confirmed(P, flags, part, skips)
        return True

    def skip_row(reason):
        """Store the SKIP decision: skipped with its reason, approval
        cleared. The skip event carries the card's time, as before."""
        conf, skips = review_decision(confirmed_set(P), skipped_map(P), sku,
                                      REVIEW_SKIP, reason)
        save_confirmed(P, {s: True for s in conf}, None, skips)
        log_review_events(P, [review_event(sku, "skip", "card", card_secs())])

    def unskip_row():
        conf, skips = review_decision(confirmed_set(P), skipped_map(P), sku,
                                      REVIEW_CLEAR)
        save_confirmed(P, {s: True for s in conf}, None, skips)
        log_review_events(P, [review_event(sku, "unskip", "card", 0.0)])

    reason = st.text_input(
        "Reason to skip (optional)", skip_reasons.get(sku, ""),
        key=f"skr_{run}_{sku}",
        placeholder="e.g. wrong model, price looks wrong, images unusable")

    b1, b2, b3, b4, b5, b6 = st.columns(6)
    # only a FOUND row is importable - approving anything else could pull an
    # unvetted row into a confirmed-scope build (the save would refuse it
    # anyway, see gui_helpers.found_status)
    can_approve = found_status(row["status"])
    if b1.button("✅ Approve & next", type="primary", disabled=not can_approve,
                 help=None if can_approve else "Approve needs a FOUND row — "
                 "paste or fix the URL and press Crawl all info first.",
                 key=f"ap_{run}_{sku}"):
        if save(True, "approve"):
            # the queue sorts unconfirmed rows first, so an approved row
            # leaves position i and the next unconfirmed card slides into
            # it - advancing here would skip that card (P1-9). A row that
            # was already confirmed does not move, so advance. The one
            # position where an approved row does NOT leave i is the last
            # unconfirmed one; card_queue_index catches that on the next
            # run and snaps to the first card still unconfirmed.
            goto(i if not bool(row["_decided"]) else i + 1)
    # SKIP stores the decision: the row is never built, in any scope. Like
    # Approve, an undecided card leaves position i (decided cards sort last)
    if b2.button("⛔ Skip", key=f"sk_{run}_{sku}",
                 help="Leave this row out of the build. Add a reason above "
                      "if you like; Approve later clears the skip."):
        skip_row(reason)
        goto(i if not bool(row["_decided"]) else i + 1)
    if b3.button("💾 Save & next", key=f"sv_{run}_{sku}"):
        if save(None):
            goto(i + 1)
    if is_skipped and b4.button("↩ Undo skip", key=f"us_{run}_{sku}"):
        unskip_row()
        goto(i)
    if b5.button("Next →", disabled=i >= len(queue) - 1,
                 key=f"nx_{run}_{sku}"):
        goto(i + 1)
    if b6.button("← Back", disabled=i == 0, key=f"bk_{run}_{sku}"):
        goto(i - 1)


CRAWL_BUCKETS = ("pending", "review", "failed")


def crawl_section(P, df):
    """Pass 2's '🔎 Crawl all info' block, shared by the table AND the card
    view. It shows when any not-yet-FOUND row (pending, review or failed)
    holds a url_link: a NEEDS LINK row stays 'failed' after a human pastes
    its link (saving changes no status), so gating on the pending/review
    counts alone hid the button from a run whose rows were all NEEDS LINK.
    --crawl-urls crawls every row with a link that is not FOUND."""
    todo = df[df["_bucket"].isin(CRAWL_BUCKETS)]
    has_link = todo["url_link"].astype(str).str.strip() != ""
    if not bool(has_link.any()):
        return
    no_link = int((~has_link).sum())
    meta = load_meta(P)
    # --discover MUST be stripped: passing it alongside --crawl-urls is the one
    # combination the enricher refuses outright
    base_flags = [f for f in meta.get("enricher_flags", [])
                  if f not in ("--force", "--retry-incomplete",
                               "--retry-missing", "--discover", "--crawl-urls")]
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
        start_enricher(P, base_flags + ["--crawl-urls"],
                       meta.get("total_rows", 0))
        st.rerun()
    st.divider()


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
    stale = reconcile_confirmations(P, df)
    # the sheet may have been re-enriched since the last review save, which
    # moves needs_human_pct - recompute before the caption reads the file
    refresh_review_metrics(P, df)
    conf = confirmed_set(P)
    skips = skipped_map(P)
    df["confirmed"] = df["sku"].astype(str).isin(conf)
    df["skipped"] = df["sku"].astype(str).isin(set(skips))
    df["_bucket"] = df["status"].map(classify)

    n = df["_bucket"].value_counts()
    pending = int(n.get("pending", 0))
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("🔵 Link found, not crawled", pending)
    m2.metric("🟢 Found (clean)", int(n.get("found", 0)))
    m3.metric("🟡 Needs review", int(n.get("review", 0)))
    m4.metric("🔴 No link / failed", int(n.get("failed", 0)))
    m5.metric("✔ Confirmed", int(df["confirmed"].sum()))
    st.caption("Review: " + review_counts_text(review_counts(df, conf, skips))
               + " — only approved rows go to the build.")

    # Step 2's stopwatch starts on the first render of this session; the
    # caption below is what the last save measured (Phase 1 item 5)
    review_clock_start(P)
    cap = review_caption(load_json(P["metrics"]))
    if cap:
        st.caption(cap)

    # the Save handler reruns immediately, so its outcome is reported here
    for kind, text in st.session_state.pop(
            f"review_msg_{P['run'].name}", []):
        (st.warning if kind == "warning" else st.success)(text)

    if stale:
        st.warning(STALE_TICK_MSG.format(n=stale))

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
    cat_opts = category_options(current_client().slug)
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
                         "retries) live in the Table view.")
    if mode == "Card review":
        # the UNFILTERED sku column: validator_flags has to resolve a CSV sku
        # against every raw on the sheet, not just the visible ones
        card_review(P, fdf, cat_opts, df["sku"].astype(str).tolist())
        # the card's 'Product URL' + Save is how a NEEDS LINK row gets its
        # link here, so the crawl button has to be reachable from this view
        crawl_section(P, df)
        return

    st.caption("Edit title, prices, URLs and image URLs directly in the "
               "table; tick ✔ to approve a row for the CSV, or **Skip** to "
               "leave it out of every build. Then press "
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

    # the same editor as the card view, for one chosen row at a time (it is
    # computed only for that row). Saving here reruns the page, so unsaved
    # table edits are lost - the caption says so.
    with st.expander("📝 Price, attributes & descriptions — one SKU"):
        st.caption("Save the table edits first: saving here reloads the "
                   "page.")
        pick_opts = [""] + fdf["sku"].astype(str).tolist()
        pick = st.selectbox("SKU", pick_opts,
                            key=f"desc_pick_{P['run'].name}")
        if pick:
            picked = fdf[fdf["sku"].astype(str) == pick].iloc[0]
            price_attribute_editor(P, picked, "table")
            description_editor(P, picked, "table")

    # the All tab duplicates the bucket rows; parts is in TAB order - review,
    # failed, pending, found, all - so All is the last one and goes first.
    # merge_confirm_flags applies a part's value only when it CHANGES that
    # sku's persisted flag, so an untouched copy of a row can never overwrite
    # a tick made in the other tab. Order therefore decides one case only: the
    # same sku changed in BOTH its bucket tab and the All tab during this
    # render - the bucket tab, being later, wins.
    ordered = [parts[-1]] + parts[:-1]

    def persist_flags(flags, rejected, changed=None, new_skips=None):
        """Save the merged ✔ / Skip maps, then stash the outcome and rerun.
        The rerun throws the page away, so anything printed here would never
        be seen - step_review pops these messages at the top of the next
        run."""
        # re-read: write_edits_back has just landed this render's edits, and
        # the fingerprints stored beside the ticks must describe THAT sheet
        save_confirmed(P, flags, load_filled_df(P), new_skips)
        n_skip = len(new_skips if new_skips is not None else skipped_map(P))
        head = "" if changed is None else f"Saved — {changed} cell(s) " \
                                          "updated, "
        msgs = [("success", f"{head}{len(confirmed_set(P))} row(s) "
                            f"confirmed, {n_skip} skipped.")]
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
        before_conf = confirmed_set(P)
        before_skips = skipped_map(P)
        try:
            changed, touched = write_edits_back_detail(P, df, ordered)
        except PermissionError:
            st.error("The filled .xlsx is open in Excel — close it and save "
                     "again.")
            return
        flags, new_skips, rejected = merge_review_flags(
            before_conf, before_skips, df, ordered)
        log_table_save(P, touched, before_conf, flags, before_skips,
                       new_skips)
        persist_flags(flags, rejected, changed, new_skips)

    if b2.button("✅ Confirm all clean FOUND"):
        # merge the tabs FIRST: a tick made in any editor and not yet saved is
        # an edit like any other, and rebuilding the map from `df` alone would
        # silently drop it (the P1-8 data loss). The merge is also what applies
        # the FOUND gate to ticks already sitting on disk.
        flags, new_skips, rejected = merge_review_flags(
            confirmed_set(P), skipped_map(P), df, ordered)
        # a bulk confirm never overrides a skip: a skipped row is the
        # reviewer's explicit 'no', and only a per-row ✔ may take it back
        for _, r in df.iterrows():
            s = str(r["sku"])
            if r["_bucket"] == "found" and found_status(r["status"]) \
                    and s not in new_skips:
                flags[s] = True
        persist_flags(flags, rejected, None, new_skips)

    meta = load_meta(P)
    # --discover MUST be stripped: passing it alongside --crawl-urls is the one
    # combination the enricher refuses outright
    base_flags = [f for f in meta.get("enricher_flags", [])
                  if f not in ("--force", "--retry-incomplete",
                               "--retry-missing", "--discover", "--crawl-urls")]
    total = meta.get("total_rows", 0)

    crawl_section(P, df)
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


@st.cache_data(max_entries=2, show_spinner=False)
def _image_zip(csv_path: str, mtime_ns: int, size: int, image_dir: str):
    """gui_helpers.build_image_zip, cached on the CSV's path + mtime + size
    (and the image folder), so a rerun never rebuilds it. max_entries keeps
    at most two zips in memory."""
    return gui_helpers.build_image_zip(csv_path, image_dir)


def build_downloads(P):
    """Step 3's hand-over buttons: the CSV and the images it references, as
    a zip laid out like the image URLs (unpack into wp-content/uploads/).
    Reads only; available in share mode too - it deletes or moves nothing."""
    csv_path = P["csv"]
    try:
        stat = csv_path.stat()
    except OSError:
        return
    cfg = st.session_state.get(_CFG_KEY) or current_client()
    image_dir = getattr(cfg, "image_dir", None) or (
        REPO_ROOT / client_config.IMAGE_ROOT_NAME / cfg.slug)
    data, summary = _image_zip(str(csv_path), stat.st_mtime_ns,
                               stat.st_size, str(image_dir))
    c1, c2 = st.columns(2)
    c1.download_button("⬇ Download CSV", csv_path.read_bytes(),
                       file_name=csv_path.name, mime="text/csv")
    if summary["csv_error"]:
        c2.warning(f"The CSV cannot be read ({summary['csv_error']}), so no "
                   "image zip. Rebuild the CSV.")
        return
    if not summary["urls"]:
        c2.info("The CSV has no image URLs, so there is no image zip.")
        return
    c2.download_button("⬇ Download images (.zip)", data,
                       file_name=f"{csv_path.stem}_images.zip",
                       mime="application/zip")
    c2.caption(gui_helpers.image_zip_caption(summary)
               + ". Unpack into wp-content/uploads/ on the store.")
    if summary["missing"] or summary["refused"]:
        with st.expander("Images not in the zip"):
            for z in summary["missing"]:
                st.text(f"missing: {z}")
            for u in summary["refused"]:
                st.text(f"refused: {u}")


def step_build(P):
    st.header("3 · Build WooCommerce CSV")
    if not P["filled"].exists():
        st.info("No enriched output yet — run Steps 1–2 first.")
        return

    review_df = load_filled_df(P)
    # drop stale ticks BEFORE counting, so the summary and the scope label
    # never promise rows the build would then refuse (P3-53)
    n_stale_top = reconcile_confirmations(P, review_df)
    if n_stale_top:
        st.warning(STALE_TICK_MSG.format(n=n_stale_top))
    conf = confirmed_set(P)
    skips = skipped_map(P)
    counts = review_counts(review_df, conf, skips)
    # the review summary the owner checks before he builds
    st.info("Review: " + review_counts_text(counts))
    if skips:
        with st.expander(f"Skipped rows ({len(skips)}) — never built"):
            st.dataframe(pd.DataFrame(
                [{"sku": s, "reason": r or "—"}
                 for s, r in sorted(skips.items())]),
                hide_index=True, width="stretch")
    # the VALUE is a stable key; only the rendering carries the wording, so a
    # label rewrite can never silently flip what built_scope records.
    # The default is ALWAYS the approved rows: the owner's design is that
    # only rows he approved in Step 2 go on. 'All FOUND rows' is an explicit
    # choice, never the fallback for an empty approval list.
    scope = st.radio(
        "Rows to include", ["confirmed", "all_found"],
        format_func=lambda v: (f"Confirmed rows only ({counts['approved']})"
                               if v == "confirmed" else
                               "All FOUND rows (skip unverified, like "
                               "build_csv.py)"),
        index=0)
    blocked = scope == "confirmed" and counts["approved"] == 0
    if blocked:
        st.error("No row is approved, so there is nothing to build. Approve "
                 "at least one row in step 2 - Review.")
    elif scope == "all_found":
        st.warning("All FOUND rows includes rows nobody approved "
                   f"({counts['not_reviewed']} not reviewed). Skipped rows "
                   "stay out.")
    n_desc = len(gui_helpers.overrides_for_build(
        gui_helpers.load_overrides_state(gui_helpers.overrides_path(P["run"]))))
    if n_desc:
        st.info(f"{n_desc} SKU(s) have edited descriptions from step 2; the "
                "build uses those edits.")
    month = st.text_input("WordPress uploads folder (YYYY/MM)",
                          value=f"{datetime.now():%Y/%m}")
    c1, c2 = st.columns(2)
    # [pricing] single_price: the build writes ONE price (basis/markup rule,
    # client_config.one_price) and never an MRP, so an estimated MRP has no
    # place
    _pricing = getattr(st.session_state.get(_CFG_KEY), "pricing", None)
    single_price = bool(getattr(_pricing, "single_price", False))
    if getattr(_pricing, "basis", "higher") == "lower":
        _rule = (f"the lower price + Rs {getattr(_pricing, 'markup', 0):,g}, "
                 "never above the MRP")
    else:
        _rule = "the higher of MRP and selling price"
    estimate_mrp = c1.checkbox(
        "Estimate missing MRP (sale price × 1.25)", disabled=single_price,
        help=(f"This client builds ONE price ({_rule}); no MRP is "
              "estimated." if single_price else None))
    if single_price:
        estimate_mrp = False
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
            pf, _ = select_build_rows(pf, scope, conf, skips)

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

    if not st.button("🛠 Build CSV", type="primary", disabled=blocked,
                     help="Approve at least one row in step 2 - Review"
                     if blocked else None):
        if P["csv"].exists():
            st.success(f"CSV already built: {P['csv'].name} — rebuild any "
                       "time, or continue to Step 4.")
            build_downloads(P)
        return

    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    import app                      # same code path as build_csv.py

    df = pd.read_excel(P["filled"])
    df = app.filter_active_inventory_rows(df)
    # P3-53: the sheet may have been re-enriched since Step 2 was last open,
    # so drop any tick whose row changed BEFORE the scope is applied
    n_stale = reconcile_confirmations(P, df)
    if n_stale:
        st.warning(STALE_TICK_MSG.format(n=n_stale))
        conf = confirmed_set(P)
    skips = skipped_map(P)
    df, skipped = select_build_rows(df, scope, conf, skips)
    if skipped:
        st.warning(f"{len(skipped)} confirmed row(s) skipped — not FOUND "
                   "(NEEDS CRAWL / NOT FOUND / CLOSE MATCH …). Fix them in "
                   "Step 2 and crawl: " + ", ".join(skipped))
    if df.empty:
        st.error("Nothing to build. Approve at least one row in step 2 - "
                 "Review.")
        return
    n_scoped = len(df)
    n_skipped_out = len(skips)

    folder = month.strip().strip("/") or f"{datetime.now():%Y/%m}"
    # the store this client publishes to, from the very config app.py was
    # configured with - never a literal, or a second client's images would be
    # written under the first one's domain (docs/CLIENT_CONFIG.md item 1)
    media_base = (getattr(app, "CLIENT", None)
                  or current_client()).media_base(folder)

    buf = io.StringIO()
    with st.spinner(f"Building CSV for {len(df)} product(s) — images are "
                    "downloaded and converted, this can take a while…"):
        with contextlib.redirect_stdout(buf):
            result = app.process_inventory(
                df, media_base=media_base, estimate_mrp=estimate_mrp,
                seo=seo, seo_plugin=seo_plugin,
                # the reviewer's description edits from step 2 (same file
                # build_csv.py --description-overrides takes)
                description_overrides=gui_helpers.overrides_for_build(
                    gui_helpers.load_overrides_state(
                        gui_helpers.overrides_path(P["run"]))))
        try:
            app.write_csv(result, P["csv"])
        except PermissionError:
            # P3-50: the build has already downloaded every image by now, but
            # nothing is recorded - no update_meta, so the run stays at its
            # previous stage and Build is simply pressed again once the file
            # is closed (a re-run reuses the .webp files on disk).
            st.error(f"Could not write {P['csv'].name}: it is open in "
                     "another program (Excel?). Close it and press Build "
                     "again.")
            return

    with_images = int((result["Images"].map(app.compact) != "").sum())
    st.success(f"Wrote **{P['csv'].name}** — {len(result)} product(s), "
               f"{with_images} with images, media base `{media_base}`")
    if scope == "confirmed":
        st.info(f"Built {len(result)} product(s) from the {n_scoped} "
                "approved row(s) only. Images were downloaded for those "
                "rows only.")
    else:
        st.info(f"Built {len(result)} product(s) from {n_scoped} row(s): "
                f"all FOUND rows minus {n_skipped_out} skipped. This "
                "includes rows nobody approved.")
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
    if s.get("filter_unmatched_rows"):
        st.caption(f"{s['filter_unmatched_rows']} row(s) state a filter value "
                   "that is not in the filter sheet — see `_filter_unmatched`.")
    if s["category_counts"]:
        with st.expander("Products per category", expanded=True):
            st.bar_chart(pd.Series(s["category_counts"], name="products"))

    with st.expander("Build log"):
        st.code(buf.getvalue() or "(no output)", language=None)
    st.dataframe(result.head(50))
    build_downloads(P)
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
    try:
        with st.spinner("Validating the new CSV…"):
            with contextlib.redirect_stdout(vbuf):
                v_issues = validate_csv.validate_export(
                    vdf, str(P["csv"]), check_images=True)
    except PermissionError:
        # same as above, one file along: validate_export writes the issues
        # report next to the CSV. Return before update_meta - a run whose
        # validation never ran must not be stamped validated.
        st.error(f"Could not write {P['issues'].name}: it is open in "
                 "another program (Excel?). Close it and press Build again.")
        return
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

# Mirrors wc_publish.EXIT_BLOCKED / EXIT_REFUSED. Duplicated rather than
# imported so this module never has to import wc_publish (which reads store
# credentials at import time). If either constant moves, these must follow.
PUBLISH_EXIT_BLOCKED = 3        # an edge challenge; later chunks are pointless
PUBLISH_EXIT_REFUSED = 4        # a guard tripped and NOTHING was written


def signoff_path(P) -> Path:
    """review_signoff.csv sits NEXT TO the CSV, because that is where
    wc_publish.py looks for it (docs/CLIENT_CONFIG.md item 3.4)."""
    return P["csv"].parent / "review_signoff.csv"


def write_review_signoff(P, skus) -> Path:
    """Record who signed off which SKUs, for `--commit --status publish`.

    Overwritten on every arming, never appended: the file must describe THIS
    commit's scope, and a stale row from an earlier, wider batch would let a
    SKU nobody looked at go live. Reviewer is the Windows account name - the
    point is attribution, not authentication."""
    reviewer = os.environ.get("USERNAME") or getpass.getuser()
    signed_at = datetime.now().isoformat(timespec="seconds")
    path = signoff_path(P)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv_module.writer(fh)
        writer.writerow(["sku", "reviewer", "signed_at"])
        for sku in sorted(skus):
            writer.writerow([sku, reviewer, signed_at])
    return path


def unconfirmed_publish_skus(P, publish_skus) -> list:
    """CSV SKUs with no human tick behind them, in CSV order.

    Step 2 confirms the enricher's RAW sku while the CSV carries
    app.prefixed_sku's form, so the two are matched through
    gui_helpers.raw_sku_for_csv_sku; a CSV sku that maps back to nothing has
    no tick by definition and counts as unconfirmed."""
    confirmed = confirmed_set(P)
    try:
        raws = load_filled_df(P)["sku"].astype(str).tolist()
    except Exception:
        raws = []
    out = []
    for sku in publish_skus:
        raw = raw_sku_for_csv_sku(sku, raws)
        if raw is None or raw not in confirmed:
            out.append(sku)
    return out


def run_publish(P, extra: list) -> tuple:
    if share_mode():
        # exit code 4 = REFUSED, nothing written - wc_publish.py's own
        # convention. No subprocess is started, so not even --check runs.
        return 4, "REFUSED: publish is off in share mode.\n"
    slug = run_client_slug(P["run"])
    cmd = ([str(VENV_PY), "wc_publish.py", str(P["csv"]),
            "--client", slug] + extra)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env[client_config.ENV_VAR] = slug
    res = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env,
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=3600)
    out = (res.stdout or "") + (res.stderr or "")
    P["publish_log"].write_text(out, encoding="utf-8")
    return res.returncode, out


HANDOVER_STEPS = (
    "1. Download the CSV and the image zip.\n"
    "2. Unzip; upload the folders into `wp-content/uploads/` on the store "
    "(hosting file manager or FTP), keeping the folders.\n"
    "3. WooCommerce > Products > Import > choose the CSV.")


def handover_section(P):
    """Step 5's manual hand-over: the same downloads as step 3 plus the
    import steps. Shown in every mode (it only reads files)."""
    st.subheader("Hand-over (manual import)")
    if not P["csv"].exists():
        st.info("No CSV yet — build it in Step 3 to download it here.")
        return
    build_downloads(P)
    st.markdown(HANDOVER_STEPS)
    st.divider()


def step_publish(P):
    st.header("5 · Publish to WooCommerce")
    handover_section(P)
    st.subheader("Publish over the REST API")
    if share_mode():
        st.info("Publish is off in share mode.")
        return
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

    cfg = current_client()
    # What wc_publish.py will actually see: client_config.apply_env OVERRIDES
    # the ambient process env with clients/<slug>/.env, so the client's own
    # file wins here too. The GUI only READS it - injecting a second client's
    # WC_SECRET into this long-lived process is exactly the leak item 3 is
    # about.
    env = dict(os.environ) | client_env(cfg)
    missing = [v for v in ("WC_URL", "WC_KEY", "WC_SECRET")
               if not env.get(v)]
    if missing:
        st.error(f"Missing credentials for client `{cfg.slug}`: "
                 f"{', '.join(missing)}. Put them in "
                 f"clients\\{cfg.slug}\\.env (or the environment), then "
                 "reload.")
        return
    if not same_store(env["WC_URL"], cfg.store_url):
        st.error(
            f"Refusing to publish: WC_URL points at "
            f"**{store_host(env['WC_URL'])}**, but client `{cfg.slug}` is "
            f"**{store_host(cfg.store_url)}**. Nothing on this page will run "
            "until the two name the same store — fix "
            f"clients\\{cfg.slug}\\.env or switch the Client selector.")
        return

    pre_key = f"preflight_{P['run'].name}"
    st.subheader("0 · Preflight")
    st.caption("Credentials, edge/challenge check, media login and the "
               "store-URL match — one call, nothing is written.")
    if st.button("🔎 Preflight"):
        with st.spinner("Preflight…"):
            code, out = run_publish(P, ["--preflight"])
        # csv-independent: preflight says nothing about the CSV, only about
        # the store, so a rebuild in Step 3 does not invalidate it
        st.session_state[pre_key] = code == 0
        st.code(out[-3000:], language=None)
        (st.success if code == 0 else st.error)(
            "Preflight passed" if code == 0
            else "Preflight FAILED — fix the store before publishing")
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
            # Only an edge challenge (EXIT_BLOCKED) or a tripped guard
            # (EXIT_REFUSED) makes the later chunks pointless - every one
            # would stop the same way, and a refusal means nothing was
            # written at all. A row-level failure must NOT stop them, or the
            # retry panel never sees the SKUs after the first bad row.
            if code in (PUBLISH_EXIT_BLOCKED, PUBLISH_EXIT_REFUSED):
                break
        return worst, "\n".join(outs)

    # The dry-run flag is the SIGNATURE of the CSV that was dry-run, never a
    # bool: Step 3 re-stamps built_scope/built_skus on every rebuild, so both
    # guards above pass again on a freshly built CSV while a bare bool would
    # still be sitting there unlocking Commit for rows nobody dry-ran (build
    # 'Confirmed rows only' -> dry run -> rebuild 'All FOUND rows' -> commit).
    # Comparing against the file on disk is also the only form that holds
    # across sessions: session_state is per-tab, the CSV is shared.
    dry_key = f"dryrun_{P['run'].name}"
    dry_sig = st.session_state.get(dry_key)
    st.subheader("Ⅰ · Dry run (nothing is written)")
    pre_ok = bool(st.session_state.get(pre_key))
    if not pre_ok:
        st.caption("Run Preflight above first — the dry run unlocks once it "
                   "passes in this session.")
    if st.button("🧪 Dry run", type="primary", disabled=not pre_ok):
        with st.spinner("Dry run…"):
            code, out = run_publish_pinned([])
        dry_sig = csv_signature(P["csv"]) if code == 0 else None
        st.session_state[dry_key] = dry_sig
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
        if code == 0:
            st.success("Dry run finished")
        elif code == PUBLISH_EXIT_REFUSED:
            st.error("Dry run refused — nothing was written (see the log)")
        else:
            st.error("Dry run FAILED — fix before committing")

    if dry_sig is None or dry_sig != csv_signature(P["csv"]):
        st.caption(
            "CSV was rebuilt since the last dry run — dry-run again before "
            "committing." if dry_sig
            else "Commit unlocks after a successful dry run in this session.")
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
    unconfirmed = set()
    if go_live:
        # Live is the one irreversible step, so it is reachable only for rows
        # a human actually ticked in Step 2 - the same set that then goes into
        # review_signoff.csv, which wc_publish re-checks on its own side.
        unconfirmed = unconfirmed_publish_skus(P, publish_skus)
        if unconfirmed:
            st.error(
                f"{len(unconfirmed)} of {len(publish_skus)} product(s) in this "
                f"CSV are not confirmed in Step 2 — starting with "
                f"`{unconfirmed[0]}`. Confirm them (or rebuild the CSV from "
                "confirmed rows only) before publishing LIVE.")
            go_live = False
    label = (f"🚀 Commit {len(publish_skus)} products — create/update as LIVE"
             if go_live
             else f"📝 Commit {len(publish_skus)} products — create/update "
                  "as DRAFTS")
    if st.button(label, type="primary", disabled=not armed):
        # --max-commit is wc_publish's blast-radius cap; the GUI's own gates
        # (built_skus match, dry run, typed run name, the tick set) are what
        # decides the scope, so hand it exactly the scope it just showed.
        extra = ["--commit", "--max-commit", str(len(publish_skus))]
        if go_live:
            extra += ["--status", "publish"]
            write_review_signoff(P, [s for s in publish_skus
                                     if s not in set(unconfirmed)])
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
                        published_count=len(publish_skus),
                        published_csv=csv_signature(P["csv"]))
            st.session_state.pop(dry_key, None)
        elif code == PUBLISH_EXIT_REFUSED:
            st.error("Publish refused — nothing was written (see the log). "
                     f"Full output in {P['publish_log'].name}.")
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

# Where the last archive_run() put its .zip (None when zipping failed or
# nothing was archived). archive_run still returns the file COUNT the Home
# page and tests\\test_gui_runmgr.py expect, so the zip path rides here
# rather than changing that return type.
LAST_ARCHIVE_ZIP: Path | None = None


def archive_run(run_dir: Path, dest: Path) -> int:
    """Copy a run's reviewable outputs into batches\\<name>\\ - filled sheet,
    CSV, issues, report, the client's ORIGINAL list under its own name, plus
    exactly the images its CSV references - write a README, then zip the
    folder to batches\\<name>.zip so there is ONE file to hand over.

    Returns the number of files copied (the zip path lands in
    LAST_ARCHIVE_ZIP). Copies only; the run folder is left untouched.
    Refused in share mode (ShareModeRefused)."""
    global LAST_ARCHIVE_ZIP
    refuse_in_share_mode("Archiving a run")
    LAST_ARCHIVE_ZIP = None
    Pr = run_paths(run_dir)
    files = {f.name: f for f in (Pr[k] for k in
                                 ("filled", "csv", "issues", "report"))}
    # the list the client actually sent, under the name they sent it under -
    # "input.xlsx" tells a hand-over reader nothing about where it came from
    meta = load_json(Pr["meta"])
    if Pr["input"].exists():
        src_name = str(meta.get("original_name") or "").strip()
        files[Path(src_name).name if src_name else Pr["input"].name] = \
            Pr["input"]

    # P2-25: a build killed mid-to_csv leaves a 0-byte (EmptyDataError) or
    # ragged (ParserError) wordpress_ready.csv - both ValueError, neither an
    # OSError. Archiving the REST of the run is far more useful than aborting,
    # so the unreadable CSV only costs the images folder and earns a README
    # line. assemble_pack owns that rule for BOTH entry points.
    # This folder is the OPERATOR's internal archive (filled sheet, report,
    # the client's list): internal=True. It is never handed over - the zip
    # below is built from the separate client pack.
    image_dir = REPO_ROOT / "product_images_for_upload"
    pack = assemble_pack(dest, files, image_dir, Pr["csv"], internal=True)
    notes = []
    if pack["csv_error"]:
        notes.append(f"\n- **The CSV could not be read** "
                     f"({pack['csv_error']}), so NO images were "
                     f"archived. Rebuild the CSV and archive again.\n")

    coverage = coverage_summary(read_filled_sheet(Pr["filled"]))
    # P3-54: "Built scope: all (43 SKUs)" next to "Published: <date> (LIVE)"
    # read as 43 live products when the publish covered an earlier build.
    # published_csv is the signature of the CSV that was actually published.
    meta = dict(meta)
    meta["published_stale"] = bool(
        meta.get("published") and meta.get("published_csv")
        and meta["published_csv"] != csv_signature(Pr["csv"]))
    meta["archived_from"] = f"runs\\{run_dir.name}"
    meta["files"] = sorted({p.name for p in dest.iterdir()} | {"README.md"})

    readme = dest / "README.md"
    readme.write_text(
        render_batch_readme(
            run_dir.name, meta, coverage,
            review_metrics=load_json(Pr["metrics"]) or None,
            images={"count": pack["images"], "missing": pack["missing"]},
            notes=notes),
        encoding="utf-8")

    # The CLIENT pack (owner rule 2026-11-19): a separate folder
    # batches\<name>_client\ with ONLY the import CSV, the images it
    # references and a README - never validation_issues.csv (internal since
    # the fifth round of 2026-11-19), the filled
    # sheet, the enricher report, a log or the source list. The zip handed
    # over (batches\<name>.zip) is made from this folder alone.
    client_dir = dest.parent / f"{dest.name}_client"
    if client_dir.exists():
        shutil.rmtree(client_dir)
    hosts = default_client_hosts()
    cpack = assemble_pack(client_dir,
                          {Pr["csv"].name: Pr["csv"]},
                          image_dir, Pr["csv"], allowed_hosts=hosts)
    cmeta = dict(meta)
    cmeta["files"] = sorted({p.name for p in client_dir.iterdir()}
                            | {"README.md"})
    cnotes = list(notes) + [
        f"not packed (internal or links to a source site): "
        f"{item.split(': ', 1)[0]}" for item in cpack["refused"]]
    client_readme = client_dir / "README.md"
    client_readme.write_text(
        render_batch_readme(
            run_dir.name, cmeta, coverage,
            review_metrics=load_json(Pr["metrics"]) or None,
            images={"count": cpack["images"], "missing": cpack["missing"]},
            source_name="-", notes=cnotes),
        encoding="utf-8")
    remove_refused(client_dir, hosts)       # last check, README included

    # one file to hand over. A zip that cannot be written (disk full, the
    # previous .zip open in Explorer) must not lose the folder we just
    # built, so it costs a README line and nothing else.
    try:
        LAST_ARCHIVE_ZIP = Path(shutil.make_archive(str(dest), "zip",
                                                    root_dir=str(client_dir)))
    except (OSError, ValueError) as e:
        LAST_ARCHIVE_ZIP = None
        for fh_path in (readme, client_readme):
            with fh_path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n- **The .zip could not be written** "
                         f"({e.__class__.__name__}: {e}) - the folder "
                         f"{client_dir.name} is complete; zip it by hand.\n")
    return pack["copied"]


def read_filled_sheet(path: Path):
    """The enriched sheet as a DataFrame, or None. Coverage counting must
    never be the reason an archive fails, so an .xlsx open in Excel or
    truncated mid-save costs the Coverage numbers, not the archive."""
    if not path.exists():
        return None
    try:
        return pd.read_excel(path, dtype=str).fillna("")
    except Exception as e:      # openpyxl/zipfile raise their own shapes
        print(f"[archive] could not read {path.name}: "
              f"{e.__class__.__name__}: {e}")
        return None


def client_runs(cfg) -> list:
    """This client's run folders, newest first. A run belonging to another
    client is not listed at all - the wizard's every step (taxonomy, images,
    store) is that client's, so offering it would only lead to the refusal
    guard_run_client() raises."""
    RUNS_DIR.mkdir(exist_ok=True)
    # a folder starting with "_" is never a run: runs\_share\ holds the
    # share-mode password file and server log (tools\share_gui.ps1), and
    # "Create run" strips a leading "_", so no real run can be named so
    runs = sorted((d for d in RUNS_DIR.iterdir()
                   if d.is_dir() and not d.name.startswith("_")),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    return [d for d in runs if run_client_slug(d) == cfg.slug]


def pick_run(cfg) -> Path | None:
    runs = client_runs(cfg)
    HOME, NEW = "🏠 All runs", "➕ New run…"
    options = [HOME, NEW] + [d.name for d in runs]
    # land on the latest run as before; Home is one click away. With NO
    # runs yet the default must be NEW, not HOME - Home's empty state tells
    # you to create a run in the sidebar, but the name box only renders for
    # NEW, so defaulting to Home would leave a fresh install unable to make
    # its first run (tests	est_gui_ui.py::test_create_run pins this).
    sel = st.sidebar.selectbox("Batch / run", options,
                               index=2 if runs else 1, key="run_picker")
    if sel == HOME:
        return None
    if sel != NEW:
        return RUNS_DIR / sel
    name = st.sidebar.text_input("New run name",
                                 value=f"run_{datetime.now():%Y%m%d}")
    if st.sidebar.button("Create run"):
        safe = re.sub(r"[^\w\-]+", "_", name.strip()).strip("_")
        if not safe:
            return None
        target = RUNS_DIR / safe
        # A folder that already exists under ANOTHER client must not be
        # adopted: run_meta.json would be overwritten with this client's slug
        # and the other store's sheet, images and CSV would come with it.
        if target.exists() and run_client_slug(target) != cfg.slug:
            st.sidebar.error(
                f"runs\\{safe} already belongs to client "
                f"`{run_client_slug(target)}`, and this session is "
                f"`{cfg.slug}`. Pick another name.")
            return None
        target.mkdir(exist_ok=True)
        meta_path = run_paths(target)["meta"]
        meta = load_json(meta_path)
        meta.setdefault("client", cfg.slug)
        meta.setdefault("created", datetime.now().isoformat(timespec="seconds"))
        save_json(meta_path, meta)
        st.rerun()
    return None


def delete_run(run_dir: Path):
    """Permanently delete a run folder. Refused in share mode
    (ShareModeRefused); OSError propagates to the caller's message."""
    refuse_in_share_mode("Deleting a run")
    shutil.rmtree(run_dir)


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


def backup_and_save_category_map(path, edited, backups_dir=None):
    """Back the taxonomy up, then write the edited first sheet back.

    The two steps belong together and are the whole of the editor's save
    path, so they live in one plain function tests\\test_gui_runmgr.py can
    call without a Streamlit session. Returns the backup path; the backup
    is taken BEFORE the write, so a botched edit is always one file copy
    away from being undone.

    `backups_dir` is the client's own clients\\<slug>\\backups\\; the
    REPO_ROOT default is kept only so the plain-function tests can call this
    with no client at all. Refused in share mode (ShareModeRefused)."""
    refuse_in_share_mode("Saving the category map")
    backup = (Path(backups_dir or (REPO_ROOT / "backups")) /
              f"category_map_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
    # parents=True: clients\\<slug>\\backups\\ may not exist yet, and the
    # client dir itself always does - the taxonomy was just read from it
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    save_first_sheet(path, edited)
    return backup


def category_map_editor(cfg):
    """Edit THIS CLIENT's category_map.xlsx in place. app.py snaps categories
    to these EXACT strings, so removals are loud: they are listed and must be
    acknowledged before the save button arms. Every save backs up the
    current file to clients\\<slug>\\backups\\ first.

    Only the FIRST sheet is editable here; the other sheets of the workbook
    ('Sub-Category Attributes', 'Filters') are preserved verbatim."""
    if share_mode():
        st.info("Taxonomy edits are off in share mode.")
        return
    path = Path(cfg.category_map)
    st.header(f"⚙ Category map — {cfg.name}")
    st.caption(f"`{path}`")
    if not path.exists():
        st.error(f"{path} not found — client `{cfg.slug}` has no taxonomy "
                 "to edit.")
        return
    st.download_button(
        "⬇ Download category_map.xlsx", path.read_bytes(),
        file_name=path.name, key="catmap_download",
        mime="application/vnd.openxmlformats-officedocument."
             "spreadsheetml.sheet")
    if os.name != "nt":
        # a hosted server (Streamlit Cloud, a container) has a throw-away
        # disk: a save survives only until the next restart or redeploy
        st.caption("This app runs on a server: a saved edit lives only on "
                   "this server until the next restart or redeploy - "
                   "download the file and commit it to GitHub to keep it.")
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
            backup = backup_and_save_category_map(path, edited,
                                                  cfg.backups_dir)
        except PermissionError:
            st.error(f"{path.name} is open in Excel — close it and "
                     "save again.")
            return
        # app.CATEGORY_COMBOS is bound at import; re-run configure() for THIS
        # client so the NEXT build in this session snaps against the edited
        # map. A bare importlib.reload would re-read whatever PIPELINE_CLIENT
        # happens to hold, which after a selector switch is not necessarily
        # the client whose map was just edited.
        os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
        os.environ[client_config.ENV_VAR] = cfg.slug
        import app
        app.configure(cfg.slug)
        category_options.clear()
        st.success(f"Saved. Backup: `{backup}`. Review dropdowns and the "
                   "next build now use the edited map.")


def main():
    # set_page_config has to be the first Streamlit call, so the client is
    # resolved before it - current_client() only reads session_state and the
    # environment, it renders nothing. A broken config falls back to a
    # generic tab title and is reported by the selector below.
    # Behind the password gate (share or password-only mode) nothing is read
    # (not even client.toml) before login, so the tab title stays generic.
    page_title = "Catalogue pipeline"
    if not gate_on():
        try:
            page_title = f"{current_client().name} pipeline"
        except ClientConfigError:
            pass
    st.set_page_config(page_title=page_title, page_icon="🛒", layout="wide")
    password_gate()                 # st.stop()s unless logged in / no gate
    try:
        cfg = client_selector()
    except ClientConfigError as exc:
        # every page below needs the taxonomy and the store URL, so this is
        # fatal - but a readable sentence, not a traceback
        st.sidebar.error(str(exc))
        # the slug the selector was pointing AT - naming the previous one
        # sent the operator to a client.toml that is perfectly fine
        attempted = (st.session_state.get(ATTEMPT_KEY)
                     or st.session_state.get(CLIENT_KEY))
        st.error("No usable client configuration — fix "
                 f"clients\\{attempted}\\client.toml "
                 "and reload.")
        return
    st.sidebar.title(f"🛒 {cfg.name}")
    if not share_mode() and st.sidebar.checkbox(
            "⚙ Category map editor",
            help="Edit the taxonomy the builds snap to. "
                 "Closes the wizard while open."):
        category_map_editor(cfg)
        return

    run_dir = pick_run(cfg)
    if run_dir is None:
        st.title(f"{cfg.name} catalogue pipeline")
        run_dirs = client_runs(cfg)
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
                                       "csv": Pd["csv"].exists()},
                                   csv_signature(Pd["csv"])),
                "rows": m.get("total_rows", ""),
                "issues": m.get("issue_count", ""),
                "published": m.get("published", ""),
                "updated": datetime.fromtimestamp(
                    d.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True,
                     width="stretch")
        st.caption("Pick a run in the sidebar to open it.")
        if share_mode():
            return              # archive / delete are off in share mode
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
                    except (OSError, ValueError) as e:
                        # a sheet open in Excel, an image deleted under us, a
                        # full disk: a friendly message, never a red traceback.
                        # ValueError too - pandas' EmptyDataError/ParserError
                        # on a truncated CSV are ValueError, not OSError (P2-25)
                        st.error(f"Could not finish archiving runs\\{target}: "
                                 f"{e.__class__.__name__} — {e}. "
                                 f"batches\\{target} may be partly written; "
                                 "remove it and retry.")
                    else:
                        st.success(f"Archived {n} file(s) to batches\\{target}."
                                   " The run folder itself is untouched.")
                        # one file to hand the client - say so, or say why
                        # there isn't one (archive_run never raises on a
                        # failed zip; the folder is complete either way)
                        zip_path = LAST_ARCHIVE_ZIP
                        if zip_path and zip_path.exists():
                            st.caption(
                                f"📦 {zip_path.name} "
                                f"({zip_path.stat().st_size / 1e6:.1f} MB) — "
                                f"one file to hand over.")
                        else:
                            st.caption("The .zip could not be written — see "
                                       "the note at the end of the "
                                       "batch README.")
            # key MUST carry the target: a static key keeps the tick alive
            # when the selectbox moves to another run, so the next run could
            # be deleted with one click and no confirmation (P2-25)
            sure = c2.checkbox(f"Yes, permanently delete runs\\{target}",
                               key=f"mgr_sure_{target}")
            if c2.button("🗑 Delete run", disabled=not sure or busy,
                         key="mgr_del"):
                try:
                    delete_run(tdir)
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
    if not guard_run_client(run_dir, cfg):
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
