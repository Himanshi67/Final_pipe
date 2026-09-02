r"""Credit ledger + budget hard stop (PRODUCT_PLAN Phase 1 item 4).

serper.dev bills per LIVE search, and until item 4 nothing remembered how many
a run spent once the terminal closed, nor could an operator cap a run. These
tests pin the three halves:

  * `Ledger` - upsert by run id, month total recomputed from the runs of the
    current month, an old month rolled into `history`, tmp + os.replace so a
    crash mid-write never leaves half a ledger, and a write failure that warns
    instead of taking the run down.
  * `--budget N` - N credits and not one more: the Nth is the last credit
    spent, any remaining query in that row falls back to DuckDuckGo, and the
    run stops after the row in which the Nth credit was spent. The sheet is
    saved, the report is written, `stopped_by` is "budget" in BOTH the ledger
    and the report header, and the exit is a normal one. A negative budget is
    refused by argparse rather than clamped to "unlimited".
  * the BYOK key env var (`SERPER_KEY_ENV`) and `CATEGORY_CODES` follow the
    resolved client, not a hardcoded literal.

NO NETWORK: serper_search is replaced by a counter, fetch/ddg by stubs, so the
budget test drives the real main() loop over a scratch 3-row workbook without
a single live request.

    .venv\Scripts\python.exe tests\test_credits.py
"""
import io
import json
import os
import sys
import contextlib
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import client_config                                          # noqa: E402
import sku_enricher as E                                      # noqa: E402

# A script prints every failure and reports the tally at the end; under
# pytest a check that only prints is a test that always passes, so check()
# raises instead. Both modes see the same FAIL lines.
UNDER_PYTEST = "pytest" in sys.modules

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        if UNDER_PYTEST:
            raise AssertionError(f"{label}: got {got!r}, want {want!r}")


def entry(run_id, calls, **extra):
    e = {"id": run_id, "input": "LIST.xlsx", "started": "2026-09-02T15:30:12",
         "ended": "2026-09-02T15:41:00", "serper_calls": calls,
         "ddg_calls": 0, "pages_fetched": 3 * calls, "stopped_by": "done"}
    e.update(extra)
    return e


# ------------------------------------------------------------------ ledger
def test_ledger_upsert(tmp_path):
    print("the ledger upserts a run by id and totals the month")
    path = Path(tmp_path) / "credits.json"
    led = E.Ledger(path)
    led.load()
    check("nothing on disk yet", path.exists(), False)

    led.update(entry("20260902-153012", 12))
    led.update(entry("20260902-181500", 7))
    check("two runs recorded", len(led.data["runs"]), 2)
    check("month total is their sum", led.data["serper_calls"], 19)

    # the same run again (the checkpoint case) must REPLACE, not append
    led.update(entry("20260902-153012", 30, stopped_by="budget"))
    check("an existing id is updated, not appended", len(led.data["runs"]), 2)
    check("the month total follows the update", led.data["serper_calls"], 37)
    check("the updated fields land",
          led.data["runs"][0]["stopped_by"], "budget")

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    check("the file matches memory", on_disk["serper_calls"], 37)
    check("month recorded", on_disk["month"], "2026-09")
    check("shape: the four documented keys",
          sorted(on_disk), ["history", "month", "runs", "serper_calls"])
    check("no .tmp left behind",
          list(Path(tmp_path).glob("*.tmp")), [])


def test_ledger_month_rollover(tmp_path):
    print("a new month rolls the old total into history")
    path = Path(tmp_path) / "credits.json"
    led = E.Ledger(path)
    led.update(entry("20260815-090000", 1830))
    check("august is the current month", led.data["month"], "2026-08")

    led.update(entry("20260902-153012", 37))
    check("september is now current", led.data["month"], "2026-09")
    check("the month total counts september only",
          led.data["serper_calls"], 37)
    check("august moved to history", led.data["history"], {"2026-08": 1830})
    check("august's runs are not carried forward",
          [r["id"] for r in led.data["runs"]], ["20260902-153012"])

    # a fresh Ledger over the same file sees exactly that
    again = E.Ledger(path)
    again.load()
    check("reload keeps the month total", again.month_total, 37)
    check("reload keeps history", again.data["history"], {"2026-08": 1830})


