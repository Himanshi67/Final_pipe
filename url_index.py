r"""url_index.py - tier 0 of the URL search: find product pages WITHOUT a
search engine.

Two free sources sit in front of the paid Serper search in sku_enricher.py:

  0a. a LOCAL URL INDEX - retailer / brand sitemaps, downloaded on a schedule
      into url_index\index.sqlite and matched on the SKU's model code in the
      URL slug;
  0b. RETAILER ON-SITE SEARCH - the retailer's own search JSON endpoint.

A match from either is ONLY a candidate. sku_enricher.lookup_sku pushes it
through the same URL filters as a search result and the unchanged assess()
verdict, so nothing here can make a row FOUND on its own.

No LLM is called anywhere. Nothing here imports sku_enricher at module load
(sku_enricher imports THIS module), so the ranking function and the brand
sites are passed in by the caller.

CLI:
    .venv\Scripts\python.exe url_index.py refresh [--source NAME] [--force]
    .venv\Scripts\python.exe url_index.py stats
    .venv\Scripts\python.exe url_index.py lookup MODEL [--brand B]

Sources: url_sources.toml (shared defaults) + the [url_index] table of
clients\<slug>\client.toml (enabled, max_age_days, extra_sources, disable).
The index itself is shared across clients - retailer URLs are not client data.
"""

from __future__ import annotations

import argparse
import random
import re
import sqlite3
import sys
import time
import zlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from urllib.robotparser import RobotFileParser

try:
    import tomllib                       # Python 3.11+
except ModuleNotFoundError:              # pragma: no cover - 3.10 and older
    import tomli as tomllib              # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parent
INDEX_DIR = ROOT / "url_index"
DB_PATH = INDEX_DIR / "index.sqlite"
SOURCES_FILE = ROOT / "url_sources.toml"

MAX_CANDIDATES = 6          # lookup() returns at most this many URLs per SKU
PER_DOMAIN = 2              # ...and at most this many from one domain
SHORT_CODE = 6              # a model code shorter than this needs the brand too
MIN_CODE = 3                # shorter than this means nothing in a slug
MAX_URLS_PER_SOURCE = 50000  # one giant site must not bloat the DB
MAX_SITEMAPS_PER_SOURCE = 80  # child sitemaps followed per source per refresh
MAX_DEPTH = 3               # sitemap-index nesting followed
MAX_BODY_BYTES = 64 * 1024 * 1024   # decompressed sitemap ceiling (gzip bombs)
DEFAULT_MAX_AGE_DAYS = 7.0
FAILED_RETRY_HOURS = 12.0   # a failed source is not retried before this
DELAY = (1.0, 2.5)          # politeness between live fetches to one host
FETCH_TIMEOUT = 30
ROBOTS_UA = "*"             # we match the generic robots.txt group

_CODE_RE = re.compile(r"[^A-Z0-9]")


# ------------------------------------------------------------ normalization
def norm_code(s) -> str:
    """THE normalization, shared by indexing and lookup: uppercase, strip
    everything that is not A-Z / 0-9. Covers every slug style seen live:
    lowercased codes, '.' or '/' turned into '-', spaces dropped or dashed,
    trailing numeric ids."""
    return _CODE_RE.sub("", str(s or "").upper())


def url_path(url) -> str:
    """The URL path, percent-decoded, without query or fragment. A '%20'
    left encoded would add the digits '20' to the slug."""
    try:
        return unquote(urlsplit(str(url or "")).path)
    except Exception:
        return ""


def slug_norm(url) -> str:
    return norm_code(url_path(url))


def domain_of(url) -> str:
    try:
        d = urlsplit(str(url or "")).netloc.lower()
    except Exception:
        return ""
    d = d.split("@")[-1].split(":")[0]
    return d[4:] if d.startswith("www.") else d


def slug_title(url) -> str:
    """'/in/tv/lg-55uq7500-55-inch/' -> 'in tv lg 55uq7500 55 inch'. A stand-in
    for a search-result title so the same title screens can run on it."""
    return re.sub(r"[\s/_\-.+]+", " ", url_path(url)).strip()


# ------------------------------------------------------------------ config
@dataclass(frozen=True)
class Source:
    name: str
    domain: str = ""
    root: str = ""                 # root sitemap; '' = take robots.txt Sitemap: lines
    child_pattern: str = ""        # sitemap-index children to follow (regex)
    child_exclude: str = ""
    url_pattern: str = ""          # product URLs to keep (regex)
    exclude_pattern: str = ""
    brand: str = ""                # the brand whose own site this is ('' = retailer)
    enabled: bool = True
    max_urls: int = MAX_URLS_PER_SOURCE


