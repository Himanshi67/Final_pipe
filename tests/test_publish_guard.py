r"""The publish blast-radius guard - PRODUCT_PLAN Phase 1, item 3.

Pins the contract in docs\CLIENT_CONFIG.md "Item 3": a publish refuses with
EXIT_REFUSED (4) and writes NOTHING when it is pointed at the wrong store, when
one command would write more rows than --max-commit, when a go-live or a delete
is not signed off in review_signoff.csv, or when the pre-publish snapshot could
not be taken. A plain dry run only warns about a store mismatch, because a dry
run cannot write - and because this suite's own dry runs go to store.invalid.

Everything here is OFFLINE: wc_publish.Store is replaced by a fake that records
calls, so no test can reach a real store. Nothing is ever run with --commit
against a live host.

    .venv\Scripts\python.exe tests\test_publish_guard.py
    .venv\Scripts\python.exe -m pytest tests/test_publish_guard.py -q
"""
import contextlib
import dataclasses
import io
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

import client_config as C                            # noqa: E402
import wc_checkpoint as CP                           # noqa: E402
import wc_publish as W                               # noqa: E402

CFG = C.load_client("shreejee")
OUR_HOST = CFG.store_url                     # the only URL that is NOT a mismatch
OTHER_HOST = "https://store.invalid"         # matches no client, resolves nowhere


# --------------------------------------------------------------- fake store
class FakeStore:
    """Every method wc_publish reaches for, and a log of what it was asked to
    do. `events` is shared with the fake checkpoint so ordering is testable."""

    def __init__(self, allow_writes=False, events=None, check=None):
        self.base = os.environ.get("WC_URL", OTHER_HOST).rstrip("/")
        self.allow_writes = allow_writes
        self.would_create = []
        self.events = events if events is not None else []
        self._check = check

    def by_sku(self, sku):
        return None                      # nothing exists yet -> every row CREATEs

    def category_id(self, hierarchy):
        return None

    def tag_ids(self, names):
        return []

    def call(self, method, path, **kw):
        self.events.append((method, path))
        return {"id": 100 + len(self.events), "permalink": ""}

    def check(self):
        if self._check is not None:
            return self._check()
        print("    (fake) store answered")
        return True

    @property
    def writes(self):
        return [e for e in self.events if e[0] in ("POST", "PUT", "DELETE")]


@pytest.fixture
def guard(monkeypatch, tmp_path):
    """A wc_publish wired to a fake store, a fake checkpoint and a clean env.

    Returns a small object: .events (ordered log), .store (the last FakeStore),
    .run(*argv) -> (exit code, stdout), .csv(n) -> a path to n publishable rows.
    """
    events = []
    state = {"store": None, "check": None, "checkpoint": lambda *a, **k: None}

    monkeypatch.setenv("WC_URL", OUR_HOST)
    monkeypatch.setenv("WC_KEY", "ck_test")
    monkeypatch.setenv("WC_SECRET", "cs_test")
    monkeypatch.setenv("WP_APP_USER", "u")
    monkeypatch.setenv("WP_APP_PASSWORD", "p")
    monkeypatch.delenv("PIPELINE_CLIENT", raising=False)

    def make_store(allow_writes=False):
        state["store"] = FakeStore(allow_writes, events, state["check"])
        return state["store"]

    def fake_create(store, label="", client=None):
        events.append(("CHECKPOINT", label))
        state["checkpoint"](store, label, client)
        return f"20260902-000000-{label}"

    monkeypatch.setattr(W, "Store", make_store)
    monkeypatch.setattr(CP, "create", fake_create)

    class Guard:
        events = None

        def run(self, *argv):
            old = sys.argv
            sys.argv = ["wc_publish.py", *argv]
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    code = W.main() or W.EXIT_OK
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (
                    W.EXIT_OK if exc.code is None else 1)
            finally:
                sys.argv = old
            return code, buf.getvalue()

        def csv(self, rows, name="out.csv"):
            path = tmp_path / name
            lines = ["sku,title,regular price,sale price,categories,description,"
                     "short description,Images,Tags"]
            for i in range(rows):
                lines.append(f"WM-{i:03d},Washer {i},9999,,,A washer,,,")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return str(path)

        def signoff(self, skus, folder=None):
            path = Path(folder or tmp_path) / W.SIGNOFF_NAME
            body = ["sku,reviewer,signed_at"]
            body += [f"{s},dhruv,2026-09-02T10:00:00" for s in skus]
            path.write_text("\n".join(body) + "\n", encoding="utf-8")
            return path

    g = Guard()
    g.events, g.state, g.tmp = events, state, tmp_path
    saved_client = W.CLIENT
    yield g
    W.CLIENT = saved_client


