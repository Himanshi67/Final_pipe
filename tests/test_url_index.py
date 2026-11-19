r"""Tier 0 of the URL search - url_index.py and its wiring in sku_enricher.

Fully OFFLINE: every HTTP answer comes from tests\fixtures\url_index\ through
an injected transport, the index lives in a temp SQLite file, and the page
fetch / extract steps of lookup_sku are stubbed. assess() is NEVER stubbed:
an index or site-search hit has to pass the same verdict as a search result.

    .venv\Scripts\python.exe -m pytest -q tests\test_url_index.py
    .venv\Scripts\python.exe tests\test_url_index.py
"""
import argparse
import contextlib
import dataclasses
import gzip
import io
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import client_config                                          # noqa: E402
import sku_enricher as E                                      # noqa: E402
import url_index as U                                         # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:                                   # pragma: no cover
    import tomli as tomllib

FIX = ROOT / "tests" / "fixtures" / "url_index"
HOST = "https://shop.example.in"
QUIET = lambda *a, **k: None                                  # noqa: E731


# ------------------------------------------------------------------ helpers
class FakeHTTP:
    """url -> Response (or a callable). Records every call with its headers."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers or {})))
        r = self.routes.get(url)
        if r is None:
            return U.Response(status=404, body=b"not found")
        return r(url, headers) if callable(r) else r

    def urls(self):
        return [u for u, _ in self.calls]


def ok(body, ctype="application/xml", **hdr):
    h = {"content-type": ctype}
    h.update(hdr)
    return U.Response(status=200, headers=h, body=body)


def site_routes(index_body=None, products2=None):
    return {
        f"{HOST}/robots.txt": ok((FIX / "robots.txt").read_bytes(), "text/plain"),
        f"{HOST}/sitemap_index.xml": ok(
            index_body or (FIX / "sitemap_index.xml").read_bytes(), etag='"v1"'),
        # a real .xml.gz, served as application/x-download
        f"{HOST}/products-1.xml.gz": ok((FIX / "products-1.xml.gz").read_bytes(),
                                        "application/x-download"),
        # a plain .xml name whose body is gzipped: only the magic bytes tell
        f"{HOST}/products-2.xml": ok(gzip.compress(
            products2 or (FIX / "products-2.xml").read_bytes()),
            "application/octet-stream"),
        f"{HOST}/blog-sitemap.xml": ok(
            b"<urlset><url><loc>https://shop.example.in/blog/x/p/1</loc></url></urlset>"),
        f"{HOST}/private/products-3.xml": ok(
            b"<urlset><url><loc>https://shop.example.in/secret/p/9</loc></url></urlset>"),
    }


def make_fetcher(routes):
    http, sleeps = FakeHTTP(routes), []
    return U.Fetcher(http=http, sleep=sleeps.append, log=QUIET), http, sleeps


def shop(**kw):
    base = dict(name="shop", root=f"{HOST}/sitemap_index.xml",
                child_pattern=r"products-\d", url_pattern=r"/p/\d+$")
    base.update(kw)
    return U.Source(**base)


def db_urls(db):
    conn = sqlite3.connect(str(db))
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT url, seen_at FROM urls")}
    finally:
        conn.close()


def build_db(path, urls):
    """[(url, source)] straight into an index file."""
    conn = U.connect(path)
    U._upsert(conn, [(u, s, U.domain_of(u), U.slug_norm(u), "", 1.0, "sm")
                     for u, s in urls])
    conn.commit()
    conn.close()
    return path


# ----------------------------------------------------------- normalization
def test_normalization_is_shared_and_covers_live_slug_styles():
    assert U.norm_code("rr21h2h24uz-hl") == U.norm_code("RR21H2H24UZ/HL") == "RR21H2H24UZHL"
    assert U.norm_code("GL N292.DDSY") == "GLN292DDSY"
    # percent-encoded spaces are decoded first - '%20' must not add '20'
    slug = U.slug_norm(f"{HOST}/godrej-rt%20eonvalor%20280b/p/1002?ref=x#top")
    assert "RTEONVALOR280B" in slug and "20EONVALOR" not in slug
    assert "REF" not in slug                       # the query is not the slug


# ----------------------------------------------------------------- refresh
def test_refresh_reads_index_gz_child_and_urlset(tmp_path):
    f, http, sleeps = make_fetcher(site_routes())
    db = tmp_path / "i.sqlite"
    res = U.refresh([shop()], db_path=db, fetcher=f, log=QUIET, now=1000.0)
    assert res[0]["status"] == "ok", res
    urls = db_urls(db)
    assert f"{HOST}/lg-55-inch-4k-tv-55uq7500psf/p/2001" in urls      # .xml.gz child
    assert f"{HOST}/samsung-7kg-washer-ww70t4020ce-tl/p/1001" in urls  # gzipped .xml
    assert f"{HOST}/about-us" not in urls                              # url_pattern
    assert f"{HOST}/blog/x/p/1" not in urls
    assert f"{HOST}/blog-sitemap.xml" not in http.urls()               # child_pattern
    assert res[0]["urls"] == 6


def test_robots_disallow_and_crawl_delay_are_obeyed(tmp_path):
    f, http, sleeps = make_fetcher(site_routes())
    res = U.refresh([shop()], db_path=tmp_path / "i.sqlite", fetcher=f,
                    log=QUIET, now=1000.0)
    assert f"{HOST}/private/products-3.xml" not in http.urls()
    assert res[0]["disallowed"] == 1
    assert f"{HOST}/secret/p/9" not in db_urls(tmp_path / "i.sqlite")
    # robots.txt says Crawl-delay: 5 - every request after the first waited
    assert len(sleeps) == len(http.calls) - 1
    assert sleeps and min(sleeps) >= 4.5


def test_robots_unreachable_means_disallow_and_the_source_fails_softly(tmp_path):
    routes = site_routes()
    routes[f"{HOST}/robots.txt"] = U.Response(status=503, body=b"busy")
    f, http, _ = make_fetcher(routes)
    logs = []
    res = U.refresh([shop()], db_path=tmp_path / "i.sqlite", fetcher=f,
                    log=logs.append, now=1000.0)
    assert res[0]["status"] == "failed"
    assert http.urls() == [f"{HOST}/robots.txt"]          # no sitemap touched
    assert len(logs) == 1 and "FAILED" in logs[0]


def test_incremental_lastmod_skip_keeps_urls_and_sends_conditional_headers(tmp_path):
    db = tmp_path / "i.sqlite"
    f, _, _ = make_fetcher(site_routes())
    U.refresh([shop()], db_path=db, fetcher=f, log=QUIET, now=1000.0)

    f2, http2, _ = make_fetcher(site_routes())
    res = U.refresh([shop()], db_path=db, fetcher=f2, log=QUIET, now=2000.0)[0]
    assert f"{HOST}/products-1.xml.gz" not in http2.urls()
    assert f"{HOST}/products-2.xml" not in http2.urls()
    assert res["skipped"] == 2 and res["pruned"] == 0
    root_hdrs = dict(http2.calls)[f"{HOST}/sitemap_index.xml"]
    assert root_hdrs.get("If-None-Match") == '"v1"'
    urls = db_urls(db)
    assert len(urls) == 6 and set(urls.values()) == {2000.0}   # all vouched for

    # a 304 on the ROOT index vouches for every child below it too
    routes = site_routes()
    routes[f"{HOST}/sitemap_index.xml"] = U.Response(status=304)
    f3, http3, _ = make_fetcher(routes)
    res = U.refresh([shop()], db_path=db, fetcher=f3, log=QUIET, now=3000.0)[0]
    assert res["status"] == "ok" and res["pruned"] == 0
    assert set(db_urls(db).values()) == {3000.0}

    # --force re-reads everything
    f4, http4, _ = make_fetcher(site_routes())
    U.refresh([shop()], db_path=db, fetcher=f4, log=QUIET, now=4000.0, force=True)
    assert f"{HOST}/products-1.xml.gz" in http4.urls()
    assert "If-None-Match" not in dict(http4.calls)[f"{HOST}/sitemap_index.xml"]


def test_prune_drops_urls_gone_from_a_changed_sitemap(tmp_path):
    db = tmp_path / "i.sqlite"
    f, _, _ = make_fetcher(site_routes())
    U.refresh([shop()], db_path=db, fetcher=f, log=QUIET, now=1000.0)
    gone = f"{HOST}/samsung-7kg-washer-ww70t4020ce-tl/p/1001"
    assert gone in db_urls(db)

    index2 = (FIX / "sitemap_index.xml").read_bytes().replace(
        b"<lastmod>2026-11-02</lastmod>", b"<lastmod>2026-11-10</lastmod>")
    products2 = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://shop.example.in/lg-gl-n292ddsy-fridge/p/1003</loc></url>
    </urlset>"""
    f2, http2, _ = make_fetcher(site_routes(index2, products2))
    res = U.refresh([shop()], db_path=db, fetcher=f2, log=QUIET, now=2000.0)[0]
    urls = db_urls(db)
    assert gone not in urls and f"{HOST}/lg-gl-n292ddsy-fridge/p/1003" in urls
    assert f"{HOST}/lg-55-inch-4k-tv-55uq7500psf/p/2001" in urls   # unchanged child kept
    assert res["pruned"] == 2


