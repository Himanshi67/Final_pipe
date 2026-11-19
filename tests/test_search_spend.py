r"""Paid-search spend in sku_enricher.lookup_sku / web_search.

Fully OFFLINE: serper_search, ddg_search, fetch and extract_page are stubbed,
the search cache lives in a temp folder, and assess() is the real one. No
serper credit and no DuckDuckGo request is spent.

Covers: [serper] query_mode (incremental vs batch), the duplicate-query skip
rules (croma site search, fresh brand sitemap), [serper] search_order
(paid_first vs free_first), the --budget gate in both modes, and the
empty-URL-index warning.

    .venv\Scripts\python.exe -m pytest -q tests\test_search_spend.py
"""
import contextlib
import io
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import client_config                                          # noqa: E402
import sku_enricher as E                                      # noqa: E402
import url_index as U                                         # noqa: E402

QUIET = lambda *a, **k: None                                  # noqa: E731
SKU = "LG-GL-N292DDSY"          # brand lg, model GL-N292DDSY: 5 tier-1 queries
AMZ = "https://www.amazon.in/LG-Frost-Free-Refrigerator-GL-N292DDSY/dp/B0C1234567"
FK = "https://www.flipkart.com/lg-242-l-frost-free-gl-n292ddsy/p/itm0123456789ab"
CROMA = "https://www.croma.com/lg-242-litres-gl-n292ddsy/p/123456"
TITLE = "LG 242 L Frost Free Double Door Refrigerator GL-N292DDSY"
STATS = ("serper_calls", "ddg_calls", "queries_skipped_dup",
         "queries_skipped_early", "index_lookups", "index_candidates",
         "rows_by_index", "sitesearch_calls", "rows_by_sitesearch",
         "rows_web_search")


def rec(url, price="₹24,990", n_img=4):
    return {"title": TITLE, "sale price": price, "regular price": "₹29,990",
            "url_link": url, "specs": "",
            "image_url": ",".join(f"https://img.example/{i}.jpg"
                                  for i in range(n_img))}


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
    for k in STATS:
        monkeypatch.setitem(E.RUN_STATS, k, 0)

    calls, serp, ddg, pages = [], {}, {}, {}

    def fake_serper(q):
        if q in serp.get("__dead__", ()):
            return None
        calls.append(("serper", q))
        E.RUN_STATS["serper_calls"] += 1          # what the real one does
        return [{"href": u, "title": TITLE} for u in serp.get(q, [])]

    def fake_ddg(q):
        calls.append(("ddg", q))
        E.RUN_STATS["ddg_calls"] += 1
        return [{"href": u, "title": TITLE} for u in ddg.get(q, [])]

    monkeypatch.setattr(E, "serper_search", fake_serper)
    monkeypatch.setattr(E, "ddg_search", fake_ddg)
    monkeypatch.setattr(E, "fetch", lambda u: (
        (f"<html>{u}</html>", "") if u in pages else (None, "http 404")))
    monkeypatch.setattr(E, "extract_page", lambda u, html: dict(pages[u]))

    def lookup(sku=SKU):
        with contextlib.redirect_stdout(io.StringIO()):
            return E.lookup_sku(sku)

    def serper_queries():
        return [q for p, q in calls if p == "serper"]

    return SimpleNamespace(calls=calls, serp=serp, ddg=ddg, pages=pages,
                           lookup=lookup, serper_queries=serper_queries,
                           monkeypatch=monkeypatch)


def queries(sku=SKU):
    return [q for tier in E.build_queries(sku)[2] for q, _ in tier]


def reset(env):
    env.calls.clear()
    for k in STATS:
        E.RUN_STATS[k] = 0


