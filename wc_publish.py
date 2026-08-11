"""wc_publish.py - push the generated CSV straight into WooCommerce.

Replaces the two manual steps at the end of the pipeline (upload images to the
Media Library, then Products > Import). Upserts on SKU, so re-running a
corrected batch updates the same products instead of duplicating them.

    set WC_URL / WC_KEY / WC_SECRET   (WooCommerce > Settings > Advanced > REST API)
    optional, for image upload:  WP_APP_USER / WP_APP_PASSWORD
                                 (WP Users > Profile > Application Passwords)

    .venv\\Scripts\\python.exe wc_publish.py --check
    .venv\\Scripts\\python.exe wc_publish.py out.csv                    # dry run
    .venv\\Scripts\\python.exe wc_publish.py out.csv --commit           # writes
    .venv\\Scripts\\python.exe wc_publish.py out.csv --sku WM-XYZ --commit
    .venv\\Scripts\\python.exe wc_publish.py --delete-sku WM-XYZ --commit

NOTHING is written without --commit, and new products are created as `draft`
unless you pass --status publish. WooCommerce keys only authenticate /wc/v3;
uploading files to the Media Library needs a WordPress Application Password,
which is why those are separate credentials.

If the store sits behind a CDN JavaScript challenge (KI-15) the API is
unreachable however valid the keys are. The real fix is to turn that mode off in
the hosting panel; where that panel is out of reach, --cookie-file lets this
script present the clearance cookie your own browser already earned:

    .venv\\Scripts\\python.exe wc_publish.py --check --cookie-file cookie.txt

Keep that file out of the repo - it is a live session credential.
"""

import argparse
import html as _html
import mimetypes
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd
import requests

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


IMAGE_ROOT = "product_images_for_upload"
TIMEOUT = 60
# Bounded retry for transport failures only - see Store.call.
NETWORK_RETRIES = 4
RETRY_BACKOFF = 2.0        # seconds, doubled each attempt

# A store behind a CDN JavaScript challenge (KI-15) answers every server-to-
# server request with an interstitial instead of the API. The clearance cookie a
# real browser earns can be borrowed to get through it - see --cookie below.
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
CHALLENGE_MARKERS = ("checking your browser", "jschallenge", "just a moment",
                     "enable javascript and cookies")

SESSION = requests.Session()
_CACHE_BUST = 0                 # see Store.call: this store caches GETs

# A browser cookie is carried for exactly ONE reason: to satisfy the edge
# challenge. WordPress's own session cookies have to be stripped out of it -
# with wordpress_sec_* present, WordPress authenticates the request by session,
# finds no REST nonce, and answers 401 woocommerce_rest_cannot_view even though
# the consumer key is perfectly valid. Measured on this store: the clearance
# cookie alone -> 200; clearance + wordpress_sec_* -> 401.
WP_SESSION_PREFIXES = ("wordpress_sec", "wordpress_logged_in",
                       "wp_woocommerce_session", "wp-settings")


def strip_wp_session(cookie):
    """Drop WordPress's session cookies, keep everything else (the clearance
    cookie is named differently on every CDN, so it cannot be an allowlist).
    Returns (cookie to send, names dropped)."""
    kept, dropped = [], []
    for part in cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name = part.split("=", 1)[0].strip()
        if name.lower().startswith(WP_SESSION_PREFIXES):
            dropped.append(name)
        else:
            kept.append(part)
    return "; ".join(kept), dropped


def configure_session(cookie="", user_agent=""):
    """Make every request in this run look like the browser the cookie came
    from. Returns (using_cookie, names dropped)."""
    cookie = (cookie or os.environ.get("WC_COOKIE", "")).strip()
    agent = (user_agent or os.environ.get("WC_USER_AGENT", "") or DEFAULT_UA).strip()
    SESSION.headers.update({"User-Agent": agent,
                            "Accept": "application/json, text/plain, */*",
                            "Accept-Language": "en-US,en;q=0.9"})
    dropped = []
    if cookie:
        cookie, dropped = strip_wp_session(cookie)
        # Seed the JAR, not a fixed Cookie header. A CDN refreshes its clearance
        # cookie via Set-Cookie as you browse; a pinned header would discard
        # every refresh and expire mid-run, which is exactly what happened.
        for part in cookie.split(";"):
            if "=" in part:
                name, value = part.split("=", 1)
                SESSION.cookies.set(name.strip(), value.strip())
    return bool(cookie), dropped


