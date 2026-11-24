r"""KI-36: the search engine chain and the "search engine failed" status.

Fully OFFLINE: every engine is a stub or runs against a mocked HTTP client
(E.creq), the search cache lives in a temp folder, and no serper / Tavily /
SerpApi credit or DuckDuckGo request is spent.

    .venv\Scripts\python.exe -m pytest -q tests\test_search_chain_ki36.py
"""
import contextlib
import io
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sku_enricher as E                                      # noqa: E402

# the REAL engines, taken at import time: tests\conftest.py replaces the two
# fallback engines with a "no key" stub for every test, so a key in the
# shell can never reach the network from another suite
REAL_TAVILY = E.tavily_search
REAL_SERPAPI = E.serpapi_search
REAL_DDG = E.ddg_search

QUIET = lambda *a, **k: None                                  # noqa: E731
SKU = "LG-GL-N292DDSY"
AMZ = "https://www.amazon.in/LG-Frost-Free-Refrigerator-GL-N292DDSY/dp/B0C1234567"
TITLE = "LG 242 L Frost Free Double Door Refrigerator GL-N292DDSY"
STATS = ("serper_calls", "ddg_calls", "tavily_calls", "serpapi_calls",
         "queries_skipped_dup", "queries_skipped_early", "rows_web_search")