def test_failed_refresh_keeps_old_urls_and_never_raises(tmp_path):
    db = tmp_path / "i.sqlite"
    f, _, _ = make_fetcher(site_routes())
    U.refresh([shop()], db_path=db, fetcher=f, log=QUIET, now=1000.0)
    routes = site_routes()
    routes[f"{HOST}/sitemap_index.xml"] = U.Response(status=500)
    f2, _, _ = make_fetcher(routes)
    res = U.refresh([shop()], db_path=db, fetcher=f2, log=QUIET, now=2000.0)[0]
    assert res["status"] == "failed"
    assert len(db_urls(db)) == 6
    row = sqlite3.connect(str(db)).execute(
        "SELECT status, last_refresh FROM sources WHERE name='shop'").fetchone()
    assert row[0].startswith("failed") and row[1] == 1000.0

    def boom(url, headers):
        raise RuntimeError("transport exploded")
    f3 = U.Fetcher(http=boom, sleep=QUIET, log=QUIET)
    res = U.refresh([shop()], db_path=db, fetcher=f3, log=QUIET, now=3000.0)[0]
    assert res["status"] == "failed"


def test_stale_detection(tmp_path):
    db = tmp_path / "i.sqlite"
    srcs = [shop(), shop(name="dead", root=f"{HOST}/dead.xml")]
    assert U.stale_sources(srcs, 7, db_path=db) == ["shop", "dead"]   # no DB yet
    assert not db.exists()                                # lookup never creates it
    f, _, _ = make_fetcher(site_routes())
    U.refresh(srcs, db_path=db, fetcher=f, log=QUIET, now=1000.0)
    assert U.stale_sources(srcs, 7, db_path=db, now=1000 + 3600) == []
    later = 1000 + 13 * 3600           # failed source: retried after 12 h only
    assert U.stale_sources(srcs, 7, db_path=db, now=later) == ["dead"]
    assert U.stale_sources(srcs, 7, db_path=db, now=1000 + 8 * 86400) == ["shop", "dead"]


