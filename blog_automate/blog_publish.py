"""blog_publish.py - push a finished post to WordPress.

Posts live at /wp-json/wp/v2/posts, which is WordPress core rather than
WooCommerce - so this authenticates with the WordPress Application Password
(WP_APP_USER / WP_APP_PASSWORD), not the WooCommerce consumer key.

Session handling, the borrowed-cookie logic and the CDN-challenge detection are
imported from the parent `wc_publish.py` rather than reimplemented. That code
cost real debugging (KI-15, and the WordPress-session cookie that reads exactly
like a bad key) and a second copy would mean a second set of the same bugs.
"""

import os
import sys
from pathlib import Path

import requests

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


def wp_auth():
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


def category_id(name, allow_writes):
    """Post category id, created if missing. On a dry run nothing is created -
    resolving a payload is not permission to write taxonomy terms (the same
    rule wc_publish.py learned the hard way)."""
    if not name:
        return None
    for hit in call("GET", "categories", params={"search": name, "per_page": 100}):
        if hit["name"].strip().lower() == name.strip().lower():
            return hit["id"]
    if not allow_writes:
        print(f"    ~ would create post category: {name}")
        return None
    created = call("POST", "categories", json={"name": name})
    print(f"    + created post category: {name}")
    return created["id"]


def publish(payload, slug="", category="", commit=False, status="draft",
            explicit_status=False):
    """Create or update the post. Returns the post dict, or None on a dry run.

    NOTHING is written without commit=True.

    `explicit_status` says the caller ASKED for this status rather than falling
    back to the default. Without it the status was dropped on every update, so
    a post that already existed could never be taken live - `--status publish`
    silently did nothing, even though blog_run.py advertises it. Same rule
    wc_publish.py settled on: never change a live post's status by accident,
    always honour an explicit request."""
    existing = find_by_slug(slug)
    verb = "UPDATE" if existing else "CREATE"
    print(f"  {verb} post: {payload['title'][:70]}")
    print(f"    slug={slug or '(auto)'}  status={status}  "
          f"{len(payload['content'])} chars of HTML")

    body = dict(payload)
    body["status"] = status
    if slug:
        body["slug"] = slug
    cat = category_id(category, commit)
    if cat is not None:
        body["categories"] = [cat]

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
    wc_publish.py does."""
    using, dropped = W.configure_session(
        W.read_cookie_arg(cookie, cookie_file), user_agent or "")
    if using:
        print("Using a borrowed browser cookie for the edge challenge.")
        if dropped:
            print(f"  Dropped {len(dropped)} WordPress session cookie(s).")
    return using