def read_cookie_arg(cookie, cookie_file):
    """--cookie-file is preferred: a cookie pasted on the command line ends up
    in shell history, and these grant access to the site."""
    if cookie_file:
        path = Path(cookie_file)
        if not path.exists():
            sys.exit(f"Cookie file not found: {path}")
        return path.read_text(encoding="utf-8").strip()
    return cookie or ""


def looks_like_challenge(response):
    if "html" not in response.headers.get("Content-Type", "").lower():
        return False
    head = response.text[:4000].lower()
    return any(marker in head for marker in CHALLENGE_MARKERS)


CHALLENGE_HELP = """the host served a browser challenge page instead of the API.

  The credentials were never reached, so this is not an auth problem.

  Proper fix - Hostinger hPanel (a separate login from WordPress, at
  hpanel.hostinger.com): Websites > Dashboard > CDN > Manage > Security,
  and move the level off "I'm under attack".

  Stopgap if you cannot reach the hosting panel - borrow your browser's
  clearance cookie:
    1. open the store in Chrome and let the check finish
    2. F12 > Network > click any request to the site > Request Headers
    3. copy the ENTIRE Cookie value into a file, then:
         wc_publish.py ... --cookie-file cookie.txt
    4. copy the User-Agent from the same panel if it differs from the
       default, and pass it with --user-agent

  The cookie is tied to that browser and IP and expires, so re-copy it when
  this message comes back. Copy it from a request whose Status is 200 - copying
  from the challenge response itself yields the pre-clearance token, which looks
  identical and never works."""


class ChallengeError(RuntimeError):
    """The edge blocked us. Distinct from a genuine API error because it means
    NO request will succeed, so the run should stop rather than repeat itself
    once per row."""


def env_or_die(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} is not set. Set WC_URL, WC_KEY and WC_SECRET as "
                 f"environment variables (never put them in the repo), then "
                 f"open a new terminal.")
    return value