ALL = ("serper", "tavily", "serpapi", "ddg")


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(E, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(E, "CACHE_ENABLED", True)
    monkeypatch.setattr(E, "CACHE_REFRESH", False)
    monkeypatch.setattr(E, "CACHE_TTL_DAYS", 14.0)
    monkeypatch.setattr(E, "IMAGE_CHECK", False)
    monkeypatch.setattr(E, "PW_ENABLED", False)
    monkeypatch.setattr(E, "MERGE_BUDGET", 0)
    monkeypatch.setattr(E, "DISCOVER_MODE", False)
    monkeypatch.setattr(E, "polite_sleep", QUIET)
    monkeypatch.setattr(E, "URL_INDEX_ON", False)
    monkeypatch.setattr(E, "SITE_SEARCH_ON", False)
    monkeypatch.setattr(E, "SITE_SEARCH_RULES", [])
    monkeypatch.setattr(E, "URL_INDEX_SOURCES", [])
    monkeypatch.setattr(E, "INDEX_FRESH_DOMAINS", set())
    monkeypatch.setattr(E, "_COOLDOWN", {})
    monkeypatch.setattr(E, "SERPER_BUDGET", 0)
    monkeypatch.setattr(E, "_budget_warned", False)
    monkeypatch.setattr(E, "QUERY_MODE", "incremental")
    monkeypatch.setattr(E, "SEARCH_ORDER", "paid_first")
    monkeypatch.setattr(E, "SEARCH_ENGINES", ALL)
    monkeypatch.setattr(E, "log", QUIET)
    for k in STATS:
        monkeypatch.setitem(E.RUN_STATS, k, 0)

    calls = []
    # per engine: a result list, None (failure), or a callable(q)
    answers = {name: None for name in ALL}

    def stub(name):
        def engine(q):
            calls.append(name)
            a = answers[name]
            a = a(q) if callable(a) else a
            if a is None:
                return None
            return [{"href": u, "title": TITLE} for u in a]
        return engine

    monkeypatch.setattr(E, "serper_search", stub("serper"))
    monkeypatch.setattr(E, "tavily_search", stub("tavily"))
    monkeypatch.setattr(E, "serpapi_search", stub("serpapi"))
    monkeypatch.setattr(E, "ddg_search", stub("ddg"))
    monkeypatch.setattr(E, "fetch", lambda u: (None, "http 404"))

    def lookup(sku=SKU):
        with contextlib.redirect_stdout(io.StringIO()):
            return E.lookup_sku(sku)

    return SimpleNamespace(calls=calls, answers=answers, lookup=lookup,
                           monkeypatch=monkeypatch)


# --------------------------------------------------------------- chain order
def test_paid_first_order_walks_the_whole_chain(env):
    assert E.engine_order() == ["serper", "tavily", "serpapi", "ddg"]
    assert E.web_search("q") == []
    assert env.calls == ["serper", "tavily", "serpapi", "ddg"]
    assert E.LAST_SEARCH_FAILED is True


def test_free_first_moves_ddg_to_the_front(env):
    env.monkeypatch.setattr(E, "SEARCH_ORDER", "free_first")
    assert E.engine_order() == ["ddg", "serper", "tavily", "serpapi"]
    E.web_search("q")
    assert env.calls == ["ddg", "serper", "tavily", "serpapi"]


def test_configured_engine_list_is_the_chain(env):
    env.monkeypatch.setattr(E, "SEARCH_ENGINES", ("tavily", "ddg"))
    E.web_search("q")
    assert env.calls == ["tavily", "ddg"]


def test_none_falls_through_to_the_next_engine(env):
    env.answers["tavily"] = [AMZ]
    res = E.web_search("q")
    assert env.calls == ["serper", "tavily"]
    assert res[0]["href"] == AMZ
    assert E.LAST_SEARCH_FAILED is False
    assert E.search_is_cached("q")


def test_an_empty_paid_answer_stops_the_chain_and_is_cached(env):
    env.answers["serper"] = []
    assert E.web_search("q") == []
    assert env.calls == ["serper"]
    assert E.LAST_SEARCH_FAILED is False
    assert E.search_is_cached("q")


def test_an_empty_ddg_answer_asks_the_next_engine(env):
    env.monkeypatch.setattr(E, "SEARCH_ORDER", "free_first")
    env.answers["ddg"] = []
    env.answers["serper"] = [AMZ]
    assert E.web_search("q")[0]["href"] == AMZ
    assert env.calls == ["ddg", "serper"]


def test_an_empty_ddg_answer_alone_is_not_a_failure_and_not_cached(env):
    env.answers["ddg"] = []
    assert E.web_search("q") == []
    assert E.LAST_SEARCH_FAILED is False
    assert not E.search_is_cached("q")


def test_a_cache_hit_asks_no_engine(env):
    E.cache_put("search", "q", [{"href": AMZ, "title": TITLE}])
    E.LAST_SEARCH_FAILED = True
    assert E.web_search("q")[0]["href"] == AMZ
    assert env.calls == []
    assert E.LAST_SEARCH_FAILED is False


def test_budget_refusal_on_serper_falls_through_to_tavily(env):
    env.monkeypatch.setattr(E, "SERPER_BUDGET", 1)
    E.RUN_STATS["serper_calls"] = 1
    env.answers["tavily"] = [AMZ]
    res = E.web_search("q")
    assert "serper" not in env.calls          # the credit was refused
    assert env.calls == ["tavily"]
    assert res[0]["href"] == AMZ
    assert E.RUN_STATS["serper_calls"] == 1


# ------------------------------------------------------- real engine parsers
class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Http:
    def __init__(self, resp=None):
        self.resp, self.seen = resp, []

    def post(self, url, **kw):
        self.seen.append(("POST", url, kw))
        return self.resp

    def get(self, url, **kw):
        self.seen.append(("GET", url, kw))
        return self.resp


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setattr(E, "log", QUIET)
    monkeypatch.setattr(E, "TAVILY_KEY_ENV", "KI36_TEST_TAVILY")
    monkeypatch.setattr(E, "SERPAPI_KEY_ENV", "KI36_TEST_SERPAPI")
    monkeypatch.setenv("KI36_TEST_TAVILY", "tv-key")
    monkeypatch.setenv("KI36_TEST_SERPAPI", "sa-key")
    monkeypatch.setattr(E, "_tavily_failed_warned", False)
    monkeypatch.setattr(E, "_serpapi_failed_warned", False)
    for k in ("tavily_calls", "serpapi_calls"):
        monkeypatch.setitem(E.RUN_STATS, k, 0)
    h = _Http()
    monkeypatch.setattr(E, "creq", h)
    return h


def test_tavily_parses_results_and_sends_the_bearer_key(http):
    http.resp = _Resp(200, {"results": [
        {"url": AMZ, "title": TITLE, "content": "x"},
        {"url": "https://www.flipkart.com/p/itm1", "title": "FK"}]})
    res = REAL_TAVILY("lg gl-n292ddsy")
    assert res == [{"href": AMZ, "title": TITLE},
                   {"href": "https://www.flipkart.com/p/itm1", "title": "FK"}]
    method, url, kw = http.seen[0]
    assert (method, url) == ("POST", "https://api.tavily.com/search")
    assert kw["headers"]["Authorization"] == "Bearer tv-key"
    assert kw["json"] == {"query": "lg gl-n292ddsy", "max_results": 10,
                          "search_depth": "basic"}
    assert kw.get("timeout")
    assert E.RUN_STATS["tavily_calls"] == 1


def test_tavily_non_200_is_a_failure(http):
    http.resp = _Resp(401, {"detail": "bad key"})
    assert REAL_TAVILY("q") is None


def test_tavily_empty_results_is_an_answer(http):
    http.resp = _Resp(200, {"results": []})
    assert REAL_TAVILY("q") == []


def test_serpapi_parses_organic_results(http):
    http.resp = _Resp(200, {"organic_results": [
        {"link": AMZ, "title": TITLE, "position": 1}]})
    assert REAL_SERPAPI("q") == [{"href": AMZ, "title": TITLE}]
    method, url, kw = http.seen[0]
    assert (method, url) == ("GET", "https://serpapi.com/search.json")
    p = kw["params"]
    assert (p["engine"], p["q"], p["api_key"], p["gl"], p["hl"], p["num"]) == (
        "google", "q", "sa-key", "in", "en", 10)
    assert E.RUN_STATS["serpapi_calls"] == 1


def test_serpapi_error_json_is_a_failure(http):
    http.resp = _Resp(200, {"error": "Invalid API key. Your API key should be here"})
    assert REAL_SERPAPI("q") is None
    http.resp = _Resp(429, {"error": "Your account has run out of searches."})
    assert REAL_SERPAPI("q") is None


def test_serpapi_no_results_error_is_an_empty_answer(http):
    http.resp = _Resp(200, {"error": "Google hasn't returned any results for "
                                     "this query."})
    assert REAL_SERPAPI("q") == []


def test_a_missing_key_is_skipped_without_an_http_call(http, monkeypatch):
    monkeypatch.delenv("KI36_TEST_TAVILY")
    monkeypatch.delenv("KI36_TEST_SERPAPI")
    assert REAL_TAVILY("q") is None
    assert REAL_SERPAPI("q") is None
    assert http.seen == []


def test_missing_keys_in_the_chain_make_no_http_call(env, http, monkeypatch):
    monkeypatch.delenv("KI36_TEST_TAVILY")
    monkeypatch.delenv("KI36_TEST_SERPAPI")
    monkeypatch.setattr(E, "tavily_search", REAL_TAVILY)
    monkeypatch.setattr(E, "serpapi_search", REAL_SERPAPI)
    env.answers["ddg"] = [AMZ]
    assert E.web_search("q")[0]["href"] == AMZ
    assert env.calls == ["serper", "ddg"]
    assert http.seen == []


def test_ddg_double_failure_returns_none(monkeypatch):
    class _Boom:
        def __enter__(self):
            raise RuntimeError("DDGSException: timeout")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(E, "DDGS", _Boom)
    monkeypatch.setattr(E.time, "sleep", QUIET)
    monkeypatch.setattr(E, "log", QUIET)
    monkeypatch.setitem(E.RUN_STATS, "ddg_calls", 0)
    assert REAL_DDG("q") is None
    assert E.RUN_STATS["ddg_calls"] == 2


# ---------------------------------------------------------------- row status
def test_a_row_where_every_engine_fails_says_so(env):
    rec = env.lookup()
    assert rec["status"] == "NOT FOUND - search engine failed"
    assert not rec["status"].startswith("FOUND")


def test_a_row_where_engines_answer_empty_keeps_no_usable_results(env):
    env.answers["serper"] = []
    assert env.lookup()["status"] == "NOT FOUND - no usable search results"


def test_one_answered_query_means_the_row_did_not_fail(env):
    first = [q for t in E.build_queries(SKU)[2] for q, _ in t][0]
    env.answers["serper"] = lambda q: [] if q == first else None
    assert env.lookup()["status"] == "NOT FOUND - no usable search results"


def test_a_cached_query_counts_as_success(env):
    first = [q for t in E.build_queries(SKU)[2] for q, _ in t][0]
    E.cache_put("search", first, [])
    assert env.lookup()["status"] == "NOT FOUND - no usable search results"


def test_not_found_status_flag():
    assert (E.not_found_status([], 0, [], search_failed=True)
            == "NOT FOUND - search engine failed")
    assert (E.not_found_status([], 0, [])
            == "NOT FOUND - no usable search results")
    # candidates exist: the old reasons win
    assert E.not_found_status(["u"], 1, [], search_failed=True).startswith(
        "NOT FOUND - 1 page(s) checked")


# -------------------------------------------------------------------- report
def test_report_counts_the_search_engine_failed_bucket(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "log", QUIET)
    out = tmp_path / "LIST_filled.xlsx"
    out.write_bytes(b"")
    report = tmp_path / "report.md"

    class _Args:
        input = "LIST.xlsx"
        no_report = False

    rows = [(2, "A", "FOUND (amazon.in)", "https://x/1.jpg", 9999),
            (3, "B", "NOT FOUND - search engine failed", "", ""),
            (4, "C", "NEEDS LINK - NOT FOUND - search engine failed", "", ""),
            (5, "D", "NOT FOUND - no usable search results", "", "")]
    monkeypatch.setitem(E.RUN_STATS, "tavily_calls", 4)
    monkeypatch.setitem(E.RUN_STATS, "serpapi_calls", 2)
    E.write_report(report, rows, out, E.time.time(), _Args(), 3)
    body = report.read_text(encoding="utf-8")
    assert "- search engine failed: 2" in body
    assert "| 1 | NOT FOUND - search engine failed |" in body
    assert "| 1 | NOT FOUND |" in body
    assert "Tavily calls: 4" in body and "SerpApi calls: 2" in body
    assert E.status_bucket("NOT FOUND - search engine failed") == \
        "NOT FOUND - search engine failed"
    assert E.status_bucket("NOT FOUND - no usable search results") == "NOT FOUND"


# -------------------------------------------------------------------- ledger
def test_ledger_records_fallback_calls_and_loads_an_old_file(tmp_path):
    month = datetime.now().strftime("%Y-%m")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = tmp_path / "credits.json"
    # a credits.json written before KI-36: no tavily / serpapi fields
    path.write_text(json.dumps({
        "month": month, "serper_calls": 7, "history": {},
        "runs": [{"id": f"{stamp}-1", "serper_calls": 7, "ddg_calls": 0}]}),
        encoding="utf-8")
    led = E.Ledger(path)
    led.load()
    assert led.month_total == 7
    assert led.month_engine_calls("tavily_calls") == 0

    led.update({"id": f"{stamp}-2", "serper_calls": 1, "ddg_calls": 0,
                "tavily_calls": 3, "serpapi_calls": 2})
    again = E.Ledger(path)
    again.load()
    assert again.month_total == 8
    assert again.month_engine_calls("tavily_calls") == 3
    assert again.month_engine_calls("serpapi_calls") == 2
    run = next(r for r in again.data["runs"] if r["id"].endswith("-2"))
    assert (run["tavily_calls"], run["serpapi_calls"]) == (3, 2)
    # the top-level layout is unchanged (test_credits pins it)
    assert sorted(json.loads(path.read_text(encoding="utf-8"))) == \
        ["history", "month", "runs", "serper_calls"]


# ============================================ review fixes (2026-11-24)
# ------------------------------------------- fix 1: ddgs "No results found."
try:
    from ddgs.exceptions import DDGSException as _DDGSExc
except Exception:                                   # pragma: no cover
    class _DDGSExc(Exception):
        pass


def _ddgs_no_results(monkeypatch):
    class _NoHits:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, *a, **k):
            raise _DDGSExc("No results found.")

    slept = []
    monkeypatch.setattr(E, "DDGS", _NoHits)
    monkeypatch.setattr(E.time, "sleep", lambda s: slept.append(s))
    return slept