# ----------------------------------------------------- 1. store-URL mismatch
def test_store_mismatch_names_both_hosts(monkeypatch):
    """A synthetic client whose store_url is somewhere else must not pass."""
    monkeypatch.setenv("WC_URL", OUR_HOST)
    assert W.store_mismatch(CFG) is None
    # www., trailing slash and case are not differences
    monkeypatch.setenv("WC_URL", OUR_HOST.replace("https://", "https://WWW.") + "/")
    assert W.store_mismatch(CFG) is None

    elsewhere = dataclasses.replace(CFG, store_url="https://someone-else.example")
    monkeypatch.setenv("WC_URL", OUR_HOST)
    message = W.store_mismatch(elsewhere)
    assert message, "a different store must be reported as a mismatch"
    assert C.store_host(OUR_HOST) in message
    assert "someone-else.example" in message

    monkeypatch.setenv("WC_URL", "")
    assert W.store_mismatch(CFG) is None, "empty WC_URL is env_or_die's message"


def test_dry_run_with_a_mismatched_url_warns_but_runs(guard, monkeypatch):
    """The correction to the contract: dry runs cannot write, so they warn."""
    monkeypatch.setenv("WC_URL", OTHER_HOST)
    code, out = guard.run(guard.csv(3))
    assert code == W.EXIT_OK, out[-800:]
    assert "WARNING" in out and "store.invalid" in out
    assert C.store_host(OUR_HOST) in out, "the warning must name BOTH hosts"
    assert not guard.events, "a dry run must not call the store at all"


def test_commit_to_the_wrong_store_is_refused(guard, monkeypatch):
    monkeypatch.setenv("WC_URL", OTHER_HOST)
    code, out = guard.run(guard.csv(2), "--commit")
    assert code == W.EXIT_REFUSED
    assert "REFUSED" in out and "Nothing was written" in out
    assert not guard.events


def test_check_to_the_wrong_store_is_refused(guard, monkeypatch):
    monkeypatch.setenv("WC_URL", OTHER_HOST)
    code, out = guard.run("--check")
    assert code == W.EXIT_REFUSED
    assert not guard.events


# ----------------------------------------------------------- 2. --max-commit
def test_max_commit_refuses_a_fat_run(guard):
    code, out = guard.run(guard.csv(30), "--commit")
    assert code == W.EXIT_REFUSED
    assert "30" in out and "--max-commit" in out
    assert not guard.events, "nothing may be written or snapshotted"


def test_max_commit_raised_lets_the_run_through(guard):
    code, out = guard.run(guard.csv(30), "--commit", "--max-commit", "40")
    assert code == W.EXIT_OK, out[-800:]
    posts = [e for e in guard.events if e[0] == "POST"]
    assert len(posts) == 30, guard.events[:5]
    assert guard.events[0][0] == "CHECKPOINT", \
        "the snapshot must be taken BEFORE the first write"
    assert guard.events[0][1] == f"before-publish-{CFG.slug}"


def test_the_default_cap_is_25(guard):
    assert W.DEFAULT_MAX_COMMIT == 25
    code, _ = guard.run(guard.csv(25), "--commit", "--no-checkpoint")
    assert code == W.EXIT_OK
    code, _ = guard.run(guard.csv(26), "--commit", "--no-checkpoint")
    assert code == W.EXIT_REFUSED


def test_a_dry_run_is_never_capped(guard):
    code, out = guard.run(guard.csv(30))
    assert code == W.EXIT_OK, out[-400:]


# ------------------------------------------------------------- 3. snapshot
def test_no_checkpoint_skips_the_snapshot(guard):
    code, out = guard.run(guard.csv(3), "--commit", "--no-checkpoint")
    assert code == W.EXIT_OK
    assert not [e for e in guard.events if e[0] == "CHECKPOINT"]
    assert "cannot be rolled back" in out


def test_a_failed_snapshot_refuses_before_any_write(guard):
    def boom(store, label, client):
        raise RuntimeError("connection dropped mid-capture")

    guard.state["checkpoint"] = boom
    code, out = guard.run(guard.csv(3), "--commit")
    assert code == W.EXIT_REFUSED
    assert "snapshot failed" in out
    assert not [e for e in guard.events if e[0] in ("POST", "PUT")]