# ------------------------------------------------------------------ lookup
def test_lookup_matches_normalized_codes(tmp_path):
    db = build_db(tmp_path / "l.sqlite", [
        (f"{HOST}/samsung-fridge-rr21h2h24uz-hl/p/2002", "shop"),
        (f"{HOST}/lg-gl-n292ddsy-fridge/p/1003", "shop")])
    hits = U.lookup("RR21H2H24UZ/HL", "SAMSUNG", db_path=db)
    assert [h["url"] for h in hits] == [f"{HOST}/samsung-fridge-rr21h2h24uz-hl/p/2002"]
    assert "rr21h2h24uz" in hits[0]["title"]
    assert U.lookup("GL-N292DDSY", "", db_path=db)
    assert U.lookup("XX", "lg", db_path=db) == []          # too short to mean anything


def test_short_or_numeric_code_needs_the_brand(tmp_path):
    db = build_db(tmp_path / "l.sqlite", [
        (f"{HOST}/haier-washer-w600/p/2003", "shop"),
        (f"{HOST}/washer-w600/p/2004", "shop"),
        (f"{HOST}/haier-washer-w6001/p/2005", "shop"),
        ("https://www.haier.com/in/washing-machines/w600.shtml", "haier"),
        (f"{HOST}/fan-123456/p/7", "shop")])
    haier = U.Source(name="haier", root="https://www.haier.com/in/sitemap.xml",
                     brand="haier")
    hits = {h["url"] for h in U.lookup("W600", "HAIER", db_path=db,
                                       sources=[shop(), haier])}
    assert hits == {f"{HOST}/haier-washer-w600/p/2003",              # brand in slug
                    "https://www.haier.com/in/washing-machines/w600.shtml"}  # brand site
    assert U.lookup("W600", "", db_path=db) == []                    # no brand: no match
    assert U.lookup("123456", "", db_path=db) == []                  # numeric needs brand