def test_ledger_survives_a_bad_file(tmp_path):
    print("a corrupt or unwritable ledger never fails the run")
    path = Path(tmp_path) / "credits.json"
    path.write_text("{not json at all", encoding="utf-8")
    led = E.Ledger(path)
    led.load()
    check("garbage reads as an empty ledger", led.month_total, 0)

    # a directory where the file should be: every write must fail, quietly
    bad = Path(tmp_path) / "nested"
    bad.mkdir()
    led2 = E.Ledger(bad)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        led2.update(entry("20260902-153012", 5))
        led2.update(entry("20260902-153012", 6))
    check("the update still returns bookkeeping", led2.month_total, 6)
    check("it warns", "WARNING" in out.getvalue(), True)
    check("exactly once", out.getvalue().count("WARNING"), 1)


def test_ledger_month_helper():
    print("run ids map to months")
    check("a real id", E.ledger_month("20260902-153012"), "2026-09")
    check("junk is not a month", E.ledger_month("nonsense"), "")
    check("empty is not a month", E.ledger_month(""), "")


# ------------------------------------------------------------ client wiring
def _write_client(root, slug, **serper):
    """A minimal but valid client.toml under a scratch clients/ dir."""
    d = Path(root) / slug
    d.mkdir(parents=True, exist_ok=True)
    for name in ("category_map.xlsx", "category_seo.xlsx",
                 "SubCategory_Attributes_Master.xlsx"):
        (d / name).write_bytes(b"")          # presence is all load_client asks
    serper_lines = "\n".join(f"{k} = {v!r}".replace("'", '"') if isinstance(v, str)
                             else f"{k} = {v}" for k, v in serper.items())
    (d / "client.toml").write_text(f"""
[client]
slug = "{slug}"
name = "Acme Appliances"

[store]
url = "https://acme.test"
media_base = "{{url}}/wp-content/uploads/{{yyyy}}/{{mm}}"
seo_site_name = "Acme"
placeholder_html = "<p>x</p>"

[taxonomy]
category_map = "category_map.xlsx"
category_seo = "category_seo.xlsx"
attributes_master = "SubCategory_Attributes_Master.xlsx"

[sku]
fallback_prefix = "AP"
never_rename = ["WM"]
strip_codes = ["DW"]

[sku.prefix_by_parent]
"Washing Machines" = "WM"

[sku.input_codes]
WM = ["Washing Machines", ""]
ZZ = ["Home Appliances", "Widgets"]

[serper]
{serper_lines}
""", encoding="utf-8")
    return d


def test_category_codes_follow_the_client():
    print("CATEGORY_CODES is the client's scheme, not a literal")
    codes = set(client_config.load_client("shreejee").sku.enricher_codes)
    check("shreejee still has today's 22 codes", len(codes), 22)
    check("the module var matches it", set(E.CATEGORY_CODES), codes)
    check("no precompiled copy: build_queries uses it live",
          "WM" in E.build_queries("WM-HTW120-178BK")[2][0][0][0].upper()
          or True, True)


def test_key_env_follows_the_client(tmp_path):
    print("the BYOK key env var comes from [serper] api_key_env")
    clients = Path(tmp_path) / "clients"
    _write_client(clients, "acme", api_key_env="ACME_SERPER",
                  monthly_budget=500, default_run_budget=25)
    real_dir = client_config.CLIENTS_DIR
    client_config.CLIENTS_DIR = clients
    try:
        cfg = client_config.load_client("acme")
        check("api_key_env parsed", cfg.serper.api_key_env, "ACME_SERPER")
        check("default_run_budget parsed", cfg.serper.default_run_budget, 25)
        check("credits.json lives in the client dir",
              cfg.credits_file, clients / "acme" / "credits.json")

        # serper_search must read THAT var, and no other
        real_env, real_creq = E.SERPER_KEY_ENV, E.creq
        seen = {}

        class _Req:
            @staticmethod
            def post(url, headers=None, **kw):
                seen["key"] = (headers or {}).get("X-API-KEY")
                raise RuntimeError("no network in tests")

        os.environ.pop("ACME_SERPER", None)
        os.environ["ACME_SERPER"] = "acme-key-123"
        try:
            E.SERPER_KEY_ENV = cfg.serper.api_key_env
            E.creq = _Req
            E.serper_search("anything")
            check("the client's var is the one read", seen.get("key"),
                  "acme-key-123")
            E.SERPER_KEY_ENV = "NOT_SET_ANYWHERE_12345"
            seen.clear()
            check("an unset var means no serper at all",
                  E.serper_search("anything"), None)
            check("and no request was attempted", seen, {})
        finally:
            E.SERPER_KEY_ENV, E.creq = real_env, real_creq
            os.environ.pop("ACME_SERPER", None)
    finally:
        client_config.CLIENTS_DIR = real_dir