# ------------------------------------------------------- 4. review_signoff
def test_status_publish_without_signoff_is_refused(guard):
    code, out = guard.run(guard.csv(3), "--commit", "--status", "publish")
    assert code == W.EXIT_REFUSED
    assert "WM-000" in out, "the first missing SKU must be named"
    assert W.SIGNOFF_NAME in out, "the expected path must be named"
    assert not guard.events


def test_a_partial_signoff_is_still_refused(guard):
    path = guard.csv(3)
    guard.signoff(["WM-000", "WM-001"])
    code, out = guard.run(path, "--commit", "--status", "publish")
    assert code == W.EXIT_REFUSED
    assert "WM-002" in out


def test_a_complete_signoff_lets_the_go_live_through(guard):
    path = guard.csv(3)
    guard.signoff(["wm-000", "WM-001", "WM-002"])       # case must not matter
    code, out = guard.run(path, "--commit", "--status", "publish")
    assert code == W.EXIT_OK, out[-800:]
    assert len([e for e in guard.events if e[0] == "POST"]) == 3


def test_a_draft_commit_needs_no_signoff_but_reports_it(guard):
    path = guard.csv(3)
    guard.signoff(["WM-000"])
    code, out = guard.run(path, "--commit")
    assert code == W.EXIT_OK, out[-800:]
    assert "signoff: 1 of 3 rows signed" in out


def test_load_signoff_reads_the_file_next_to_the_csv(guard):
    path = guard.csv(2)
    assert W.load_signoff(path) == {}
    guard.signoff(["WM-000"])
    assert W.load_signoff(path) == {"WM-000": ("dhruv", "2026-09-02T10:00:00")}
    assert W.signoff_path(path).name == W.SIGNOFF_NAME


def test_delete_sku_needs_a_signoff(guard):
    code, out = guard.run(guard.csv(1), "--delete-sku", "WM-000", "--commit")
    assert code == W.EXIT_REFUSED
    assert "WM-000" in out
    assert not guard.events, "not even the lookup may run"


# ------------------------------------------------------------ 5. --preflight
def test_preflight_passes(guard):
    code, out = guard.run("--preflight")
    assert code == W.EXIT_OK, out
    assert "[OK  ] store URL" in out
    assert "Preflight passed." in out


def test_preflight_reports_a_challenge_as_blocked(guard):
    def challenged():
        raise W.ChallengeError(f"GET products -> {W.CHALLENGE_HELP}")

    guard.state["check"] = challenged
    code, out = guard.run("--preflight")
    assert code == W.EXIT_BLOCKED
    assert "browser challenge" in out


def test_preflight_on_the_wrong_store_refuses(guard, monkeypatch):
    monkeypatch.setenv("WC_URL", OTHER_HOST)
    code, out = guard.run("--preflight")
    assert code == W.EXIT_REFUSED
    assert "REFUSED" in out


def test_preflight_fails_without_media_credentials(guard, monkeypatch):
    monkeypatch.delenv("WP_APP_USER", raising=False)
    monkeypatch.delenv("WP_APP_PASSWORD", raising=False)
    code, out = guard.run("--preflight")
    assert code == W.EXIT_REFUSED
    assert "WP_APP_USER" in out
    # ...unless the run was never going to upload anything
    code, out = guard.run("--preflight", "--images", "url")
    assert code == W.EXIT_OK, out


# ------------------------------------------------- 6. per-client checkpoints
def test_checkpoint_dir_is_scoped_to_the_client():
    assert CP.checkpoint_dir(CFG) == CP.CHECKPOINT_ROOT / CFG.slug
    other = dataclasses.replace(CFG, slug="acme")
    assert CP.checkpoint_dir(other) == CP.CHECKPOINT_ROOT / "acme"
    assert CP.checkpoint_dir(CFG) != CP.checkpoint_dir(other), \
        "two clients' snapshots must never share a folder"
    assert CP.checkpoint_dir(None) == CP.CHECKPOINT_ROOT


def test_resolve_client_prefers_the_store_being_captured():
    class S:
        base = OUR_HOST

    assert CP.resolve_client(S()).slug == CFG.slug
    explicit = dataclasses.replace(CFG, slug="acme")
    assert CP.resolve_client(S(), explicit).slug == "acme"