# ------------------------------------------------------------ (a) + (b)
def test_a_product_on_query_1_costs_exactly_one_credit(env):
    q1 = queries()[0]
    assert "site:amazon.in" in q1
    env.serp[q1] = [AMZ]
    env.pages[E.canonical_url(AMZ)] = rec(E.canonical_url(AMZ))
    out = env.lookup()
    assert out["status"].startswith("FOUND (amazon.in)")
    assert env.serper_queries() == [q1]
    assert E.RUN_STATS["serper_calls"] == 1
    assert E.RUN_STATS["queries_skipped_early"] == len(queries()) - 1

    # batch mode (cold cache again) pays for the whole tier: invariant B
    env.monkeypatch.setattr(E, "CACHE_ENABLED", False)
    env.monkeypatch.setattr(E, "QUERY_MODE", "batch")
    reset(env)
    assert env.lookup()["status"].startswith("FOUND (amazon.in)")
    assert E.RUN_STATS["serper_calls"] == len(queries()) == 5


def test_discover_mode_is_incremental_too(env):
    env.monkeypatch.setattr(E, "DISCOVER_MODE", True)
    qs = queries()
    assert len(qs) == 3                        # amazon, flipkart, generic
    env.serp[qs[1]] = [FK]
    env.pages[E.canonical_url(FK)] = rec(E.canonical_url(FK))
    assert env.lookup()["status"].startswith("FOUND (flipkart.com)")
    assert env.serper_queries() == qs[:2]


def test_b_nothing_found_sends_what_batch_sends(env):
    env.monkeypatch.setattr(E, "CACHE_ENABLED", False)   # every run cold
    out = env.lookup()
    assert out["status"].startswith("NOT FOUND")
    incremental = env.serper_queries()
    env.monkeypatch.setattr(E, "QUERY_MODE", "batch")
    reset(env)
    env.lookup()
    assert incremental == env.serper_queries() == queries()


def test_b_nothing_found_minus_the_skipped_duplicate(env):
    env.monkeypatch.setattr(E, "CACHE_ENABLED", False)
    croma_rule(env, state="ok", pairs=[])
    env.lookup()
    sent = env.serper_queries()
    assert len(sent) == len(queries()) - 1
    assert E.RUN_STATS["queries_skipped_dup"] == 1


def test_fetch_budget_spent_stops_paid_queries(env):
    """Every query returns pages that fail verification: once the SKU's
    page-fetch budget is spent, no further paid query is sent."""
    env.monkeypatch.setattr(E, "FETCH_BUDGET", 2)
    qs = queries()
    for q, u in ((qs[0], AMZ), (qs[1], FK)):
        env.serp[q] = [u]
        cu = E.canonical_url(u)
        env.pages[cu] = dict(rec(cu), title="Samsung 253 L Refrigerator RT28C3053S8")
    env.lookup()
    assert len(env.serper_queries()) == 2
    assert E.RUN_STATS["queries_skipped_early"] == len(qs) - 2


# ------------------------------------------------------------------ (c)
def test_c_all_cached_output_identical_and_free(env):
    qs = queries()
    # several candidates over several queries, so order and winner matter
    env.serp[qs[0]] = [AMZ]
    env.serp[qs[1]] = [FK]
    env.serp[qs[2]] = [CROMA]
    env.pages[E.canonical_url(AMZ)] = rec(E.canonical_url(AMZ), price="")
    env.pages[E.canonical_url(FK)] = rec(E.canonical_url(FK), n_img=1)
    env.pages[E.canonical_url(CROMA)] = rec(E.canonical_url(CROMA), n_img=2)
    env.monkeypatch.setattr(E, "QUERY_MODE", "batch")
    first = env.lookup()                       # cold: fills the cache
    assert all(E.search_is_cached(q) for q in qs)

    def forbidden(q):
        raise AssertionError("a provider was asked on a warm cache")
    env.monkeypatch.setattr(E, "serper_search", forbidden)
    env.monkeypatch.setattr(E, "ddg_search", forbidden)
    reset(env)
    batch = env.lookup()
    env.monkeypatch.setattr(E, "QUERY_MODE", "incremental")
    reset(env)
    incremental = env.lookup()
    assert batch == incremental == first
    assert E.RUN_STATS["serper_calls"] == 0
    assert E.RUN_STATS["queries_skipped_early"] == 0