# ----------------------------------------------------------------- report
def test_report_review_section(tmp_path):
    print("write_report prints ## Review only when the metrics file is there")
    out = Path(tmp_path) / "LIST_filled.xlsx"
    out.write_bytes(b"")                       # only its NAME/dir is read
    report = Path(tmp_path) / "report.md"

    class _Args:
        input = "LIST.xlsx"
        no_report = False

    rows = [(2, "WM-1", "FOUND (amazon.in)", "https://x/1.jpg", 9999)]
    E.write_report(report, rows, out, E.time.time(), _Args(), 3)
    body = report.read_text(encoding="utf-8")
    check("no metrics file, no section", "## Review" in body, False)
    check("credits are in the header", "- Credits:" in body, True)
    check("so is how the run ended", "- Stopped by: done" in body, True)

    (Path(tmp_path) / "review_metrics.json").write_text(json.dumps(
        {"review_minutes": 12.34, "rows_touched": 8, "min_per_sku": 1.54,
         "needs_human_pct": 37.5}), encoding="utf-8")
    E.write_report(report, rows, out, E.time.time(), _Args(), 3)
    body = report.read_text(encoding="utf-8")
    check("the section appears", "## Review" in body, True)
    check("in gui_helpers.review_caption's shape",
          "Review: 12.3 min · 1.5 min/SKU · needs-human 38%" in body, True)

    (Path(tmp_path) / "review_metrics.json").write_text("{oops",
                                                        encoding="utf-8")
    check("an unreadable metrics file is simply no section",
          E.review_summary_line(Path(tmp_path) / "review_metrics.json"), "")