class Store:
    def __init__(self, allow_writes=False):
        self.base = env_or_die("WC_URL").rstrip("/")
        self.auth = (env_or_die("WC_KEY"), env_or_die("WC_SECRET"))
        # a dry run must not create categories or tags either - resolving a
        # payload is not permission to write taxonomy terms
        self.allow_writes = allow_writes
        self.would_create = []
        self._cat_cache, self._tag_cache = {}, {}

    # -- plumbing ---------------------------------------------------------
    def call(self, method, path, **kw):
        url = f"{self.base}/wp-json/wc/v3/{path.lstrip('/')}"
        if method == "GET":
            # This store runs LiteSpeed Cache, which served a STALE product
            # response - it reported 0 images on a product that had 8. On an
            # upsert that is worse than cosmetic: a cached "no such SKU" would
            # make us create a duplicate instead of updating. A unique
            # parameter per request defeats the cache; WooCommerce ignores it.
            global _CACHE_BUST
            _CACHE_BUST += 1
            params = dict(kw.get("params") or {})
            params["_cb"] = f"{os.getpid()}-{_CACHE_BUST}"
            kw["params"] = params
            kw.setdefault("headers", {})
            kw["headers"] = {**kw["headers"], "Cache-Control": "no-cache",
                             "Pragma": "no-cache"}
        # This host drops connections intermittently - measured: 3 failures in
        # a 1,279-product write, and one that killed a whole checkpoint capture
        # partway through. A dropped socket is not a reason to lose a batch, so
        # transport errors get a bounded retry. HTTP errors do NOT: a 401 or a
        # 404 means the same thing however many times it is asked.
        #
        # Safe for the writes here as well as the reads: every PUT this
        # pipeline sends is an upsert keyed on id or SKU, so re-sending one
        # whose reply was lost re-applies identical values.
        last = None
        for attempt in range(NETWORK_RETRIES):
            try:
                r = SESSION.request(method, url, auth=self.auth,
                                    timeout=TIMEOUT, **kw)
                break
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError) as exc:
                last = exc
                if attempt == NETWORK_RETRIES - 1:
                    raise RuntimeError(
                        f"{method} {path} -> network failed after "
                        f"{NETWORK_RETRIES} attempts: {exc}")
                delay = RETRY_BACKOFF * (2 ** attempt)
                print(f"    (network hiccup on {method} {path}, retrying in "
                      f"{delay:.0f}s - attempt {attempt + 2}/{NETWORK_RETRIES})")
                time.sleep(delay)
        if looks_like_challenge(r):
            raise ChallengeError(f"{method} {path} -> {CHALLENGE_HELP}")
        if r.status_code >= 400:
            detail = ""
            try:
                body = r.json()
                detail = f" - {body.get('code', '')}: {body.get('message', '')}"
            except Exception:
                detail = f" - {r.text[:200]}"
            raise RuntimeError(f"{method} {path} -> HTTP {r.status_code}{detail}")
        return r.json() if r.content else {}

    def check(self):
        """Confirm the key works and report what it can see."""
        products = self.call("GET", "products", params={"per_page": 1})
        systems = self.call("GET", "system_status")
        env = systems.get("environment", {})
        print(f"Connected to {self.base}")
        print(f"  WooCommerce {env.get('version', '?')} on WordPress "
              f"{env.get('wp_version', '?')}")
        print(f"  currency: {systems.get('settings', {}).get('currency', '?')}")
        print(f"  products visible to this key: "
              f"{'yes' if isinstance(products, list) else 'no'}")
        return True

    # -- taxonomy ---------------------------------------------------------
    def _term(self, endpoint, name, cache, parent=0):
        key = (name.lower(), parent)
        if key in cache:
            return cache[key]
        params = {"search": name, "per_page": 100}
        if endpoint == "products/categories":
            params["parent"] = parent
        for hit in self.call("GET", endpoint, params=params):
            if hit["name"].strip().lower() == name.strip().lower():
                cache[key] = hit["id"]
                return hit["id"]
        label = "category" if endpoint.endswith("categories") else "tag"
        if not self.allow_writes:
            if (label, name) not in self.would_create:
                self.would_create.append((label, name))
                print(f"    ~ would create {label}: {name}")
            return None
        payload = {"name": name}
        if endpoint == "products/categories" and parent:
            payload["parent"] = parent
        created = self.call("POST", endpoint, json=payload)
        cache[key] = created["id"]
        print(f"    + created {label}: {name}")
        return created["id"]

    def category_id(self, hierarchy):
        """'Home Appliances > Washing Machines' -> id of the leaf, creating any
        level that does not exist yet. None on a dry run when it is missing."""
        parent = 0
        leaf = None
        for level in [p.strip() for p in str(hierarchy).split(">") if p.strip()]:
            leaf = self._term("products/categories", level, self._cat_cache, parent)
            if leaf is None:              # dry run: cannot resolve deeper
                return None
            parent = leaf
        return leaf

    def tag_ids(self, names):
        found = [self._term("products/tags", n, self._tag_cache) for n in names]
        return [i for i in found if i is not None]

    # -- products ---------------------------------------------------------
    def by_sku(self, sku):
        found = self.call("GET", "products", params={"sku": sku})
        return found[0] if found else None