# ------------------------------------------- 7. cross-store restore (item 3)
def _snapshot(store=OTHER_HOST, client="acme", pid=5):
    """A checkpoint payload whose one product differs from the live store."""
    payload = {"created_at": "2026-09-01T10:00:00", "label": "before-seo",
               "product_count": 1,
               "products": [{"id": pid, "sku": "WM-000", "name": "Old name",
                             "meta_data": [{"key": "rank_math_title",
                                            "value": "old title"}]}]}
    if store is not None:
        payload["store"] = store
    if client is not None:
        payload["client"] = client
    return payload


def _live(pid=5):
    return [{"id": pid, "sku": "WM-000", "name": "New name",
             "meta_data": [{"key": "rank_math_title", "value": "new title"}]}]


def _restore(guard, snapshot, **kw):
    store = FakeStore(allow_writes=True, events=guard.events)
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        try:
            CP.restore(store, snapshot, _live(), ["meta_data"], set(),
                       commit=True, client=CFG, **kw)
            code = W.EXIT_OK
        except SystemExit as exc:
            code = exc.code
    return code, buf.getvalue(), store


def test_a_snapshot_from_another_store_is_refused(guard):
    """The skeptic's replay: an acme snapshot restored into the shreejee
    store, PUT products/5 and all."""
    code, out, store = _restore(guard, _snapshot())
    assert code == W.EXIT_REFUSED
    assert not store.writes, "nothing may be written across stores"
    assert C.store_host(OTHER_HOST) in out and C.store_host(OUR_HOST) in out


def test_force_store_lets_a_cross_store_restore_through(guard):
    code, out, store = _restore(guard, _snapshot(), force_store=True)
    assert code == W.EXIT_OK, out[-500:]
    assert ("PUT", "products/5") in store.writes
    assert "--force-store" in out


def test_an_unstamped_legacy_snapshot_is_refused(guard):
    """The 7 unscoped files in checkpoints\\ record no store at all."""
    code, out, store = _restore(guard, _snapshot(store=None, client=None))
    assert code == W.EXIT_REFUSED
    assert not store.writes
    assert "predates store stamping" in out


def test_an_unstamped_legacy_snapshot_passes_with_force_store(guard):
    code, out, store = _restore(guard, _snapshot(store=None, client=None),
                                force_store=True)
    assert code == W.EXIT_OK, out[-500:]
    assert ("PUT", "products/5") in store.writes


def test_a_snapshot_from_this_store_restores(guard):
    code, out, store = _restore(guard, _snapshot(store=OUR_HOST,
                                                 client=CFG.slug))
    assert code == W.EXIT_OK, out[-500:]
    assert ("PUT", "products/5") in store.writes


def test_a_client_slug_mismatch_is_refused(guard):
    """Same host, another client's name on it - the config is lying somewhere,
    which is not the moment to write a catalogue back."""
    code, out, store = _restore(guard, _snapshot(store=OUR_HOST,
                                                 client="acme"))
    assert code == W.EXIT_REFUSED
    assert not store.writes
    assert "acme" in out and CFG.slug in out


def test_checkpoint_store_mismatch_is_quiet_when_it_matches():
    assert CP.checkpoint_store_mismatch(
        _snapshot(store=OUR_HOST, client=CFG.slug), OUR_HOST, CFG) == ""
    # www./scheme/trailing-slash differences are the same store
    assert CP.checkpoint_store_mismatch(
        _snapshot(store=OUR_HOST, client=CFG.slug),
        OUR_HOST.replace("https://", "http://www.") + "/", CFG) == ""
    assert CP.checkpoint_store_mismatch(_snapshot(), OUR_HOST, CFG)


def test_the_cli_offers_force_store():
    text = (ROOT / "wc_checkpoint.py").read_text(encoding="utf-8")
    assert '"--force-store"' in text, "the CLI must expose the override"


# ---------------------------------------------- 8. sign-off completeness (C)
def test_a_blank_reviewer_is_not_a_signoff(guard):
    path = guard.csv(1)
    (guard.tmp / W.SIGNOFF_NAME).write_text(
        "sku,reviewer,signed_at\nWM-000,,2026-09-02T10:00:00\n",
        encoding="utf-8")
    code, out = guard.run(path, "--commit", "--status", "publish")
    assert code == W.EXIT_REFUSED
    assert "WM-000" in out and "reviewer" in out
    assert not guard.events


def test_a_blank_signed_at_is_not_a_signoff(guard):
    path = guard.csv(1)
    (guard.tmp / W.SIGNOFF_NAME).write_text(
        "sku,reviewer,signed_at\nWM-000,dhruv,\n", encoding="utf-8")
    code, out = guard.run(path, "--commit", "--status", "publish")
    assert code == W.EXIT_REFUSED
    assert "WM-000" in out and "signed_at" in out
    assert not guard.events