# ------------------------------------------------------------------ (d)
def croma_rule(env, state, pairs):
    rule = U.SiteSearchRule(
        name="croma",
        url_template="https://api.croma.com/searchservices/v1/search?query={q}")
    env.monkeypatch.setattr(E, "SITE_SEARCH_ON", True)
    env.monkeypatch.setattr(E, "SITE_SEARCH_RULES", [rule])

    def fake_site_search(r, model):
        E._SITE_SEARCH_OUTCOME["state"] = state
        return list(pairs)
    env.monkeypatch.setattr(E, "site_search", fake_site_search)


def test_d_croma_site_search_empty_drops_the_croma_query(env):
    croma_rule(env, state="ok", pairs=[])
    env.lookup()
    assert env.serper_queries()
    assert not any("site:croma.com" in q for q in env.serper_queries())
    assert E.RUN_STATS["queries_skipped_dup"] == 1


@pytest.mark.parametrize("state", ["error", "off"])
def test_d_croma_site_search_error_keeps_the_query(env, state):
    croma_rule(env, state=state, pairs=[])
    env.lookup()
    assert any("site:croma.com" in q for q in env.serper_queries())
    assert E.RUN_STATS["queries_skipped_dup"] == 0


def test_d_cached_croma_query_is_still_read(env):
    """A CACHED croma query costs nothing: it is read, never skipped, so a
    warm-cache replay is not changed by the skip rule."""
    q = next(q for q in queries() if "site:croma.com" in q)
    E.cache_put("search", q, [])
    croma_rule(env, state="ok", pairs=[])
    env.lookup()
    assert E.RUN_STATS["queries_skipped_dup"] == 0


# ------------------------------------------------------------------ (e)
def brand_index(env, last_refresh, url_count=1800, hits=()):
    src = U.Source(name="lg", root="https://www.lg.com/in/sitemap.xml",
                   brand="lg")
    env.monkeypatch.setattr(E, "URL_INDEX_ON", True)
    env.monkeypatch.setattr(E, "URL_INDEX_SOURCES", [src])
    env.monkeypatch.setattr(U, "stats", lambda *a, **k: [
        {"name": "lg", "url_count": url_count, "last_refresh": last_refresh,
         "status": "ok"}])
    env.monkeypatch.setattr(U, "lookup", lambda *a, **k: list(hits))
    with contextlib.redirect_stdout(io.StringIO()):
        E.index_health([src], 7)


def test_e_fresh_brand_sitemap_without_a_hit_drops_the_brand_query(env):
    brand_index(env, last_refresh=time.time() - 3600)
    assert "lg.com" in E.INDEX_FRESH_DOMAINS
    env.lookup()
    assert env.serper_queries()
    assert not any("site:lg.com" in q for q in env.serper_queries())
    assert E.RUN_STATS["queries_skipped_dup"] == 1


def test_e_stale_brand_sitemap_keeps_the_brand_query(env):
    brand_index(env, last_refresh=time.time() - 30 * 86400)
    assert not E.INDEX_FRESH_DOMAINS
    env.lookup()
    assert any("site:lg.com" in q for q in env.serper_queries())
    assert E.RUN_STATS["queries_skipped_dup"] == 0


def test_e_empty_brand_source_or_lookup_error_keeps_the_query(env):
    env.monkeypatch.setattr(E, "CACHE_ENABLED", False)   # both runs cold
    brand_index(env, last_refresh=time.time(), url_count=0)
    env.lookup()
    assert any("site:lg.com" in q for q in env.serper_queries())

    brand_index(env, last_refresh=time.time())

    def boom(*a, **k):
        raise RuntimeError("db locked")
    env.monkeypatch.setattr(U, "lookup", boom)
    reset(env)
    env.lookup()
    assert any("site:lg.com" in q for q in env.serper_queries())