def test_ddg_no_results_exception_is_an_empty_answer_without_sleep(monkeypatch):
    slept = _ddgs_no_results(monkeypatch)
    monkeypatch.setattr(E, "log", QUIET)
    monkeypatch.setitem(E.RUN_STATS, "ddg_calls", 0)
    assert REAL_DDG("q") == []
    assert slept == []
    assert E.RUN_STATS["ddg_calls"] == 1          # no second attempt


def test_ddg_no_results_row_is_no_usable_results_not_engine_failed(env):
    _ddgs_no_results(env.monkeypatch)
    env.monkeypatch.setattr(E, "ddg_search", REAL_DDG)
    rec = env.lookup()                            # serper/tavily/serpapi: None
    assert rec["status"] == "NOT FOUND - no usable search results"
    assert E.SEARCH_FAILED_STATUS not in rec["status"]


# ------------------------------------- fix 4: only serper's empty is cached
def test_an_empty_tavily_answer_stops_the_chain_but_is_not_cached(env):
    env.answers["tavily"] = []
    assert E.web_search("q") == []
    assert env.calls == ["serper", "tavily"]
    assert E.LAST_SEARCH_FAILED is False
    assert not E.search_is_cached("q")


def test_an_empty_serpapi_answer_stops_the_chain_but_is_not_cached(env):
    env.answers["serpapi"] = []
    assert E.web_search("q") == []
    assert env.calls == ["serper", "tavily", "serpapi"]
    assert not E.search_is_cached("q")