def test_lookup_ranks_by_domain_rank_and_caps(tmp_path):
    urls = []
    for dom, n in (("www.reliancedigital.in", 3), ("www.vijaysales.com", 3),
                   ("www.lg.com", 2), ("www.poorvika.com", 3)):
        urls += [(f"https://{dom}/lg-55uq7500psf-{i}/p/{i}", dom) for i in range(n)]
    db = build_db(tmp_path / "l.sqlite", urls)
    hits = U.lookup("55UQ7500PSF", "LG", db_path=db, rank=E.domain_rank)
    assert len(hits) == U.MAX_CANDIDATES
    per = {}
    for h in hits:
        per[h["domain"]] = per.get(h["domain"], 0) + 1
    assert max(per.values()) <= U.PER_DOMAIN
    assert hits[0]["domain"] == "reliancedigital.in"
    ranks = [E.domain_rank(h["url"]) for h in hits]
    assert ranks == sorted(ranks)


def test_disabled_source_and_like_fallback(tmp_path):
    db = build_db(tmp_path / "l.sqlite", [
        (f"{HOST}/lg-gl-n292ddsy-fridge/p/1003", "shop"),
        ("https://old.example.in/lg-gl-n292ddsy/p/1", "old")])
    hits = U.lookup("GL-N292DDSY", "LG", db_path=db, sources=[shop()])
    assert [h["source"] for h in hits] == ["shop"]
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE meta SET value='0' WHERE key='fts'")        # no FTS5
    conn.commit()
    conn.close()
    assert [h["url"] for h in U.lookup("GL-N292DDSY", "LG", db_path=db,
                                       sources=[shop()])] == [hits[0]["url"]]


# -------------------------------------------------------- site search parse
def _rules():
    data = tomllib.loads((ROOT / "url_sources.toml").read_text(encoding="utf-8"))
    return {r["name"]: U._make_rule({**r, "enabled": True}, "t")
            for r in data["site_search"]}


def test_site_search_extract_filters_on_the_model_code():
    rules = _rules()
    rel = U.extract_site_search(rules["reliancedigital"],
                                (FIX / "reliance_search.json").read_text(), "GL-N292DDSY")
    assert [u for u, _ in rel] == [
        "https://www.reliancedigital.in/product/lg-242-l-frost-free-gl-n292ddsy-mnzpxj-10018481",
        "https://www.reliancedigital.in/product/lg-242-l-refrigerator-exact-field-only-10018482"]
    cro = U.extract_site_search(rules["croma"],
                                (FIX / "croma_search.json").read_text(), "GL-N292DDSY")
    assert [u for u, _ in cro] == ["https://www.croma.com/lg-242-litres-gl-n292ddsy/p/300002"]
    assert "%3Arelevance" in U.build_search_url(rules["croma"], "GL N292")
    assert "GL%20N292" in U.build_search_url(rules["croma"], "GL N292")


# ------------------------------------------------------- tier 0 integration
SKU = "LG-GL-N292DDSY"
GOOD = "https://www.reliancedigital.in/product/lg-242-l-gl-n292ddsy-10018481"


