"""blog_publish.py - push a finished post to WordPress.

Posts live at /wp-json/wp/v2/posts, which is WordPress core rather than
WooCommerce - so this authenticates with the WordPress Application Password
(WP_APP_USER / WP_APP_PASSWORD), not the WooCommerce consumer key.

The client's WordPress credentials (clients/<slug>/.env) are applied lazily by
ensure_client_env() on the first credential read - never at import - so that
importing this module has no effect on the process environment. See that
function for the pytest / APPLY_CLIENT_ENV escape hatches.

Session handling, the borrowed-cookie logic and the CDN-challenge detection are
imported from the parent `wc_publish.py` rather than reimplemented. That code
cost real debugging (KI-15, and the WordPress-session cookie that reads exactly
like a bad key) and a second copy would mean a second set of the same bugs.
"""

import mimetypes
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

PARENT = Path(__file__).resolve().parent.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

try:
    import wc_publish as W
except ImportError as exc:                          # pragma: no cover
    raise SystemExit(
        f"blog_publish.py needs wc_publish.py from the parent folder "
        f"({PARENT}); it could not be imported ({exc}).")

TIMEOUT = 60

# The client's own WP credentials (clients/<slug>/.env) - PRODUCT_PLAN Phase 1
# item 3, applied LAZILY. See ensure_client_env().
CLIENT = None                  # resolved on first credential use
APPLY_CLIENT_ENV = True        # set False to keep this module out of os.environ
_ENV_APPLIED = False


def ensure_client_env():
    """Apply clients/<slug>/.env to os.environ once, on first credential use.

    Deliberately NOT done at import time: importing this module must not
    rewrite the process environment as a side effect, which under pytest would
    change WC_URL / WP_APP_* for every later test in the session. The apply is
    therefore skipped entirely when $PYTEST_CURRENT_TEST is set or when the
    module flag APPLY_CLIENT_ENV is False; CLIENT is still resolved so callers
    can see which client was picked.

    A blog generator must never inherit whichever store the last shell export
    named. Missing config or a missing .env is not an error: the environment as
    it stands is then simply left alone.
    """
    global CLIENT, _ENV_APPLIED
    if _ENV_APPLIED:
        return CLIENT
    _ENV_APPLIED = True
    try:
        import client_config as _client_config
        CLIENT = _client_config.load_client()
        if APPLY_CLIENT_ENV and not os.environ.get("PYTEST_CURRENT_TEST"):
            _client_config.apply_env(CLIENT)
    except Exception:                               # pragma: no cover
        CLIENT = None
    return CLIENT


def have_creds():
    ensure_client_env()
    return bool(os.environ.get("WP_APP_USER", "").strip()
                and os.environ.get("WP_APP_PASSWORD", "").strip()
                and os.environ.get("WC_URL", "").strip())


def wp_auth():
    ensure_client_env()
    user = os.environ.get("WP_APP_USER", "").strip()
    password = os.environ.get("WP_APP_PASSWORD", "").strip()
    if not (user and password):
        raise RuntimeError(
            "WP_APP_USER / WP_APP_PASSWORD are not set. A WordPress "
            "Application Password is required to create posts (WP Users > "
            "Profile > Application Passwords). WooCommerce consumer keys do "
            "not authenticate /wp/v2 routes.")
    return (user, password)


def base_url():
    ensure_client_env()
    url = os.environ.get("WC_URL", "").strip()
    if not url:
        raise RuntimeError("WC_URL is not set.")
    return url.rstrip("/")


def call(method, path, **kw):
    url = f"{base_url()}/wp-json/wp/v2/{path.lstrip('/')}"
    response = W.SESSION.request(method, url, auth=wp_auth(), timeout=TIMEOUT, **kw)
    if W.looks_like_challenge(response):
        raise W.ChallengeError(f"{method} {path} -> {W.CHALLENGE_HELP}")
    if response.status_code >= 400:
        detail = ""
        try:
            body = response.json()
            detail = f" - {body.get('code', '')}: {body.get('message', '')}"
        except Exception:
            detail = f" - {response.text[:200]}"
        raise RuntimeError(f"{method} {path} -> HTTP {response.status_code}{detail}")
    return response.json() if response.content else {}


def find_by_slug(slug):
    """An existing post with this slug, so a re-run updates rather than
    creating a second copy of the same article."""
    if not slug:
        return None
    found = call("GET", "posts", params={"slug": slug, "status": "any",
                                         "per_page": 1})
    return found[0] if isinstance(found, list) and found else None


def term_id(taxonomy, name, allow_writes):
    """Term id, created if missing. On a dry run nothing is created -
    resolving a payload is not permission to write taxonomy terms (the same
    rule wc_publish.py learned the hard way)."""
    if not name:
        return None
    for hit in call("GET", taxonomy, params={"search": name, "per_page": 100}):
        if hit["name"].strip().lower() == name.strip().lower():
            return hit["id"]
    if not allow_writes:
        print(f"    ~ would create {taxonomy[:-1]}: {name}")
        return None
    created = call("POST", taxonomy, json={"name": name})
    print(f"    + created {taxonomy[:-1]}: {name}")
    return created["id"]