# ----------------------------- fix 3: exception log names the TYPE, no key
class _ConnErr(Exception):
    pass


def _raising_http(monkeypatch, msg):
    class _Boom:
        def post(self, url, **kw):
            raise _ConnErr(msg)

        def get(self, url, **kw):
            raise _ConnErr(msg)
    monkeypatch.setattr(E, "creq", _Boom())


def test_serpapi_exception_logs_type_only_never_the_key(http, monkeypatch):
    lines = []
    monkeypatch.setattr(E, "log", lambda *a, **k: lines.append(" ".join(map(str, a))))
    _raising_http(monkeypatch, "HTTPSConnectionPool: https://serpapi.com/"
                               "search.json?q=q&api_key=sa-key timed out")
    assert REAL_SERPAPI("q") is None
    assert REAL_SERPAPI("q") is None              # warn once
    blob = "\n".join(lines)
    assert "sa-key" not in blob and "api_key" not in blob
    assert len(lines) == 1
    assert "SerpApi" in lines[0] and "_ConnErr" in lines[0]


def test_tavily_exception_logs_type_only(http, monkeypatch):
    lines = []
    monkeypatch.setattr(E, "log", lambda *a, **k: lines.append(" ".join(map(str, a))))
    _raising_http(monkeypatch, "Bearer tv-key timed out")
    assert REAL_TAVILY("q") is None
    blob = "\n".join(lines)
    assert "tv-key" not in blob
    assert len(lines) == 1
    assert "Tavily" in lines[0] and "_ConnErr" in lines[0]