def rec(url, title, price="₹24,990", n_img=4):
    return {"title": title, "sale price": price, "regular price": "₹29,990",
            "url_link": url, "specs": "",
            "image_url": ",".join(f"https://img.example/{i}.jpg" for i in range(n_img))}


@pytest.fixture
def tier0(monkeypatch, tmp_path):
    """lookup_sku with the index ON over a temp DB, fetch/extract stubbed,
    web_search recorded. assess() is the real one."""
    monkeypatch.setattr(E, "IMAGE_CHECK", False)
    monkeypatch.setattr(E, "PW_ENABLED", False)
    monkeypatch.setattr(E, "MERGE_BUDGET", 0)
    monkeypatch.setattr(E, "polite_sleep", QUIET)
    monkeypatch.setattr(E, "URL_INDEX_ON", True)
    monkeypatch.setattr(E, "SITE_SEARCH_ON", False)
    monkeypatch.setattr(E, "SITE_SEARCH_RULES", [])
    monkeypatch.setattr(E, "_COOLDOWN", {})
    for k in ("index_lookups", "index_candidates", "rows_by_index",
              "sitesearch_calls", "rows_by_sitesearch", "rows_web_search"):
        monkeypatch.setitem(E.RUN_STATS, k, 0)
    db = tmp_path / "idx.sqlite"
    monkeypatch.setattr(U, "DB_PATH", db)
    src = U.Source(name="rd", root="https://www.reliancedigital.in/sitemap.xml")
    monkeypatch.setattr(E, "URL_INDEX_SOURCES", [src])
    pages, searched, search_results = {}, [], []

    def fake_fetch(u):
        return (f"<html>{u}</html>", "") if u in pages else (None, "http 404")

    monkeypatch.setattr(E, "fetch", fake_fetch)
    monkeypatch.setattr(E, "extract_page", lambda u, html: dict(pages[u]))

    def fake_search(q):
        searched.append(q)
        return list(search_results)

    monkeypatch.setattr(E, "web_search", fake_search)

    def index(*urls):
        build_db(db, [(u, "rd") for u in urls])

    return SimpleNamespace(pages=pages, searched=searched, index=index,
                           search_results=search_results)


def quiet_lookup(sku=SKU):
    with contextlib.redirect_stdout(io.StringIO()):
        return E.lookup_sku(sku)


def test_complete_index_winner_spends_no_search(tier0):
    tier0.index(GOOD)
    tier0.pages[GOOD] = rec(GOOD, "LG 242 L Frost Free Refrigerator GL-N292DDSY")
    out = quiet_lookup()
    assert out["status"] == "FOUND (reliancedigital.in)"
    assert tier0.searched == []
    assert E.RUN_STATS["rows_by_index"] == 1
    assert E.RUN_STATS["index_candidates"] == 1
    assert E.RUN_STATS["rows_web_search"] == 0


def test_nothing_verified_falls_through_to_the_search_tiers(tier0):
    tier0.index(GOOD)
    tier0.pages[GOOD] = rec(GOOD, "Samsung 253 L Refrigerator RT28C3053S8")
    out = quiet_lookup()
    assert tier0.searched, "the normal search tiers must run"
    assert E.RUN_STATS["rows_web_search"] == 1
    assert E.RUN_STATS["rows_by_index"] == 0
    assert out["status"].startswith("NOT FOUND")


def test_verified_but_incomplete_skips_the_search_tiers(tier0):
    tier0.index(GOOD)
    tier0.pages[GOOD] = rec(GOOD, "LG 242 L Frost Free Refrigerator GL-N292DDSY",
                            price="")
    out = quiet_lookup()
    assert out["status"].startswith("FOUND (reliancedigital.in) - no price shown")
    assert tier0.searched == []
    assert E.RUN_STATS["rows_by_index"] == 1


def test_wrong_brand_page_behind_a_matching_slug_is_rejected(tier0):
    """The index finds URLs BECAUSE the code is in the slug. A page that is
    another brand's product must still be rejected by assess()."""
    tier0.index(GOOD)
    tier0.pages[GOOD] = rec(GOOD, "Samsung 253 L Double Door Refrigerator RT28C3053S8")
    out = quiet_lookup()
    assert not out["status"].startswith("FOUND")
    assert tier0.searched                       # fell through to search