# ------------------------------------------------------------------ (f)
def test_f_free_first_asks_ddg_first_and_skips_serper_when_it_answers(env):
    env.monkeypatch.setattr(E, "SEARCH_ORDER", "free_first")
    env.ddg["q one"] = [AMZ]
    res = E.web_search("q one")
    assert [p for p, _ in env.calls] == ["ddg"]
    assert res and res[0]["href"] == AMZ
    assert E.RUN_STATS["serper_calls"] == 0
    # cached under the bare query: the next read is free in either order
    env.monkeypatch.setattr(E, "SEARCH_ORDER", "paid_first")
    env.calls.clear()
    assert E.web_search("q one") == res and env.calls == []


def test_f_free_first_falls_to_serper_when_ddg_gives_nothing(env):
    env.monkeypatch.setattr(E, "SEARCH_ORDER", "free_first")
    env.serp["q two"] = [FK]
    res = E.web_search("q two")
    assert [p for p, _ in env.calls] == ["ddg", "serper"]
    assert res[0]["href"] == FK
    assert E.search_is_cached("q two")


def test_paid_first_is_unchanged(env):
    env.serp["q three"] = [FK]
    E.web_search("q three")
    assert [p for p, _ in env.calls] == ["serper"]


# ------------------------------------------------------------------ (g)
@pytest.mark.parametrize("mode", ["incremental", "batch"])
@pytest.mark.parametrize("order", ["paid_first", "free_first"])
def test_g_budget_gate_refuses_serper_past_the_budget(env, mode, order):
    env.monkeypatch.setattr(E, "QUERY_MODE", mode)
    env.monkeypatch.setattr(E, "SEARCH_ORDER", order)
    env.monkeypatch.setattr(E, "SERPER_BUDGET", 2)
    env.lookup()                               # nothing found: 5 queries
    assert E.RUN_STATS["serper_calls"] == 2


# ------------------------------------------------------------------ (h)
def test_h_empty_index_warning_names_the_fix(env, capsys):
    src = U.Source(name="lg", root="https://www.lg.com/in/sitemap.xml")
    env.monkeypatch.setattr(U, "stats", lambda *a, **k: [])
    E.index_health([src], 7)
    out = capsys.readouterr().out
    assert "URL index is EMPTY" in out
    assert r".venv\Scripts\python.exe url_index.py refresh" in out

    env.monkeypatch.setattr(U, "stats", lambda *a, **k: [
        {"name": "lg", "url_count": 0, "last_refresh": time.time()}])
    E.index_health([src], 7)
    assert "URL index is EMPTY" in capsys.readouterr().out

    env.monkeypatch.setattr(U, "stats", lambda *a, **k: [
        {"name": "lg", "url_count": 10, "last_refresh": time.time()}])
    E.index_health([src], 7)
    assert "EMPTY" not in capsys.readouterr().out


# ------------------------------------------------------------- config
def test_client_toml_serper_modes_are_read_and_validated(tmp_path):
    cfg = client_config.load_client("shreejee")
    # the live client.toml is owner-tunable: check the values are legal,
    # never which one the owner picked
    assert cfg.serper.query_mode in client_config.SERPER_QUERY_MODES
    assert cfg.serper.search_order in client_config.SERPER_SEARCH_ORDERS
    where = "test"
    got = client_config._load_serper(
        {"serper": {"query_mode": "Batch", "search_order": "free_first"}}, where)
    assert (got.query_mode, got.search_order) == ("batch", "free_first")
    assert client_config._load_serper({}, where).query_mode == "incremental"
    with pytest.raises(client_config.ClientConfigError):
        client_config._load_serper({"serper": {"query_mode": "lazy"}}, where)
    with pytest.raises(client_config.ClientConfigError):
        client_config._load_serper({"serper": {"search_order": "ddg"}}, where)