def category_id(name, allow_writes):
    return term_id("categories", name, allow_writes)


def tag_ids(names, allow_writes):
    ids = []
    for name in names or ():
        resolved = term_id("tags", name, allow_writes)
        if resolved is not None:
            ids.append(resolved)
    return ids


def upload_featured(image_url, commit):
    """Sideload the post's lead image into the media library and return its id.

    Hotlinking the store's uploads folder works only once the WooCommerce
    import has actually put the file there; a post owning its own copy does
    not depend on that ordering."""
    if not image_url:
        return None
    name = Path(urlparse(image_url).path).name or "blog-image.webp"
    if not commit:
        print(f"    ~ would upload featured image: {name}")
        return None
    fetched = W.SESSION.get(image_url, timeout=TIMEOUT)
    if fetched.status_code >= 400 or not fetched.content:
        print(f"    [warn] featured image not fetchable ({fetched.status_code}) "
              f"- posting without one: {image_url}")
        return None
    mime = fetched.headers.get("Content-Type") or \
        mimetypes.guess_type(name)[0] or "image/webp"
    media = call("POST", "media", data=fetched.content, headers={
        "Content-Type": mime.split(";")[0].strip(),
        "Content-Disposition": f'attachment; filename="{name}"',
    })
    print(f"    + uploaded featured image: {name} -> media {media['id']}")
    return media["id"]


def publish(payload, slug="", category="", commit=False, status="draft",
            last_seen_modified=None, force_update=False, featured_image="",
            explicit_status=False):
    """Create or update the post. Returns the post dict, or None on a dry run.

    NOTHING is written without commit=True.

    `explicit_status` says the caller ASKED for this status rather than falling
    back to the default. Without it the status was dropped on every update, so
    a post that already existed could never be taken live - `--status publish`
    silently did nothing, even though blog_run.py advertises it. Same rule
    wc_publish.py settled on: never change a live post's status by accident,
    always honour an explicit request."""
    if not commit and not have_creds():
        # a dry run is a preview; it should not need credentials just to tell
        # you what it would send
        print("  DRY RUN (offline): WP_APP_USER / WP_APP_PASSWORD / WC_URL are "
              "not all set, so nothing was looked up on the site.")
        print(f"  WOULD CREATE OR UPDATE: {payload['title'][:70]}")
        print(f"    slug={slug or '(auto)'}  status={status}  "
              f"category={category or '(none)'}  "
              f"tags={', '.join(payload.get('tags') or []) or '(none)'}")
        print(f"    {len(payload['content'])} chars of HTML")
        return None

    existing = find_by_slug(slug)
    verb = "UPDATE" if existing else "CREATE"
    print(f"  {verb} post: {payload['title'][:70]}")
    print(f"    slug={slug or '(auto)'}  status={status}  "
          f"{len(payload['content'])} chars of HTML")

    if existing and last_seen_modified and not force_update:
        current = existing.get("modified_gmt") or ""
        if current and current != last_seen_modified:
            raise RuntimeError(
                f"Post {existing['id']} was edited on the site "
                f"({current}) after this tool last wrote it "
                f"({last_seen_modified}). An update overwrites the title, body "
                f"and excerpt wholesale, so those edits would be lost. "
                f"Re-run with --force-update to overwrite anyway.")

    body = {k: v for k, v in payload.items() if k != "tags"}
    body["status"] = status
    if slug:
        body["slug"] = slug
    cat = category_id(category, commit)
    if cat is not None:
        body["categories"] = [cat]
    tags = tag_ids(payload.get("tags"), commit)
    if tags:
        body["tags"] = tags
    media = upload_featured(featured_image, commit)
    if media is not None:
        body["featured_media"] = media

    if not commit:
        print("    DRY RUN - nothing was sent. Re-run with --commit to apply.")
        return None

    if existing:
        # never silently flip a published post back to draft on a re-run -
        # but an explicit --status is how a reviewed draft is taken live
        if not explicit_status:
            body.pop("status", None)
        result = call("POST", f"posts/{existing['id']}", json=body)
    else:
        result = call("POST", "posts", json=body)
    print(f"    -> id {result['id']}  {result.get('link', '')}")
    return result


def configure(cookie_file="", cookie="", user_agent=""):
    """Borrow the browser cookie for the CDN challenge, exactly as
    wc_publish.py does. Also the CLI's cue to apply the client's .env, so a
    real run has the credentials before the first call rather than after."""
    ensure_client_env()
    using, dropped = W.configure_session(
        W.read_cookie_arg(cookie, cookie_file), user_agent or "")
    if using:
        print("Using a borrowed browser cookie for the edge challenge.")
        if dropped:
            print(f"  Dropped {len(dropped)} WordPress session cookie(s).")
    return using