def test_index_hit_gets_exactly_the_verdict_a_search_hit_gets(tier0, monkeypatch):
    """docs/NEXT_STEPS.md: assess() calls a page 'strong' when the code is in
    the URL and the title starts with the brand. An index hit always has the
    code in its slug - so prove the index path changes NOTHING: the same URL
    and page give the same status via the index as via a search result."""
    wrong = "https://www.reliancedigital.in/product/lg-gl-n292ddsy-vs-gl-s292rpzy-1"
    page = rec(wrong, "LG 260 L Frost Free Refrigerator GL-S292RPZY")
    tier0.pages[wrong] = page
    tier0.index(wrong)
    via_index = quiet_lookup()["status"]

    monkeypatch.setattr(E, "URL_INDEX_ON", False)
    tier0.search_results.append({"href": wrong, "title": page["title"]})
    via_search = quiet_lookup()["status"]
    assert via_index == via_search


def test_site_search_resolves_when_the_index_has_nothing(tier0, monkeypatch):
    rule = _rules()["reliancedigital"]
    monkeypatch.setattr(E, "SITE_SEARCH_ON", True)
    monkeypatch.setattr(E, "SITE_SEARCH_RULES", [rule])
    monkeypatch.setattr(E, "site_search", lambda r, m: [(GOOD, "LG GL-N292DDSY")])
    tier0.pages[GOOD] = rec(GOOD, "LG 242 L Frost Free Refrigerator GL-N292DDSY")
    out = quiet_lookup()
    assert out["status"] == "FOUND (reliancedigital.in)"
    assert tier0.searched == []
    assert E.RUN_STATS["rows_by_sitesearch"] == 1