def test_a_wholly_blank_row_names_both_fields(guard):
    path = guard.csv(1)
    (guard.tmp / W.SIGNOFF_NAME).write_text(
        "sku,reviewer,signed_at\nWM-000,,\n", encoding="utf-8")
    assert W.check_signoff(["WM-000"], path) is not None
    message = W.check_signoff(["WM-000"], path)
    assert "reviewer" in message and "signed_at" in message


# ------------------------------------------------------------ 9. blog import
def test_blog_publish_imports_without_a_client_env():
    """A missing clients/<slug>/.env leaves the environment alone, silently."""
    import importlib
    sys.path.insert(0, str(ROOT / "blog_automate"))
    import blog_publish                                   # noqa: F401
    importlib.reload(blog_publish)
    assert hasattr(blog_publish, "CLIENT")


def _write_client(dirpath, slug, store_url, env_lines):
    """A minimal clients/<slug>/ - client.toml, the taxonomy file load_client
    insists on, and a .env."""
    home = dirpath / slug
    home.mkdir(parents=True, exist_ok=True)
    (home / "category_map.xlsx").write_bytes(b"not really a workbook")
    (home / "client.toml").write_text(
        f'[client]\nslug = "{slug}"\nname = "{slug.title()}"\n\n'
        f'[store]\nurl = "{store_url}"\n\n'
        f'[sku]\nfallback_prefix = "GEN"\n'
        f'[sku.prefix_by_parent]\n"Washing Machines" = "WM"\n',
        encoding="utf-8")
    (home / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    return home


def test_importing_blog_publish_does_not_touch_os_environ(monkeypatch, tmp_path):
    """Even with a real clients/<slug>/.env on disk, the import is inert -
    otherwise one import rewrites WC_URL for the whole pytest session."""
    import importlib
    _write_client(tmp_path, "acme", OTHER_HOST,
                  [f"WC_URL={OTHER_HOST}", "WP_APP_USER=acme-bot",
                   "WP_APP_PASSWORD=acme-pw"])
    monkeypatch.setattr(C, "CLIENTS_DIR", tmp_path)
    monkeypatch.setenv("PIPELINE_CLIENT", "acme")
    monkeypatch.setenv("WC_URL", "sentinel")
    monkeypatch.setenv("WP_APP_USER", "sentinel")
    monkeypatch.setenv("WP_APP_PASSWORD", "sentinel")
    before = dict(os.environ)

    sys.path.insert(0, str(ROOT / "blog_automate"))
    import blog_publish
    importlib.reload(blog_publish)
    assert dict(os.environ) == before, "import must have no env side effects"

    # ...and pytest is detected, so even an explicit call stays inert
    blog_publish.ensure_client_env()
    assert os.environ["WC_URL"] == "sentinel"
    assert blog_publish.CLIENT is not None and blog_publish.CLIENT.slug == "acme"
    importlib.reload(blog_publish)


def test_ensure_client_env_applies_the_env_outside_pytest(monkeypatch, tmp_path):
    """The same call with $PYTEST_CURRENT_TEST cleared (what a real CLI run
    looks like) DOES apply the client's credentials, overriding the shell."""
    import importlib
    _write_client(tmp_path, "acme", OTHER_HOST,
                  [f"WC_URL={OTHER_HOST}", "WP_APP_USER=acme-bot",
                   "WP_APP_PASSWORD=acme-pw"])
    monkeypatch.setattr(C, "CLIENTS_DIR", tmp_path)
    monkeypatch.setenv("PIPELINE_CLIENT", "acme")
    # monkeypatch records these three, so the apply below is undone at teardown
    monkeypatch.setenv("WC_URL", "sentinel")
    monkeypatch.setenv("WP_APP_USER", "sentinel")
    monkeypatch.setenv("WP_APP_PASSWORD", "sentinel")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    sys.path.insert(0, str(ROOT / "blog_automate"))
    import blog_publish
    importlib.reload(blog_publish)
    assert os.environ["WC_URL"] == "sentinel", "still nothing at import"

    assert blog_publish.base_url() == OTHER_HOST.rstrip("/")
    assert os.environ["WP_APP_USER"] == "acme-bot"
    assert blog_publish.wp_auth() == ("acme-bot", "acme-pw")
    importlib.reload(blog_publish)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-header",
                                  "-p", "no:cacheprovider"]))