@dataclass(frozen=True)
class SiteSearchRule:
    name: str
    url_template: str              # '{q}' = the URL-encoded model code
    extract: str = "json"          # json | regex | css
    items: str = ""                # json: dotted path to the result list
    url_field: str = "url"         # json: dotted path inside an item
    url_prefix: str = ""           # prepended to a relative url
    name_field: str = "name"
    model_field: str = ""          # json: exact model field, when the API has one
    pattern: str = ""              # regex: named groups (?P<url>) (?P<title>)
    selector: str = ""             # css: anchors to read
    attr: str = "href"
    url_must_match: str = ""       # regex a built product URL must match ('/p/')
    key_page: str = ""             # page whose hidden <input>s hold API keys
    key_inputs: tuple = ()         # ids of those inputs; '{key:<id>}' in url_template
    max_results: int = 6
    enabled: bool = True


_SOURCE_KEYS = {f for f in Source.__dataclass_fields__}
_RULE_KEYS = {f for f in SiteSearchRule.__dataclass_fields__}


class ConfigError(Exception):
    pass


def _make_source(raw, where) -> Source:
    if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
        raise ConfigError(f"{where}: every source needs a name")
    unknown = set(raw) - _SOURCE_KEYS
    if unknown:
        raise ConfigError(f"{where}: source '{raw.get('name')}' has unknown "
                          f"key(s) {sorted(unknown)}")
    kw = dict(raw)
    kw["name"] = str(kw["name"]).strip()
    for key in ("child_pattern", "child_exclude", "url_pattern", "exclude_pattern"):
        if kw.get(key):
            try:
                re.compile(kw[key])
            except re.error as exc:
                raise ConfigError(f"{where}: source '{kw['name']}' {key} is not a "
                                  f"valid regex: {exc}") from None
    if not kw.get("domain"):
        kw["domain"] = domain_of(kw.get("root", ""))
    if not kw.get("root") and not kw.get("domain"):
        raise ConfigError(f"{where}: source '{kw['name']}' needs a root or a domain")
    kw["brand"] = str(kw.get("brand") or "").strip().lower()
    kw["enabled"] = bool(kw.get("enabled", True))
    kw["max_urls"] = max(1, min(int(kw.get("max_urls") or MAX_URLS_PER_SOURCE),
                                MAX_URLS_PER_SOURCE))
    return Source(**kw)


def _make_rule(raw, where) -> SiteSearchRule:
    if not isinstance(raw, dict) or not raw.get("name") or not raw.get("url_template"):
        raise ConfigError(f"{where}: every site_search entry needs name and url_template")
    unknown = set(raw) - _RULE_KEYS
    if unknown:
        raise ConfigError(f"{where}: site_search '{raw.get('name')}' has unknown "
                          f"key(s) {sorted(unknown)}")
    if "{q}" not in raw["url_template"]:
        raise ConfigError(f"{where}: site_search '{raw['name']}' url_template has no {{q}}")
    if raw.get("extract", "json") not in ("json", "regex", "css"):
        raise ConfigError(f"{where}: site_search '{raw['name']}' extract must be "
                          f"json, regex or css")
    kw = dict(raw)
    kw["enabled"] = bool(kw.get("enabled", True))
    kw["key_inputs"] = tuple(str(k) for k in kw.get("key_inputs") or ())
    for k in kw["key_inputs"]:
        if "{key:%s}" % k not in kw["url_template"]:
            raise ConfigError(f"{where}: site_search '{kw['name']}' url_template "
                              f"has no {{key:{k}}} for key input '{k}'")
    if kw["key_inputs"] and not kw.get("key_page"):
        raise ConfigError(f"{where}: site_search '{kw['name']}' has key_inputs "
                          f"but no key_page")
    if kw.get("url_must_match"):
        try:
            re.compile(kw["url_must_match"])
        except re.error as exc:
            raise ConfigError(f"{where}: site_search '{kw['name']}' url_must_match "
                              f"is not a valid regex: {exc}") from None
    return SiteSearchRule(**kw)


