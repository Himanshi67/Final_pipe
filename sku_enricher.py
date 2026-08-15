#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SKU Enricher
============
Reads SKUs from an Excel (.xlsx) or CSV file, searches the web for each product
(Amazon.in, Flipkart, the brand's official store, and other Indian retailers -
all searched before anything is accepted; strict accessory-proof matching) and
fills in:

    title | regular price (MRP, when shown) | sale price | url_link | image_url | status

Usage:
    python sku_enricher.py "WM MODEL LIST FOR WEBSITE.xlsx"
    python sku_enricher.py input.xlsx --limit 10
    python sku_enricher.py input.xlsx --sku-col "Model No" --sheet "Sheet1"
    python sku_enricher.py input.xlsx --force        (redo rows already filled)

Output:  <input name>_filled.xlsx   (the original file is never modified)
Re-running the same command RESUMES: rows that already have a url_link are skipped.

One-time install:
    pip install -r requirements.txt
"""

import argparse
import json
import atexit
import gzip
import hashlib
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin, urlsplit, urlunsplit

# A comma-joined URL list cannot be split on commas alone: Amazon's rendition
# modifiers legitimately contain them (`._AC_UL165_SR165,165_.jpg`), and a plain
# split shredded those into fragments like '165_.jpg'. Seen live losing 7 of 12
# images on one product. Split only where a comma is followed by a URL start.
_URL_SPLIT_RE = re.compile(r',(?=\s*(?:https?://|//))')


def split_image_list(raw):
    """Split a stored image list without breaking URLs that contain commas."""
    text = str(raw or '').strip()
    if not text:
        return []
    parts = _URL_SPLIT_RE.split(text)
    if len(parts) == 1 and ',' in text and '://' not in text:
        parts = text.split(',')          # not a URL list - old behaviour
    return [p.strip() for p in parts if p.strip()]


# ----------------------------- dependencies ------------------------------
try:
    from curl_cffi import requests as creq
    from bs4 import BeautifulSoup
    import openpyxl
except ImportError as e:
    sys.exit(f"Missing dependency ({e.name}). Run:  pip install -r requirements.txt")

try:                                   # search library: new name, then old
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        sys.exit("Missing dependency (ddgs). Run:  pip install -r requirements.txt")

# ----------------------------- configuration -----------------------------

PRIORITY_DOMAINS = [                   # tried in this order
    "amazon.in",
    "flipkart.com",
    "croma.com",
    "reliancedigital.in",
    "vijaysales.com",
    "tatacliq.com",
    "jiomart.com",
]

KNOWN_STORES = PRIORITY_DOMAINS + [    # acceptable fallbacks
    "haierindia.com", "haier.com", "samsung.com", "lg.com", "ifbappliances.com",
    "godrej.com", "godrejappliances.com", "whirlpoolindia.com",
    "bosch-home.in", "panasonic.com", "voltasbeko.com",
    "greateasternretail.com", "bajajmall.in", "sathya.store",
    "poojara.com", "lotmobiles.com",
    # TV / appliance brands that show up in these catalogues
    "sony.co.in", "tcl.com", "hisense.com", "onida.com", "lloyd.in",
    "bluestarindia.com", "usha.com", "havells.com", "philips.co.in",
    "crompton.co.in", "hindwareappliances.com", "elicaindia.com",
]

# official brand stores — searched in addition to the marketplaces
BRAND_SITES = {
    "haier": "haierindia.com",
    "samsung": "samsung.com",
    "lg": "lg.com",
    "ifb": "ifbappliances.com",
    "godrej": "godrejappliances.com",
    "whirlpool": "whirlpoolindia.com",
    "bosch": "bosch-home.in",
    "panasonic": "panasonic.com",
    "voltas": "voltasbeko.com",
    "sony": "sony.co.in",
    "tcl": "tcl.com",
    "hisense": "hisense.com",
    "onida": "onida.com",
    "usha": "usha.com",
    "lloyd": "lloyd.in",
}

BLOCKED_DOMAINS = [                    # never use these results
    "youtube.com", "facebook.com", "instagram.com", "x.com", "twitter.com",
    "pinterest.", "reddit.com", "quora.com", "wikipedia.org",
    "indiamart.com", "alibaba.com", "aliexpress.com", "ebay.",
    "91mobiles.com", "smartprix.com", "pricebaba.com", "mysmartprice.com",
]

# Generic category codes: an operator's shorthand for WHAT the product is, not
# who made it. Stripped from SKU text when building search queries, and - more
# importantly - never mistaken for the brand (see sku_parts).
#
# Measured on a 41-SKU multi-category crawl: 'WM-HTW120-178BK' searched with
# brand='WM', 'SM-BANDHAN-...' with brand='SM' and 'HWD-3WFMR' with brand='HWD'.
# assess() then demands the "brand" appear in the listing, so page after page
# came back 'rejected: brand not found in listing' - the single largest cause
# of NOT FOUND on ERP-coded lists.
CATEGORY_CODES = ("WM", "TV", "AC", "REF", "FRIDGE", "MWO", "DW",
                  "WH", "SM", "HWD", "IC", "ICT", "DI", "DF", "LED", "MW",
                  # store-side prefixes from app.SKU_PREFIX_BY_PARENT, so a
                  # published SKU pasted back in as input still parses
                  "CF", "KA", "HA", "SA", "EC", "AP")

# ---- strict-match filters: accessories & compatibility listings ----------
# Listings whose titles contain the model code but are NOT the product
# (covers, stands, "made for ..." etc.) are rejected by these rules.
ACCESSORY_RE = re.compile(
    r"\b(?:cover|stand|trolley|mat|pipe|hose|filter|lint|remote|bracket|tray|"
    r"protector|guard|skin|sticker|decal|spare|belt|coupling|gasket|seal|"
    r"descaler|descaling|detergent|softener|refill|rat mesh)s?\b", re.I)

COMPAT_PHRASES = ("made for", "compatible with", "for use with",
                  "replacement for", "designed for")

# capacity (kg) pattern and category words used to reject wrong-variant
# candidates when the exact model code could not be verified
KG_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-?\s*kg\b", re.I)

CATEGORY_WORDS = {
    "WM": ("washing machine", "washer"),
    "TV": ("tv", "television"),
    "AC": ("air conditioner", "split ac", "window ac"),
    "REF": ("refrigerator", "fridge"),
    "FRIDGE": ("refrigerator", "fridge"),
    "MWO": ("microwave",),
    "DW": ("dishwasher",),
}

MAX_IMAGES = 8            # max image URLs stored per product
MIN_IMAGES = 3            # a listing needs at least this many images to be
                          # accepted as final (else other sources are tried)
SEARCH_RESULTS = 10       # search results pulled per query
FETCH_BUDGET = 6          # max product pages fetched per SKU
SEARCH_POOL = 12          # stop searching once this many candidates wait -
                          # the fetch budget can never reach the ones beyond
                          # (2x FETCH_BUDGET keeps round-robin diversity)
DISCOVER_MODE = False     # --discover: links + photos only (see build_queries)
FETCH_TIMEOUT = 30        # seconds
MERGE_BUDGET = 2          # extra pages fetched purely to complete a verified
                          # row (see the price hunt at the end of lookup_sku)
MAX_SPECS = 24            # spec-table rows kept per product
SPECS_ENABLED = True      # scrape the page's spec table (--no-specs turns off)

OUT_COLS = ["title", "regular price", "sale price", "url_link", "image_url",
            "specs", "status"]

# app.py matches its input headers as exact strings (REQUIRED_INPUT_COLUMNS,
# app.py line 12) and raises if any is absent. OUT_COLS covers five of the
# eight; these are the rest, so the filled sheet drops straight into app.py.
# 'categories' and 'Brands' are created empty on purpose - app.py's
# infer_brand()/detect_category() fill them from category_map.xlsx.
APP_EXTRA_COLS = ["sku", "categories", "Brands"]

# ------------------------------ disk cache --------------------------------
# Nothing used to be cached, so every run re-paid the Serper credits and
# re-downloaded every page. That made the most valuable operation in this
# pipeline - re-running a finished batch through an IMPROVED parser - cost the
# same as scraping it from scratch, so it was never done and every parser fix
# only ever helped future batches.
#
# With the cache on, a second pass over a completed batch does no network I/O
# at all: the stored HTML is replayed through the current parsers. It also
# makes an interrupted batch resume without refetching, and turns the stored
# pages into ready-made test fixtures.
#
# Deliberate limits:
#   - only SUCCESSFUL fetches are stored. Caching a 403 would freeze a
#     transient block into a permanent one.
#   - entries expire (--cache-ttl, default 14 days), because a price that is
#     a fortnight old is not a price.
#   - a cache that cannot be read or written is a miss, never an error. It
#     must not be able to take a batch down.

CACHE_DIR = Path(__file__).resolve().parent / ".cache"
CACHE_ENABLED = True
CACHE_TTL_DAYS = 14.0
CACHE_REFRESH = False     # bypass reads but still write: force a fresh fetch
CACHE_STATS = {"page_hit": 0, "page_miss": 0, "page_write": 0,
               "search_hit": 0, "search_miss": 0, "search_write": 0}

# what the run actually spent, for the batch report. serper.dev bills per
# search call, so a cached search is a credit not spent.
RUN_STATS = {"serper_calls": 0, "ddg_calls": 0, "pages_fetched": 0,
             "blocked_domains": {}}

# Live round-trips made so far - cache hits excluded. fetch() and web_search()
# bump it just before they go to the network; polite_sleep() reads it to tell a
# real request apart from a cache hit.
NET_CALLS = 0
SLEEP_SAVED = 0.0         # seconds of politeness skipped, for the batch report


def polite_sleep(lo, hi, since=None):
    """Wait between requests so a retailer sees a human-paced crawl.

    The delay is owed to the REMOTE HOST, so a cache hit does not owe it: no
    server was touched and there is nothing to be polite to. Passing `since`
    (a NET_CALLS snapshot from just before the call) makes the sleep fire only
    when the wrapped call actually went out.

    This is what makes the documented free replay actually free. Re-running a
    finished batch through improved parsers does no network I/O, but every
    delay used to fire anyway, so a warm-cache replay of ~190 pages spent
    ~14 minutes asleep to do ~30 seconds of parsing."""
    global SLEEP_SAVED
    naptime = random.uniform(lo, hi)
    if since is not None and NET_CALLS == since:
        SLEEP_SAVED += naptime
        return
    time.sleep(naptime)


def _cache_file(kind, key):
    h = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()
    return CACHE_DIR / kind / h[:2] / (h + ".json.gz")


def cache_get(kind, key):
    """Stored value, or None for a miss. Never raises."""
    if not CACHE_ENABLED or CACHE_REFRESH:
        return None
    path = _cache_file(kind, key)
    try:
        if not path.exists():
            return None
        if CACHE_TTL_DAYS and (time.time() - path.stat().st_mtime
                               > CACHE_TTL_DAYS * 86400):
            return None
        with gzip.open(path, "rt", encoding="utf-8") as f:
            entry = json.load(f)
    except Exception:
        return None                  # corrupt or half-written entry = a miss
    if entry.get("key") != key:      # sha1 collision, or an older entry format
        return None
    return entry.get("data")


def cache_put(kind, key, data):
    """Store a value. Silent on failure - a full or read-only disk must not
    stop a batch that is otherwise working."""
    if not CACHE_ENABLED:
        return
    path = _cache_file(kind, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"key": key, "ts": time.time(), "data": data}, f)
        tmp.replace(path)            # atomic: a killed run leaves no half file
        CACHE_STATS[("page_write" if kind == "page" else "search_write")] += 1
    except Exception:
        pass


def cache_size(kind=""):
    """(entries, bytes) currently on disk, for one namespace or all of them."""
    n = total = 0
    root = CACHE_DIR / kind if kind else CACHE_DIR
    try:
        for p in root.rglob("*.json.gz"):
            n += 1
            total += p.stat().st_size
    except Exception:
        pass
    return n, total


def cache_info():
    n, total = cache_size()
    pages, page_bytes = cache_size("page")     # was a second full-tree walk
    log(f"Cache: {CACHE_DIR}")
    log(f"  {n} entr(ies), {total / 1_048_576:.1f} MB "
        f"({pages} page(s) at {page_bytes / 1_048_576:.1f} MB, "
        f"{n - pages} search(es))")
    log(f"  entries expire after {CACHE_TTL_DAYS:g} day(s) "
        f"(--cache-ttl, 0 = never)")


def cache_clear():
    n, total = cache_size()
    if not n:
        log(f"Cache is already empty ({CACHE_DIR}).")
        return
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    log(f"Cleared {n} cache entr(ies), {total / 1_048_576:.1f} MB, from {CACHE_DIR}.")

# ------------------------------- helpers ---------------------------------

def log(msg):
    print(msg, flush=True)


def norm(s):
    """lowercase alphanumerics only — for fuzzy matching"""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def indian_format(num):
    """22599 -> '22,599' (Indian digit grouping, no currency symbol)"""
    try:
        f = float(str(num).replace(",", "").strip())
    except (TypeError, ValueError):
        return str(num or "").strip()
    if f == int(f):
        s, dec = str(int(f)), ""
    else:
        s, dec = f"{f:.2f}".split(".")
        dec = "." + dec
    if len(s) <= 3:
        return s + dec
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail]) + dec


CURRENCY_NUM_RE = re.compile(r"(?:₹|rs\.?|inr)\s*([\d][\d,.\s]*\d|\d)", re.I)
BARE_NUM_RE = re.compile(r"(?<![\d.,])(\d[\d,.]*)(?![\d.,])")


def parse_amount(tok):
    """'22,599.00'->22599  '1,23,456'->123456  '22.599,00'->22599 (European)"""
    tok = re.sub(r"\s", "", str(tok or ""))
    if not tok:
        return None
    # European grouping: '22.599,00' - comma is the decimal separator
    if re.search(r",\d{2}$", tok) and ("." not in tok or tok.rfind(".") < tok.rfind(",")):
        tok = tok.replace(".", "").replace(",", ".")
    else:
        tok = tok.replace(",", "")
        # '22.599' is grouping, not a decimal fraction
        if tok.count(".") > 1 or ("." in tok and len(tok.split(".")[-1]) not in (1, 2)):
            tok = tok.replace(".", "")
    try:
        return float(tok)
    except ValueError:
        return None


def price_candidates(text):
    """Every plausible price in a blob, best-effort. When the text carries a
    currency marker ONLY currency-adjacent numbers count, so
    '1 offer from ₹22,599' yields 22599 rather than 1."""
    s = str(text or "")
    hits = CURRENCY_NUM_RE.findall(s) or BARE_NUM_RE.findall(s)
    vals = [v for v in (parse_amount(h) for h in hits)
            if v is not None and 1 <= v < 100_000_000]
    big = [v for v in vals if v >= 100]     # drop counters like '1 offer', '3 left'
    return big or vals


def clean_price(text, prefer="first"):
    """'₹22,599.00' -> '22,599'.  prefer: first | min | max (for ranges)."""
    vals = price_candidates(text)
    if not vals:
        return ""
    val = {"min": min(vals), "max": max(vals)}.get(prefer, vals[0])
    return indian_format(val) if val > 0 else ""


STORE_TITLE_SUFFIX = re.compile(
    r"\s*(?:[-|:]\s*|\bat\s+)?(?:croma|reliance digital|ifb appliances|"
    r"haier appliances|amazon\.in|flipkart(?:\.com)?|vijay sales|tata cliq|"
    r"jiomart|samsung india)\s*$", re.I)

# The named list above only catches stores someone thought of. A brand site's
# <title> ends with its own branding too - haier.com serves
# "...H32K82FX (2025)-Haier India", and 9 of 43 titles in the LED batch carried
# a suffix like it straight onto the storefront. This is the general form: a
# SHORT trailing segment, after a dash or pipe, that ends in a site-ish word.
# Length-capped and keyword-anchored so it cannot eat real title content.
SITE_BRAND_SUFFIX = re.compile(
    r"\s*[-|–—]\s*[\w .&']{0,20}?"
    r"(?:india|\.in|\.com|online|e-?store|electronics|appliances)\s*$", re.I)


def clean_title(t):
    """'Buy X Online - Croma' / 'Buy X at Reliance Digital' -> 'X'"""
    t = re.sub(r"^\s*buy\s+", "", str(t or "").strip(), flags=re.I)
    for _ in range(2):
        t = STORE_TITLE_SUFFIX.sub("", t).strip()
        # only strip a general site suffix while enough title survives it -
        # never let branding-removal gut a short title
        stripped = SITE_BRAND_SUFFIX.sub("", t).strip()
        if len(stripped) >= 20:
            t = stripped
        t = re.sub(r"\s+online\s*$", "", t, flags=re.I).strip()
    return t


def domain_of(url):
    try:
        d = urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""


def absolutize_url(base, u):
    """'//cdn/x.jpg' and '/media/x.jpg' -> absolute URL. extract() used to drop
    both with a bare u.startswith('http') test, losing whole galleries."""
    u = str(u or "").strip().strip('"\'')
    if not u or u.startswith(("data:", "javascript:", "about:")):
        return ""
    u = u.replace("&amp;", "&")
    if u.startswith("//"):
        return "https:" + u
    if u.startswith(("http://", "https://")):
        return u
    if re.match(r"^[a-zA-Z][\w+.-]*:", u):
        return ""                       # mailto:, tel:, unknown scheme
    try:
        return urljoin(base, u) if base else ""
    except Exception:
        return ""


IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|avif)(?:$|[?#])", re.I)
BAD_EXT_RE = re.compile(r"\.(?:svg|gif|ico|bmp|js|css|json|mp4|webm)(?:$|[?#])", re.I)
IMAGE_JUNK_RE = re.compile(
    r"sprite|logo|favicon|placeholder|blank|spacer|loader|loading|spinner|"
    r"banner|badge|payment|visa|mastercard|rupay|upi|emi|whatsapp|rating|"
    r"star-?\d|arrow|play-?button|pixel|1x1|/flags?/|/social/|/ads?/|/icons?/|"
    # page chrome: headers, nav menus, splash/app assets, CMS fragments
    r"app-?icon|splash|experience-fragment|navigation-card|/header/|/footer/|"
    r"/nav(?:bar|igation)?/|/menu/|/banners?/|/promo|[-_/]icon\.|/search/|"
    r"/static-pages?/|homepage|feedback|[-_/]close[-_.]|_jcr_content|"
    r"vs-plus|/desktop/|/subcategory/|/category/image|"
    # storefront UI chrome, often mis-resolved against the product path
    r"[-_/](?:cart|basket|wishlist|compare|account|profile|login|user|"
    r"hamburger|burger|share|heart|star|zoom|magnif)[-_.]",
    re.I)

# an <img>'s own alt/class often admits it is page furniture even when the URL
# looks innocent (shop.haierindia.com ships alt="category-img" nav tiles on
# every product page)
JUNK_IMG_ATTR_RE = re.compile(
    r"logo|category-img|categoryimg|nav|menu|banner|icon|thumb-nav|"
    r"placeholder|social|payment|footer|header", re.I)


def junk_img_element(el):
    for attr in ("alt", "class", "id"):
        val = el.get(attr)
        if isinstance(val, (list, tuple)):
            val = " ".join(val)
        if val and JUNK_IMG_ATTR_RE.search(str(val)):
            return True
    return False


MIN_GOOD_PX = 400        # below this a rendition is a thumbnail, not a photo


# a number only means "pixels" in one of these shapes. Matching bare digits
# instead reads sizes out of hashes and uuids ('ur558uB41', '7ea9b3744b37').
SIZE_PATH_RES = (
    re.compile(r"(\d{2,4})\s*[xX×]\s*\d{2,4}"),          # 1200x1200
    re.compile(r"\d{2,4}\s*[xX×]\s*(\d{2,4})"),
    re.compile(r"(\d{2,4})[Ww]x\d{2,4}[Hh]"),                 # 320Wx357H
    re.compile(r"\d{2,4}[Ww]x(\d{2,4})[Hh]"),
    re.compile(r"[_.]S[LXY](\d{2,4})_"),                      # amazon ._SL1500_.
    re.compile(r"[_-]p?(\d{2,4})(?=\.\w{3,4}$)"),             # ..._1200.jpg
    re.compile(r"/(\d{2,4})/"),                               # /416/416/
)
SIZE_QUERY_RE = re.compile(r"(?:^|&)(?:w|width|h|height|size|sz|maxwidth)=(\d{2,4})\b", re.I)


def image_size_hint(u):
    """Biggest pixel dimension the URL actually advertises, for picking the
    largest variant of a photo ('..._1200.jpg' over '..._60.jpg'). 0 means the
    URL says nothing about size, which is normal and implies nothing bad."""
    try:
        pr = urlsplit(u)
    except Exception:
        return 0
    nums = []
    for rx in SIZE_PATH_RES:
        nums += [int(n) for n in rx.findall(pr.path)]
    nums += [int(n) for n in SIZE_QUERY_RE.findall(pr.query)]
    nums = [n for n in nums if 8 <= n <= 4000]
    return max(nums) if nums else 0


RESIZE_PARAMS = {"optimize", "fit", "height", "width", "resize", "quality",
                 "dpr", "sharpen"}


# Amazon encodes the rendition in the FILENAME: 71dxcllI6RL._SL1500_.jpg means
# "longest side 1500", ._AC_SX466_. means something narrower again. Dropping the
# modifier returns the original upload. Measured on a live product image:
# ._SL1500_. gave 1500x927 / 113 KB, the bare name gave 2392x1479 / 220 KB - so
# the pipeline was discarding 60% of the resolution it had already found.
AMAZON_IMG_HOSTS = ("media-amazon.com", "images-amazon.com", "ssl-images-amazon.com")
AMAZON_RENDITION_RE = re.compile(r"\._[A-Z0-9_,%-]+_\.(?=[A-Za-z]{3,4}$)")


def upsize_image_url(u):
    """Ask a CDN for the biggest rendition it will serve.

    Magento-style stores (/media/catalog/product/) serve the ORIGINAL file once
    the resize params are dropped - measured 34.9 KB vs 14.2 KB on vijaysales.
    Amazon does the same when its filename modifier is removed. Both are scoped
    narrowly, because other CDNs 404 without their params (tatacliq does,
    verified) and a broken URL costs us the image entirely."""
    try:
        pr = urlsplit(u)
    except Exception:
        return u
    if any(h in pr.netloc.lower() for h in AMAZON_IMG_HOSTS):
        return urlunsplit((pr.scheme, pr.netloc,
                           AMAZON_RENDITION_RE.sub(".", pr.path),
                           pr.query, pr.fragment))
    if "/media/catalog/product/" not in pr.path.lower() or not pr.query:
        return u
    keep = [kv for kv in pr.query.split("&")
            if kv and kv.split("=")[0].lower() not in RESIZE_PARAMS]
    return urlunsplit((pr.scheme, pr.netloc, pr.path, "&".join(keep), pr.fragment))


def is_thumbnail(u):
    """A URL that openly advertises a small rendition. Catalogue images need
    real photos - a 60px thumbnail is worse than no extra image at all."""
    h = image_size_hint(u)
    return 0 < h < MIN_GOOD_PX
TINY_SIZE_RE = re.compile(r"/\d{1,2}x\d{1,2}/|[?&](?:w|width)=\d{1,2}\b", re.I)


def looks_like_product_image(u):
    """Gate for the widened image search: without it the site logo and payment
    icons would land in image_url on every generic-domain row."""
    low = u.lower()
    if BAD_EXT_RE.search(low) or IMAGE_JUNK_RE.search(low) or TINY_SIZE_RE.search(low):
        return False
    if "amazon" in low and "/images/i/" not in low:
        return False            # amazon: only /images/I/ is gallery, G/S/P is chrome
    return bool(IMAGE_EXT_RE.search(low)
                or re.search(r"/(?:image|images|media|catalog|product)s?/", low))


# query params that only vary the rendering of the SAME photo
SIZE_PARAMS = {"w", "h", "width", "height", "q", "quality", "fit", "crop", "dpr",
               "format", "fm", "auto", "resize", "size", "scale", "tr", "v",
               "ver", "version", "t", "ts", "cache", "rev", "_"}


def image_key(u):
    """Dedupe key: URL minus size/cache params only. Splitting on '?' wholesale
    collapsed galleries where the query string IS the selector (?id=2, &idx=3)."""
    try:
        pr = urlsplit(u)
    except Exception:
        return u
    path = re.sub(r"/\d{2,4}x\d{2,4}/", "/SZ/", pr.path)          # /416/416/ style
    path = re.sub(r"[_-]\d{2,4}x\d{2,4}(?=\.\w{3,4}$)", "_SZ", path)
    path = re.sub(r"\._[A-Z0-9,_]+_\.", ".", path)                # amazon _SX679_
    # '..._1200.jpg' / '..._1200_p1911.jpg' / '..._60.jpg' are one photo at
    # several sizes. Single digits ('_1', '_2') are gallery indices and must
    # stay distinct, so the run requires two digits or more.
    path = re.sub(r"(?:[_-]p?\d{2,4})+(?=\.\w{3,4}$)", "_SZ", path)
    keep = sorted(kv for kv in pr.query.split("&")
                  if kv and kv.split("=")[0].lower() not in SIZE_PARAMS)
    return pr.netloc.lower() + path + ("?" + "&".join(keep) if keep else "")


def image_dir(u):
    try:
        pr = urlsplit(u)
        return pr.netloc.lower() + pr.path.rsplit("/", 1)[0]
    except Exception:
        return ""


def dedupe_images(urls, base="", near="", limit=None):
    """Absolutize, drop junk, collapse size variants, cap at `limit`
    (MAX_IMAGES by default).
    `near` is the page's declared product image: anything sitting in the same
    directory is ranked first, which keeps site furniture (logos living under
    /images/ while the gallery lives under /tvs/) out of the top slots."""
    if limit is None:
        limit = MAX_IMAGES
    seen, out = {}, []
    for u in urls:
        u = upsize_image_url(absolutize_url(base, u))
        if not u or not looks_like_product_image(u):
            continue
        k = image_key(u)
        if k in seen:
            # same photo, different size: keep the larger rendition in place
            i = seen[k]
            if image_size_hint(u) > image_size_hint(out[i]):
                out[i] = u
            continue
        seen[k] = len(out)
        out.append(u)
    # full-size photos first, and within each group the ones sitting beside the
    # page's declared product image. Both keys in one stable sort so a big photo
    # never gets pushed below a thumbnail.
    d = image_dir(absolutize_url(base, near)) if near else ""
    out.sort(key=lambda u: (is_thumbnail(u), 0 if d and image_dir(u) == d else 1))
    return out[:limit]


def largest_srcset(val):
    """'a.jpg 400w, b.jpg 1200w' -> 'b.jpg'"""
    best, best_w = "", -1
    for part in str(val or "").split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = -1
        if len(bits) > 1:
            m = re.match(r"(\d+(?:\.\d+)?)[wx]$", bits[-1])
            if m:
                w = float(m.group(1))
        if w >= best_w:
            best, best_w = bits[0], w
    return best


IMG_ATTRS = ("data-zoom-image", "data-large-image", "data-old-hires", "data-src",
             "data-lazy", "data-lazy-src", "data-original", "data-image", "src")


# (attribute, value) pairs for the page-declared product image, in priority
# order. Order is load-bearing - see the bucket note in collect_images.
META_IMAGE_KEYS = (("property", "og:image"), ("property", "og:image:url"),
                   ("property", "og:image:secure_url"),
                   ("name", "twitter:image"), ("name", "twitter:image:src"))


def collect_images(soup, html, base_url=""):
    """Gallery images in tiers of decreasing trust. Later tiers only run when
    the earlier ones came up short, because a page-wide <img> sweep also picks
    up related products and page furniture."""
    def usable(urls):
        return dedupe_images([u for u in urls if u], base_url)

    prods = jsonld_products(soup)
    main = main_product(prods)
    anchor = ""                       # the page's own product image, if declared

    # tier 1 - declared by the page as ITS product
    #
    # One pass per tag name rather than one CSS select per selector. The five
    # meta selectors alone walked the whole document five times; on the 189-page
    # cache this tier went 12.33s -> 2.19s (-82%) for identical output.
    # The buckets exist to preserve ORDER: og:image must still come before
    # twitter:image regardless of where they sit in the document, because the
    # first usable entry becomes the gallery anchor.
    tier1 = list(jsonld_images(main)) if main else []
    for el in soup.find_all(attrs={"itemprop": "image"}):
        tier1.append(el.get("content") or el.get("src") or el.get("href") or "")

    buckets = {key: [] for key in META_IMAGE_KEYS}
    for meta in soup.find_all("meta"):
        for key in META_IMAGE_KEYS:
            if meta.get(key[0]) == key[1]:
                buckets[key].append(meta.get("content", ""))
    for key in META_IMAGE_KEYS:
        tier1.extend(buckets[key])

    for el in soup.find_all("link"):
        rel = el.get("rel") or []
        if not isinstance(rel, (list, tuple)):
            rel = [rel]
        if "image_src" in rel or ("preload" in rel and el.get("as") == "image"):
            tier1.append(el.get("href") or "")
    out = list(tier1)
    ok1 = usable(out)
    if ok1:
        anchor = ok1[0]
    if len(ok1) >= MIN_IMAGES:
        return [u for u in out if u]

    # tier 2 - other JSON-LD products, then responsive/lazy attributes
    for p in prods:
        if p is not main:
            out.extend(jsonld_images(p))
    # same single-pass treatment: the '[data-srcset]' arm makes this selector
    # document-wide, and soupsieve charges for that. 5.07s -> 0.26s (-95%).
    for el in soup.find_all(True):
        if not (el.has_attr("data-srcset")
                or (el.name in ("source", "img") and el.has_attr("srcset"))):
            continue
        if junk_img_element(el):
            continue
        out.append(largest_srcset(el.get("srcset") or el.get("data-srcset")))
    for el in soup.find_all("img"):
        if junk_img_element(el):
            continue
        for a in IMG_ATTRS:                     # best attribute wins, one per <img>
            if el.get(a):
                out.append(el.get(a))
                break
    if len(usable(out)) >= MIN_IMAGES:
        return dedupe_images([u for u in out if u], base_url, near=anchor)

    # tier 3 - raw sweep, for galleries only present in inline JS
    out.extend(re.findall(r"https?://[^\s\"'<>\\)]+?\.(?:jpe?g|png|webp)",
                          html.replace("\\/", "/"))[:60])
    return dedupe_images([u for u in out if u], base_url, near=anchor)


# The label must not run straight into another letter. Without the lookahead,
# 'Was' matches inside 'Washing' - which is on every washing-machine page,
# hundreds of times - and each of those false starts pays for the optional
# tag-run group behind it. Measured on the 189-page cache: 7.70s -> 6.08s
# (-21%) for byte-identical output. (\b alone will not do it: the label may
# legitimately end in '.', as in 'M.R.P.', and \b after an optional dot is not
# the boundary you want.)
MRP_LABEL_RE = re.compile(
    r"\b(?:M\.?R\.?P\.?|Maximum Retail Price|List Price|Was)(?![A-Za-z])\s*"
    r"(?:\([^)]{0,40}\))?\s*[:\-]?\s*(?:<[^>]{0,120}>\s*){0,6}"
    r"(?:₹|Rs\.?|INR)\s*([\d][\d,.]*)", re.I)

# Substrings that mark a strike-through price in a class name. Matched
# case-sensitively against the raw class string, which is what the CSS
# '[class*=...]' selectors this replaced did - hence both 'mrp' and 'MRP'.
STRIKE_CLASS_MARKS = ("strike", "Strike", "mrp", "MRP", "old-price",
                      "oldPrice", "regular-price")


def mrp_near_label(html, sale=""):
    """MRP by the WORD 'MRP' rather than a CSS class - labels don't rotate the
    way obfuscated class names do (flipkart's div.yRaY8j and friends)."""
    ps = price_value(sale)
    for m in MRP_LABEL_RE.finditer(str(html or "")):
        v = clean_price(m.group(1))
        pv = price_value(v)
        if pv and (ps is None or pv > ps):
            return v
    return ""


def strike_elements(soup, limit=40):
    """Elements that mark a struck-through / was-price, in document order.

    The equivalent CSS - 'del, s, strike, [class*=strike], [class*=mrp], ...' -
    is seven attribute-substring selectors, and soupsieve re-walks the document
    for each. One pass testing the class string directly gives the same
    elements: 15.20s -> 0.67s (-96%) across the 189-page cache, verified
    element-for-element. Stops at `limit`, so a page with a thousand <del>s in
    a changelog does not pay for all of them."""
    out = []
    for el in soup.find_all(True):
        if el.name in ("del", "s", "strike"):
            out.append(el)
        else:
            names = el.get("class")
            names = " ".join(names) if isinstance(names, (list, tuple)) else (names or "")
            if names and any(mark in names for mark in STRIKE_CLASS_MARKS):
                out.append(el)
        if len(out) >= limit:
            break
    return out


def mrp_from_strike(soup, sale=""):
    ps = price_value(sale)
    for el in strike_elements(soup):
        v = clean_price(el.get_text(" ", strip=True))
        pv = price_value(v)
        if pv and (ps is None or pv > ps):
            return v
    return ""


def domain_rank(url):
    d = domain_of(url)
    for i, p in enumerate(PRIORITY_DOMAINS):
        if d.endswith(p):
            return i
    for p in KNOWN_STORES:
        if d.endswith(p):
            return len(PRIORITY_DOMAINS)
    return len(PRIORITY_DOMAINS) + 1


def is_blocked(url):
    d = domain_of(url)
    if not d:
        return True
    if d.split(".")[0] in ("stage", "staging", "dev", "test", "uat", "preprod"):
        return True                     # never use staging/test environments
    for b in BLOCKED_DOMAINS:
        if b.endswith("."):                 # prefix entries: 'pinterest.', 'ebay.'
            if b in d:
                return True
        elif d == b or d.endswith("." + b):
            return True
    return False


def canonical_url(url):
    d = domain_of(url)
    if "amazon." in d:
        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
        if m:
            return f"https://www.{d}/dp/{m.group(1)}"
    if "flipkart.com" in d:
        return url.split("?")[0]
    return url


JUNK_PATH_RE = re.compile(
    # documents and media served where a page was expected
    r"\.(?:pdf|docx?|xml|xlsx?|jpe?g|png|webp|gif|svg|mp4|zip)($|\?)|"
    # editorial / support / corporate pages
    r"/blog/|/help/|/support/|/customer-care|/store-locator|/compare|"
    r"/wishlist|/cart\b|/reviews?\b|/questions|/about|/faqs?\b|/sitemap|"
    r"/(?:terms|privacy|policies|warranty-policy|careers?|press-?room|newsroom)\b|"
    # anything behind a login is never a product page
    r"/(?:login|sign-?in|register|my-?account|checkout)\b|"
    # search result pages
    r"/search\b|[?&](?:q|query|keyword|search)=|"
    # category / brand / tag LISTING pages. A trailing-segment match only, so
    # Shopify's /collections/<c>/products/<p> - a real product - still passes.
    r"/(?:category|categories|subcategory|sub-category|tag)/|"
    r"/(?:collections?|brands?)/[^/?#]*/?(?:$|[?#])|/page/\d+|"
    r"[?&]category_uid=|/products\?",
    re.I)


def is_product_url(url):
    """Only fetch actual product pages - not search/category/blog/PDF links."""
    d, low = domain_of(url), url.lower()
    if JUNK_PATH_RE.search(low):
        return False
    if d.endswith("amazon.in"):
        return "/dp/" in low
    if d.endswith("flipkart.com"):
        return "/p/" in low
    if d.endswith("croma.com"):
        return "/p/" in low
    if d.endswith("reliancedigital.in"):
        return "/product" in low
    if d.endswith("tatacliq.com"):
        return "/p-" in low
    if d.endswith("jiomart.com"):
        return "/p/" in low
    return True

# ------------------------------ SKU parsing ------------------------------

def sku_parts(sku):
    """('SAMSUNG', 'WA70BG4441YYTL') from 'SAMSUNG-WA70BG4441YYTL'

    A leading CATEGORY_CODE is not a brand. 'WM-HTW120-178BK' means "washing
    machine, model HTW120-178BK" - taking 'WM' as the brand poisons the search
    query AND assess()'s brand-presence gate, which is the dominant NOT FOUND
    cause on ERP-coded lists. When the code is all there is, the brand is
    simply unknown ('') and assess() skips the brand check rather than
    rejecting every real page for want of a word that was never a brand."""
    sku = str(sku).strip()
    # '63HNCCC21V9SN(WATER DISPENSER)' - a bracketed note is never the brand
    sku = re.sub(r"\([^)]*\)", " ", sku).strip(" -,")
    brand = ""
    for token in re.split(r"[-\s]", sku):
        if not token or token.upper() in CATEGORY_CODES:
            continue                       # a category code, not a maker
        # A brand name carries no digits. Without this the token AFTER the
        # category code gets promoted instead, which is the MODEL - 'WM-HTW120
        # -178BK' would hand back brand='HTW120' and 'IC-CJ1360WPC'
        # brand='CJ1360WPC'. When the list codes no maker at all the honest
        # answer is that the brand is unknown.
        if re.fullmatch(r"[A-Za-z][A-Za-z&.']{1,}", token):
            brand = token
        break
    models = []
    for t in re.split(r"[\s,/]+", sku):
        t = t.strip("()-.")
        # strip a brand prefix glued onto the model code
        # ('SAMSUNG-WW12DG5B24ABTL' -> 'WW12DG5B24ABTL', else the quoted
        #  search '"SAMSUNG-WW12DG5B24ABTL"' matches nothing anywhere)
        if brand:
            t = re.sub(r"^" + re.escape(brand) + r"[-_]+", "", t, flags=re.I)
        # ...and the same for a category code, which is glued on exactly the
        # same way ('WM-HTW120-178BK' -> 'HTW120-178BK'). This used to happen
        # for free because the code WAS the brand; now that it is correctly
        # not, the model still has to be freed of it or the quoted search
        # '"WM-HTW120-178BK"' matches nothing anywhere either.
        t = re.sub(r"^(?:" + "|".join(CATEGORY_CODES) + r")[-_]+", "", t,
                   flags=re.I)
        if (len(t) >= 4 and re.search(r"\d", t) and re.search(r"[A-Za-z]", t)
                and (not brand or norm(t) != norm(brand))):
            models.append(t)
    models.sort(key=len, reverse=True)     # longest token = model code, usually
    return brand, (models[0] if models else "")


def build_queries(sku):
    brand, model = sku_parts(sku)
    rest = str(sku)
    if brand:
        rest = re.sub(r"^\s*" + re.escape(brand) + r"[-\s]*", "", rest, flags=re.I)
    rest = re.sub(r"^(" + "|".join(CATEGORY_CODES) + r")\b[\s-]*", "", rest, flags=re.I).strip()

    brand_site = BRAND_SITES.get(brand.lower(), "") if brand else ""

    def phrase(*parts):
        """Join the parts that exist. With no brand this must not emit a query
        with a leading space or a doubled one."""
        return " ".join(p for p in parts if p)

    # Two tiers, run lazily by lookup_sku. The model code is the strongest
    # signal there is - when its searches produce candidates, the full-title
    # searches only re-find the same listings, so they cost 3 searches per SKU
    # for nothing. They still exist as tier 2, for SKUs whose model code
    # parses badly or is not indexed: tier 2 runs when tier 1 came up empty
    # or everything it found failed verification.
    tier1, tier2 = [], []     # [(query, restrict-to-domain or None), ...]
    if model:
        # short/ambiguous codes ('600C', '7.0KG') search better with the brand
        qm = f'"{model}"' if len(model) >= 6 or not brand else phrase(brand, f'"{model}"')
        tier1.append((f'{qm} site:amazon.in', "amazon.in"))
        tier1.append((f'{qm} site:flipkart.com', "flipkart.com"))
        if not DISCOVER_MODE:
            # pass 1 of the two-pass flow needs A reviewable link, not every
            # marketplace's copy - croma and the brand site rarely beat the
            # two above, and a discover miss is the human's to fix anyway
            tier1.append((f'{qm} site:croma.com', "croma.com"))
            if brand_site:
                tier1.append((f'{qm} site:{brand_site}', brand_site))
        tier1.append((phrase(brand, model, 'price buy online'), None))
    if rest and norm(rest) != norm(model):
        tier2.append((phrase(brand, rest, 'site:amazon.in'), "amazon.in"))
        tier2.append((phrase(brand, rest, 'site:flipkart.com'), "flipkart.com"))
        tier2.append((phrase(brand, rest, 'price india'), None))
    if not tier1:             # no model code parsed: the title IS the query
        tier1, tier2 = tier2, []
    if not tier1:
        tier1 = [(f'{sku} price india', None)]
    return brand, model, [t for t in (tier1, tier2) if t]

# -------------------------------- search ---------------------------------

_serper_failed_warned = False

def serper_search(query):
    """Optional Google search via serper.dev — used if SERPER_API_KEY is set."""
    global _serper_failed_warned
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        return None
    try:
        RUN_STATS["serper_calls"] += 1        # billed whatever the response is
        r = creq.post("https://google.serper.dev/search",
                      headers={"X-API-KEY": key, "Content-Type": "application/json"},
                      json={"q": query, "gl": "in", "num": SEARCH_RESULTS},
                      timeout=20)
        if r.status_code != 200:
            # error bodies parse fine but have no "organic" key, which used to
            # look like a trusted empty result and mask a dead/over-quota key
            if not _serper_failed_warned:
                log(f"      serper.dev HTTP {r.status_code} (bad key or out of "
                    f"credits?) - falling back to DuckDuckGo")
                _serper_failed_warned = True
            return None
        return [{"href": it.get("link", ""), "title": it.get("title", "")}
                for it in r.json().get("organic", [])]
    except Exception:
        return None


def ddg_search(query):
    for attempt in (1, 2):
        try:
            RUN_STATS["ddg_calls"] += 1
            with DDGS() as d:
                res = list(d.text(query, region="in-en", max_results=SEARCH_RESULTS))
            return [{"href": it.get("href") or it.get("url") or "",
                     "title": it.get("title", "")} for it in res]
        except Exception as e:
            if attempt == 1:
                # a rate-limit needs the full cool-down; any other hiccup
                # (DNS blip, parse error) never benefits from 30 idle seconds.
                # The ddgs/duckduckgo_search libraries surface throttling
                # inconsistently - RatelimitException, or a generic exception
                # whose MESSAGE says '202 Ratelimit' - so match both surfaces.
                blob = f"{type(e).__name__} {e}".lower()
                wait = 30 if "ratelimit" in blob or "rate limit" in blob else 10
                log(f"      search hiccup ({type(e).__name__}) - waiting {wait}s...")
                time.sleep(wait)
    return []


def web_search(query):
    """Cached in front of the providers, so a repeated run of the same batch
    spends no Serper credits at all. An EMPTY serper answer is cached (Google
    genuinely found nothing); an empty DuckDuckGo answer is not, because that
    is nearly always the library failing rather than a real absence."""
    global NET_CALLS
    hit = cache_get("search", query)
    if hit is not None:
        CACHE_STATS["search_hit"] += 1
        log("      (cached search)")
        return hit
    CACHE_STATS["search_miss"] += 1
    NET_CALLS += 1

    res = serper_search(query)
    if res is not None:      # serper answered (even if empty) - trust Google
        cache_put("search", query, res)
        return res
    res = ddg_search(query)
    if res:
        cache_put("search", query, res)
    return res

# -------------------------------- fetching -------------------------------

# Chrome builds current as of 2026. The old ("chrome", "chrome120",
# "chrome110") pool advertised 2023 browsers, which is itself a block trigger.
IMPERSONATE_POOL = ("chrome146", "chrome145", "chrome142", "chrome136", "chrome131")
IMPERSONATE_LAST = "firefox147"       # final try only: a different TLS stack

BLOCKED_TITLES = ("amazon.in", "amazon.com", "robot check", "access denied",
                  "sorry! something went wrong!", "retry later",
                  "site maintenance")

BLOCKED_TITLE_SUBSTR = ("just a moment", "attention required", "access denied",
                        "robot check", "are you a human", "security check",
                        "verify you are human")

BLOCK_MARKERS = (
    ("Enter the characters you see below", "amazon captcha"),
    ("To discuss automated access to Amazon data", "amazon bot notice"),
    ("Just a moment...", "cloudflare challenge"),
    ("cf_chl_opt", "cloudflare challenge"),
    ("cf-browser-verification", "cloudflare challenge"),
    ("Checking your browser before accessing", "cloudflare challenge"),
    ("/_Incapsula_Resource", "imperva/incapsula"),
    ("Incapsula incident ID", "imperva/incapsula"),
    ("Pardon Our Interruption", "imperva bot page"),
    ("Access to this page has been denied", "perimeterx"),
    ("px-captcha", "perimeterx"),
    ("captcha-delivery.com", "datadome"),
    ("unusual traffic from your computer network", "google rate-limit"),
)


def looks_blocked(html):
    """Return a short REASON when the page is an interstitial rather than the
    product, '' when it looks real. Returning the reason (instead of a bare
    True) is what lets a blocked row be reported as blocked instead of
    silently becoming NOT FOUND."""
    # head + tail: amazon's markers sit early, akamai/imperva footers land last
    scan = html if len(html) <= 300000 else html[:150000] + html[-20000:]
    for needle, why in BLOCK_MARKERS:
        if needle in scan:
            return why
    m = re.search(r"<title>\s*([^<]*?)\s*</title>", scan[:20000], re.I)
    if m:
        t = m.group(1).strip().lower()
        if t in BLOCKED_TITLES or any(s in t for s in BLOCKED_TITLE_SUBSTR):
            return f"blocked page '{t[:40]}'"
    return ""


FETCH_ATTEMPTS = 3
HARD_BLOCK_CODES = (403, 429, 503)
DEAD_CODES = (404, 410)

_SESSIONS = {}          # domain -> curl_cffi Session (its own cookie jar)
_PROFILES = {}          # domain -> impersonation profile currently in use
_LAST_URL = {}          # domain -> last URL fetched there, for a real Referer


def get_session(dom, fresh=False):
    """One cookie jar per domain: cookies are domain-scoped anyway, and a
    burned identity on amazon.in shouldn't reset the jar for flipkart."""
    if fresh or dom not in _SESSIONS:
        old = _SESSIONS.get(dom)          # was _SESSIONS[dom]: a KeyError on
        if old is not None:               # every first use, swallowed silently
            try:
                old.close()
            except Exception:
                pass
        _SESSIONS[dom] = creq.Session()
        _PROFILES.pop(dom, None)
        _LAST_URL.pop(dom, None)
    return _SESSIONS[dom]


# Must equal app.py's MIN_IMAGE_EDGE. It said so before too, but the value was
# 500 against app.py's 1000, so stage 1 kept every 500-999px image, counted it
# in the row's image total and reported "FOUND (amazon.in)" with 8 images -
# then app.py silently refused those same images at download time and the
# product shipped with fewer, or none. The sheet a human reviews has to state
# what will actually ship. tests/test_hd_images.py pins the two together.
IMAGE_MIN_PX = 1000       # HD floor; --min-image-px overrides
IMAGE_PROBE_BYTES = 65536  # enough of the file to read its header
IMAGE_PROBE_MAX = 14       # per record; a gallery longer than this is padding
# extract() keeps this many candidates so the size filter downstream has spares
# to fall back on. It used to cut to MAX_IMAGES first, so a page whose top 8
# candidates included 5 thumbnails ended at 3 images even though good ones were
# sitting just below the cut. Equal to IMAGE_PROBE_MAX so every image in the
# pool actually gets measured; lookup_sku trims back to MAX_IMAGES afterwards.
IMAGE_POOL = IMAGE_PROBE_MAX
IMAGE_PROBE_WORKERS = 8    # concurrent header probes; these are 64 KB GETs to
                           # a CDN, not the polite-delay page fetches
IMAGE_CHECK = True         # --no-image-check turns this off
_IMG_DIMS = {}             # url -> (w, h) | None, measured once per run


def image_dimensions(url):
    """(width, height) read from the file HEADER only, or None if unknown.

    Asks for the first 64 KB with a Range request rather than the whole file:
    every format we care about carries its dimensions in the first few
    kilobytes, so this costs a fraction of a download.

    None means "could not tell", and callers must KEEP those - dropping a good
    photo because a CDN refused a Range request is worse than the thumbnail we
    were trying to avoid."""
    if url in _IMG_DIMS:
        return _IMG_DIMS[url]
    dims = None
    try:
        from io import BytesIO
        from PIL import Image
        dom = domain_of(url)
        # A fresh request rather than the per-domain Session: these run in a
        # thread pool (see probe_many), and the shared Session carries a cookie
        # jar that is not safe to hammer concurrently. Image CDNs need no
        # cookie continuity, so there is nothing to lose here.
        r = creq.get(
            url, headers={"Range": f"bytes=0-{IMAGE_PROBE_BYTES - 1}",
                          "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
            impersonate=_PROFILES.get(dom) or IMPERSONATE_POOL[0], timeout=20)
        if r.status_code < 400 and r.content:
            with Image.open(BytesIO(r.content)) as im:
                dims = im.size
    except Exception:
        dims = None                      # unreadable -> unknown, never "small"
    if len(_IMG_DIMS) >= 20000:
        # ~10 MB of URL keys on a very large batch. Evict the OLDEST half
        # rather than clearing: the gallery pipeline queries the same URLs
        # back-to-back (photos pass, then banners pass, after a thread-pool
        # prefetch), and a full clear mid-gallery would re-probe all of them
        # over the network.
        for k in list(_IMG_DIMS)[:10000]:
            del _IMG_DIMS[k]
    _IMG_DIMS[url] = dims
    return dims


# Every rule rewrites a URL towards the CDN's ORIGINAL rendition. They are
# candidates, not conclusions: upgrade_gallery() measures the result and keeps
# the rewrite only when it really returns a bigger image, so a rule that is
# wrong for a given site costs a probe rather than the photo.
RENDITION_RULES = (
    # Amazon: the rendition lives in the filename (._SL1500_. / ._AC_SX466_.)
    ("amazon-original", lambda u: re.sub(r"\._[A-Z0-9_,%-]+_\.(?=[A-Za-z]{3,4}$)",
                                         ".", u)),
    # Shopify / WordPress: _600x600 or -600x600 appended before the extension
    ("suffix-size", lambda u: re.sub(r"[_-]\d{2,4}x\d{2,4}(?=\.[A-Za-z]{3,4}($|\?))",
                                     "", u)),
    # Tata CLiQ serves a size segment AND repeats it in the filename
    ("tatacliq-1500", lambda u: re.sub(r"\d{2,4}Wx\d{2,4}H", "1500Wx1500H", u)),
    # ImageKit-style transform parameter
    ("drop-tr", lambda u: re.sub(r"([?&])tr=[^&]*(&|$)", r"\1", u).rstrip("?&")),
    # Cloudinary-style transform path segment
    ("cloudinary-original", lambda u: re.sub(r"/upload/[^/]*[wh]_\d+[^/]*/",
                                             "/upload/", u)),
    # Generic resize query parameters
    ("drop-resize-params", lambda u: drop_query_params(
        u, ("width", "height", "w", "h", "size", "quality", "fit", "resize",
            "crop", "sw", "sh"))),
)


def drop_query_params(url, names):
    try:
        pr = urlsplit(url)
    except Exception:
        return url
    if not pr.query:
        return url
    keep = [kv for kv in pr.query.split("&")
            if kv and kv.split("=")[0].lower() not in names]
    return urlunsplit((pr.scheme, pr.netloc, pr.path, "&".join(keep), pr.fragment))


def learn_best_rule(sample_url):
    """Find which rewrite gives the biggest image for THIS site, by measuring.

    Only the first image of a gallery is tested. Galleries come from one CDN
    and share a URL shape, so whatever wins here applies to the rest - which
    keeps this to a handful of probes per listing instead of one per rule per
    image.

    Returns (rule_name, transform, gain) or None when nothing beats the URL we
    already have."""
    # build every candidate first, then measure them all at once - the rules
    # are independent, so probing them one after another is pure waiting
    candidates = []
    for name, fn in RENDITION_RULES:
        try:
            cand = fn(sample_url)
        except Exception:
            continue
        if cand and cand != sample_url:
            candidates.append((name, fn, cand))
    probe_many([sample_url] + [c for _, _, c in candidates])

    base = image_dimensions(sample_url)
    base_px = max(base) if base else 0
    best = None
    for name, fn, cand in candidates:
        dims = image_dimensions(cand)
        if not dims:
            continue
        if max(dims) > max(base_px, best[2] if best else 0):
            best = (name, fn, max(dims))
    if best and best[2] > base_px:
        return best
    return None


def upgrade_gallery(urls, min_px=IMAGE_MIN_PX):
    """Raise every image in a gallery to the largest rendition its CDN serves,
    then drop the ones still too small to be product photos.

    Returns (kept, dropped, note). `note` describes the upgrade for the log."""
    urls = [u for u in urls if u and u.strip()]
    if not IMAGE_CHECK or not urls:
        return list(urls), [], ""

    note = ""
    rule = learn_best_rule(urls[0])
    if rule:
        name, fn, px = rule
        before = image_dimensions(urls[0])
        rewritten = []
        for u in urls:
            try:
                cand = fn(u)
            except Exception:
                cand = u
            rewritten.append(cand or u)
        probe_many(rewritten)           # one round trip's worth of waiting, not N
        # a rewrite that breaks an individual URL must not lose the image
        urls = [c if image_dimensions(c) else u
                for u, c in zip(urls, rewritten)]
        note = (f"upgraded via {name}: "
                f"{before[0]}x{before[1]} -> {px}px" if before
                else f"upgraded via {name} to {px}px")

    probe_many(urls[:IMAGE_PROBE_MAX])
    kept, dropped = [], []
    for i, u in enumerate(urls):
        if i >= IMAGE_PROBE_MAX:
            kept.append(u)
            continue
        dims = image_dimensions(u)
        if dims and max(dims) < min_px:
            dropped.append((u, f"{dims[0]}x{dims[1]}"))
        else:
            kept.append(u)
    kept = photos_before_banners(kept)
    return kept, dropped, note


def banner_shaped(dims):
    """True for an image with the OpenGraph share ratio (1.91:1).

    A page's og:image is a SOCIAL banner - usually the product on a coloured
    background with marketing text baked in. It is 'declared', so it lands in
    tier 1 of collect_images and, on any page without JSON-LD, becomes the
    first image and therefore the storefront thumbnail. Measured on the LED
    batch: 9 of 37 products led with a 1200x628 banner instead of a photo."""
    if not dims or not dims[1]:
        return False
    ratio = dims[0] / dims[1]
    return 1.85 <= ratio <= 2.05


def photos_before_banners(urls):
    """Reorder so a real product photo is first. Never drops anything - a
    banner is still a picture of the product, it just should not be the
    thumbnail when a proper photo exists."""
    photos = [u for u in urls if not banner_shaped(image_dimensions(u))]
    banners = [u for u in urls if banner_shaped(image_dimensions(u))]
    return (photos + banners) if (photos and banners) else list(urls)


def probe_many(urls):
    """Measure a batch of images concurrently, filling the same memo that
    image_dimensions() reads.

    Probing a gallery one image at a time is the single slowest thing the
    quality pass does: a listing with 12 images plus 6 rule candidates is ~18
    round trips, and serially that is most of a minute per SKU. They are
    independent GETs of 64 KB, so a small pool collapses it to a few seconds."""
    todo = [u for u in dict.fromkeys(urls) if u and u not in _IMG_DIMS]
    if not todo:
        return
    if len(todo) == 1:
        image_dimensions(todo[0])
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(IMAGE_PROBE_WORKERS, len(todo))) as pool:
        list(pool.map(image_dimensions, todo))


def drop_small_images(urls, min_px=IMAGE_MIN_PX):
    """Remove images that are POSITIVELY too small to be product photos.

    The URL-based guards (`is_thumbnail`, `image_size_hint`) only see sizes a
    CDN chose to advertise. Amazon stops advertising one the moment the
    rendition modifier is dropped to get the original, so a listing whose
    photos are genuinely 500x500 sailed through and the row claimed 8 images
    that all looked soft on the storefront. Measuring is the only way to know.

    Returns (kept, dropped) where dropped is [(url, 'WxH'), ...]."""
    if not IMAGE_CHECK or not urls:
        return list(urls), []
    kept, dropped = [], []
    for i, u in enumerate(urls):
        if i >= IMAGE_PROBE_MAX:
            kept.append(u)               # past the cap, trust the earlier ones
            continue
        dims = image_dimensions(u)
        if dims and max(dims) < min_px:
            dropped.append((u, f"{dims[0]}x{dims[1]}"))
        else:
            kept.append(u)
    return kept, dropped


def pick_profile(dom, attempt):
    """Never re-use the profile that just failed on this domain."""
    if attempt >= FETCH_ATTEMPTS - 1:
        return IMPERSONATE_LAST
    pool = [p for p in IMPERSONATE_POOL if p != _PROFILES.get(dom)]
    _PROFILES[dom] = random.choice(pool)
    return _PROFILES[dom]


def nav_headers(url, dom):
    """Only Referer and Sec-Fetch-Site: curl_cffi's impersonation already sends
    a matching Accept / Sec-Ch-Ua / Sec-Fetch-* set, and overriding those by
    hand breaks the fingerprint consistency that makes it work."""
    h = {"Accept-Language": "en-IN,en;q=0.9"}
    prev = _LAST_URL.get(dom)
    if prev and prev != url:
        h["Referer"] = prev
        h["Sec-Fetch-Site"] = "same-origin"
    else:
        # bare origin: google strips the query from cross-origin referrers, so
        # a '?q=...' referer is a fabrication no real browser sends
        h["Referer"] = "https://www.google.com/"
        h["Sec-Fetch-Site"] = "cross-site"
    return h


def fetch(url):
    """Cached front door to fetch_live. A cache hit does no network I/O, which
    is what makes re-running a finished batch through improved parsers free."""
    global NET_CALLS
    hit = cache_get("page", url)
    if hit:
        CACHE_STATS["page_hit"] += 1
        log(f"      (cached page, {len(hit) // 1024} KB)")
        return hit, ""
    CACHE_STATS["page_miss"] += 1
    NET_CALLS += 1
    html, reason = fetch_live(url)
    if html:
        RUN_STATS["pages_fetched"] += 1
        cache_put("page", url, html)
    elif is_block_reason(reason):
        RUN_STATS["blocked_domains"][domain_of(url)] = (
            RUN_STATS["blocked_domains"].get(domain_of(url), 0) + 1)
    return html, reason


def fetch_live(url):
    """Returns (html, reason). html is None on failure; reason is '' on success
    and a short phrase otherwise ('http 403', 'cloudflare challenge', ...).
    Every failure path names itself - silence used to make a hard block look
    identical to a product that simply isn't listed."""
    dom, reason, hard = domain_of(url), "no attempt", 0
    for attempt in range(FETCH_ATTEMPTS):
        prof = pick_profile(dom, attempt)
        tag = f"[{prof}] {attempt + 1}/{FETCH_ATTEMPTS}"
        try:
            r = get_session(dom).get(url, impersonate=prof, timeout=FETCH_TIMEOUT,
                                     headers=nav_headers(url, dom),
                                     allow_redirects=True)
        except Exception as e:
            reason = type(e).__name__
            log(f"      fetch error ({reason}) {tag}")
        else:
            code = r.status_code
            if code in DEAD_CODES:
                log(f"      http {code} - page is gone, not retrying")
                return None, f"http {code}"
            if code in HARD_BLOCK_CODES:
                hard += 1
                reason = f"http {code}"
                log(f"      BLOCKED http {code} {tag}")
                get_session(dom, fresh=True)       # jar is probably flagged
            elif code != 200:
                reason = f"http {code}"
                log(f"      http {code} {tag}")
            elif len(r.text) < 3000:
                reason = f"short body ({len(r.text)}b)"
                log(f"      {reason} {tag}")
            else:
                why = looks_blocked(r.text)
                if not why:
                    _LAST_URL[dom] = url
                    return r.text, ""
                hard += 1
                reason = why
                log(f"      BLOCKED: {why} {tag}")
                get_session(dom, fresh=True)
        if hard >= 2:
            log(f"      {dom} is hard-blocking - giving up on it early")
            break
        if attempt < FETCH_ATTEMPTS - 1:
            time.sleep(5 + attempt * 5 + random.uniform(0, 3))
    return None, reason


DOMAIN_COOLDOWN_SKUS = 3      # skip a hard-blocking domain for this many SKUs
MAX_PAGES_PER_DOMAIN = 2      # per SKU, so one hostile site can't eat the budget

_SKU_SEQ = 0
_COOLDOWN = {}                # domain -> _SKU_SEQ value when it may be used again


def trip_domain(dom, reason):
    _COOLDOWN[dom] = _SKU_SEQ + DOMAIN_COOLDOWN_SKUS
    log(f"      cooling off {dom} for {DOMAIN_COOLDOWN_SKUS} SKUs ({reason})")


def domain_cooling(dom):
    return _COOLDOWN.get(dom, 0) > _SKU_SEQ


def is_block_reason(why):
    """A genuine block, as opposed to a timeout or a missing page.

    DEAD_CODES are explicitly NOT blocks. A bare `startswith("http 4")` swept
    404 and 410 in with 403/429, so ONE dead product URL put the whole domain
    on a DOMAIN_COOLDOWN_SKUS cooldown, spent a headless render on a page that
    is gone, and made the row read 'NOT FOUND - all sources blocked
    (amazon.in http 404)' when nothing had blocked anything."""
    if not why:
        return False
    if why in [f"http {c}" for c in DEAD_CODES]:
        return False
    return (why.startswith("http 4") or why == "http 503"
            or any(w in why for w in ("captcha", "challenge", "denied",
                                      "blocked", "bot", "rate-limit")))


# ---------------------- headless fallback (--js-fallback) ------------------
# Last resort for pages that are blocked or that build their price in JS.
# Croma is the known case: it passes the block check but renders price
# client-side, which is why those rows say "no price shown".

JS_PRICE_DOMAINS = ("croma.com", "tatacliq.com", "vijaysales.com",
                    "shop.haierindia.com")
PW_ENABLED = False
PW_MAX_PER_SKU = 1
PW_MAX_PER_RUN = 25
PW_NAV_TIMEOUT_MS = 45000
PW_SETTLE_MS = 2500

# Optional on-disk browser profile (--js-profile). An ephemeral context starts
# every run with no cookies, so a site that hands out a clearance cookie after
# one challenge re-challenges on the next run. A persistent profile keeps those
# cookies, which is the one thing the older standalone scraper did better.
# Off by default: a shared profile is per-machine state, and two concurrent runs
# must not point at the same directory.
PW_PROFILE_DIR = None

_PW = {"state": "off", "pw": None, "browser": None, "ctx": None,
       "used": 0, "sku_used": 0}          # state: off | ready | dead


def _pw_start():
    """Launch one browser for the whole run. Any failure marks the fallback
    dead so a batch never retries a broken install per page."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("      JS fallback unavailable - install it once with:\n"
            "        .venv\\Scripts\\python.exe -m pip install playwright\n"
            "        .venv\\Scripts\\python.exe -m playwright install chromium")
        _PW["state"] = "dead"
        return False
    try:
        _PW["pw"] = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled"]
        ctx_opts = dict(
            locale="en-IN", timezone_id="Asia/Kolkata",
            viewport={"width": 1366, "height": 768},
            # bundled chromium advertises 'HeadlessChrome/...' without this
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/146.0.0.0 Safari/537.36"),
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"})
        if PW_PROFILE_DIR:
            # one call both launches and contexts; there is no separate browser
            profile = Path(PW_PROFILE_DIR).expanduser().resolve()
            profile.mkdir(parents=True, exist_ok=True)
            try:
                ctx = _PW["pw"].chromium.launch_persistent_context(
                    str(profile), channel="chrome", headless=True,
                    args=args, **ctx_opts)
            except Exception:
                ctx = _PW["pw"].chromium.launch_persistent_context(
                    str(profile), headless=True, args=args, **ctx_opts)
            _PW["browser"] = None
            _PW["ctx"] = ctx
            log(f"      JS fallback using saved profile: {profile}")
        else:
            try:                # real Chrome if installed: better fingerprint,
                browser = _PW["pw"].chromium.launch(   # and no 130MB download
                    channel="chrome", headless=True, args=args)
            except Exception:
                browser = _PW["pw"].chromium.launch(headless=True, args=args)
            _PW["browser"] = browser
            _PW["ctx"] = browser.new_context(**ctx_opts)
        _PW["ctx"].set_default_timeout(PW_NAV_TIMEOUT_MS)
        _PW["state"] = "ready"
        return True
    except Exception as e:
        log(f"      JS fallback unusable ({type(e).__name__}: {str(e)[:110]}).\n"
            "        Run once:  .venv\\Scripts\\python.exe -m playwright install chromium")
        _pw_shutdown()
        _PW["state"] = "dead"
        return False


def _pw_shutdown():
    for k in ("ctx", "browser"):
        try:
            if _PW[k]:
                _PW[k].close()
        except Exception:
            pass
        _PW[k] = None
    try:
        if _PW["pw"]:
            _PW["pw"].stop()
    except Exception:
        pass
    _PW["pw"] = None


def render_fetch(url):
    """Render a page with a real browser. Returns HTML or None; never raises,
    so it can never take a batch down."""
    if not PW_ENABLED or _PW["state"] == "dead":
        return None
    if _PW["used"] >= PW_MAX_PER_RUN or _PW["sku_used"] >= PW_MAX_PER_SKU:
        return None
    if _PW["state"] == "off" and not _pw_start():
        return None
    _PW["used"] += 1
    _PW["sku_used"] += 1
    log(f"      JS render ({_PW['used']}/{PW_MAX_PER_RUN})...")
    page = None
    try:
        page = _PW["ctx"].new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=PW_NAV_TIMEOUT_MS)
        page.wait_for_timeout(PW_SETTLE_MS)
        html = page.content()
        if len(html) <= 3000 or looks_blocked(html):
            return None
        # a rendered page is cached like any other, so the expensive browser
        # round-trip is paid once even though fetch() is what reads it back
        RUN_STATS["pages_fetched"] += 1
        cache_put("page", url, html)
        return html
    except KeyboardInterrupt:
        raise               # must propagate or Ctrl+C stops working mid-render
    except Exception as e:
        log(f"      JS render failed ({type(e).__name__})")
        return None
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass


atexit.register(_pw_shutdown)


def _close_sessions():
    """A-8: one curl_cffi Session is kept open per domain for the whole run and
    nothing closed them. Harmless over 43 SKUs; over a long unattended batch
    across many domains it leaks sockets."""
    for session in list(_SESSIONS.values()):
        try:
            session.close()
        except Exception:
            pass
    _SESSIONS.clear()


atexit.register(_close_sessions)

# -------------------------------- parsing --------------------------------

def jsonld_products(soup):
    prods = []
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or sc.get_text() or "", strict=False)
        except Exception:
            continue
        # FIFO, not a LIFO stack: a pop()-based walk returns @graph and array
        # products REVERSED, so prods[0] could be a variant or breadcrumb entry
        queue, qi = [data], 0
        while qi < len(queue):
            node = queue[qi]
            qi += 1
            if isinstance(node, list):
                queue.extend(node)
            elif isinstance(node, dict):
                t = node.get("@type", "")
                types = t if isinstance(t, list) else [t]
                if "Product" in types or "ProductGroup" in types:
                    prods.append(node)
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        queue.append(v)
    return prods


def main_product(prods):
    """The page's OWN product. A JSON-LD block also carries variants,
    breadcrumb items and related products, so prods[0] is not safe."""
    best, score = {}, -1
    for p in prods:
        if not isinstance(p, dict):
            continue
        s = ((3 if p.get("offers") else 0) + (2 if p.get("image") else 0)
             + (2 if p.get("sku") or p.get("mpn") else 0)
             + (1 if p.get("name") else 0))
        if s > score:                   # strict > keeps ties in document order
            best, score = p, s
    return best


def offer_prices(prod):
    """(mrp, sale) from any offer shape: a bare Offer, a list, AggregateOffer,
    priceSpecification, or hasVariant. The old offer_price read offers[0] only
    and returned '' whenever that first entry had no price key."""
    sales, lists, highs = [], [], []

    def walk(node, depth=0):
        if depth > 5 or not isinstance(node, (dict, list)):
            return
        if isinstance(node, list):
            for n in node:
                walk(n, depth + 1)
            return
        kind = str(node.get("priceType", "")) + str(node.get("@type", ""))
        bucket = lists if re.search(r"list|strike|msrp|regular|was", kind, re.I) else sales
        for key, dest in (("price", bucket), ("lowPrice", sales), ("highPrice", highs)):
            v = price_value(clean_price(node.get(key)))
            if v:
                dest.append(v)
        for k in ("offers", "priceSpecification", "itemOffered", "hasVariant"):
            if node.get(k):
                walk(node[k], depth + 1)

    walk(prod.get("offers"))
    walk(prod.get("hasVariant"))
    if not sales and not lists:
        return "", ""
    # document order, not min(): on a single-offer page this is byte-identical
    # to the old offers[0] behaviour, so working rows cannot silently change
    sale = sales[0] if sales else None
    mrp = max([v for v in lists if sale is None or v > sale], default=None)
    if mrp is None and highs and sale:
        top = max(highs)
        if 1.02 <= top / sale <= 4.0:           # plausible discount, not a bundle
            mrp = top
    return (indian_format(mrp) if mrp else ""), (indian_format(sale) if sale else "")


def jsonld_images(prod):
    img = prod.get("image")
    if isinstance(img, str):
        return [img]
    if isinstance(img, dict):
        return [img.get("url", "")]
    if isinstance(img, list):
        return [(i.get("url", "") if isinstance(i, dict) else str(i)) for i in img]
    return []


def parse_amazon(html, soup):
    title = mrp = sale = ""
    el = soup.select_one("#productTitle")
    if el:
        title = el.get_text(strip=True)
    for sel in ("#corePriceDisplay_desktop_feature_div span.a-price:not(.a-text-price) span.a-offscreen",
                "#apex_desktop span.a-price:not(.a-text-price) span.a-offscreen",
                "span.a-price:not(.a-text-price) span.a-offscreen"):
        el = soup.select_one(sel)
        if el:
            sale = clean_price(el.get_text())
            if sale:
                break
    # MRP only from the main price block (page-wide match can grab an
    # unrelated widget's strike-through price)
    for sel in ("#corePriceDisplay_desktop_feature_div span.a-price.a-text-price span.a-offscreen",
                "#apex_desktop span.a-price.a-text-price span.a-offscreen"):
        el = soup.select_one(sel)
        if el:
            mrp = clean_price(el.get_text())
            break
    # gallery: the hiRes set IS the gallery; only supplement from other
    # sources when it is sparse (page-wide scans can pick up non-gallery
    # images). Dedupe by amazon image id (same photo in several sizes).
    imgs = re.findall(r'"hiRes":"(https://[^"]+)"', html)
    if len(imgs) < MIN_IMAGES:
        imgs += re.findall(r'"large":"(https://[^"]+)"', html)
    if len(imgs) < MIN_IMAGES:
        el = soup.select_one("#landingImage")
        if el:
            dyn = el.get("data-a-dynamic-image")
            if dyn:
                try:
                    imgs += list(json.loads(dyn).keys())
                except Exception:
                    pass
            if el.get("src"):
                imgs.append(el.get("src"))
    # Nav-strip thumbnails advertise their size in the URL (._SS40_, ._UL80_);
    # probing them over the network just to discard them wastes a request per
    # thumb. But on a sparse fallback page the landingImage src may be the
    # ONLY reference to the real photo, in a small rendition that
    # RENDITION_RULES can upgrade to the original - so when filtering would
    # empty the list, keep the tiny candidates and let the upgrade pipeline
    # measure them.
    def _tiny(u):
        sizes = [int(t) for t in re.findall(r"_(?:S[SXYL]|U[SLXY])(\d+)_", u)]
        return bool(sizes) and max(sizes) < 200
    kept = [u for u in imgs if not _tiny(u)]
    imgs = kept or imgs
    seen_ids, uniq = set(), []
    for u in imgs:
        m = re.search(r"/images/I/([^.]+)\.", u)
        key = m.group(1) if m else u
        if key not in seen_ids:
            seen_ids.add(key)
            uniq.append(u)
    return title, mrp, sale, uniq


def parse_flipkart(html, soup):
    title = mrp = sale = ""
    imgs = []
    p = main_product(jsonld_products(soup))
    if p:
        title = str(p.get("name", "")).strip()
        mrp, sale = offer_prices(p)         # JSON-LD MRP used to be discarded
        imgs = jsonld_images(p)
    if not title:
        el = soup.select_one("span.VU-ZEz") or soup.select_one("span.B_NuCI") or soup.find("h1")
        if el:
            title = el.get_text(strip=True)
    if not sale:
        el = soup.select_one("div.Nx9bqj")
        if el:
            sale = clean_price(el.get_text())
    if not sale:
        m = re.search(r'"price"\s*:\s*"?([\d.]+)"?', html)
        if m:
            sale = clean_price(m.group(1))
    if not mrp:
        el = soup.select_one("div.yRaY8j")      # strike-through MRP
        if el:
            mrp = clean_price(el.get_text())
    # accept any rukminim gallery URL; requiring 'original-ima' collapsed the
    # gallery whenever flipkart changed its URL scheme
    fk = re.findall(r'https://rukminim\d+\.flixcart\.com/image/[^"\s\\<>]+', html)
    fk.sort(key=lambda u: 0 if "original-ima" in u else 1)
    imgs = fk + list(imgs)
    imgs = [re.sub(r"/image/\d+/\d+/", "/image/2048/2048/", u) for u in imgs]
    return title, mrp, sale, imgs


def parse_croma(html, soup, url):
    """Croma renders PRICE via JavaScript (not present in raw HTML - verified),
    but the full image gallery and title ARE in the static page."""
    title = mrp = sale = ""
    imgs = []
    el = soup.select_one('meta[property="og:title"]')
    if el and el.get("content"):
        title = el.get("content").strip()
    elif soup.title:
        title = soup.title.get_text(strip=True)
    # 'Buy Haier 10 kg ... Online - Croma'  ->  'Haier 10 kg ...'
    title = clean_title(title)

    m = re.search(r"/p/(\d{4,})", url)
    pid = m.group(1) if m else ""
    if pid:
        pat = (r'https://media\.tatacroma\.com/[^\s"\'()<>]*?/'
               + re.escape(pid) + r'_\d+[^\s"\'()<>]*')
        by_idx = {}
        for u in re.findall(pat, html):
            u = u.split("?tr=")[0]                # drop thumbnail size suffix
            mi = re.search("/" + re.escape(pid) + r"_(\d+)", u)
            k = int(mi.group(1)) if mi else 99
            if k not in by_idx:
                by_idx[k] = u
        imgs = [by_idx[k] for k in sorted(by_idx)]
    el = soup.select_one('meta[property="og:image"]')
    if el and el.get("content"):
        imgs = imgs + [el.get("content")]     # append rather than fall back

    # best-effort price scan in case Croma ever embeds it server-side
    for pat in (r'"finalPrice"[^0-9]{0,40}?(\d[\d.]*)',
                r'"sellingPrice"[^0-9]{0,40}?(\d[\d.]*)',
                r'"price"\s*:\s*"?(\d[\d.]*)'):
        m = re.search(pat, html)
        if m:
            sale = clean_price(m.group(1))
            if sale:
                break
    m = re.search(r'"mrp"\s*:\s*"?(\d[\d.]*)', html)
    if m:
        mrp = clean_price(m.group(1))
    return title, mrp, sale, imgs


def parse_reliance(html, soup):
    """reliancedigital.in: title/price/MRP are server-rendered (verified);
    gallery images share the numeric item code from the og:image URL."""
    title = mrp = sale = ""
    imgs = []
    h = html.replace("\\/", "/")
    el = soup.find("h1")
    if el:
        title = el.get_text(strip=True)
    if not title:
        el = soup.select_one('meta[property="og:title"]')
        if el and el.get("content"):
            title = el.get("content")
    title = clean_title(title)
    m = re.search(r"MRP[^₹]{0,120}₹\s*([\d,]+(?:\.\d+)?)", h)
    if m:
        mrp = clean_price(m.group(1))
    og = soup.select_one('meta[property="og:image"]')
    og_url = og.get("content", "") if og else ""
    item = ""
    m = re.search(r"/(\d{6,})/\d+/", og_url)
    if m:
        item = m.group(1)
    if not item:
        m = re.search(r"Item Code[^0-9]{0,80}(\d{6,})", h)
        if m:
            item = m.group(1)
    if item:
        found = re.findall(r"https://cdn\.jiostore\.online/[^\s\"'<>\\]+", h)
        found = [u for u in found
                 if f"/{item}/" in u and "/products/pictures/" in u]

        def rk(u):
            m2 = re.search("/" + item + r"/(\d+)/", u)
            return int(m2.group(1)) if m2 else 99
        found.sort(key=rk)
        imgs = found
    if og_url:
        imgs = imgs + [og_url]      # append, don't fall back: dedupe handles overlap
    return title, mrp, sale, imgs   # sale left to the generic top-up (proven)


def parse_ifb(html, soup):
    """ifbappliances.com: the product's price is the ₹ value that sits right
    before the 'MRP (Incl. of all taxes)' label (verified); gallery = adobe
    asset URNs that repeat across the page (related products appear once)."""
    title = mrp = sale = ""
    el = soup.select_one('meta[property="og:title"]')
    if el and el.get("content"):
        title = clean_title(el.get("content"))
    elif soup.title:
        title = clean_title(soup.title.get_text(strip=True))
    m = re.search(r"₹\s*([\d,]{4,})[^₹]{0,300}?MRP[^₹]{0,120}₹\s*([\d,]{4,})",
                  html)
    if m:
        sale, mrp = clean_price(m.group(1)), clean_price(m.group(2))
    urls = re.findall(
        r"https://[a-z0-9.-]*ifbappliances\.com/adobe/assets/urn:aaid:aem:[a-f0-9-]+",
        html)
    counts, order = {}, []
    for u in urls:
        if u not in counts:
            order.append(u)
        counts[u] = counts.get(u, 0) + 1
    imgs = [u for u in order if counts[u] >= 2] or order   # MAX_IMAGES caps later
    return title, mrp, sale, imgs


def parse_generic(html, soup, url=""):
    title = mrp = sale = ""
    p = main_product(jsonld_products(soup))
    if p:
        title = str(p.get("name", "")).strip()
        mrp, sale = offer_prices(p)         # MRP was never read here before
    if not title:
        el = soup.select_one('meta[property="og:title"]')
        if el and el.get("content"):
            title = el.get("content").strip()
        elif soup.title:
            title = soup.title.get_text(strip=True)
    title = clean_title(title)
    if not sale:
        for sel in ('meta[property="product:price:amount"]',
                    'meta[itemprop="price"]'):
            el = soup.select_one(sel)
            if el and el.get("content"):
                sale = clean_price(el.get("content"))
                if sale:
                    break
    if not mrp:
        el = soup.select_one('meta[property="product:original_price:amount"]')
        if el and el.get("content"):
            mrp = clean_price(el.get("content"))
    return title, mrp, sale, collect_images(soup, html, url)

# ---------------------- spec tables (retailer pages) ----------------------
# Every retailer publishes a full specification table - energy rating,
# warranty, dimensions, capacity, features - and none of it was being read.
# app.py had to infer attributes from the listing TITLE alone, which is why
# products ended up with three or four attributes out of the twenty the page
# actually stated.
#
# These are FACTS COPIED off the page, not guesses, so they are safe under the
# KI-14 rule (never invent a spec). The rest of the pipeline treats them as
# lower-priority than the title-derived rows, which are normalised per
# category; a scraped row only fills a label nothing else supplied.

SPEC_LABEL_DENY_RE = re.compile(
    # marketplace bookkeeping, not product facts
    r"^(?:asin|item model number|best sellers? rank|date first available|"
    r"customer reviews?|product dimensions l?x?w?x?h?|packer|importer|"
    r"manufacturer|country of origin|net quantity|generic name|included "
    r"components|item weight|package|seller|delivery|return|warranty summary|"
    r"emi|offers?|price|mrp|discount|availability|share|compare|quantity|"
    r"add to|buy|description|about this item|highlights?|features?|"
    r"specifications?|overview|reviews?|ratings?|q ?& ?a|faq)\b", re.I)

# a value that is really a sentence, a price, or page furniture
SPEC_VALUE_DENY_RE = re.compile(
    r"^(?:₹|rs\.?\s*\d|see more|show more|read more|view all|n/?a|-|—|yes/no|"
    r"click|select|choose|add to|available|\d+\s*(?:ratings?|reviews?))",
    re.I)

SPEC_SPLIT_RE = re.compile(r"\s*[:∶]\s+")


def clean_spec_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().strip(":∶ ​")


def usable_spec(label, value):
    """A (label, value) pair is a real spec only if it reads like one. Without
    this the sweep below returns nav links, review counts and whole
    paragraphs, all of which would go straight onto the storefront."""
    if not label or not value:
        return False
    if not (2 <= len(label) <= 40) or not (1 <= len(value) <= 120):
        return False
    if label.lower() == value.lower():
        return False
    if not re.search(r"[A-Za-z]", label):
        return False
    if SPEC_LABEL_DENY_RE.match(label) or SPEC_VALUE_DENY_RE.match(value):
        return False
    if value.count(".") >= 2 and len(value.split()) > 12:
        return False                      # a paragraph, not a spec value
    if "<" in value or "{" in value:
        return False
    return True


def jsonld_specs(prods):
    """schema.org additionalProperty - the standards-compliant source, so it is
    tried first and trusted most."""
    out = []
    for prod in prods:
        props = prod.get("additionalProperty")
        if isinstance(props, dict):
            props = [props]
        for prop in props or []:
            if not isinstance(prop, dict):
                continue
            label = clean_spec_text(prop.get("name"))
            value = clean_spec_text(prop.get("value"))
            if usable_spec(label, value):
                out.append((label, value))
    return out


def table_specs(soup):
    """Two-column tables and definition lists, which is how essentially every
    retailer marks a spec sheet up once JSON-LD runs out."""
    out = []
    # find_all beats select for a bare tag name - it skips the CSS engine
    # entirely. Measured on the 189-page cache: 1.23s -> 0.42s (-66%).
    for row in soup.find_all("tr", limit=400):
        cells = row.find_all(["th", "td"], recursive=False) or \
            row.find_all(["th", "td"])
        if len(cells) != 2:
            continue
        label = clean_spec_text(cells[0].get_text(" ", strip=True))
        value = clean_spec_text(cells[1].get_text(" ", strip=True))
        if usable_spec(label, value):
            out.append((label, value))
    for dl in soup.find_all("dl", limit=40):
        terms = dl.find_all("dt")
        for term in terms:
            definition = term.find_next_sibling("dd")
            if not definition:
                continue
            label = clean_spec_text(term.get_text(" ", strip=True))
            value = clean_spec_text(definition.get_text(" ", strip=True))
            if usable_spec(label, value):
                out.append((label, value))
    return out


def bullet_specs(soup):
    """'Label: Value' bullets - amazon's detail bullets and the feature lists
    most brand sites use. Only pairs with a colon count; a plain marketing
    bullet has no label to hang an attribute on."""
    out = []
    for item in soup.find_all("li", limit=300):        # -68%, see table_specs
        text = clean_spec_text(item.get_text(" ", strip=True))
        if not text or len(text) > 160:
            continue
        parts = SPEC_SPLIT_RE.split(text, maxsplit=1)
        if len(parts) != 2:
            continue
        label, value = clean_spec_text(parts[0]), clean_spec_text(parts[1])
        if usable_spec(label, value):
            out.append((label, value))
    return out


def extract_specs(soup, prods=None):
    """The product's specification table as an ordered list of (label, value).

    Sources in descending trust: JSON-LD additionalProperty, then two-column
    tables / definition lists, then 'Label: Value' bullets. First writer of a
    label wins, so a structured value is never overwritten by a scraped one.

    The sources are thunks, not a tuple of results. Built eagerly, the early
    return below could never actually save anything: a page whose JSON-LD
    already filled MAX_SPECS still paid for a 400-row table scan and a 300-item
    bullet scan first. 50 of the 189 cached pages hit that ceiling."""
    rows, seen = [], set()
    for make_source in (
            lambda: jsonld_specs(prods if prods is not None else jsonld_products(soup)),
            lambda: table_specs(soup),
            lambda: bullet_specs(soup)):
        if len(rows) >= MAX_SPECS:
            break
        for label, value in make_source():
            key = norm(label)
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append((label, value))
            if len(rows) >= MAX_SPECS:
                return rows
    return rows


def format_specs(rows):
    """'Label: Value | Label: Value' - one spreadsheet cell, still readable by
    a human reviewing the sheet, and unambiguous to split on because both
    separators are stripped out of the parts."""
    parts = []
    for label, value in rows:
        label = label.replace("|", "/").replace(":", "").strip()
        value = value.replace("|", "/").strip()
        if label and value:
            parts.append(f"{label}: {value}")
    return " | ".join(parts)


def extract(url, html):
    soup = BeautifulSoup(html, "lxml")
    d = domain_of(url)
    site = True                                 # a site-specific parser ran
    if "amazon." in d:
        t, m, s, i = parse_amazon(html, soup)
    elif "flipkart.com" in d:
        t, m, s, i = parse_flipkart(html, soup)
    elif "croma.com" in d:
        t, m, s, i = parse_croma(html, soup, url)
    elif "reliancedigital.in" in d:
        t, m, s, i = parse_reliance(html, soup)
    elif "ifbappliances.com" in d:
        t, m, s, i = parse_ifb(html, soup)
    else:
        site = False
        t, m, s, i = parse_generic(html, soup, url)
    # ALWAYS top up. The old 'if not t or not s' gate meant a parser returning a
    # title and price but a single image never consulted parse_generic at all -
    # the main reason products with a full gallery came out with one image.
    if site:
        t2, m2, s2, i2 = parse_generic(html, soup, url)
        if "amazon." in d:
            t2 = ""    # amazon titles only count from #productTitle - if it's
                       # missing the page is an interstitial, not the product
        # site-parser images stay FIRST (higher confidence), and this is a merge
        # rather than a replace, so the primary image of a working row is unchanged
        t, m, s, i = t or t2, m or m2, s or s2, list(i) + list(i2)
    if not m:
        m = mrp_near_label(html, s) or mrp_from_strike(soup, s)
    # MRP sanity (<= sale, or many times it) is judged once, downstream, by
    # bogus_mrp() - status_for() flags it there instead of this function
    # silently discarding data a human might want to see: a real bundle SKU
    # legitimately prices several times its components, and a number this
    # function throws away never even reaches the "check" a flag would give it.
    specs = format_specs(extract_specs(soup)) if SPECS_ENABLED else ""
    return {"title": t, "regular price": m, "sale price": s,
            "url_link": url,
            # a POOL, not the final gallery - upgrade_gallery() measures these
            # and drops the small ones, then the caller trims to MAX_IMAGES
            "image_url": ",".join(dedupe_images(i, url, limit=IMAGE_POOL)),
            "specs": specs}

# ------------------------------ per-SKU flow ------------------------------

def price_value(s):
    """'22,599' -> 22599.0, '' -> None"""
    try:
        return float(str(s).replace(",", "").strip()) if s else None
    except ValueError:
        return None


def looks_like_accessory(title, brand):
    """Cheap pre-filter applied to SEARCH-RESULT titles (may be truncated)."""
    t = " " + re.sub(r"\s+", " ", str(title or "")).strip().lower() + " "
    if any(ph in t for ph in COMPAT_PHRASES):
        return True
    if brand and f" for {brand.lower()} " in t:
        return True
    if ACCESSORY_RE.search(t[:60]):        # accessory noun near start of title
        return True
    return False


# Products whose model digits are NOT a kilogram capacity. A TV's screen size
# and an AC's tonnage code sit in exactly the same place as a washer's load
# rating, so the /10 and /100 guesses below turned 'UA43H4500' into 4.3 kg and
# '185V' into 18.5 kg. assess() then demanded that phantom capacity appear in
# the title and rejected perfectly good TV and AC pages as "title lacks
# capacity/model info". The title always says which kind of product it is.
NON_CAPACITY_RE = re.compile(
    r"\b(?:tv|television|qled|oled|qned|led\s*tv|smart\s*tv|inch|"
    r"air\s*conditioner|split\s*ac|window\s*ac|inverter\s*ac|ton|tr)\b", re.I)


def expected_caps(sku, model, title=""):
    """Capacities (kg) implied by the SKU: '7.5KG' text, or model digits
    (HW90 -> 9, HW120 -> 12, WW12DG -> 12, HWM75 -> 7.5).

    An explicit '7.5KG' in the SKU is a fact and always counts. The model-digit
    reading is a GUESS, so it is skipped for product classes whose model digits
    mean something else entirely (see NON_CAPACITY_RE)."""
    caps = set()
    m = KG_RE.search(str(sku))
    if m:
        caps.add(float(m.group(1)))
    if NON_CAPACITY_RE.search(f"{title} {sku}"):
        return caps                            # digits here are inches or tons
    dm = re.search(r"(\d+)([A-Za-z]*)(\d?)", model or "")
    # A number carrying its own unit is that unit, not a load rating. Measured
    # live: 'ICT-1200W-KISTO' became 12 kg, 'IC-CJ1360WPC' 13.6 kg and
    # 'DI-ARDO-1000W' 10 kg, and assess() then rejected every real page for
    # those cooktops and irons with "title lacks capacity/model info". Watts,
    # volts, litres and hertz are all coded straight into these model numbers.
    # The letters must be GLUED to the digits (a following separate word is
    # not a unit) and TERMINAL (letters sandwiched between digits are a model
    # series, not a unit: Samsung 'WA70H4200' and LG 'T70VBMB1Z' are 7 kg
    # washers, and bailing on the H/V blinded the capacity guard for them).
    if (dm and dm.group(2) and not dm.group(3)
            and dm.group(2)[0].upper() in ("W", "V", "L", "H")):
        return caps
    if dm:
        g = float(dm.group(1))
        for v in (g, g / 10.0, g / 100.0):     # 650 -> 6.5, 701 -> 7, 90 -> 9
            if 4 <= v <= 20:
                caps.add(v)
    return caps


def sku_category_words(sku):
    """Category keywords implied by SKU codes like 'WM' -> washing machine."""
    toks = set(re.split(r"[\s\-,/]+", str(sku).upper()))
    for code, words in CATEGORY_WORDS.items():
        if code in toks:
            return words
    return ()


UNIT_TOKEN_RE = re.compile(
    r"^[\d.]+(?:rpm|kgs?|hz|cm|mm|inch|in|l|ltrs?|litres?|w|v|watts?|"
    r"stars?|yrs?|years?|mins?|gb|tb)$", re.I)


def title_model_tokens(title):
    """Model-code-looking tokens in a listing title (6+ chars, letters+digits,
    not unit values like '1400rpm')."""
    out = []
    for t in re.split(r"[^A-Za-z0-9.]+", str(title)):
        t = t.strip(".")
        if len(t) < 6:
            continue
        if not (re.search(r"\d", t) and re.search(r"[A-Za-z]", t)):
            continue
        if UNIT_TOKEN_RE.match(t):
            continue
        out.append(norm(t))
    return out


def common_prefix_len(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def result_looks_relevant(title, sku, model):
    """Cheap screen on SEARCH-RESULT titles: for category-coded SKUs, skip
    results that clearly belong to another category (TVs, laptops...)."""
    cat = sku_category_words(sku)
    if not cat:
        return True
    t = str(title or "").lower()
    if any(w in t for w in cat):
        return True
    if KG_RE.search(t):
        return True
    mn = norm(model)
    core = mn[:-2] if len(mn) >= 8 else mn
    if core and core in norm(t):
        return True
    return False


def assess(rec, sku, model, brand, min_price, max_price):
    """
    Strict verdict on an extracted listing.
    Returns (verdict, reason):
      'strong' - model code verified, safe
      'close'  - model matches except trailing characters (variant) - flag it
      'weak'   - unverified brand-led match - flag it
      ''       - rejected, with reason
    """
    title = re.sub(r"\s+", " ", rec["title"]).strip()
    t_low = " " + title.lower() + " "
    title_n, url_n = norm(title), norm(rec["url_link"])
    brand_n, model_n = norm(brand), norm(model)

    # -- hard rejects: accessory / compatibility listings ------------------
    for ph in COMPAT_PHRASES:
        if ph in t_low:
            return "", f"compatibility wording ('{ph}') - accessory listing"
    if brand_n and f" for {brand.lower()}" in t_low:
        return "", f"'for {brand}' wording - accessory listing"
    starts_with_brand = bool(brand_n) and title_n.startswith(brand_n)
    first_words = " ".join(title.lower().split()[:6])
    if ACCESSORY_RE.search(first_words):
        return "", "accessory keyword at start of title"
    if ACCESSORY_RE.search(t_low) and not starts_with_brand:
        return "", "accessory keyword and title does not start with brand"

    # -- the listing must be in English ------------------------------------
    # a brand site's Arabic/Chinese locale describes the right product in the
    # wrong language; the title would go straight onto an English storefront
    if mostly_non_latin(title):
        return "", "listing is not in English (wrong locale page)"

    # -- brand must appear somewhere in the listing ------------------------
    # KI-3: check each string separately. Concatenating them let a title ending
    # "...sam" plus a URL starting "sung..." fake a "samsung" match.
    if brand_n and brand_n not in title_n and brand_n not in url_n:
        return "", "brand not found in listing"

    # -- price sanity bounds ----------------------------------------------
    p = price_value(rec["sale price"])
    if p is not None:
        if min_price and p < min_price:
            return "", f"price {p:.0f} below --min-price {min_price:.0f}"
        if max_price and p > max_price:
            return "", f"price {p:.0f} above --max-price {max_price:.0f}"

    # -- model verification ------------------------------------------------
    if model_n and (model_n in title_n
                    or (model_n in url_n and starts_with_brand)):
        return "strong", ""

    # -- everything below is UNVERIFIED: guard against wrong variants ------
    caps = expected_caps(sku, model, title)
    tm = KG_RE.search(title)
    if caps and tm:
        v = float(tm.group(1))
        if not any(abs(v - c) <= 0.15 for c in caps):
            return "", (f"capacity mismatch: listing says {v:g} kg, "
                        f"SKU implies {sorted(caps)}")
    if caps and not tm:
        # a title that names neither the capacity nor the model is usually a
        # homepage/category page - never trust it as an unverified match
        core_t = model_n[:-2] if len(model_n) >= 8 else model_n
        if not (core_t and core_t in title_n):
            return "", "title lacks capacity/model info - cannot verify"
    cat = sku_category_words(sku)
    if cat and not any(w in t_low for w in cat):
        return "", f"category mismatch: title lacks '{cat[0]}'"

    if model_n:
        # a title that names a DIFFERENT model code is a different product -
        # unless the codes share a long prefix (colour/variant suffix)
        for tn in title_model_tokens(title):
            if tn and tn != model_n and tn not in model_n and model_n not in tn:
                cp = common_prefix_len(tn, model_n)
                if cp >= 4 and cp / max(len(tn), len(model_n)) >= 0.7:
                    return "close", "model variant (shared prefix)"
                return "", f"listing shows a different model code ({tn})"
        core = model_n[:-2] if len(model_n) >= 8 else ""
        if core and (core in title_n or core in url_n):    # KI-3, same bug
            return "close", "model matches except trailing characters"
        # last resort for internal codes never printed on listings: a clean,
        # brand-led product page may surface - but only flagged, never FOUND
        if starts_with_brand:
            return "weak", "model code not verified"
        return "", "model code not found in listing title/URL"

    # SKU has no clean model code -> brand match is the best we can do
    if brand_n and brand_n in title_n:
        return "weak", ""
    return "", "no reliable match"


def mostly_non_latin(text):
    """True for a title from a site's non-English locale.

    Seen live: haier.com served the ARABIC page for H43S80GFX, and the Arabic
    title went into the spreadsheet and would have gone onto the storefront.
    Ignores digits and punctuation so model codes and sizes do not skew it."""
    letters = [c for c in str(text) if c.isalpha()]
    if len(letters) < 6:
        return False
    latin = sum(1 for c in letters if "a" <= c.lower() <= "z")
    return latin / len(letters) < 0.6


def bogus_mrp(mrp, sale):
    """An 'MRP' at or below the selling price is impossible, and one several
    times the price is a number scraped off an unrelated element. Flagged
    rather than deleted: Indian listings really do carry inflated MRPs, so a
    human decides."""
    m, s = price_value(mrp), price_value(sale)
    if m is None or s is None or s <= 0:
        return ""
    if m <= s:
        return "MRP <= price"
    if m > s * 4:
        return f"MRP {m:.0f} is {m / s:.1f}x the price"
    return ""


def domain_quality(url):
    """2 = priority marketplace, 1 = known store or brand site, 0 = anything
    else. Unknown domains are usually aggregators, marketplaces-of-marketplaces
    or thin affiliate pages, so they must never outrank a real retailer."""
    d = domain_of(url)
    if not d:
        return 0
    if any(d.endswith(p) for p in PRIORITY_DOMAINS):
        return 2
    if any(d.endswith(k) for k in KNOWN_STORES) or d in BRAND_SITES.values():
        return 1
    return 0


def probe(url, min_images=MIN_IMAGES):
    """--probe: fetch one URL and show exactly what came back. Exercises the
    real fetch/block/render path without touching any spreadsheet."""
    log(f"probing: {url}")
    log(f"domain:  {domain_of(url)}  (quality {domain_quality(url)}, "
        f"product url: {is_product_url(url)})")
    html, why = fetch(url)
    if not html and PW_ENABLED:
        log("trying the headless fallback...")
        html = render_fetch(url)
        why = why or "render"
    if not html:
        log(f"RESULT: no page  (reason: {why or 'unknown'})")
        return
    log(f"fetched {len(html) // 1024} KB")
    rec = extract(url, html)
    imgs = split_image_list(rec["image_url"])
    log(f"title  : {rec['title'][:100] or '(none)'}")
    log(f"mrp    : {rec['regular price'] or '(none)'}")
    log(f"sale   : {rec['sale price'] or '(none)'}")
    log(f"images : {len(imgs)} candidate(s) (need {min_images}; a real row "
        f"measures these and keeps at most {MAX_IMAGES})")
    for i in imgs:
        log(f"    {'THUMB' if is_thumbnail(i) else '     '} {i[:110]}")
    spec_rows = parse_spec_cell(rec.get("specs", ""))
    log(f"specs  : {len(spec_rows)}")
    for label, value in spec_rows:
        log(f"          {label}: {value[:70]}")


def image_count(rec):
    return len(split_image_list(rec.get("image_url", "")))


def cap_images(rec, limit=MAX_IMAGES):
    """Trim a record's candidate pool down to the gallery that ships.

    extract() deliberately over-collects (IMAGE_POOL) so the size filter has
    spares; anything that reaches the spreadsheet without passing through
    upgrade_gallery still has to be cut back here."""
    imgs = split_image_list(rec.get("image_url", ""))
    if len(imgs) > limit:
        rec["image_url"] = ",".join(imgs[:limit])
    return rec


def status_for(rec, url, min_images=MIN_IMAGES, donors=()):
    """The FOUND status line and its honest caveats.

    `donors` names the other sources a merged field came from. It is disclosed
    rather than hidden: the reviewer reads the status to decide what to trust,
    and a price taken from a different site than the title and images is
    exactly the kind of thing they should be told about."""
    status = f"FOUND ({domain_of(url)})"
    money = [d for d in donors if d.startswith(("price", "MRP"))]
    if money:
        status += " - " + " and ".join(money)
    if not rec.get("sale price"):
        status += " - no price shown"
    n_img = image_count(rec)
    if n_img < min_images:
        status += f" - only {n_img} image(s)"
    if rec.get("_gallery_below_floor"):
        status += f" - gallery below {IMAGE_MIN_PX}px floor, verify"
    why = bogus_mrp(rec.get("regular price"), rec.get("sale price"))
    if why:
        status += f" - {why}, check"
    if not domain_quality(url):
        status += " - unlisted store, verify"
    return status


def flag_gallery_collision(rec, sku, row, seen_galleries):
    """If `rec`'s gallery exactly matches an EARLIER row's under a different
    SKU, append a flag to its status - the wrong-match signature
    validate_csv.py otherwise only catches after the CSV is built
    (docs/PIPELINE_GUIDE.md: "one matched the other's listing"). Records this
    row's gallery either way, mutating `seen_galleries` in place so later rows
    can be compared against it. Only the WHOLE gallery matching fires; a
    single shared marketing photo between two real listings is normal and
    would just be noise on every row."""
    if not (rec.get("url_link")
            and str(rec.get("status", "")).startswith(("FOUND", "CLOSE"))):
        return
    imgs = frozenset(split_image_list(rec.get("image_url", "")))
    if not imgs:
        return
    prior = seen_galleries.get(imgs)
    if prior and prior[1] and prior[1] != sku:
        rec["status"] = (f"{rec['status']} - gallery matches "
                         f"row {prior[0]} ({prior[1]}), verify")
    else:
        seen_galleries[imgs] = (row, sku)


def merge_sources(best, hits, min_images=MIN_IMAGES):
    """Fill the winner's gaps from the OTHER verified hits for the same SKU.

    Every record here passed assess() as "strong", which means the model code
    was verified in its title or URL - so they are the same product, not merely
    similar ones. That matters, because the two source types are complementary
    and each is useless alone: brand sites (haier.com, samsung.com) publish the
    full gallery and no price at all, marketplaces publish the price. Measured
    on the 43-SKU LED batch before this existed: 51% of verified rows had no
    MRP and 9 had no price whatsoever, purely because one source won the whole
    row and its gaps were never filled.

    A value the winner already has is never overwritten with a different one.
    The two exceptions are additive rather than substitutive, and both exist
    because "the winner's version" is not always "the best version":
      - the GALLERY is topped up past min_images from other known retailers,
        because a brand site routinely publishes eight photos where the
        marketplace that won on price published two;
      - an MRP that bogus_mrp() rejects is replaced by a plausible one, since
        keeping a number known to be wrong helps nobody.
    """
    others = [h for h in hits if h is not best]
    if not others:
        return best, []
    # a better source is a more trustworthy donor
    others.sort(key=lambda r: -domain_quality(r.get("url_link", "")))
    donors = []

    if not best.get("sale price"):
        for h in others:
            if h.get("sale price") and not bogus_mrp(h.get("regular price"),
                                                     h.get("sale price")):
                best["sale price"] = h["sale price"]
                if not best.get("regular price"):
                    best["regular price"] = h.get("regular price", "")
                donors.append(f"price from {domain_of(h['url_link'])}")
                break

    # An MRP is only meaningful beside a price - and unjudgeable without one,
    # since bogus_mrp() cannot compare it to anything. No price, no MRP.
    # A present-but-bogus MRP counts as a gap: it was going to be flagged for a
    # human anyway, so a donor's plausible figure is strictly better.
    own_mrp_bad = bool(best.get("regular price")) and bool(
        bogus_mrp(best.get("regular price"), best.get("sale price")))
    if best.get("sale price") and (not best.get("regular price") or own_mrp_bad):
        for h in others:
            # only trust an MRP that is sane against the price we will publish
            if h.get("regular price") and not bogus_mrp(h["regular price"],
                                                        best["sale price"]):
                best["regular price"] = h["regular price"]
                donors.append(f"MRP from {domain_of(h['url_link'])}")
                break

    # Gallery: reach min_images from ANY verified hit (a thin row is worse than
    # a mixed-source one), then keep topping up to MAX_IMAGES but only from
    # known retailers. The winner's own photos are always first in the list, so
    # the primary product image never changes hands.
    merged = split_image_list(best.get("image_url", ""))
    for h in others:
        if len(merged) >= MAX_IMAGES:
            break
        if len(merged) >= min_images and not domain_quality(h.get("url_link", "")):
            continue                  # past the floor, unlisted stores add risk
        extra = split_image_list(h.get("image_url", ""))
        if not extra:
            continue
        # Measure what we are about to splice in. Only the WINNER's gallery went
        # through upgrade_gallery, so donated images used to bypass the size
        # filter entirely and a merged row could ship photos under IMAGE_MIN_PX
        # that the pipeline claims to have removed.
        probe_many(extra[:IMAGE_PROBE_MAX])
        extra, too_small = drop_small_images(extra, IMAGE_MIN_PX)
        if too_small:
            log(f"      skipped {len(too_small)} donated image(s) under "
                f"{IMAGE_MIN_PX}px from {domain_of(h.get('url_link', ''))}")
        if not extra:
            continue
        before = len(merged)
        merged = dedupe_images(merged + extra, best.get("url_link", ""))
        if len(merged) > before:
            best["image_url"] = ",".join(merged)
            donors.append(f"{len(merged) - before} image(s) from "
                          f"{domain_of(h['url_link'])}")

    # Specs: the page that won on price is often the one with the thinnest spec
    # table. Labels the winner already stated are never replaced.
    if SPECS_ENABLED:
        own = parse_spec_cell(best.get("specs", ""))
        started_with = len(own)              # was re-parsed just to count it
        have = {norm(label) for label, _ in own}
        for h in others:
            if len(own) >= MAX_SPECS:
                break
            for label, value in parse_spec_cell(h.get("specs", "")):
                key = norm(label)            # was computed twice per row
                if key in have or len(own) >= MAX_SPECS:
                    continue
                have.add(key)
                own.append((label, value))
        n_new = len(own) - started_with
        if n_new:
            best["specs"] = format_specs(own)
            donors.append(f"{n_new} spec(s) from other sources")
    return best, donors


def parse_spec_cell(text):
    """The inverse of format_specs: 'Label: Value | ...' -> [(label, value)]."""
    rows = []
    for chunk in str(text or "").split("|"):
        if ":" not in chunk:
            continue
        label, value = chunk.split(":", 1)
        label, value = clean_spec_text(label), clean_spec_text(value)
        if label and value:
            rows.append((label, value))
    return rows


def hunt_donors(candidates, tried, sku, model, brand, min_price, max_price,
                budget=MERGE_BUDGET):
    """Fetch a couple more pages for the sole purpose of COMPLETING a row that
    is already verified.

    merge_sources can only merge what was fetched, and the main loop spends its
    FETCH_BUDGET looking for a WINNER, not for the missing half of one. So a
    brand site that wins on title and gallery but publishes no price at all was
    written out as "no price shown" while a marketplace listing for the very
    same model sat unfetched in the candidate list - the exact case item 4 of
    AUTOMATION_OPPORTUNITIES describes.

    Only spent when there is a verified winner to complete, so a SKU that was
    never found pays nothing. Donors are held to the same assess() == "strong"
    bar as the winner: the model code must be verified on the donor page too,
    or its price belongs to a different product."""
    picks = [u for u in candidates
             if u not in tried and domain_quality(u)
             and not domain_cooling(domain_of(u))]
    picks.sort(key=domain_rank)            # a price lives on a marketplace
    found, used, seen_dom = [], 0, set()
    for u in picks:
        if used >= budget:
            break
        dom = domain_of(u)
        if dom in seen_dom:
            continue                       # one page per domain, never a crawl
        seen_dom.add(dom)
        used += 1
        log(f"    completing: {u[:90]}")
        _net_before = NET_CALLS
        html, why = fetch(u)
        polite_sleep(1.5, 3.0, since=_net_before)
        if not html:
            if is_block_reason(why):
                trip_domain(dom, why)
            continue
        rec = extract(u, html)
        if not rec.get("title"):
            continue
        verdict, _ = assess(rec, sku, model, brand, min_price, max_price)
        if verdict == "strong":
            rec["url_link"] = rec.get("url_link") or u
            found.append(rec)
    return found


def rec_score(rec, min_images=MIN_IMAGES):
    """Rank candidates by SOURCE first, completeness second. Ranking on
    completeness alone let a thin unknown site with a big gallery beat a real
    retailer, which is how random sites started winning."""
    n_img = len(split_image_list(rec.get("image_url", "")))
    completeness = ((2 if rec.get("sale price") else 0)
                    + (1 if rec.get("regular price") else 0)
                    + min(n_img, min_images))
    return (domain_quality(rec.get("url_link", "")), completeness)


def extract_page(u, html):
    """extract() plus the JS fallbacks, shared by the search crawl and
    --crawl-urls. Returns None when the page holds no parseable product."""
    dom = domain_of(u)
    rec = extract(u, html)
    if not rec["title"]:
        # fetched fine but nothing parsed out, usually a JS-built page.
        # This used to drop out silently, which read exactly like "checked
        # it and rejected it" in the log.
        log("      no product data in the page (built by JavaScript?)"
            + ("" if PW_ENABLED else " - try --js-fallback"))
        if PW_ENABLED:
            rendered = render_fetch(u)
            if rendered:
                rec = extract(u, rendered)
        if not rec["title"]:
            return None
    # price built client-side (croma and friends): one more try with a real
    # browser rather than writing the row off as "no price shown"
    if not rec["sale price"] and any(j in dom for j in JS_PRICE_DOMAINS):
        if PW_ENABLED:
            rendered = render_fetch(u)
            if rendered:
                rrec = extract(u, rendered)
                if rrec["sale price"]:
                    rec = rrec
                    log("      price recovered via JS render")
        elif _PW["state"] != "dead":
            log("      price looks JS-rendered - rerun with --js-fallback "
                "to capture it")
    return rec


def finalize_gallery(rec):
    """Measure the gallery before the row can claim it. Done here rather than
    in extract() so only a listing we are about to accept pays for the probes -
    most fetched pages never get this far."""
    keep, small, note = upgrade_gallery(
        split_image_list(rec.get("image_url", "")), IMAGE_MIN_PX)
    if note:
        log(f"      images {note}")
    if small:
        log(f"      dropped {len(small)} image(s) under {IMAGE_MIN_PX}px: "
            + ", ".join(d for _, d in small[:4]))
    if not keep and small:
        # Every candidate was measured and IS a real photo, just under the HD
        # floor - a small photo on the storefront beats an imageless listing,
        # and a human can always ask for a better one later. Largest-first,
        # capped at MIN_IMAGES so a thin fallback gallery does not silently
        # read as a full one. status_for() surfaces _gallery_below_floor so
        # the reviewer knows these did not clear the normal bar.
        def _area(u):
            d = image_dimensions(u)
            return d[0] * d[1] if d else 0
        ranked = sorted(small, key=lambda t: -_area(t[0]))
        keep = [u for u, _ in ranked[:MIN_IMAGES]]
        rec["_gallery_below_floor"] = True
        log(f"      none reached {IMAGE_MIN_PX}px - kept the {len(keep)} "
            f"largest available instead of shipping zero images")
    # ALWAYS reassign: extract() hands over an IMAGE_POOL-sized candidate
    # list, so leaving it untouched when nothing was dropped would write more
    # than MAX_IMAGES into the sheet
    rec["image_url"] = ",".join(keep[:MAX_IMAGES])


def lookup_sku(sku, min_price=0.0, max_price=0.0, min_images=MIN_IMAGES):
    brand, model, tiers = build_queries(sku)

    global _SKU_SEQ
    _SKU_SEQ += 1
    _PW["sku_used"] = 0
    seen = set()
    best_strong, best_score = None, (-1, -1)     # (domain quality, completeness)
    close_hits, weak_hits, strong_hits = [], [], []
    fetched, per_dom, fail_reasons, tried = 0, {}, [], set()
    candidates = []           # every URL any tier surfaced, for hunt_donors

    def run_tier(queries):
        """Search one tier; returns its NEW candidate URLs, best domains first."""
        found = []
        for q, dom in queries:
            if len(found) >= SEARCH_POOL:
                break         # the fetch budget can never reach more anyway
            log(f"    searching: {q}")
            _net_before = NET_CALLS
            for r in web_search(q):
                u = r.get("href", "")
                if not u or is_blocked(u):
                    continue
                if dom and not domain_of(u).endswith(dom):
                    continue
                u = canonical_url(u)
                if u in seen:
                    continue
                seen.add(u)
                if not is_product_url(u):
                    continue
                if looks_like_accessory(r.get("title", ""), brand):
                    log(f"      skipped accessory result: {str(r.get('title'))[:70]}")
                    continue
                if not result_looks_relevant(r.get("title", ""), sku, model):
                    log(f"      skipped off-category result: {str(r.get('title'))[:70]}")
                    continue
                found.append(u)
            polite_sleep(0.6, 1.4, since=_net_before)
        # Round-robin: take the best URL from EACH domain before any domain's
        # second. Straight domain_rank order let amazon occupy every fetch
        # slot, so one hostile site could bury a SKU that flipkart had.
        _nth = {}

        def _order(u):
            d = domain_of(u)
            _nth[d] = _nth.get(d, 0) + 1
            return (_nth[d], domain_rank(u))

        found.sort(key=_order)
        return found

    def crawl(batch):
        """Fetch and strictly verify, best domains first. Returns a rec only
        when it is COMPLETE (price + enough images + a real retailer);
        anything less is banked in the hit lists for the fallback logic."""
        nonlocal best_strong, best_score, fetched
        for u in batch:
            if fetched >= FETCH_BUDGET:
                break
            dom = domain_of(u)
            if domain_cooling(dom):
                fail_reasons.append((dom, "cooling off"))
                continue
            if per_dom.get(dom, 0) >= MAX_PAGES_PER_DOMAIN:
                continue
            per_dom[dom] = per_dom.get(dom, 0) + 1
            fetched += 1
            tried.add(u)
            log(f"    checking:  {u[:95]}")
            _net_before = NET_CALLS
            html, why = fetch(u)
            polite_sleep(1.5, 3.0, since=_net_before)
            if not html:
                fail_reasons.append((dom, why))
                if is_block_reason(why):
                    trip_domain(dom, why)
                    html = render_fetch(u)  # headless retry, if --js-fallback
                if not html:
                    continue
            rec = extract_page(u, html)
            if rec is None:
                fail_reasons.append((dom, "no parseable product data"))
                continue
            verdict, reason = assess(rec, sku, model, brand, min_price, max_price)
            if verdict == "strong":
                finalize_gallery(rec)
                n_img = image_count(rec)
                known = domain_quality(u)
                rec["url_link"] = rec.get("url_link") or u
                rec["status"] = status_for(rec, u, min_images)
                strong_hits.append(rec)
                if (rec["sale price"] and n_img >= min_images and known
                        and not bogus_mrp(rec["regular price"], rec["sale price"])):
                    return rec              # complete AND from a real retailer
                score = rec_score(rec, min_images)
                if score > best_score:
                    best_strong, best_score = rec, score
                if not known:
                    log(f"      complete but from an unlisted store ({domain_of(u)}) - "
                        f"still looking for a known retailer...")
                else:
                    log(f"      verified but incomplete (price: "
                        f"{'yes' if rec['sale price'] else 'no'}, images: {n_img}) - "
                        f"trying next source...")
            elif verdict == "close":
                close_hits.append(rec)
            elif verdict == "weak":
                weak_hits.append(rec)
            else:
                log(f"      rejected: {reason}")
        return None

    # -- tiers run lazily: search a tier, crawl what it found, and only fall
    # to the next tier when nothing was verified. The old shape ran ALL the
    # searches up front, so every SKU paid for the title queries even when
    # the very first model-code search had already found the listing.
    for tier in tiers:
        fresh = run_tier(tier)
        candidates += fresh
        winner = crawl(fresh)
        if winner:
            return winner
        if best_strong:
            # verified, just incomplete. The next tier would only re-find the
            # same listing; hunt_donors/merge_sources below complete the row.
            break

    if best_strong:
        # the winner is verified but incomplete. Before settling for that, go
        # and fetch the missing half from a source the budget never reached -
        # then fill the gaps from every verified hit for this same SKU.
        if MERGE_BUDGET and (not best_strong.get("sale price")
                             or image_count(best_strong) < min_images):
            strong_hits += hunt_donors(candidates, tried, sku, model, brand,
                                       min_price, max_price, MERGE_BUDGET)
        best_strong, donors = merge_sources(best_strong, strong_hits, min_images)
        if donors:
            best_strong["status"] = status_for(
                best_strong, best_strong["url_link"], min_images, donors)
            log("      merged: " + "; ".join(donors))
        return best_strong
    # best SOURCE first, then completeness - see rec_score
    # These never went through upgrade_gallery (only a listing we are about to
    # accept pays for the probes), so they still carry the IMAGE_POOL-sized
    # candidate list and have to be trimmed before they reach the sheet.
    if close_hits:
        rec = max(close_hits, key=lambda r: rec_score(r, min_images))
        rec["status"] = f"CLOSE MATCH - CHECK ({domain_of(rec['url_link'])})"
        return cap_images(rec)
    if weak_hits:
        rec = max(weak_hits, key=lambda r: rec_score(r, min_images))
        rec["status"] = f"CHECK MANUALLY ({domain_of(rec['url_link'])})"
        return cap_images(rec)
    return {"status": not_found_status(candidates, fetched, fail_reasons)}


def not_found_status(candidates, fetched, fail_reasons):
    """Say WHY nothing was found. 'blocked' and 'genuinely not listed' used to
    look identical in the sheet, which sent people hunting for products that
    were there all along."""
    if not candidates:
        return "NOT FOUND - no usable search results"
    blocked = [(d, w) for d, w in fail_reasons if is_block_reason(w)]
    if blocked and len(blocked) >= max(1, fetched):
        detail = ", ".join(f"{d} {w}" for d, w in blocked[:3])
        return f"NOT FOUND - all sources blocked ({detail})"[:200]
    if blocked:
        return (f"NOT FOUND - {len(blocked)} source(s) blocked, "
                f"{max(0, fetched - len(blocked))} checked, none matched")[:200]
    return f"NOT FOUND - {fetched} page(s) checked, none matched"[:200]

# ------------------------------ excel I/O ---------------------------------

# ---------------------------------------------------------------------------
# The two-pass flow: --discover finds a link and photos so a human can review
# them, --crawl-urls then extracts everything from the link that survived that
# review. Splitting it this way does not save crawling - the photos only exist
# on the product page, so pass 1 has to fetch it anyway. What it buys is the
# ORDER: nobody spends a full extraction on a listing that turns out to be the
# wrong product, and pass 2 replays pass 1's cached pages for free, paying
# network only for the URLs a human actually changed.
# ---------------------------------------------------------------------------

def discovery_status(rec):
    """Rewrite a pass-1 verdict into review vocabulary.

    Deliberately NOT the FOUND/CLOSE/CHECK words: those mean 'this row is
    finished', and app.py imports on `status.startswith("FOUND")`. A discovery
    sheet has no specs and an unreviewed link, so it must not be importable by
    accident."""
    url = str(rec.get("url_link") or "").strip()
    original = str(rec.get("status") or "").strip()
    if not url:
        return f"NEEDS LINK - {original or 'NOT FOUND'}"[:250]
    dom = domain_of(url)
    if original.startswith("FOUND"):
        return f"NEEDS CRAWL ({dom})"
    # CLOSE MATCH / CHECK MANUALLY: there IS a link, it is just not certain to
    # be the right product. Still crawlable, so the human only has to touch it
    # if it is actually wrong.
    return f"NEEDS CRAWL - CHECK LINK ({dom})"


def crawl_url(url, sku="", min_images=MIN_IMAGES):
    """PASS 2: extract everything from ONE known URL. Runs no searches.

    The human chose this listing, so it is the only source: there is no
    donor-hunting here, because hunting donors means searching, and the whole
    point of this pass is that it spends no search credits. A page that turns
    out to have no price says so rather than borrowing one from elsewhere."""
    _net_before = NET_CALLS
    html, why = fetch(url)
    if not html:
        html = render_fetch(url)          # headless retry, if --js-fallback
    if not html:
        return {"status": f"ERROR: {why or 'could not fetch'}"[:250]}

    rec = extract_page(url, html)
    if rec is None:
        return {"status": "ERROR: no product data in the page "
                          "(built by JavaScript? try --js-fallback)"}

    finalize_gallery(rec)
    rec["url_link"] = rec.get("url_link") or url
    rec["status"] = status_for(rec, url, min_images)

    # Sanity-check the page against the SKU, but never REJECT it: a human put
    # this URL here, and silently discarding their choice would be worse than
    # crawling a row they have to look at again. So it is recorded on the row.
    if sku:
        brand, model, _ = build_queries(sku)
        verdict, reason = assess(rec, sku, model, brand, 0, 0)
        if verdict != "strong":
            # assess() does not always give a reason (a 'close' verdict often
            # has none). "VERIFY:" with nothing after it tells a reviewer
            # exactly nothing, so fall back to naming the verdict.
            rec["status"] = (f"{rec['status']} - VERIFY: "
                             f"{reason or f'{verdict} match only'}")[:250]
    polite_sleep(1.5, 3.0, since=_net_before)
    return rec


def norm_header(h):
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


SKU_HEADERS = ["sku", "skucode", "skus", "skuid", "model", "modelno",
               "modelnumber", "modelname", "itemcode", "productcode", "code",
               # BUSY ERP stock exports: the model lives in the item-detail
               # column, wrapped in branch/unit noise that clean_busy_sku strips
               "busyitemdetail", "itemdetails", "itemdetail"]

BUSY_SKU_HEADERS = {"busyitemdetail", "itemdetails", "itemdetail"}

# A split AC exports as two BUSY lines - the indoor and the outdoor unit - for
# the SAME retail product. Searching '...(IDU)' finds nothing, and enriching
# both halves would list one air conditioner twice.
BUSY_UNIT_RE = re.compile(
    r"\(\s*(?:INNER|OUTER|INV|IDU|ODU|INDOOR|OUTDOOR)\s*\)|"
    r"\b(?:INNER|OUTER|INDOOR|OUTDOOR|IDU|ODU)\b", re.IGNORECASE)


def clean_busy_sku(raw):
    """BUSY item detail -> the model code a retailer would recognise.

    Only the unit designators and bracketed branch notes are removed. The model
    itself is left alone, punctuation included: build_queries() already splits
    brand from model, and stripping every non-alphanumeric here (as the older
    standalone scraper did) turned 'WA80F10B3NTL' style codes into unsearchable
    runs of characters."""
    text = str(raw or "").strip()
    if not text:
        return ""
    text = BUSY_UNIT_RE.sub(" ", text)
    text = re.sub(r"\([^)]*\)", " ", text)          # any remaining branch note
    text = re.sub(r"\s+", " ", text).strip(" -,")
    # '... - GHAZIABAD' is the stocking branch, not part of the model. It was
    # going into the search query ('VOLTAS 185V VECTRA PRISM - GHAZIABAD
    # site:amazon.in' matches nothing) and, once the sku column is mirrored,
    # onto the storefront. Only a trailing ' - ' segment with NO DIGITS is
    # dropped: a model code always carries digits, a branch name does not.
    parts = re.split(r"\s+-\s+", text)
    while len(parts) > 1 and not re.search(r"\d", parts[-1]):
        parts.pop()
    return " - ".join(parts).strip(" -,")

COL_ALIASES = {
    "title": ["title", "producttitle", "productname", "name"],
    "regular price": ["regularprice", "mrp", "listprice"],
    "sale price": ["saleprice", "salesprice", "sellingprice", "offerprice", "price"],
    "url_link": ["urllink", "url", "link", "producturl", "urlofthewebsite", "website"],
    "image_url": ["imageurl", "imageurls", "images", "imageaddress", "imagelink", "image"],
    # the retailer's own spec table, 'Label: Value | Label: Value'. app.py reads
    # it into real WooCommerce attributes; an older sheet without the column
    # simply gets it appended on the next run.
    "specs": ["specs", "specifications", "spectable", "attributes"],
    "status": ["status", "lookupstatus"],
}


def open_sheet(path, sheet=None):
    wb = openpyxl.load_workbook(path)
    ws = wb[sheet] if sheet else wb.active
    return wb, ws


def load_input(path, sheet):
    if path.suffix.lower() in (".csv", ".tsv"):
        import csv as _csv
        delim = "\t" if path.suffix.lower() == ".tsv" else ","
        wb = openpyxl.Workbook()
        ws = wb.active
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            for r_i, row in enumerate(_csv.reader(f, delimiter=delim), 1):
                for c_i, val in enumerate(row, 1):
                    ws.cell(row=r_i, column=c_i, value=val)
        return wb, ws
    return open_sheet(path, sheet)


def map_columns(ws, sku_col_arg):
    headers = {}
    for c in range(1, ws.max_column + 1):
        h = norm_header(ws.cell(row=1, column=c).value)
        if h and h not in headers:
            headers[h] = c

    sku_col = None
    if sku_col_arg:
        sku_col = headers.get(norm_header(sku_col_arg))
        if not sku_col:
            sys.exit(f"Could not find a column headed '{sku_col_arg}' in row 1.\n"
                     f"Headers found: {list(headers.keys())}")
    else:
        for cand in SKU_HEADERS:
            if cand in headers:
                sku_col = headers[cand]
                break
        if not sku_col:
            sku_col = 1          # fall back to first column

    cols = {}
    next_new = ws.max_column + 1
    for out, aliases in COL_ALIASES.items():
        found = None
        for a in aliases:
            if a in headers:
                found = headers[a]
                break
        if not found:
            ws.cell(row=1, column=next_new, value=out)
            found = next_new
            next_new += 1
        elif norm_header(ws.cell(row=1, column=found).value) != norm_header(out):
            # bound through an alias: rename the header to the canonical name so
            # app.py, which matches exact strings, still finds the column. Safe
            # because this workbook is the output copy - the input is untouched.
            was = ws.cell(row=1, column=found).value
            ws.cell(row=1, column=found, value=out)
            log(f"  column '{was}' renamed to '{out}' (app.py needs the exact name)")
        cols[out] = found
    return sku_col, cols


def ensure_app_columns(ws, sku_col, clean=None):
    """Append whatever app.py needs but the lookup never produces, so the
    filled sheet imports without hand-editing (see APP_EXTRA_COLS).

    `clean` transforms each value on its way into the 'sku' column. It exists
    for BUSY ERP exports, where the item-detail cell that gets SEARCHED is
    cleaned ('VOLTAS 185V VECTRA PRISM (INNER) - GHAZIABAD' -> 'VOLTAS 185V
    VECTRA PRISM') but the RAW cell used to be what was mirrored - so the live
    WooCommerce SKU came out as 'AP-VOLTAS-185V-VECTRA-PRISM-(INNER)-GHAZIABAD',
    branch name and unit designator included, and the two halves of one split
    AC got two different SKUs that app.py could no longer collapse.

    Returns the column index of 'sku'."""
    headers = {}
    for c in range(1, ws.max_column + 1):
        h = norm_header(ws.cell(row=1, column=c).value)
        if h and h not in headers:
            headers[h] = c

    added = []
    for want in APP_EXTRA_COLS:
        if norm_header(want) not in headers:
            c = ws.max_column + 1
            ws.cell(row=1, column=c, value=want)
            headers[norm_header(want)] = c
            added.append(want)
    if added:
        log("Added columns app.py requires: " + ", ".join(added))

    # mirror the detected SKU column into 'sku' - app.py looks for that exact
    # header, while the input's own may be 'Item Details', 'Model No', etc.
    sku_out = headers[norm_header("sku")]
    if sku_out != sku_col:
        filled = repaired = 0
        for r in range(2, ws.max_row + 1):
            src = ws.cell(row=r, column=sku_col).value
            if src is None or not str(src).strip():
                continue
            want = str(src).strip()
            if clean:
                want = clean(want) or want
            have = str(ws.cell(row=r, column=sku_out).value or "").strip()
            if have == want:
                continue
            # Self-healing, like the KI-4 recovery path: a sheet filled by an
            # earlier run holds the RAW value here, and silently leaving it
            # would carry the bad SKU through every resume.
            if have and not clean:
                continue                    # a value someone may have edited
            ws.cell(row=r, column=sku_out, value=safe_cell(want))
            if have:
                repaired += 1
            else:
                filled += 1
        if filled:
            log(f"Copied {filled} SKU value(s) into the 'sku' column")
        if repaired:
            log(f"Rewrote {repaired} 'sku' value(s) that still held the raw "
                f"ERP text instead of the cleaned model code")
    return sku_out


ILLEGAL = re.compile(r"[\x00-\x08\x0b-\x1f]")

def safe_cell(value):
    return ILLEGAL.sub("", str(value)) if isinstance(value, str) else value


def ensure_writable(path):
    """Fail fast (before any scraping) if the output file is locked by Excel."""
    if path.exists():
        try:
            with open(path, "r+b"):
                pass
        except PermissionError:
            sys.exit(f"'{path.name}' is open in Excel (or locked by another "
                     f"program). Close it and run again.")


def safe_save(wb, out):
    """Save the workbook. If Excel has the file open (Windows locks it), wait
    for the user to close it instead of crashing and losing progress.
    Returns the path actually written (a _recovery file as last resort)."""
    for attempt in range(30):                     # up to ~5 minutes
        try:
            wb.save(out)
            return out
        except PermissionError:
            log(f"  !! Cannot save '{out.name}' - it is open in Excel. "
                f"Please close it; retrying in 10s ({attempt + 1}/30)...")
            time.sleep(10)
    alt = out.with_name(out.stem + "_recovery.xlsx")
    log(f"  !! Still locked - saving progress to '{alt.name}' instead. "
        f"(Rename it back to '{out.name}' before the next run to resume.)")
    wb.save(alt)
    return alt

# ------------------------------ batch report ------------------------------
# Everything below used to exist only as terminal scrollback: what the run
# cost, which domains blocked it, and - the one that actually matters - which
# rows a human still has to look at. Closing the window lost all of it, and a
# background run never had a window to begin with.
#
# The report describes the whole OUTPUT FILE, not just this run's rows, so it
# stays true across the resume-and-continue cycle a real batch goes through.

REVIEW_BUCKETS = (
    ("NOT FOUND", "not found"),
    ("ERROR", "errored"),
    ("CLOSE MATCH", "close match - verify the model code"),
    ("CHECK MANUALLY", "unverified - check before publishing"),
)


def status_bucket(status):
    """The status line minus its per-row detail, so 200 unique strings collapse
    into the handful of outcomes worth counting."""
    s = str(status or "").strip()
    if not s:
        return "(not processed)"
    if s.startswith("FOUND"):
        head, _, tail = s.partition(" - ")
        return "FOUND" if not tail else "FOUND (incomplete)"
    for prefix, _ in REVIEW_BUCKETS:
        if s.startswith(prefix):
            return prefix
    return s.split(" - ")[0][:40]


def collect_report_rows(ws, sku_col, cols):
    """(sku, status) for every data row, straight off the sheet."""
    rows = []
    for r in range(2, ws.max_row + 1):
        sku = ws.cell(row=r, column=sku_col).value
        if sku is None or not str(sku).strip():
            continue
        rows.append((r, str(sku).strip(),
                     str(ws.cell(row=r, column=cols["status"]).value or "").strip(),
                     str(ws.cell(row=r, column=cols["image_url"]).value or "").strip(),
                     str(ws.cell(row=r, column=cols["sale price"]).value or "").strip()))
    return rows


def write_report(path, rows, out_file, started, args, min_images):
    """Write the run summary next to the output. Never raises: a report that
    cannot be written must not fail a batch whose data is already saved."""
    buckets, review = {}, []
    no_price = thin_images = 0
    for r, sku, status, images, sale in rows:
        bucket = status_bucket(status)
        buckets[bucket] = buckets.get(bucket, 0) + 1
        if status.startswith("FOUND"):
            if not sale:
                no_price += 1
            if len(split_image_list(images)) < min_images:
                thin_images += 1
        for prefix, why in REVIEW_BUCKETS:
            if status.startswith(prefix):
                review.append((r, sku, status, why))
                break

    took = time.time() - started
    searched = CACHE_STATS["search_hit"] + CACHE_STATS["search_miss"]
    fetches = CACHE_STATS["page_hit"] + CACHE_STATS["page_miss"]
    lines = [
        f"# Batch report - {out_file.name}",
        "",
        f"- run finished: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- duration: {took / 60:.1f} min",
        f"- input: `{args.input}`",
        f"- output: `{out_file.name}`  ({len(rows)} data row(s))",
        f"- flags: `{' '.join(sys.argv[1:]) or '(none)'}`",
        "",
        "## Outcome",
        "",
        "| rows | status |",
        "|---:|---|",
    ]
    for bucket, n in sorted(buckets.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {n} | {bucket} |")
    lines += [
        "",
        f"Of the verified rows: {no_price} with no price, "
        f"{thin_images} with fewer than {min_images} images. "
        f"`--retry-incomplete` re-attempts exactly those.",
        "",
        "## Cost",
        "",
        f"- serper.dev calls this run: **{RUN_STATS['serper_calls']}** "
        f"(~1 credit each; the free tier is 2,500/month)",
        f"- DuckDuckGo fallback calls: {RUN_STATS['ddg_calls']}",
        f"- searches served from cache: {CACHE_STATS['search_hit']}/{searched}"
        + (f" - about {CACHE_STATS['search_hit']} credit(s) not spent"
           if CACHE_STATS["search_hit"] else ""),
        f"- pages: {RUN_STATS['pages_fetched']} downloaded, "
        f"{CACHE_STATS['page_hit']}/{fetches} served from cache",
    ]
    n_cache, bytes_cache = cache_size()
    lines.append(f"- cache now holds {n_cache} entr(ies), "
                 f"{bytes_cache / 1_048_576:.1f} MB")

    if RUN_STATS["blocked_domains"]:
        lines += ["", "## Domains that blocked us", "",
                  "| times | domain |", "|---:|---|"]
        for dom, n in sorted(RUN_STATS["blocked_domains"].items(),
                             key=lambda kv: -kv[1]):
            lines.append(f"| {n} | {dom} |")
        lines.append("")
        lines.append("A blocked row stays eligible for the next run - it is "
                     "transient, unlike NOT FOUND.")

    lines += ["", f"## Needs a human ({len(review)} row(s))", ""]
    if not review:
        lines.append("Nothing flagged - every row is verified.")
    else:
        lines += ["| row | sku | why | status |", "|---:|---|---|---|"]
        for r, sku, status, why in review[:200]:
            lines.append(f"| {r} | {sku} | {why} | {status[:80]} |")
        if len(review) > 200:
            lines.append("")
            lines.append(f"...and {len(review) - 200} more - open the sheet.")

    lines += [
        "",
        "## Next",
        "",
        "```",
        f'.venv\\Scripts\\python.exe validate_csv.py "{out_file.name}"',
        f'.venv\\Scripts\\python.exe build_csv.py "{out_file.name}" --month '
        f"{datetime.now():%Y/%m}",
        "```",
        "",
    ]
    try:
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except Exception as e:
        log(f"  (could not write the report: {type(e).__name__})")
        return None

# --------------------------------- main -----------------------------------

def main():
    global PW_ENABLED, PW_MAX_PER_RUN, PW_PROFILE_DIR, CACHE_ENABLED, CACHE_REFRESH
    global CACHE_TTL_DAYS, CACHE_DIR, MERGE_BUDGET, SPECS_ENABLED, DISCOVER_MODE
    global IMAGE_CHECK, IMAGE_MIN_PX
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="Fill product title / price / URL / images for SKUs from "
                    "Amazon.in, Flipkart and other Indian retailers.")
    ap.add_argument("input", nargs="?",
                    help="Path to .xlsx or .csv containing a SKU column")
    ap.add_argument("--sheet", help="Worksheet name (default: first sheet)")
    ap.add_argument("--sku-col", help="Header of the SKU column (default: auto-detect)")
    ap.add_argument("--limit", type=int, default=0, help="Process at most N SKUs this run")
    ap.add_argument("--force", action="store_true",
                    help="Re-process every row, even ones already filled")
    ap.add_argument("--retry-missing", action="store_true",
                    help="Re-attempt rows previously marked NOT FOUND / ERROR "
                         "(by default they stay skipped so runs move forward)")
    ap.add_argument("--retry-incomplete", action="store_true",
                    help="Re-attempt rows already FOUND but missing a price or "
                         "enough images; their existing good cells are kept")
    flow = ap.add_argument_group(
        "two-pass flow",
        "Review the links and photos BEFORE paying for a full extraction. "
        "Run --discover, fix the url_link cells the sheet marks NEEDS LINK, "
        "then run --crawl-urls on the same file.")
    flow.add_argument("--discover", action="store_true",
                      help="PASS 1: find a product link and photos for each SKU "
                           "and stop there - no spec table, no donor pages. "
                           "Rows are marked NEEDS CRAWL or NEEDS LINK, never "
                           "FOUND, so a half-done sheet cannot be imported.")
    flow.add_argument("--crawl-urls", action="store_true",
                      help="PASS 2: crawl the url_link already in the sheet and "
                           "extract everything from it. Runs NO searches, so it "
                           "spends no serper credits; pass 1's cached pages "
                           "replay for free and only links you changed are "
                           "fetched.")

    ap.add_argument("--js-fallback", action="store_true",
                    help="Use a headless browser as a last resort for blocked "
                         "or JavaScript-rendered pages (slow; needs: "
                         ".venv\\Scripts\\python.exe -m playwright install chromium)")
    ap.add_argument("--js-budget", type=int, default=PW_MAX_PER_RUN,
                    metavar="N",
                    help=f"Maximum headless renders per run "
                         f"(default: {PW_MAX_PER_RUN})")
    ap.add_argument("--js-profile", metavar="DIR", default=None,
                    help="Keep the headless browser's cookies in DIR between "
                         "runs, so a site that clears you once does not "
                         "re-challenge next run. Do not point two concurrent "
                         "runs at the same directory.")
    ap.add_argument("--probe", metavar="URL",
                    help="Fetch and parse a single URL, print what was "
                         "extracted, and exit. Touches no spreadsheet.")
    ap.add_argument("--delay", nargs=2, type=float, default=[3.0, 7.0],
                    metavar=("MIN", "MAX"),
                    help="Random pause between SKUs in seconds (default: 3 7)")
    ap.add_argument("--min-price", type=float, default=0,
                    help="Reject listings priced below this (kills accessories "
                         "like covers/stands; e.g. 8000 for washing machines)")
    ap.add_argument("--max-price", type=float, default=0,
                    help="Reject listings priced above this (0 = no cap)")
    ap.add_argument("--min-images", type=int, default=MIN_IMAGES,
                    help=f"Required image count before a listing is accepted "
                         f"as final; rows below this are flagged "
                         f"(default: {MIN_IMAGES})")
    ap.add_argument("--min-image-px", type=int, default=IMAGE_MIN_PX, metavar="N",
                    help=f"Drop gallery images whose longest edge measures less "
                         f"than this. Measured from the file header, not guessed "
                         f"from the URL (default: {IMAGE_MIN_PX})")
    ap.add_argument("--no-image-check", action="store_true",
                    help="Skip measuring image dimensions. Faster, but the row's "
                         "image count then includes anything the page linked, "
                         "however small.")
    ap.add_argument("--merge-budget", type=int, default=MERGE_BUDGET, metavar="N",
                    help=f"Extra pages fetched to complete a row that is "
                         f"verified but has no price or too few images "
                         f"(default: {MERGE_BUDGET}; 0 disables)")
    ap.add_argument("--no-specs", action="store_true",
                    help="Do not scrape the retailer's specification table "
                         "into the 'specs' column")

    cache_group = ap.add_argument_group("cache")
    cache_group.add_argument("--no-cache", action="store_true",
                             help="Do not read or write the page/search cache")
    cache_group.add_argument("--refresh-cache", action="store_true",
                             help="Ignore what is cached and fetch fresh pages, "
                                  "but store the new copies (use when prices "
                                  "have moved)")
    cache_group.add_argument("--cache-ttl", type=float, default=CACHE_TTL_DAYS,
                             metavar="DAYS",
                             help=f"Age at which a cached page/search stops "
                                  f"counting (default: {CACHE_TTL_DAYS:g}; "
                                  f"0 = never expire)")
    cache_group.add_argument("--cache-dir", metavar="PATH",
                             help=f"Where to keep it (default: {CACHE_DIR})")
    cache_group.add_argument("--cache-info", action="store_true",
                             help="Show what the cache holds, then exit")
    cache_group.add_argument("--cache-clear", action="store_true",
                             help="Delete every cached page and search, then exit")

    ap.add_argument("--report", metavar="PATH",
                    help="Where to write the run summary "
                         "(default: <output>_report.md)")
    ap.add_argument("--no-report", action="store_true",
                    help="Do not write a batch report")
    args = ap.parse_args()

    PW_ENABLED, PW_MAX_PER_RUN = args.js_fallback, args.js_budget
    PW_PROFILE_DIR = args.js_profile
    CACHE_ENABLED = not args.no_cache
    CACHE_REFRESH = args.refresh_cache
    CACHE_TTL_DAYS = args.cache_ttl
    MERGE_BUDGET = max(0, args.merge_budget)
    SPECS_ENABLED = not args.no_specs
    IMAGE_CHECK = not args.no_image_check
    IMAGE_MIN_PX = max(0, args.min_image_px)

    if args.discover and args.crawl_urls:
        sys.exit("--discover and --crawl-urls are the two halves of one flow. "
                 "Run --discover, review the sheet, then run --crawl-urls on "
                 "the same file.")
    if args.discover:
        # pass 1 is about the link and the photos. The spec table and the
        # donor pages are what pass 2 is for, and both cost extra fetches;
        # DISCOVER_MODE also trims the query set (see build_queries).
        SPECS_ENABLED = False
        MERGE_BUDGET = 0
        DISCOVER_MODE = True
    if args.cache_dir:
        CACHE_DIR = Path(args.cache_dir).expanduser().resolve()

    if args.cache_clear:
        cache_clear()
        return
    if args.cache_info:
        cache_info()
        return
    if args.probe:
        probe(args.probe, args.min_images)
        return
    if not args.input:
        sys.exit("Give an input file, or use --probe URL to test a single page.")

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"File not found: {src}")

    # KI-5: '<stem>_filled.xlsx' is this script's OWN output. Feeding it back in
    # would build '<stem>_filled_filled.xlsx' and re-scrape every row from
    # scratch, paying the search credits a second time.
    if "_filled" in src.stem:
        original = src.with_name(src.stem.split("_filled")[0] + src.suffix)
        # the two-pass flow lands here constantly: pass 2 IS about the filled
        # sheet, so the message has to name the flag the user actually wants
        want = "--crawl-urls" if args.crawl_urls else "--retry-incomplete"
        hint = (f'  .venv\\Scripts\\python.exe {Path(sys.argv[0]).name} '
                f'"{original.name}" {want}\n'
                f"  (that reads and updates '{src.name}' in place)"
                if original.exists() else
                f"  The original list '{original.name}' is not next to it - put "
                f"it back, or rename this file to something without '_filled'.")
        sys.exit(f"'{src.name}' is enricher output, not an input list.\n"
                 f"To fill in its gaps, run:\n{hint}")

    out = src.with_name(src.stem + "_filled.xlsx")
    recovery = out.with_name(out.stem + "_recovery.xlsx")
    resume_from = out if out.exists() else None

    # KI-4: safe_save diverts to '_recovery' when Excel holds the output open.
    # That copy carries the newest rows, so resuming from the stale original
    # silently re-scrapes everything done since the lock.
    if recovery.exists() and (resume_from is None
                              or recovery.stat().st_mtime > out.stat().st_mtime):
        log(f"!! '{recovery.name}' is newer than '{out.name}' (it was written "
            f"because the output was locked). Resuming from it; this run writes "
            f"back to '{out.name}'.")
        resume_from = recovery

    ensure_writable(out)

    if args.crawl_urls and resume_from is None:
        sys.exit(f"--crawl-urls crawls the links in '{out.name}', and that file "
                 f"does not exist yet.\nRun pass 1 first:\n"
                 f'  .venv\\Scripts\\python.exe {Path(sys.argv[0]).name} '
                 f'"{src.name}" --discover')

    if resume_from is not None:
        log(f"Resuming existing output: {resume_from.name}")
        wb, ws = open_sheet(resume_from, args.sheet)
    else:
        wb, ws = load_input(src, args.sheet)

    sku_col, cols = map_columns(ws, args.sku_col)
    log(f"Sheet: '{ws.title}' | SKU column: {ws.cell(row=1, column=sku_col).value!r} (col {sku_col})")
    busy_input = norm_header(ws.cell(row=1, column=sku_col).value) in BUSY_SKU_HEADERS
    busy_seen = {}
    # A second SKU whose gallery byte-for-byte matches an earlier row's is the
    # wrong-match signature validate_csv.py otherwise only catches after the
    # CSV is built (docs/PIPELINE_GUIDE.md: "one matched the other's listing").
    # Seeding from rows already FOUND/CLOSE lets a retry/resume run catch a
    # collision against OLD rows too, not just ones touched this run.
    seen_galleries = {}
    for pr in range(2, ws.max_row + 1):
        pr_status = str(ws.cell(row=pr, column=cols["status"]).value or "")
        if not pr_status.startswith(("FOUND", "CLOSE")):
            continue
        pr_imgs = frozenset(split_image_list(
            str(ws.cell(row=pr, column=cols["image_url"]).value or "")))
        if pr_imgs and pr_imgs not in seen_galleries:
            pr_sku = str(ws.cell(row=pr, column=sku_col).value or "").strip()
            seen_galleries[pr_imgs] = (pr, pr_sku)
    if busy_input:
        log("  BUSY ERP export detected - stripping unit designators "
            "((IDU)/(ODU)/INNER/OUTER) and collapsing split-unit duplicates")
    log("Output columns: " + ", ".join(f"{k} -> col {v}" for k, v in cols.items()))
    ensure_app_columns(ws, sku_col, clean_busy_sku if busy_input else None)
    if args.crawl_urls:
        log("PASS 2 (--crawl-urls): crawling the links in the sheet. "
            "No searches will run, so this costs no serper credits.")
    elif os.environ.get("SERPER_API_KEY"):
        log("Search: Google via serper.dev (API key detected)")
    else:
        log("Search: DuckDuckGo (free). Tip: set SERPER_API_KEY for better results.")
    if args.discover:
        log("PASS 1 (--discover): finding a link and photos only - no spec "
            "table, no donor pages. Review the sheet, then run --crawl-urls.")

    done = found = missed = no_link = 0
    stopped = False
    started = time.time()
    for r in range(2, ws.max_row + 1):
        sku = ws.cell(row=r, column=sku_col).value
        if sku is None or not str(sku).strip():
            continue
        if busy_input:
            cleaned = clean_busy_sku(sku)
            if not cleaned:
                continue
            # the indoor and outdoor halves of one split AC collapse to the same
            # model: enrich it once, and point the second row at the first
            first = busy_seen.get(cleaned.upper())
            if first:
                ws.cell(row=r, column=cols["status"],
                        value=f"SKIPPED - same unit as row {first}")
                log(f"\n  Row {r}: {sku} -> same product as row {first}, skipped")
                continue
            busy_seen[cleaned.upper()] = r
            sku = cleaned
        has_url = str(ws.cell(row=r, column=cols["url_link"]).value or "").strip()
        has_status = str(ws.cell(row=r, column=cols["status"]).value or "").strip()

        if args.crawl_urls:
            # PASS 2 crawls exactly the links in the sheet and nothing else. A
            # row with no link is the human's to fill in, so say so on the row
            # rather than silently passing over it.
            if not has_url:
                ws.cell(row=r, column=cols["status"],
                        value=has_status if has_status.startswith("NEEDS LINK")
                        else "NEEDS LINK - no url_link in this row")
                no_link += 1
                continue
            if not args.force and has_status.startswith("FOUND"):
                continue                  # already crawled from this link
        elif args.discover:
            if not args.force and has_url:
                continue                  # already has a link waiting for review
            if not args.force and has_status and not args.retry_missing:
                continue                  # looked before and found nothing
        elif not args.force:
            if has_url:
                incomplete = ((has_status.startswith("FOUND") and " - " in has_status)
                              or not ws.cell(row=r, column=cols["sale price"]).value
                              or not ws.cell(row=r, column=cols["image_url"]).value)
                if not (args.retry_incomplete and incomplete):
                    continue              # found earlier and complete (or
                                          # retrying incomplete not requested)
            elif has_status and not args.retry_missing:
                if "blocked" not in has_status.lower():
                    continue              # NOT FOUND / ERROR earlier - skip so
                                          # the run continues with fresh rows
                                          # (a blocked row is transient, so it
                                          # stays eligible for the next run)
        if args.limit and done >= args.limit:
            break

        done += 1
        log(f"\n[{done}] Row {r}: {sku}")
        _net_before_sku = NET_CALLS
        try:
            if args.crawl_urls:
                log(f"    crawling: {has_url[:95]}")
                rec = crawl_url(has_url, str(sku).strip(), args.min_images)
            else:
                rec = lookup_sku(str(sku).strip(), args.min_price, args.max_price,
                                 args.min_images)
                if args.discover:
                    rec["status"] = discovery_status(rec)
        except KeyboardInterrupt:
            log("\nStopped by user - progress saved.")
            stopped = True
            rec = None
        except Exception as e:
            rec = {"status": f"ERROR: {type(e).__name__}: {e}"[:250]}

        if rec:
            # A rec carrying a url_link describes ONE specific listing. Keeping
            # the previous listing's price next to this one's URL and title
            # makes a row that is individually plausible and jointly wrong -
            # observed live: a flipkart URL and title sitting on prices scraped
            # from digi1.in. Guarded on url_link, not on FOUND, because a
            # CLOSE/CHECK result is still a different listing; a NOT FOUND or
            # ERROR rec carries only a status, so a failed retry still cannot
            # wipe a good row.
            relisted = bool(rec.get("url_link"))
            flag_gallery_collision(rec, str(sku).strip(), r, seen_galleries)
            for key, col in cols.items():
                if rec.get(key):
                    ws.cell(row=r, column=col, value=safe_cell(rec[key]))
                elif relisted and key in ("regular price", "sale price",
                                          "image_url", "specs"):
                    ws.cell(row=r, column=col, value=None)
            st = rec.get("status", "")
            if args.discover:
                # in pass 1 "found" means there is a link to review, which is
                # the only thing pass 1 was asked for
                if rec.get("url_link"):
                    found += 1
                else:
                    missed += 1
            elif st.startswith(("FOUND", "CLOSE", "CHECK")):
                found += 1
            else:
                missed += 1
            log(f"    -> {st}")
            # A running tally, not just the end-of-run total: KI-observed pain
            # was finding out how many credits a batch spent only after it
            # finished. Printed every 10 rows so it stays readable on a long run.
            if RUN_STATS["serper_calls"] and done % 10 == 0:
                log(f"    [{RUN_STATS['serper_calls']} serper.dev call(s) this "
                    f"run so far - the free tier is 2,500/month]")
            if RUN_STATS["serper_calls"] == 2000:
                log("    WARNING: this run alone has used 2,000 serper.dev "
                    "calls (80% of the 2,500/month free tier) - check your "
                    "serper.dev dashboard before continuing.")

        out = safe_save(wb, out)
        if stopped:
            break
        try:
            polite_sleep(args.delay[0], args.delay[1], since=_net_before_sku)
        except KeyboardInterrupt:
            log("\nStopped by user - progress saved.")
            break

    out = safe_save(wb, out)
    _pw_shutdown()               # close the browser before the summary prints
    log(f"\nDone. Processed {done} SKUs: {found} found, {missed} not found / errors.")
    log(f"Output file: {out}")
    if CACHE_ENABLED and (CACHE_STATS["page_hit"] or CACHE_STATS["search_hit"]):
        log(f"Cache: {CACHE_STATS['search_hit']} search(es) and "
            f"{CACHE_STATS['page_hit']} page(s) replayed from disk "
            f"(~{CACHE_STATS['search_hit']} serper credit(s) not spent).")
    if SLEEP_SAVED >= 1:
        log(f"Politeness delays skipped: {SLEEP_SAVED / 60:.1f} min "
            f"(no host was contacted for those steps).")
    if RUN_STATS["serper_calls"]:
        log(f"serper.dev calls this run: {RUN_STATS['serper_calls']} "
            f"(~1 credit each).")
    if not args.no_report:
        report_path = (Path(args.report) if args.report
                       else out.with_name(out.stem + "_report.md"))
        # written from the SHEET, so it covers rows carried over from earlier
        # runs too - a resumed batch is one batch, not several
        written = write_report(report_path,
                               collect_report_rows(ws, sku_col, cols),
                               out, started, args, args.min_images)
        if written:
            log(f"Batch report: {written}")
    if args.discover:
        log(f"\nPass 1 done: {found} row(s) have a link to review, "
            f"{missed} need one pasted in.")
        log("Next: open the sheet, check the photos and links, and fill "
            "url_link on every row marked NEEDS LINK. Then run:")
        log(f'  .venv\\Scripts\\python.exe sku_enricher.py "{out.name}" --crawl-urls')
        log("Nothing is final yet - NEEDS CRAWL rows are not importable, "
            "and app.py will not pick them up.")
    elif args.crawl_urls:
        if no_link:
            log(f"\n{no_link} row(s) were skipped for having no url_link. "
                f"Fill those in and re-run --crawl-urls; the links already "
                f"crawled will not be fetched again.")
        log("Next: review the sheet, then build the CSV with build_csv.py.")
    elif missed:
        log("Rows marked NOT FOUND / CHECK MANUALLY are worth a quick manual look.")


if __name__ == "__main__":
    main()