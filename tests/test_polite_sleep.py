"""Politeness delays must fire for every LIVE request and for no cache hit.

The saving is real but the risk is one-sided: dropping a delay that was owed to
a retailer is how a scraper gets blocked, and a block costs far more than the
minutes saved. These tests pin both directions.

    .venv\\Scripts\\python.exe tests\\test_polite_sleep.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sku_enricher as E                                    # noqa: E402

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


class Recorder:
    """Swallow the real sleep and record what would have been slept."""

    def __init__(self):
        self.naps = []

    def __call__(self, seconds):
        self.naps.append(seconds)

    @property
    def total(self):
        return sum(self.naps)


def with_patched(fn):
    """Run fn with time.sleep recorded and the cache/network stubbed out."""
    real_sleep, real_get, real_put = E.time.sleep, E.cache_get, E.cache_put
    real_live, real_serper, real_ddg = E.fetch_live, E.serper_search, E.ddg_search
    rec = Recorder()
    E.time.sleep = rec
    E.cache_put = lambda *a, **k: None
    try:
        return fn(rec)
    finally:
        (E.time.sleep, E.cache_get, E.cache_put) = real_sleep, real_get, real_put
        (E.fetch_live, E.serper_search, E.ddg_search) = real_live, real_serper, real_ddg


PAGE = "<html><head><title>x</title></head><body>" + ("y" * 4000) + "</body></html>"


# -- 1. a cache HIT owes nothing --------------------------------------------
def t_cache_hit(rec):
    E.cache_get = lambda kind, key: PAGE
    before = E.NET_CALLS
    html, _ = E.fetch("https://example.com/p/1")
    E.polite_sleep(1.5, 3.0, since=before)
    check("cache hit returns the page", bool(html), True)
    check("cache hit makes no network call", E.NET_CALLS, before)
    check("cache hit sleeps 0 times", len(rec.naps), 0)


# -- 2. a cache MISS goes live and MUST still wait ---------------------------
def t_cache_miss(rec):
    E.cache_get = lambda kind, key: None
    E.fetch_live = lambda url: (PAGE, "")
    before = E.NET_CALLS
    html, _ = E.fetch("https://example.com/p/2")
    E.polite_sleep(1.5, 3.0, since=before)
    check("live fetch returns the page", bool(html), True)
    check("live fetch counts one network call", E.NET_CALLS, before + 1)
    check("live fetch sleeps exactly once", len(rec.naps), 1)
    check("live delay is within 1.5-3.0s", 1.5 <= rec.naps[0] <= 3.0, True)


# -- 3. a FAILED live fetch still waits (a 403 is still a request made) ------
def t_failed_fetch_still_waits(rec):
    E.cache_get = lambda kind, key: None
    E.fetch_live = lambda url: (None, "http 403")
    before = E.NET_CALLS
    html, why = E.fetch("https://example.com/p/3")
    E.polite_sleep(1.5, 3.0, since=before)
    check("blocked fetch returns no html", html, None)
    check("blocked fetch still counts as network", E.NET_CALLS, before + 1)
    check("blocked fetch still sleeps", len(rec.naps), 1)


# -- 4. searches behave the same way ----------------------------------------
def t_search(rec):
    E.cache_get = lambda kind, key: [{"href": "https://x.test/a", "title": "a"}]
    before = E.NET_CALLS
    E.web_search("cached query")
    E.polite_sleep(0.6, 1.4, since=before)
    check("cached search makes no network call", E.NET_CALLS, before)
    check("cached search does not sleep", len(rec.naps), 0)

    E.cache_get = lambda kind, key: None
    E.serper_search = lambda q: [{"href": "https://x.test/b", "title": "b"}]
    before = E.NET_CALLS
    E.web_search("live query")
    E.polite_sleep(0.6, 1.4, since=before)
    check("live search counts one network call", E.NET_CALLS, before + 1)
    check("live search sleeps once", len(rec.naps), 1)
    check("search delay is within 0.6-1.4s", 0.6 <= rec.naps[0] <= 1.4, True)


# -- 5. a MIXED SKU (some cached, one live) must still be paced --------------
# The between-SKU delay is snapshotted once per SKU, so one live fetch among
# many cache hits has to keep the whole SKU polite.
def t_mixed_sku(rec):
    calls = {"n": 0}

    def half_cached(kind, key):
        calls["n"] += 1
        return None if calls["n"] == 3 else PAGE

    E.cache_get = half_cached
    E.fetch_live = lambda url: (PAGE, "")
    before = E.NET_CALLS
    for i in range(4):
        E.fetch(f"https://example.com/p/mixed{i}")
    E.polite_sleep(3.0, 7.0, since=before)          # the between-SKU delay
    check("one of four fetches went live", E.NET_CALLS, before + 1)
    check("mixed SKU still sleeps", len(rec.naps), 1)
    check("mixed delay is within 3.0-7.0s", 3.0 <= rec.naps[0] <= 7.0, True)


# -- 6. a headless RENDER is a live round-trip, even on a cached page --------
# P2-33: --js-fallback renders croma pages whose plain HTML pass 1 already
# cached. fetch() makes no network call, so if render_fetch does not count its
# navigation the whole SKU is treated as cache-only and hits the host at
# browser speed with no delay at all.
class _FakePage:
    def __init__(self, html):
        self.html = html

    def goto(self, *a, **k):
        pass

    def wait_for_timeout(self, *a, **k):
        pass

    def content(self):
        return self.html

    def close(self):
        pass


def t_render_on_cache_hit(rec):
    saved = (E.PW_ENABLED, dict(E._PW))
    try:
        E.cache_get = lambda kind, key: PAGE
        E.PW_ENABLED = True
        E._PW.update({"state": "on", "used": 0, "sku_used": 0,
                      "ctx": type("Ctx", (), {
                          "new_page": lambda self: _FakePage(PAGE)})()})
        before = E.NET_CALLS
        html, _ = E.fetch("https://www.croma.com/p/9")       # cache hit
        check("plain fetch was a cache hit", E.NET_CALLS, before)
        rendered = E.render_fetch("https://www.croma.com/p/9")
        E.polite_sleep(1.5, 3.0, since=before)
        check("render returns the page", bool(rendered), True)
        check("render counts one network call", E.NET_CALLS, before + 1)
        check("render on a cached page still sleeps", len(rec.naps), 1)
        check("render delay is within 1.5-3.0s",
              1.5 <= rec.naps[0] <= 3.0, True)
    finally:
        E.PW_ENABLED = saved[0]
        E._PW.clear()
        E._PW.update(saved[1])


# -- 7. no snapshot at all == the old unconditional behaviour ---------------
def t_default_is_backward_compatible(rec):
    E.polite_sleep(0.1, 0.2)
    check("polite_sleep with no `since` always sleeps", len(rec.naps), 1)


CASES = [("cache hit", t_cache_hit),
         ("cache miss", t_cache_miss),
         ("failed fetch", t_failed_fetch_still_waits),
         ("search", t_search),
         ("mixed SKU", t_mixed_sku),
         ("render on cache hit", t_render_on_cache_hit),
         ("default", t_default_is_backward_compatible)]


# -- pytest entry points -----------------------------------------------------
# One test per case. with_patched() always restores the module globals it
# swapped, so these are safe to collect alongside the other suites.
def test_cache_hit():
    with_patched(t_cache_hit)


def test_cache_miss():
    with_patched(t_cache_miss)


def test_failed_fetch_still_waits():
    with_patched(t_failed_fetch_still_waits)


def test_search():
    with_patched(t_search)


def test_mixed_sku():
    with_patched(t_mixed_sku)


def test_render_on_cache_hit():
    with_patched(t_render_on_cache_hit)


def test_default_is_backward_compatible():
    with_patched(t_default_is_backward_compatible)


def main():
    for name, fn in CASES:
        print(f"\n{name}:")
        with_patched(fn)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


# NEVER run this at import time: pytest imports the module during collection
# and a module-level sys.exit aborts the whole session with INTERNALERROR.
if __name__ == "__main__":
    sys.exit(main())