def load_config(path=None, extra_sources=(), disable=()):
    """(sources, site_search_rules), both in file order, ENABLED ones only.
    `extra_sources` (client.toml) are appended - a same-named extra replaces
    the shared default; `disable` names are dropped from both lists."""
    path = Path(path or SOURCES_FILE)
    data = {}
    if path.is_file():
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except Exception as exc:
            raise ConfigError(f"could not read {path}: {type(exc).__name__}: {exc}") from exc
    where = path.name
    sources = [_make_source(s, where) for s in data.get("source", [])]
    rules = [_make_rule(r, where) for r in data.get("site_search", [])]
    for raw in extra_sources or ():
        extra = _make_source(dict(raw), "client.toml [url_index] extra_sources")
        sources = [s for s in sources if s.name != extra.name] + [extra]
    off = {str(n).strip().lower() for n in (disable or ())}
    sources = [s for s in sources if s.enabled and s.name.lower() not in off]
    rules = [r for r in rules if r.enabled and r.name.lower() not in off]
    return sources, rules


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sources(
    name TEXT PRIMARY KEY, root_url TEXT, last_refresh REAL, last_attempt REAL,
    status TEXT, url_count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS sitemaps(
    url TEXT PRIMARY KEY, source TEXT, lastmod TEXT, etag TEXT,
    last_modified_hdr TEXT, fetched_at REAL, parent TEXT);
CREATE INDEX IF NOT EXISTS sitemaps_parent ON sitemaps(parent);
CREATE TABLE IF NOT EXISTS urls(
    url TEXT PRIMARY KEY, source TEXT, domain TEXT, slug_norm TEXT,
    lastmod TEXT, seen_at REAL, sitemap TEXT);
CREATE INDEX IF NOT EXISTS urls_source ON urls(source, seen_at);
CREATE INDEX IF NOT EXISTS urls_sitemap ON urls(sitemap);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS urls_fts USING fts5(
    slug_norm, content='urls', content_rowid='rowid', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS urls_ai AFTER INSERT ON urls BEGIN
    INSERT INTO urls_fts(rowid, slug_norm) VALUES (new.rowid, new.slug_norm);
END;
CREATE TRIGGER IF NOT EXISTS urls_ad AFTER DELETE ON urls BEGIN
    INSERT INTO urls_fts(urls_fts, rowid, slug_norm)
        VALUES ('delete', old.rowid, old.slug_norm);
END;
CREATE TRIGGER IF NOT EXISTS urls_au AFTER UPDATE OF slug_norm ON urls BEGIN
    INSERT INTO urls_fts(urls_fts, rowid, slug_norm)
        VALUES ('delete', old.rowid, old.slug_norm);
    INSERT INTO urls_fts(rowid, slug_norm) VALUES (new.rowid, new.slug_norm);
END;
"""


def connect(db_path=None, create=True):
    """Open (and create) the index. Returns None when it does not exist and
    create is False - a lookup must never create an empty DB as a side effect."""
    path = Path(db_path or DB_PATH)
    if not create and not path.is_file():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key='fts'").fetchone()
    if row is None:
        try:
            conn.executescript(FTS_SCHEMA)
            fts = "1"
        except sqlite3.OperationalError:       # no FTS5 / no trigram tokenizer
            fts = "0"
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('fts', ?)", (fts,))
        conn.commit()
    return conn


def has_fts(conn) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key='fts'").fetchone()
    return bool(row and row[0] == "1")


# ----------------------------------------------------------------- fetching
@dataclass
class Response:
    status: int = 0
    headers: dict = field(default_factory=dict)   # lower-case keys
    body: bytes = b""
    error: str = ""

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


@dataclass
class RobotsInfo:
    parser: RobotFileParser | None
    crawl_delay: float = 0.0
    sitemaps: tuple = ()
    allow_all: bool = False
    disallow_all: bool = False
    reason: str = ""               # why everything is disallowed, for the log

    def allowed(self, url) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all or self.parser is None:
            return True
        return self.parser.can_fetch(ROBOTS_UA, url)


def gunzip_capped(body: bytes, limit=MAX_BODY_BYTES) -> bytes:
    """Decompress when the body STARTS with the gzip magic bytes (1f 8b).
    Servers label .xml.gz as application/x-download, octet-stream or gzip,
    and some gzip a plain .xml, so neither header nor extension is trusted."""
    if body[:2] != b"\x1f\x8b":
        return body
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(body, limit)
    if d.unconsumed_tail:
        raise ValueError(f"decompressed sitemap exceeds {limit // 1_048_576} MB")
    return out


class Fetcher:
    """Live HTTP for the index and for site search. curl_cffi with Chrome
    impersonation (the pipeline's own client library), one session per host,
    and a politeness delay before EVERY live request to a host that already
    saw one: max(random DELAY, the host's robots.txt Crawl-delay).

    `http(url, headers) -> Response` and `sleep` are injectable, so the tests
    run fully offline."""

    def __init__(self, http=None, sleep=time.sleep, delay=DELAY, log=print):
        self._http = http or self._curl_get
        self._sleep = sleep
        self.delay = delay
        self.log = log
        self._last = {}            # host -> monotonic time of the last request
        self._robots = {}          # scheme://host -> RobotsInfo
        self._sessions = {}
        self.requests = 0

    # -- transport ---------------------------------------------------------
    def _curl_get(self, url, headers):
        from curl_cffi import requests as creq      # lazy: keep import light
        host = urlsplit(url).netloc
        s = self._sessions.get(host)
        if s is None:
            s = self._sessions[host] = creq.Session()
        try:
            r = s.get(url, impersonate="chrome", timeout=FETCH_TIMEOUT,
                      headers=headers or {}, allow_redirects=True)
        except Exception as exc:
            return Response(error=type(exc).__name__)
        return Response(status=r.status_code,
                        headers={k.lower(): v for k, v in r.headers.items()},
                        body=r.content or b"")

    def _wait(self, host, crawl_delay=0.0):
        last = self._last.get(host)
        if last is not None:
            want = max(random.uniform(*self.delay), float(crawl_delay or 0))
            gap = time.monotonic() - last
            if gap < want:
                self._sleep(want - gap)
        self._last[host] = time.monotonic()

    def get(self, url, headers=None) -> Response:
        """One live request, after the per-host politeness wait."""
        parts = urlsplit(url)
        host = parts.netloc.lower()
        info = self._robots.get(f"{parts.scheme}://{host}")
        self._wait(host, info.crawl_delay if info else 0.0)
        self.requests += 1
        return self._http(url, headers or {})

    # -- robots.txt --------------------------------------------------------
    def robots(self, url) -> RobotsInfo:
        """RFC 9309 semantics: 2xx parse it; 4xx (other than 401/403) = no
        rules; 401/403, 5xx or no answer = disallow everything."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc.lower()}"
        info = self._robots.get(origin)
        if info is not None:
            return info
        r = self.get(origin + "/robots.txt")
        if r.status and 200 <= r.status < 300:
            text = gunzip_capped(r.body).decode("utf-8", "replace")
            rp = RobotFileParser()
            rp.parse(text.splitlines())
            try:
                cd = float(rp.crawl_delay(ROBOTS_UA) or 0)
            except (TypeError, ValueError):
                cd = 0.0
            maps = tuple(rp.site_maps() or ())
            info = RobotsInfo(rp, cd, maps)
        elif r.status in (401, 403) or r.status >= 500 or not r.status:
            info = RobotsInfo(None, disallow_all=True,
                              reason=f"robots.txt {r.error or 'http %s' % r.status}")
        else:
            info = RobotsInfo(None, allow_all=True)
        self._robots[origin] = info
        return info

    def allowed(self, url) -> bool:
        return self.robots(url).allowed(url)


# ------------------------------------------------------------ sitemap parse
def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


_LOC_RE = re.compile(rb"<loc>\s*(.*?)\s*</loc>", re.S | re.I)


def parse_sitemap(body: bytes):
    """('index' | 'urlset', [(loc, lastmod), ...]). Namespace-agnostic; falls
    back to a <loc> regex for XML a strict parser refuses."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        locs = [m.group(1).decode("utf-8", "replace").strip()
                for m in _LOC_RE.finditer(body)]
        kind = "index" if b"<sitemapindex" in body[:4096] else "urlset"
        return kind, [(u, "") for u in locs if u]
    kind = "index" if _local(root.tag) == "sitemapindex" else "urlset"
    out = []
    for el in root:
        if _local(el.tag) not in ("url", "sitemap"):
            continue
        loc = lastmod = ""
        for child in el:
            name = _local(child.tag)
            if name == "loc":
                loc = (child.text or "").strip()
            elif name == "lastmod":
                lastmod = (child.text or "").strip()
        if loc:
            out.append((loc, lastmod))
    return kind, out


# ------------------------------------------------------------------ refresh
def _rx(p):
    return re.compile(p, re.I) if p else None


def refresh_source(conn, source: Source, fetcher: Fetcher, force=False,
                   now=None, log=print) -> dict:
    """Refresh ONE source. Never raises: a failed source logs one line, is
    marked failed and keeps its old URLs."""
    now = time.time() if now is None else now
    started = time.monotonic()
    res = {"name": source.name, "status": "failed", "urls": 0, "fetched": 0,
           "skipped": 0, "failed": 0, "disallowed": 0, "pruned": 0}
    try:
        _refresh(conn, source, fetcher, force, now, res, log)
    except Exception as exc:                    # never take the run down
        res["status"] = "failed"
        res["error"] = f"{type(exc).__name__}: {exc}"[:200]
        conn.rollback()
    status = res["status"] if res["status"] != "failed" else (
        "failed: " + res.get("error", "unknown"))[:200]
    if res["status"] == "failed":
        conn.execute(
            "INSERT INTO sources(name, root_url, last_attempt, status) VALUES (?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET root_url=excluded.root_url, "
            "last_attempt=excluded.last_attempt, status=excluded.status",
            (source.name, source.root, now, status))
        conn.commit()
        log(f"  url index: {source.name} FAILED ({res.get('error', 'unknown')}) "
            f"- old URLs kept")
    else:
        log(f"  url index: {source.name} {res['urls']:,} URL(s) "
            f"({res['fetched']} sitemap(s) fetched, {res['skipped']} unchanged, "
            f"{res['failed']} failed, {res['pruned']} pruned) "
            f"in {time.monotonic() - started:.0f}s")
    return res


def _refresh(conn, source, fetcher, force, now, res, log):
    robots = fetcher.robots(source.root or f"https://{source.domain}/")
    roots = [source.root] if source.root else [
        u for u in robots.sitemaps if domain_of(u).endswith(source.domain)]
    if not roots:
        res["error"] = "no root sitemap configured and none in robots.txt"
        return
    child_rx, child_ex = _rx(source.child_pattern), _rx(source.child_exclude)
    url_rx, url_ex = _rx(source.url_pattern), _rx(source.exclude_pattern)

    known = {row[0]: row[1:] for row in conn.execute(
        "SELECT url, lastmod, etag, last_modified_hdr FROM sitemaps WHERE source=?",
        (source.name,))}
    queue = [(u, 0, "", "") for u in roots]
    visited, root_ok, kept = set(), False, 0
    batch = []

    def keep_old(sm_url):
        """Vouch for every URL under a sitemap that was NOT re-read this time
        (unchanged lastmod, 304, transient failure) - and, for an index, under
        all its descendants - so the prune below does not delete them."""
        todo, done = [sm_url], set()
        while todo:
            u = todo.pop()
            if u in done:
                continue
            done.add(u)
            conn.execute("UPDATE urls SET seen_at=? WHERE sitemap=?", (now, u))
            todo += [r[0] for r in conn.execute(
                "SELECT url FROM sitemaps WHERE parent=? AND source=?",
                (u, source.name))]

    while queue:
        sm_url, depth, parent_lastmod, parent = queue.pop(0)
        if sm_url in visited or len(visited) >= MAX_SITEMAPS_PER_SOURCE:
            continue
        visited.add(sm_url)
        is_root = depth == 0
        if not fetcher.allowed(sm_url):
            res["disallowed"] += 1
            if is_root:
                res["error"] = (f"robots.txt disallows {sm_url}"
                                + (f" ({robots.reason})" if robots.reason else ""))
            continue
        prev = known.get(sm_url)
        if (not force and not is_root and prev and parent_lastmod
                and prev[0] == parent_lastmod):
            res["skipped"] += 1                 # <lastmod> unchanged
            keep_old(sm_url)
            continue
        headers = {}
        if prev and not force:
            if prev[1]:
                headers["If-None-Match"] = prev[1]
            if prev[2]:
                headers["If-Modified-Since"] = prev[2]
        r = fetcher.get(sm_url, headers)
        if r.status == 304:
            res["skipped"] += 1
            keep_old(sm_url)
            root_ok = root_ok or is_root
            continue
        if r.status != 200 or not r.body:
            res["failed"] += 1
            keep_old(sm_url)                    # transient: keep what we had
            if is_root:
                res["error"] = r.error or f"http {r.status}" + (
                    " (empty body)" if r.status == 200 else "")
            continue
        try:
            kind, entries = parse_sitemap(gunzip_capped(r.body))
        except Exception as exc:
            res["failed"] += 1
            keep_old(sm_url)
            if is_root:
                res["error"] = f"unparseable sitemap ({type(exc).__name__})"
            continue
        res["fetched"] += 1
        root_ok = root_ok or is_root
        conn.execute(
            "INSERT OR REPLACE INTO sitemaps VALUES (?,?,?,?,?,?,?)",
            (sm_url, source.name, parent_lastmod or (prev[0] if prev else ""),
             r.headers.get("etag", ""), r.headers.get("last-modified", ""), now,
             parent))
        if kind == "index":
            if depth + 1 > MAX_DEPTH:
                continue
            for loc, lastmod in entries:
                if child_rx and not child_rx.search(loc):
                    continue
                if child_ex and child_ex.search(loc):
                    continue
                queue.append((loc, depth + 1, lastmod, sm_url))
            continue
        for loc, lastmod in entries:
            if kept >= source.max_urls:
                break
            if not loc.startswith(("http://", "https://")):
                continue
            if url_rx and not url_rx.search(loc):
                continue
            if url_ex and url_ex.search(loc):
                continue
            kept += 1
            batch.append((loc, source.name, domain_of(loc), slug_norm(loc),
                          lastmod, now, sm_url))
        if len(batch) >= 5000:
            _upsert(conn, batch)
            batch = []
    _upsert(conn, batch)

    if not root_ok:
        conn.rollback()
        res["error"] = res.get("error") or "root sitemap unusable"
        return
    # prune: only after a usable refresh, only this source, only rows that
    # neither this refresh nor a kept-as-unchanged sitemap vouched for
    cur = conn.execute("DELETE FROM urls WHERE source=? AND seen_at<?",
                       (source.name, now))
    res["pruned"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    count = conn.execute("SELECT COUNT(*) FROM urls WHERE source=?",
                         (source.name,)).fetchone()[0]
    res["urls"] = count
    res["status"] = "partial" if res["failed"] else "ok"
    conn.execute(
        "INSERT OR REPLACE INTO sources(name, root_url, last_refresh, last_attempt, "
        "status, url_count) VALUES (?,?,?,?,?,?)",
        (source.name, source.root, now, now, res["status"], count))
    conn.commit()


def _upsert(conn, rows):
    if not rows:
        return
    conn.executemany(
        "INSERT INTO urls(url, source, domain, slug_norm, lastmod, seen_at, sitemap) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
        "source=excluded.source, domain=excluded.domain, slug_norm=excluded.slug_norm, "
        "lastmod=excluded.lastmod, seen_at=excluded.seen_at, sitemap=excluded.sitemap",
        rows)


def refresh(sources, names=None, force=False, db_path=None, fetcher=None,
            log=print, now=None) -> list:
    """Refresh the given sources (all, or only `names`). Never raises."""
    pick = [s for s in sources if not names or s.name in names]
    if not pick:
        return []
    fetcher = fetcher or Fetcher(log=log)
    conn = connect(db_path)
    try:
        return [refresh_source(conn, s, fetcher, force=force, now=now, log=log)
                for s in pick]
    finally:
        conn.close()


def stale_sources(sources, max_age_days=DEFAULT_MAX_AGE_DAYS, db_path=None,
                  now=None) -> list:
    """Names of the sources that need a refresh: never refreshed, or older
    than max_age_days. A source that FAILED recently is left alone for
    FAILED_RETRY_HOURS, so a dead site does not slow every run start."""
    now = time.time() if now is None else now
    conn = connect(db_path, create=False)
    if conn is None:
        return [s.name for s in sources]
    try:
        rows = {r[0]: r[1:] for r in conn.execute(
            "SELECT name, last_refresh, last_attempt FROM sources")}
    finally:
        conn.close()
    out = []
    for s in sources:
        last_refresh, last_attempt = rows.get(s.name, (None, None))
        if last_refresh and now - last_refresh <= max_age_days * 86400:
            continue
        if last_attempt and now - last_attempt < FAILED_RETRY_HOURS * 3600:
            continue
        out.append(s.name)
    return out


# ------------------------------------------------------------------- lookup
def bounded_rx(model_n):
    """The code as a WHOLE token of the raw path: any separators allowed
    between its characters ('rr21h2h24uz-hl', 'gl n292ddsy'), but no letter or
    digit glued on either side. A short code must match this way; a long one
    that does ranks ahead of one that only matches inside a longer code."""
    return re.compile(r"(?<![A-Z0-9])" + r"[^A-Z0-9]*".join(map(re.escape, model_n))
                      + r"(?![A-Z0-9])")


def lookup(model, brand="", sources=None, db_path=None, rank=None,
           brand_domains=(), limit=MAX_CANDIDATES, per_domain=PER_DOMAIN) -> list:
    """Candidate product URLs whose slug contains the model code.

    [{'url', 'source', 'domain', 'title'}], best first: the caller's `rank`
    (sku_enricher.domain_rank) and then the tightest slug. A code shorter than
    SHORT_CODE, or all digits, also needs the brand in the slug or the URL to
    sit on that brand's own site - '600C' alone is in a thousand slugs."""
    model_n, brand_n = norm_code(model), norm_code(brand)
    if len(model_n) < MIN_CODE:
        return []
    short = len(model_n) < SHORT_CODE or model_n.isdigit()
    if short and not brand_n:
        return []
    names = None if sources is None else [s.name for s in sources]
    if names is not None and not names:
        return []
    own = {d.lower() for d in brand_domains or () if d}
    for s in sources or ():
        dom = s.domain or domain_of(s.root)
        if s.brand and norm_code(s.brand) == brand_n and dom:
            own.add(dom.lower())
    conn = connect(db_path, create=False)
    if conn is None:
        return []
    try:
        if has_fts(conn) and len(model_n) >= 3:
            sql = ("SELECT u.url, u.source, u.domain, u.slug_norm FROM urls_fts f "
                   "JOIN urls u ON u.rowid = f.rowid WHERE urls_fts MATCH ?")
            args = [f'"{model_n}"']
        else:
            sql = ("SELECT url, source, domain, slug_norm FROM urls "
                   "WHERE slug_norm LIKE ?")
            args = [f"%{model_n}%"]
        rows = conn.execute(sql + " LIMIT 2000", args).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    bounded = bounded_rx(model_n)
    hits = []
    for url, src, dom, slug in rows:
        if model_n not in (slug or ""):
            continue                            # FTS is a prefilter only
        if names is not None and src not in names:
            continue                            # a disabled source's leftovers
        if short and brand_n not in slug and not any(
                dom == d or dom.endswith("." + d) for d in own):
            continue
        whole = bool(bounded.search(url_path(url).upper()))
        if short and not whole:
            continue          # 'W600' must not match the slug of a 'W6001'
        hits.append((rank(url) if rank else 0, not whole,
                     len(slug) - len(model_n), url, src, dom))
    hits.sort()
    out, per = [], {}
    for _, _, _, url, src, dom in hits:
        if per.get(dom, 0) >= per_domain:
            continue
        per[dom] = per.get(dom, 0) + 1
        out.append({"url": url, "source": src, "domain": dom, "title": slug_title(url)})
        if len(out) >= limit:
            break
    return out


def stats(db_path=None) -> list:
    conn = connect(db_path, create=False)
    if conn is None:
        return []
    try:
        return [dict(zip(("name", "url_count", "last_refresh", "status"), r))
                for r in conn.execute(
                    "SELECT s.name, (SELECT COUNT(*) FROM urls u WHERE u.source=s.name), "
                    "s.last_refresh, s.status FROM sources s ORDER BY s.name")]
    finally:
        conn.close()


def stats_lines(db_path=None) -> list:
    path = Path(db_path or DB_PATH)
    rows = stats(path)
    if not rows:
        return [f"URL index: {path} - empty (it is built at the next enricher run, "
                f"or: url_index.py refresh)"]
    total = sum(r["url_count"] or 0 for r in rows)
    out = [f"URL index: {path}", f"  {len(rows)} source(s), {total:,} URL(s)"]
    for r in rows:
        when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_refresh"]))
                if r["last_refresh"] else "never")
        out.append(f"  {r['name']:<18} {r['url_count'] or 0:>7,} URL(s)  "
                   f"last refresh {when}  [{r['status'] or '-'}]")
    return out


# -------------------------------------------------------------- site search
def build_search_url(rule: SiteSearchRule, model, keys=None) -> str:
    """The live request URL. It may carry page-embedded API keys, so it is
    NEVER logged, cached or written anywhere - see search_cache_key/redact."""
    url = rule.url_template.replace("{q}", quote(str(model).strip(), safe=""))
    for k, v in (keys or {}).items():
        url = url.replace("{key:%s}" % k, quote(str(v), safe=""))
    return url


def search_cache_key(rule: SiteSearchRule, model) -> str:
    """The cache key for one site search. A keyed endpoint is keyed on
    '<name>:<normalized code>' so no key value can reach the .cache folder;
    the others on their URL, exactly like a web search on its query."""
    if rule.key_inputs:
        return f"{rule.name}:{norm_code(model)}"
    return build_search_url(rule, model)


def redact(text, keys=None) -> str:
    """`text` with every key value replaced by '<key>' - for log lines."""
    out = str(text or "")
    for v in (keys or {}).values():
        if v:
            out = out.replace(str(v), "<key>").replace(quote(str(v), safe=""), "<key>")
    return out


_INPUT_RE = re.compile(r"<input\b[^>]*>", re.I | re.S)
_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")


def read_hidden_inputs(html, ids) -> dict:
    """{id: value} for the <input id=...> elements named in `ids` (attribute
    order free, quotes optional). Missing or empty ones are left out."""
    want, out = set(ids or ()), {}
    for m in _INPUT_RE.finditer(html or ""):
        attrs = {a.lower(): (b or c or d)
                 for a, b, c, d in _ATTR_RE.findall(m.group(0))}
        i = attrs.get("id", "")
        if i in want and attrs.get("value"):
            out[i] = attrs["value"].strip()
    return out


def json_shape_ok(rule: SiteSearchRule, text) -> bool:
    """True when a JSON answer still has the result list where the rule
    expects it (an empty list is fine - that is a normal miss)."""
    import json
    try:
        data = json.loads(text)
    except ValueError:
        return False
    items = _dig(data, rule.items) if rule.items else data
    return isinstance(items, list)


def _first(item, paths):
    """The first non-empty value among 'a|b|c' alternative dotted paths."""
    for path in str(paths or "").split("|"):
        v = _dig(item, path.strip()) if path.strip() else None
        if isinstance(v, list):
            v = v[0] if v else None
        if v not in (None, ""):
            return v
    return None


def _dig(obj, path):
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


def _absolute(u, prefix):
    u = str(u or "").strip()
    if not u or u.startswith(("http://", "https://")):
        return u
    if not prefix:
        return ""
    return prefix.rstrip("/") + "/" + u.lstrip("/")


def extract_site_search(rule: SiteSearchRule, text, model) -> list:
    """[(url, title)] from one search response, keeping ONLY results that
    carry the model code: an exact model field match, or the normalized code
    inside the normalized result name (or URL path, for regex/css rules).
    Retailer search is fuzzy - the exact product came back at rank 2 behind
    a lookalike - so an unfiltered result is never a candidate."""
    model_n = norm_code(model)
    if len(model_n) < MIN_CODE:
        return []
    out = []
    if rule.extract == "json":
        import json
        try:
            data = json.loads(text)
        except ValueError:
            return []
        items = _dig(data, rule.items) if rule.items else data
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            url = _absolute(_first(item, rule.url_field), rule.url_prefix)
            name = str(_first(item, rule.name_field) or "")
            exact = bool(rule.model_field) and norm_code(
                _first(item, rule.model_field)) == model_n
            if url and (exact or model_n in norm_code(name)):
                out.append((url, name))
    elif rule.extract == "regex":
        for m in re.finditer(rule.pattern, text or "", re.S | re.I):
            g = m.groupdict()
            url = _absolute(g.get("url"), rule.url_prefix)
            name = re.sub(r"<[^>]+>", " ", g.get("title") or "")
            if url and (model_n in norm_code(name) or model_n in slug_norm(url)):
                out.append((url, name.strip()))
    else:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(text or "", "html.parser")
        for el in soup.select(rule.selector):
            url = _absolute(el.get(rule.attr), rule.url_prefix)
            name = el.get_text(" ", strip=True)
            if url and (model_n in norm_code(name) or model_n in slug_norm(url)):
                out.append((url, name))
    must = re.compile(rule.url_must_match, re.I) if rule.url_must_match else None
    seen, dedup = set(), []
    for url, name in out:
        if must and not must.search(url):
            continue
        if url not in seen:
            seen.add(url)
            dedup.append((url, name))
    return dedup[:max(1, rule.max_results)]


# --------------------------------------------------------------------- CLI
def _client_settings(slug):
    """(max_age_days, extra_sources, disable) from client.toml, or defaults
    when there is no client config at all."""
    try:
        import client_config
        cfg = client_config.load_client(slug)
        ui = cfg.url_index
        return ui.max_age_days, ui.extra_sources, ui.disable
    except Exception:
        return DEFAULT_MAX_AGE_DAYS, (), ()


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(
        description="Local URL index (tier 0): retailer sitemaps in SQLite, "
                    "matched on the model code in the URL slug.")
    ap.add_argument("--client", default=None,
                    help="Client slug whose [url_index] table applies (default: "
                         "$PIPELINE_CLIENT, else shreejee)")
    ap.add_argument("--db", default=None, help=f"Index file (default {DB_PATH})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rf = sub.add_parser("refresh", help="Download / update the sitemaps")
    rf.add_argument("--source", action="append", metavar="NAME",
                    help="Only this source (repeatable)")
    rf.add_argument("--force", action="store_true",
                    help="Ignore lastmod / ETag and re-read every sitemap")
    sub.add_parser("stats", help="Sources, URL counts, last refresh")
    lk = sub.add_parser("lookup", help="Show the candidate URLs for a model code")
    lk.add_argument("model")
    lk.add_argument("--brand", default="")
    args = ap.parse_args(argv)

    _, extra, disable = _client_settings(args.client)
    try:
        sources, _rules = load_config(extra_sources=extra, disable=disable)
    except ConfigError as exc:
        sys.exit(str(exc))

    if args.cmd == "refresh":
        if args.source:
            unknown = set(args.source) - {s.name for s in sources}
            if unknown:
                sys.exit(f"Unknown or disabled source(s): {', '.join(sorted(unknown))}. "
                         f"Enabled: {', '.join(s.name for s in sources) or '(none)'}")
        res = refresh(sources, names=args.source, force=args.force, db_path=args.db)
        bad = [r["name"] for r in res if r["status"] == "failed"]
        print(f"Refreshed {len(res) - len(bad)}/{len(res)} source(s)"
              + (f"; failed: {', '.join(bad)}" if bad else ""))
        return 1 if bad and len(bad) == len(res) else 0
    if args.cmd == "stats":
        for line in stats_lines(args.db):
            print(line)
        return 0
    # lookup: rank like the enricher does, when it can be imported
    rank, brand_sites = None, ()
    try:
        import sku_enricher as E
        rank = E.domain_rank
        site = E.BRAND_SITES.get(args.brand.lower())
        brand_sites = (site,) if site else ()
    except Exception:
        pass
    hits = lookup(args.model, args.brand, sources=sources, db_path=args.db,
                  rank=rank, brand_domains=brand_sites)
    if not hits:
        print("no candidates")
    for h in hits:
        print(f"{h['source']:<16} {h['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