def test_crawl_urls_never_touches_tier_0(tier0, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("tier 0 ran during --crawl-urls")
    monkeypatch.setattr(E, "SITE_SEARCH_ON", True)
    monkeypatch.setattr(E, "index_candidates", forbidden)
    monkeypatch.setattr(E, "site_search", forbidden)
    monkeypatch.setattr(U, "lookup", forbidden)
    monkeypatch.setattr(U, "refresh", forbidden)
    tier0.pages[GOOD] = rec(GOOD, "LG 242 L Frost Free Refrigerator GL-N292DDSY")
    with contextlib.redirect_stdout(io.StringIO()):
        out = E.crawl_url(GOOD, SKU)
        assert out["status"] == "FOUND (reliancedigital.in)"
        args = argparse.Namespace(crawl_urls=True, no_url_index=False,
                                  refresh_index=False, index_max_age=None)
        assert E.configure_url_index(args, client_config.load_client("shreejee")) is False
    assert E.URL_INDEX_ON is False and E.SITE_SEARCH_ON is False
    assert tier0.searched == []


@pytest.fixture
def configure(monkeypatch):
    """configure_url_index with refresh/stale stubbed and globals restored."""
    for name in ("URL_INDEX_ON", "SITE_SEARCH_ON", "URL_INDEX_SOURCES",
                 "SITE_SEARCH_RULES"):
        monkeypatch.setattr(E, name, getattr(E, name))
    monkeypatch.delenv("PIPELINE_URL_INDEX", raising=False)
    calls = {"refresh": [], "stale": []}
    monkeypatch.setattr(U, "stale_sources",
                        lambda srcs, age, **k: calls["stale"].append(age) or ["lg"])
    monkeypatch.setattr(U, "refresh",
                        lambda srcs, names=None, **k: calls["refresh"].append(names) or [])
    cfg = client_config.load_client("shreejee")

    def run(cfg=cfg, **flags):
        args = argparse.Namespace(**{"crawl_urls": False, "no_url_index": False,
                                     "refresh_index": False, "index_max_age": None,
                                     **flags})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            on = E.configure_url_index(args, cfg)
        return on, buf.getvalue()
    return SimpleNamespace(run=run, calls=calls, cfg=cfg)


def test_no_url_index_flag_env_and_client_switch_disable_tier_0(configure, monkeypatch):
    on, log = configure.run(no_url_index=True)
    assert on is False and "--no-url-index" in log and not E.URL_INDEX_ON
    monkeypatch.setenv("PIPELINE_URL_INDEX", "off")
    on, log = configure.run()
    assert on is False and "PIPELINE_URL_INDEX" in log
    monkeypatch.delenv("PIPELINE_URL_INDEX")
    off_cfg = dataclasses.replace(
        configure.cfg, url_index=client_config.UrlIndexConfig(enabled=False))
    on, log = configure.run(cfg=off_cfg)
    assert on is False and "enabled = false" in log
    assert configure.calls["refresh"] == []          # nothing was downloaded


def test_tier_0_on_refreshes_only_stale_sources(configure):
    on, log = configure.run(index_max_age=3)
    assert on is True and E.URL_INDEX_ON and E.SITE_SEARCH_ON
    assert configure.calls["stale"] == [3]
    assert configure.calls["refresh"] == [["lg"]]
    on, _ = configure.run(refresh_index=True)        # --refresh-index: every source
    assert len(configure.calls["refresh"][-1]) == len(E.URL_INDEX_SOURCES) > 1
    assert "croma" not in [s.name for s in E.URL_INDEX_SOURCES]       # disabled
    assert [r.name for r in E.SITE_SEARCH_RULES] == ["reliancedigital", "croma"]


def test_client_disable_list_and_extra_sources(configure):
    cfg = dataclasses.replace(configure.cfg, url_index=client_config.UrlIndexConfig(
        disable=("lg", "croma"),
        extra_sources=({"name": "mydealer", "root": "https://dealer.example/sitemap.xml"},)))
    configure.run(cfg=cfg)
    names = [s.name for s in E.URL_INDEX_SOURCES]
    assert "lg" not in names and names[-1] == "mydealer"
    assert [r.name for r in E.SITE_SEARCH_RULES] == ["reliancedigital"]


def test_client_toml_url_index_table_is_validated(tmp_path):
    data = {"url_index": {"enabled": "yes"}}
    with pytest.raises(client_config.ClientConfigError):
        client_config._load_url_index(data, "t")
    with pytest.raises(client_config.ClientConfigError):
        client_config._load_url_index({"url_index": {"max_age_days": -1}}, "t")
    ui = client_config._load_url_index({}, "t")
    assert ui.enabled is True and ui.max_age_days == 7.0


# ------------------------------------------------- site search, live path
@pytest.fixture
def sitesearch(monkeypatch, tmp_path):
    monkeypatch.setattr(E, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(E, "CACHE_ENABLED", True)
    monkeypatch.setattr(E, "CACHE_REFRESH", False)
    monkeypatch.setattr(E, "SITE_SEARCH_ON", True)
    monkeypatch.setattr(E, "polite_sleep", QUIET)
    monkeypatch.setattr(E, "_SITE_SEARCH_SKIP", set())
    monkeypatch.setattr(E, "_SITE_KEYS", {})
    monkeypatch.setitem(E.RUN_STATS, "sitesearch_calls", 0)

    def install(routes):
        http = FakeHTTP(routes)
        monkeypatch.setattr(E, "_SITE_FETCHER",
                            U.Fetcher(http=http, sleep=QUIET, log=QUIET))
        return http

    def run(rule, model):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hits = E.site_search(rule, model)
        return hits, buf.getvalue()
    return SimpleNamespace(install=install, run=run, cache=tmp_path / "cache")


def test_site_search_is_cached_and_robots_checked(sitesearch):
    rule = _rules()["reliancedigital"]
    url = U.build_search_url(rule, "GL-N292DDSY")
    http = sitesearch.install({
        "https://www.reliancedigital.in/robots.txt": ok(b"User-agent: *\nDisallow: /cart\n",
                                                        "text/plain"),
        url: ok((FIX / "reliance_search.json").read_bytes(), "application/json")})
    hits, _ = sitesearch.run(rule, "GL-N292DDSY")
    assert len(hits) == 2 and E.RUN_STATS["sitesearch_calls"] == 1
    n = len(http.calls)
    hits2, log = sitesearch.run(rule, "GL-N292DDSY")
    assert hits2 == hits and len(http.calls) == n and "cached site search" in log


def test_site_search_skips_an_endpoint_robots_disallows(sitesearch):
    rule = _rules()["reliancedigital"]
    url = U.build_search_url(rule, "GL-N292DDSY")
    http = sitesearch.install({
        "https://www.reliancedigital.in/robots.txt": ok(b"User-agent: *\nDisallow: /ext/\n",
                                                        "text/plain"),
        url: ok((FIX / "reliance_search.json").read_bytes())})
    hits, log = sitesearch.run(rule, "GL-N292DDSY")
    assert hits == [] and url not in http.urls() and "off for this run" in log
    hits, log2 = sitesearch.run(rule, "WW70T4020CE")
    assert hits == [] and log2 == ""                 # one line per run, no request


def _vs_routes(page=None, answer=None):
    rule = _rules()["vijaysales"]
    keys = U.read_hidden_inputs((FIX / "vijaysales_page.html").read_text(),
                                rule.key_inputs)
    url = U.build_search_url(rule, "WW70T4020CE/TL", keys)
    return rule, keys, url, {
        "https://www.vijaysales.com/robots.txt": ok(b"User-agent: *\nDisallow: /checkout\n",
                                                    "text/plain"),
        rule.key_page: ok(page if page is not None
                          else (FIX / "vijaysales_page.html").read_bytes(), "text/html"),
        url: ok(answer if answer is not None
                else (FIX / "vijaysales_unbxd.json").read_bytes(), "application/json"),
        # search.unbxd.io/robots.txt -> 404 = no rules (RFC 9309)
    }


def test_vijaysales_keys_are_read_from_the_page_and_never_leak(sitesearch):
    rule, keys, url, routes = _vs_routes()
    assert keys == {"unbxd-apiKey": "fakeapikey0123456789abcdef",
                    "unbxd-siteKey": "FAKESITEKEY-0000-ss-test"}
    http = sitesearch.install(routes)
    hits, log = sitesearch.run(rule, "WW70T4020CE/TL")
    assert [u for u, _ in hits] == [
        "https://www.vijaysales.com/p/11111/samsung-7-kg-front-load-ww70t4020ce-tl"]
    assert url in http.urls() and "https://search.unbxd.io/robots.txt" in http.urls()
    # a second model: the keys are reused from memory, no second page fetch
    sitesearch.run(rule, "WW80T4020CE/TL")
    assert http.urls().count(rule.key_page) == 1
    # no key value in the log, the cache key, or any cache file
    ckey = U.search_cache_key(rule, "WW70T4020CE/TL")
    assert ckey == "vijaysales:WW70T4020CETL"
    blobs = [gzip.decompress(p.read_bytes()).decode("utf-8")
             for p in sitesearch.cache.rglob("*.json.gz")]
    assert blobs
    for secret in keys.values():
        assert secret not in log and secret not in ckey
        assert all(secret not in b for b in blobs)


def test_vijaysales_failure_disables_it_for_the_run(sitesearch):
    rule, keys, url, routes = _vs_routes(page=b"<html><body>no keys here</body></html>")
    http = sitesearch.install(routes)
    hits, log = sitesearch.run(rule, "WW70T4020CE/TL")
    assert hits == [] and log.count("off for this run") == 1
    n = len(http.calls)
    hits, log2 = sitesearch.run(rule, "WW80T4020CE/TL")
    assert hits == [] and len(http.calls) == n and log2 == ""

    # the endpoint changes shape -> off as well, nothing cached
    rule, keys, url, routes = _vs_routes(answer=b'{"error": "moved"}')
    E._SITE_SEARCH_SKIP.clear()
    E._SITE_KEYS.clear()
    sitesearch.install(routes)
    hits, log = sitesearch.run(rule, "WW70T4020CE/TL")
    assert hits == [] and "expected JSON shape" in log
    for secret in keys.values():
        assert secret not in log
    assert not list(sitesearch.cache.rglob("*.json.gz"))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