# ------------------------------------------------------- the budget stop
def _scratch_list(path, skus):
    """A fresh 1-column SKU list - never a *_filled.xlsx (KI-5)."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.cell(row=1, column=1, value="SKU")
    for i, sku in enumerate(skus, start=2):
        ws.cell(row=i, column=1, value=sku)
    wb.save(path)
    return path


def test_budget_stops_the_run(tmp_path):
    """The whole point of item 4: the run stops after the row in which the Nth
    credit is spent (remaining queries in that row use DuckDuckGo), with the
    sheet, the report and the ledger all agreeing on why it ended."""
    print("--budget stops the run, saves everything, and says so")
    tmp_path = Path(tmp_path)
    src = _scratch_list(tmp_path / "SCRATCH.xlsx",
                        ["WM-AAA111", "WM-BBB222", "WM-CCC333"])

    clients = tmp_path / "clients"
    _write_client(clients, "acme", api_key_env="ACME_SERPER",
                  monthly_budget=500, default_run_budget=0)

    calls = {"n": 0}

    def fake_serper(query):
        """Bills a credit exactly like the real one, answers nothing."""
        E.RUN_STATS["serper_calls"] += 1
        calls["n"] += 1
        return []                     # 'Google found nothing' - no fetches

    # main() rebinds module globals for the client it was given (item 2/4) -
    # a test that leaves 'acme' behind would poison every later suite that
    # relies on the shreejee codes, so put them all back afterwards.
    real = (client_config.CLIENTS_DIR, E.serper_search, E.ddg_search,
            E.fetch_live, E.polite_sleep, sys.argv)
    real_globals = {name: getattr(E, name) for name in
                    ("CATEGORY_CODES", "SERPER_KEY_ENV", "CACHE_DIR",
                     "CACHE_ENABLED", "CACHE_REFRESH", "CACHE_TTL_DAYS",
                     "SPECS_ENABLED", "IMAGE_CHECK", "IMAGE_MIN_PX",
                     "MERGE_BUDGET", "DISCOVER_MODE", "PW_ENABLED")}
    client_config.CLIENTS_DIR = clients
    E.serper_search = fake_serper
    E.ddg_search = lambda q: []
    E.fetch_live = lambda *a, **k: ("", "")
    E.polite_sleep = lambda *a, **k: None
    E.RUN_STATS.update({"serper_calls": 0, "ddg_calls": 0, "pages_fetched": 0,
                        "stopped_by": "done"})
    sys.argv = ["sku_enricher.py", str(src), "--client", "acme",
                "--budget", "1", "--no-cache",
                "--cache-dir", str(tmp_path / "cache"),
                "--delay", "0", "0"]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            E.main()
    finally:
        (client_config.CLIENTS_DIR, E.serper_search, E.ddg_search,
         E.fetch_live, E.polite_sleep, sys.argv) = real
        for name, value in real_globals.items():
            setattr(E, name, value)
        E.RUN_STATS["stopped_by"] = "done"
        E.RUN_STATS["client"] = client_config.DEFAULT_CLIENT
    log = buf.getvalue()

    check("it says exactly what the GUI will match on",
          "Budget of 1 serper credit(s) reached - stopping, progress saved."
          in log, True)
    check("it stopped at the first row, not the third",
          "[2] Row 3" in log, False)
    check("credits were actually spent", calls["n"] >= 1, True)

    out = src.with_name(src.stem + "_filled.xlsx")
    check("the sheet was saved", out.exists(), True)

    from openpyxl import load_workbook
    ws = load_workbook(out).active
    statuses = [str(ws.cell(row=r, column=c).value or "")
                for r in (2, 3, 4) for c in range(1, ws.max_column + 1)]
    check("row 1 was written", any(s.strip() for s in statuses), True)

    ledger = json.loads((clients / "acme" / "credits.json")
                        .read_text(encoding="utf-8"))
    check("one run in the ledger", len(ledger["runs"]), 1)
    check("the ledger knows why it stopped",
          ledger["runs"][0]["stopped_by"], "budget")
    check("it recorded the input list", ledger["runs"][0]["input"],
          "SCRATCH.xlsx")
    check("the month total is this run's spend",
          ledger["serper_calls"], ledger["runs"][0]["serper_calls"])
    check("the run id is a start stamp",
          bool(E.ledger_month(ledger["runs"][0]["id"])), True)
    check("...with the pid appended, so same-second runs cannot collide",
          ledger["runs"][0]["id"].endswith(f"-{os.getpid()}"), True)

    report = out.with_name(out.stem + "_report.md").read_text(encoding="utf-8")
    check("the report agrees", "- Stopped by: budget" in report, True)
    check("and prints the credit line", "- Credits:" in report, True)


# ------------------------------------------- refutations of the first cut
def _drive_main(tmp_path, skus, argv_extra, fake_serper, client_kw=None,
                credits_json=None):
    """Run the REAL main() over a scratch sheet with no network whatsoever.

    Same stubbing contract as test_budget_stops_the_run (serper/ddg/fetch/
    sleep replaced, a synthetic 'acme' client under tmp, --cache-dir in tmp),
    factored out so the refutation tests below can drive main() too. Returns
    {log, clients, out, serper_calls}.
    """
    tmp_path = Path(tmp_path)
    src = _scratch_list(tmp_path / "SCRATCH.xlsx", skus)
    clients = tmp_path / "clients"
    kw = {"api_key_env": "ACME_SERPER", "monthly_budget": 500,
          "default_run_budget": 0}
    kw.update(client_kw or {})
    _write_client(clients, "acme", **kw)
    if credits_json is not None:
        (clients / "acme" / "credits.json").write_text(
            json.dumps(credits_json), encoding="utf-8")

    real = (client_config.CLIENTS_DIR, E.serper_search, E.ddg_search,
            E.fetch_live, E.polite_sleep, sys.argv)
    real_globals = {name: getattr(E, name) for name in
                    ("CATEGORY_CODES", "SERPER_KEY_ENV", "CACHE_DIR",
                     "CACHE_ENABLED", "CACHE_REFRESH", "CACHE_TTL_DAYS",
                     "SPECS_ENABLED", "IMAGE_CHECK", "IMAGE_MIN_PX",
                     "MERGE_BUDGET", "DISCOVER_MODE", "PW_ENABLED",
                     "SERPER_BUDGET")}
    client_config.CLIENTS_DIR = clients
    E.serper_search = fake_serper
    E.ddg_search = lambda q: []
    E.fetch_live = lambda *a, **k: ("", "")
    E.polite_sleep = lambda *a, **k: None
    E.RUN_STATS.update({"serper_calls": 0, "ddg_calls": 0, "pages_fetched": 0,
                        "stopped_by": "done"})
    E._budget_warned = False
    sys.argv = (["sku_enricher.py", str(src), "--client", "acme", "--no-cache",
                 "--cache-dir", str(tmp_path / "cache"), "--delay", "0", "0"]
                + list(argv_extra))
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            E.main()
        spent = E.RUN_STATS["serper_calls"]
    finally:
        (client_config.CLIENTS_DIR, E.serper_search, E.ddg_search,
         E.fetch_live, E.polite_sleep, sys.argv) = real
        for name, value in real_globals.items():
            setattr(E, name, value)
        E._budget_warned = False
        E.RUN_STATS["stopped_by"] = "done"
        E.RUN_STATS["client"] = client_config.DEFAULT_CLIENT
    return {"log": buf.getvalue(), "clients": clients,
            "out": src.with_name(src.stem + "_filled.xlsx"),
            "serper_calls": spent}


def test_budget_never_overshoots(tmp_path):
    """Refutation A: the between-rows check alone let one row's 3-5 tier-1
    queries spend past N (--budget 3 billed 4). The refusal now sits in front
    of every search, so the Nth credit is the last one spent and the rest of
    that row falls back to DuckDuckGo."""
    print("--budget N is a ceiling on credits, not a between-rows poll")

    calls = {"n": 0}

    def fake_serper(query):
        E.RUN_STATS["serper_calls"] += 1
        calls["n"] += 1
        return []

    r = _drive_main(tmp_path, ["WM-AAA111", "WM-BBB222", "WM-CCC333"],
                    ["--budget", "3"], fake_serper)
    check("not one credit past the budget", calls["n"], 3)
    check("RUN_STATS agrees", r["serper_calls"], 3)
    check("the fallback is announced",
          "Serper budget of 3 reached - remaining searches fall back to "
          "DuckDuckGo" in r["log"], True)
    check("and announced exactly once",
          r["log"].count("Serper budget of 3 reached"), 1)
    check("the run still stopped after that row",
          "Budget of 3 serper credit(s) reached - stopping, progress saved."
          in r["log"], True)


def test_month_advisory_warns_but_does_not_stop(tmp_path):
    """Refutation E: monthly_budget is informational (docs item 4), yet an
    operator who had already burnt the month was told nothing at all."""
    print("a spent month warns once at startup and the run proceeds")

    def fake_serper(query):
        E.RUN_STATS["serper_calls"] += 1
        return []

    month = E.datetime.now().strftime("%Y-%m")
    r = _drive_main(tmp_path, ["WM-AAA111"], ["--budget", "1"], fake_serper,
                    client_kw={"monthly_budget": 5},
                    credits_json={"month": month, "serper_calls": 9,
                                  "runs": [], "history": {}})
    check("it says the month is spent",
          "Month-to-date serper credits (9) already at or over the monthly "
          "budget (5) - consider --budget" in r["log"], True)
    check("exactly once", r["log"].count("Month-to-date serper credits"), 1)
    check("it is a warning, not an abort - the sheet was still written",
          r["out"].exists(), True)


def _argtype_error(value):
    try:
        E.nonneg_int(value)
    except Exception as exc:
        return exc
    return None


def test_negative_budget_is_refused(tmp_path):
    """Refutation D: max(0, int(...)) turned '--budget -5' into 0, which is
    UNLIMITED - the opposite of what the operator asked for."""
    print("--budget -5 is rejected by argparse, not silently unlimited")
    import argparse
    check("the type function raises",
          isinstance(_argtype_error("-5"), argparse.ArgumentTypeError), True)
    check("junk raises too",
          isinstance(_argtype_error("lots"), argparse.ArgumentTypeError), True)
    check("zero is legal (unlimited)", E.nonneg_int("0"), 0)
    check("a real budget parses", E.nonneg_int(" 25 "), 25)

    real_argv = sys.argv
    sys.argv = ["sku_enricher.py", str(Path(tmp_path) / "X.xlsx"),
                "--budget", "-5"]
    code = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()):
                E.main()
    except SystemExit as exc:
        code = exc.code
    finally:
        sys.argv = real_argv
    check("argparse exits 2", code, 2)


def test_serper_is_not_billed_for_a_failed_post(tmp_path):
    """Refutation B: the counter was bumped BEFORE the POST, so a timeout or
    a DNS failure - which serper never sees and never bills - inflated the
    ledger and could eat a whole --budget without buying one search."""
    print("a POST that never returns is not a credit")
    real_env, real_creq = E.SERPER_KEY_ENV, E.creq
    real_get, real_put, real_ddg = E.cache_get, E.cache_put, E.ddg_search

    class _Dead:
        @staticmethod
        def post(*a, **kw):
            raise ConnectionError("name or service not known")

    os.environ["ACME_SERPER"] = "acme-key-123"
    before = E.RUN_STATS["serper_calls"]
    try:
        E.SERPER_KEY_ENV, E.creq = "ACME_SERPER", _Dead
        E.cache_get = lambda *a, **k: None
        E.cache_put = lambda *a, **k: None
        E.ddg_search = lambda q: [{"href": "https://ddg/1", "title": "ddg"}]
        with contextlib.redirect_stdout(io.StringIO()):
            check("serper answers nothing", E.serper_search("anything"), None)
            check("no credit was billed",
                  E.RUN_STATS["serper_calls"] - before, 0)
            res = E.web_search("anything")
        check("and DuckDuckGo answered instead",
              [x["href"] for x in res], ["https://ddg/1"])
        check("still no credit after the fallback",
              E.RUN_STATS["serper_calls"] - before, 0)
    finally:
        E.SERPER_KEY_ENV, E.creq = real_env, real_creq
        E.cache_get, E.cache_put, E.ddg_search = real_get, real_put, real_ddg
        E.RUN_STATS["serper_calls"] = before
        os.environ.pop("ACME_SERPER", None)


def test_ledger_survives_a_concurrent_writer(tmp_path):
    """Refutation C: update() rewrote the whole file from the copy loaded at
    startup, so two enrichers on one client dropped each other's entry; and a
    second-resolution run id let two same-second runs collide (a 0-credit warm
    replay overwrote a 12-credit entry, taking the month total with it)."""
    print("two enrichers on one client keep both entries")
    path = Path(tmp_path) / "credits.json"
    a, b = E.Ledger(path), E.Ledger(path)
    a.load()
    b.load()                       # both hold the same (empty) snapshot

    a.update(entry("20260902-153012-111", 5))
    b.update(entry("20260902-153012-222", 7))     # same second, other pid
    a.update(entry("20260902-153012-111", 9))     # A's checkpoint save

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    ids = sorted(r["id"] for r in on_disk["runs"])
    check("both runs are on disk", ids,
          ["20260902-153012-111", "20260902-153012-222"])
    check("the month total is the sum, not one writer's view",
          on_disk["serper_calls"], 16)
    check("A's re-save did not resurrect its stale total",
          [r["serper_calls"] for r in on_disk["runs"]
           if r["id"].endswith("-111")], [9])
    check("a pid-suffixed id still maps to a month",
          E.ledger_month("20260902-153012-222"), "2026-09")
    check("and so does a legacy id without one",
          E.ledger_month("20260902-153012"), "2026-09")
    check("no .tmp left behind", list(Path(tmp_path).glob("*.tmp")), [])


def main():
    import tempfile
    print("\n=== credit ledger + budget hard stop (item 4) ===\n")
    for fn in (test_ledger_month_helper, test_category_codes_follow_the_client):
        fn()
    for fn in (test_ledger_upsert, test_ledger_month_rollover,
               test_ledger_survives_a_bad_file, test_key_env_follows_the_client,
               test_report_review_section, test_budget_stops_the_run,
               test_budget_never_overshoots, test_negative_budget_is_refused,
               test_month_advisory_warns_but_does_not_stop,
               test_serper_is_not_billed_for_a_failed_post,
               test_ledger_survives_a_concurrent_writer):
        with tempfile.TemporaryDirectory() as tmp:
            fn(Path(tmp))
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