def compact(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def price_str(value):
    text = compact(value)
    if not text or text.lower() in ("nan", "none"):
        return ""
    try:
        number = float(text)
    except ValueError:
        return ""
    return str(int(number)) if number.is_integer() else f"{number:.2f}"


_MEDIA_BY_NAME = {}        # filename -> media id, so one run uploads each once


def find_media(path):
    """The id of an attachment ALREADY holding this exact filename, or None.

    Without this, every `--commit` re-uploaded every image: WordPress does not
    overwrite, it mints `name-1.webp`, `name-2.webp`, ... So the documented
    workflow - "re-running a corrected batch updates the same products" - grew
    the Media Library by a full copy of the catalogue each time.

    Reuse is gated on the byte size matching, so a genuinely CHANGED image
    still uploads rather than silently pointing at the stale one. Servers that
    do not report filesize (pre-WP 6.0) fall back to reuse, which is the
    common case of an unchanged re-push."""
    if path.name in _MEDIA_BY_NAME:
        return _MEDIA_BY_NAME[path.name]
    user = os.environ.get("WP_APP_USER", "").strip()
    password = os.environ.get("WP_APP_PASSWORD", "").strip()
    if not (user and password):
        return None
    base = os.environ["WC_URL"].rstrip("/") + "/wp-json/wp/v2/media"
    try:
        r = SESSION.get(base, auth=(user, password), timeout=TIMEOUT,
                        params={"search": path.stem, "per_page": 100,
                                "_fields": "id,source_url,media_details"})
        if looks_like_challenge(r):
            raise ChallengeError(f"media lookup {path.name} -> {CHALLENGE_HELP}")
        if r.status_code >= 400:
            return None            # cannot tell -> fall through and upload
        local_size = path.stat().st_size
        for item in r.json():
            details = item.get("media_details") or {}
            names = {os.path.basename(str(item.get("source_url", ""))),
                     os.path.basename(str(details.get("file", "")))}
            if path.name not in names:
                continue
            remote_size = details.get("filesize")
            if remote_size is not None and int(remote_size) != local_size:
                return None        # same name, different bytes: upload afresh
            _MEDIA_BY_NAME[path.name] = item["id"]
            return item["id"]
    except ChallengeError:
        raise
    except Exception:
        return None                # a lookup failure must not block the push
    return None


def upload_image(path, alt):
    """Send a local file to the Media Library. Needs a WordPress Application
    Password - WooCommerce consumer keys do not authenticate /wp/v2 routes."""
    user = os.environ.get("WP_APP_USER", "").strip()
    password = os.environ.get("WP_APP_PASSWORD", "").strip()
    if not (user and password):
        return None
    existing = find_media(path)
    if existing is not None:
        return existing
    url = os.environ["WC_URL"].rstrip("/") + "/wp-json/wp/v2/media"
    mime = mimetypes.guess_type(path.name)[0] or "image/webp"
    r = SESSION.post(
        url, auth=(user, password), timeout=TIMEOUT,
        headers={"Content-Disposition": f'attachment; filename="{path.name}"',
                 "Content-Type": mime},
        data=path.read_bytes())
    if looks_like_challenge(r):
        raise ChallengeError(f"media upload {path.name} -> {CHALLENGE_HELP}")
    if r.status_code >= 400:
        raise RuntimeError(f"media upload {path.name} -> HTTP {r.status_code} "
                           f"- {r.text[:200]}")
    media_id = r.json()["id"]
    _MEDIA_BY_NAME[path.name] = media_id
    SESSION.post(f"{url}/{media_id}", auth=(user, password), timeout=TIMEOUT,
                 json={"alt_text": alt, "title": alt})
    return media_id


def images_for(row, sku, title, mode, allow_writes=True):
    """mode: url = trust the Images column, upload = send the local .webp
    files, skip = no images.

    allow_writes=False is a dry run: uploading to the Media Library is a WRITE,
    so it must not happen just because a payload is being assembled."""
    urls = split_image_list(compact(row.get("Images")))
    if mode == "skip" or not urls:
        return [], []
    if mode == "url":
        return [{"src": u.strip(), "alt": title} for u in urls], []

    payload, notes = [], []
    root = Path(os.getcwd()) / IMAGE_ROOT
    # Take the filenames from the URLs, NOT from the position in the list.
    # app.py names each file after the index of the SOURCE url and only writes
    # the ones that survived download and the MIN_IMAGE_EDGE floor, so the
    # numbering is sparse: a row whose Images column holds _2, _3, _5 was being
    # asked for _1, _2, _3. That reported _1 as missing, silently dropped _5,
    # and - because nothing prunes product_images_for_upload\ between runs -
    # could upload a PREVIOUS batch's leftover _1.webp as the primary image.
    ready = [root / os.path.basename(urlsplit(u).path) for u in urls]
    missing = [p.name for p in ready if not p.exists()]
    if missing:
        notes.append(f"missing local file(s): {', '.join(missing)}")
    ready = [p for p in ready if p.exists()]

    if not allow_writes:
        notes.append(f"{len(ready)} local .webp file(s) ready - NOT uploaded "
                     f"(dry run); they upload on --commit")
        return [], notes

    if not (os.environ.get("WP_APP_USER", "").strip()
            and os.environ.get("WP_APP_PASSWORD", "").strip()):
        notes.append("WP_APP_USER / WP_APP_PASSWORD not set - cannot upload")
        return [], notes
    # A gallery is N independent POSTs and the store's round-trip latency, not
    # bandwidth, dominates - measured ~1 min for 32 images done one at a time.
    # map() preserves order, so position 1 stays the featured image; 4 workers
    # keeps the burst polite. Filenames within one product are distinct
    # (SKU_n.webp), so the _MEDIA_BY_NAME dedupe cache cannot race against
    # itself here.
    with ThreadPoolExecutor(max_workers=min(4, len(ready))) as pool:
        ids = list(pool.map(lambda p: upload_image(p, title), ready))
    payload = [{"id": mid, "alt": title} for mid in ids if mid is not None]
    return payload, notes


def attributes_for(row):
    attrs, index = [], 1
    while f"Attribute {index} name" in row:
        name = compact(row.get(f"Attribute {index} name"))
        values = compact(row.get(f"Attribute {index} value(s)"))
        if name and values:
            attrs.append({
                "name": name,
                # a custom (per-product) attribute: passing no id keeps this
                # from depending on global attribute taxonomies existing
                "options": [v.strip() for v in values.split(",") if v.strip()],
                "visible": compact(row.get(f"Attribute {index} visible")) != "0",
                "variation": False,
            })
        index += 1
    return attrs


def meta_from_columns(row):
    """Every `Meta: <key>` column becomes one meta_data entry.

    `Meta: ` is the WooCommerce CSV importer's own convention for "write this
    as post meta", so honouring it here means the two publishing routes carry
    identical data. It is also why the SEO pass needs no plugin-specific code:
    `Meta: _yoast_wpseo_title` and `Meta: rank_math_title` are just columns.
    """
    out = []
    for column in row.index if hasattr(row, "index") else row.keys():
        name = str(column)
        if not name.startswith("Meta: "):
            continue
        key, value = name[len("Meta: "):].strip(), compact(row.get(column))
        if key and value:
            out.append({"key": key, "value": value})
    return out


def build_payload(store, row, status, image_mode):
    sku = compact(row.get("sku"))
    title = compact(row.get("title"))
    regular, sale = price_str(row.get("regular price")), price_str(row.get("sale price"))
    # WooCommerce: regular_price is the struck-through price, sale_price the
    # one charged. With no MRP the selling price IS the regular price.
    if not regular:
        regular, sale = sale, ""

    tags = [t.strip() for t in compact(row.get("Tags")).split(",") if t.strip()]
    image_payload, notes = images_for(row, sku, title, image_mode,
                                      store.allow_writes)
    # None on a dry run when the term does not exist yet - never send it
    category = store.category_id(compact(row.get("categories")))
    categories = [{"id": category}] if category is not None else []

    payload = {
        "name": title,
        "type": "simple",
        "sku": sku,
        "status": status,
        "catalog_visibility": "visible",
        "description": compact(row.get("description")),
        "short_description": compact(row.get("short description")),
        "regular_price": regular,
        "sale_price": sale,
        "categories": categories,
        "tags": [{"id": i} for i in store.tag_ids(tags)],
        "attributes": attributes_for(row),
        "images": image_payload,
        "meta_data": [
            {"key": "_cross_reference_tag", "value": compact(row.get("cross reference tag"))},
            {"key": "_configuration", "value": compact(row.get("configuration"))},
            {"key": "_pipeline_source", "value": "Final_pipe/wc_publish.py"},
        ],
    }
    if compact(row.get("Brands")):
        payload["meta_data"].append({"key": "_brand", "value": compact(row.get("Brands"))})

    # SEO meta and slug, present only when the CSV was built with --seo. The
    # slug is the one field the CSV importer cannot carry (it has no slug
    # column), so this route is the only one that can set it.
    payload["meta_data"].extend(meta_from_columns(row))
    if compact(row.get("slug")):
        payload["slug"] = compact(row.get("slug"))
    return payload, notes


def describe(payload, existing):
    seo = [m for m in payload["meta_data"]
           if "yoast" in m["key"] or "rank_math" in m["key"]]
    bits = [f"name={payload['name'][:60]}",
            f"price={payload['regular_price'] or '-'}"
            + (f"/{payload['sale_price']}" if payload["sale_price"] else ""),
            f"tags={len(payload['tags'])}",
            f"attrs={len(payload['attributes'])}",
            f"images={len(payload['images'])}"]
    if seo:
        bits.append(f"seo={len(seo)}")
    if payload.get("slug"):
        bits.append(f"slug={payload['slug'][:40]}")
    if existing:
        old = price_str(existing.get("regular_price"))
        changes = []
        # WordPress returns the name HTML-escaped ('Haier 1.5 Ton 3 Star
        # Inverter Split AC &#8211; White'), so a raw != comparison reported
        # "changes: name" on essentially every update whether or not anything
        # had actually changed, which made the dry run's diff useless.
        if compact(_html.unescape(str(existing.get("name", "")))) != payload["name"]:
            changes.append("name")
        if old != payload["regular_price"]:
            changes.append(f"regular_price {old or '-'} -> {payload['regular_price'] or '-'}")
        bits.append("changes: " + (", ".join(changes) if changes else "none detected"))
    return "  ".join(bits)



def use_utf8_stdout():
    """A Windows console is cp1252, and scraped titles/SKUs are not. Without
    this a single unmappable character aborts the run with a UnicodeEncodeError
    traceback - mid-batch, after work has already been done. sku_enricher.py
    has always done this; the rest of the entry points had not."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

def main():
    use_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="Upsert products into WooCommerce from the generated CSV. "
                    "Dry run unless --commit is given.")
    ap.add_argument("csv", nargs="?", help="wordpress_ready_clean_repaired.csv")
    ap.add_argument("--commit", action="store_true",
                    help="Actually write. Without this nothing is sent.")
    ap.add_argument("--check", action="store_true",
                    help="Verify the credentials and exit")
    ap.add_argument("--sku", action="append", default=[], metavar="SKU",
                    help="Only this SKU (repeatable) - use for a single-product test")
    ap.add_argument("--limit", type=int, default=0, help="At most N products")
    ap.add_argument("--status", default=None, choices=["draft", "publish", "private"],
                    help="Status to set. New products default to draft. Passing "
                         "this explicitly ALSO applies it to products that "
                         "already exist, which is how a draft goes live.")
    ap.add_argument("--images", default="auto",
                    choices=["auto", "upload", "url", "skip"],
                    help="auto = upload local .webp when WP_APP_* is set, "
                         "else trust the Images column")
    ap.add_argument("--delete-sku", metavar="SKU",
                    help="Permanently delete the product with this SKU "
                         "(needs --commit). For cleaning up test products.")
    ap.add_argument("--cookie-file", metavar="PATH",
                    help="File holding your browser's Cookie header, for a "
                         "store behind a CDN JS challenge. Preferred over "
                         "--cookie: nothing lands in shell history.")
    ap.add_argument("--cookie", metavar="VALUE",
                    help="The Cookie header value itself (Chrome F12 > Network "
                         "> any request > Request Headers > Cookie)")
    ap.add_argument("--user-agent", metavar="UA",
                    help="User-Agent to send. A clearance cookie is bound to the "
                         "browser that earned it, so this must match the browser "
                         "you copied the cookie from.")
    args = ap.parse_args()

    using_cookie, dropped = configure_session(
        read_cookie_arg(args.cookie, args.cookie_file), args.user_agent or "")
    if using_cookie:
        print("Using a borrowed browser cookie for the edge challenge.")
        if dropped:
            # keeping these would make WordPress authenticate by session and
            # then reject the consumer key for want of a REST nonce
            print(f"  Dropped {len(dropped)} WordPress session cookie(s) so the "
                  f"API key is what authenticates: {', '.join(dropped)}")
    store = Store(allow_writes=args.commit)

    if args.check:
        store.check()
        have_media = bool(os.environ.get("WP_APP_USER") and os.environ.get("WP_APP_PASSWORD"))
        print(f"  media upload credentials: {'present' if have_media else 'NOT set'}"
              f"{'' if have_media else ' (set WP_APP_USER / WP_APP_PASSWORD to upload images)'}")
        return

    if args.delete_sku:
        found = store.by_sku(args.delete_sku)
        if not found:
            print(f"No product with SKU {args.delete_sku}")
            return
        print(f"Product {found['id']} '{found['name']}' (SKU {args.delete_sku})")
        if not args.commit:
            print("DRY RUN - would permanently delete it. Re-run with --commit.")
            return
        store.call("DELETE", f"products/{found['id']}", params={"force": "true"})
        print(f"Deleted product {found['id']}.")
        return

    if not args.csv:
        sys.exit("Give the CSV to publish, or use --check / --delete-sku.")
    path = Path(args.csv)
    if not path.exists():
        sys.exit(f"File not found: {path}")

    df = pd.read_csv(path, dtype=str).fillna("")
    # every other input problem in this file exits with a sentence; a CSV
    # without a sku column used to exit with a bare KeyError traceback
    if "sku" not in df.columns:
        sys.exit(f"{path.name} has no 'sku' column, so nothing can be keyed on "
                 f"it.\nColumns found: {', '.join(map(str, df.columns[:12]))}"
                 f"{' ...' if len(df.columns) > 12 else ''}\n"
                 f"Generate the file with build_csv.py, which always writes one.")
    if args.sku:
        wanted = {s.strip().upper() for s in args.sku}
        df = df[df["sku"].str.strip().str.upper().isin(wanted)]
        if df.empty:
            sys.exit(f"No row in {path.name} has SKU {', '.join(args.sku)}")
    if args.limit:
        df = df.head(args.limit)

    image_mode = args.images
    if image_mode == "auto":
        image_mode = ("upload"
                      if os.environ.get("WP_APP_USER") and os.environ.get("WP_APP_PASSWORD")
                      else "url")

    print(f"{'PUBLISHING' if args.commit else 'DRY RUN'}: {len(df)} product(s) "
          f"from {path.name} -> {store.base}  (images: {image_mode}, "
          f"status: {args.status or 'draft for new, unchanged for existing'})\n")

    created = updated = failed = 0
    for _, row in df.iterrows():
        sku = compact(row.get("sku"))
        if not sku:
            print("  [skip] row with no SKU")
            continue
        try:
            existing = store.by_sku(sku)
            payload, notes = build_payload(store, row, args.status or "draft",
                                           image_mode)
            verb = "UPDATE" if existing else "CREATE"
            print(f"  {verb} {sku}")
            print(f"    {describe(payload, existing)}")
            for note in notes:
                print(f"    [warn] {note}")
            if not args.commit:
                continue
            if existing:
                # An empty images array is DESTRUCTIVE on an update: WooCommerce
                # reads it as "remove every image". So --images skip, or an
                # upload that produced nothing, would strip the gallery off a
                # live product. Omitting the key leaves the images alone.
                # (Cost one live product its gallery before this existed.)
                if not payload.get("images"):
                    payload.pop("images", None)
                # Never CHANGE the status of a product that already exists
                # unless it was asked for. Without this a routine re-push of a
                # corrected batch would quietly pull every live product back to
                # draft. An explicit --status is honoured, which is how a
                # reviewed draft is taken live.
                if args.status is None:
                    payload.pop("status", None)
                result = store.call("PUT", f"products/{existing['id']}", json=payload)
                updated += 1
            else:
                result = store.call("POST", "products", json=payload)
                created += 1
            print(f"    -> id {result['id']}  {result.get('permalink', '')}")
        except ChallengeError as exc:
            # the edge is refusing everything; the remaining rows would print
            # the same wall of text and fail identically
            print(f"    !! {exc}")
            print(f"\nStopped at {sku}: the CDN is blocking this machine, so no "
                  f"row can succeed. Nothing further was attempted.")
            failed += 1
            break
        except Exception as exc:
            failed += 1
            print(f"    !! {exc}")

    if args.commit:
        print(f"\nDone: {created} created, {updated} updated, {failed} failed.")
    else:
        print(f"\nDry run complete - nothing was written. "
              f"Re-run with --commit to apply.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        # connectivity/challenge failures are the user's problem to fix, not a
        # bug to read a traceback about
        sys.exit(str(exc))