# --------------- fix 2: a "search engine failed" row stays eligible on re-run
FAILED_ROWS = [("WM-AAA111", ""),                                  # untouched
               ("WM-BBB222", "NOT FOUND - search engine failed"),
               ("WM-CCC333", "NEEDS LINK - NOT FOUND - search engine failed"),
               ("WM-DDD444", "NOT FOUND - no usable search results")]


def _run_main_over(tmp, flags):
    """Run main() in-process on a scratch list + filled sheet; return the
    SKUs lookup_sku was asked for. No network: lookup_sku is stubbed."""
    from openpyxl import Workbook
    import client_config
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_credits import _write_client                    # noqa: E402

    src = tmp / "SCRATCH.xlsx"
    wb = Workbook()
    wb.active.append(["SKU"])
    for sku, _ in FAILED_ROWS:
        wb.active.append([sku])
    wb.save(src)
    wb = Workbook()
    wb.active.append(["SKU", "url_link", "status"])
    for sku, status in FAILED_ROWS:
        wb.active.append([sku, "", status or None])
    wb.save(tmp / "SCRATCH_filled.xlsx")
    _write_client(tmp / "clients", "acme", api_key_env="ACME_SERPER",
                  monthly_budget=0, default_run_budget=0)

    asked = []

    def fake_lookup(sku, *a, **k):
        asked.append(sku)
        return {"status": "NOT FOUND - stub"}

    real = (client_config.CLIENTS_DIR, E.lookup_sku, E.polite_sleep, sys.argv)
    real_globals = {name: getattr(E, name) for name in
                    ("CATEGORY_CODES", "SERPER_KEY_ENV", "CACHE_DIR",
                     "CACHE_ENABLED", "CACHE_REFRESH", "CACHE_TTL_DAYS",
                     "SPECS_ENABLED", "IMAGE_CHECK", "IMAGE_MIN_PX",
                     "MERGE_BUDGET", "DISCOVER_MODE", "PW_ENABLED",
                     "SEARCH_ENGINES", "SEARCH_ORDER", "QUERY_MODE",
                     "TAVILY_KEY_ENV", "SERPAPI_KEY_ENV",
                     "URL_INDEX_ON", "SITE_SEARCH_ON")
                    if hasattr(E, name)}
    client_config.CLIENTS_DIR = tmp / "clients"
    E.lookup_sku = fake_lookup
    E.polite_sleep = lambda *a, **k: None
    sys.argv = (["sku_enricher.py", str(src), "--client", "acme",
                 "--no-cache", "--no-url-index",
                 "--cache-dir", str(tmp / "cache"), "--delay", "0", "0"]
                + list(flags))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            E.main()
    finally:
        (client_config.CLIENTS_DIR, E.lookup_sku, E.polite_sleep,
         sys.argv) = real
        for name, value in real_globals.items():
            setattr(E, name, value)
        E.RUN_STATS["stopped_by"] = "done"
        E.RUN_STATS["client"] = client_config.DEFAULT_CLIENT
    return asked


def test_plain_rerun_retries_search_engine_failed_rows(tmp_path):
    assert _run_main_over(tmp_path, []) == ["WM-AAA111", "WM-BBB222",
                                            "WM-CCC333"]


def test_discover_rerun_retries_search_engine_failed_rows(tmp_path):
    assert _run_main_over(tmp_path, ["--discover"]) == [
        "WM-AAA111", "WM-BBB222", "WM-CCC333"]


def test_gui_eligible_rows_counts_search_engine_failed_rows():
    import pandas as pd
    import gui_helpers
    assert gui_helpers.SEARCH_FAILED_STATUS == E.SEARCH_FAILED_STATUS
    df = pd.DataFrame([{"sku": s, "url_link": "", "status": st,
                        "sale price": "", "image_url": ""}
                       for s, st in FAILED_ROWS])
    assert gui_helpers.eligible_rows(df, []) == 3
    assert gui_helpers.eligible_rows(df, ["--discover"]) == 3
    # substring match: a suffix after the status keeps the row eligible
    df.loc[1, "status"] = "NOT FOUND - search engine failed (retry 2)"
    assert gui_helpers.eligible_rows(df, []) == 3
