"""Share mode: the password gate and the restricted GUI (gui.py + gui_helpers).

No network, no subprocess, no store. Every AppTest run uses a scratch
RUNS_DIR; subprocess.Popen / subprocess.run are stubbed wherever a function
could start one.

    .venv\\Scripts\\python.exe -m pytest tests\\test_share_mode.py -q
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
os.environ.setdefault("PIPELINE_REVIEW_IMAGE_PREVIEW", "off")

from streamlit.testing.v1 import AppTest  # noqa: E402

import gui_helpers as gh  # noqa: E402

GOOD_PW = "correct-horse-battery-staple"
SHARE_VARS = (gh.SHARE_MODE_ENV, gh.SHARE_PASSWORD_ENV, gh.SHARE_BUDGET_ENV)


# --------------------------------------------------------------- fixtures --


@pytest.fixture(autouse=True)
def clean_share_env(monkeypatch):
    """Every test starts with the three switches unset, a fresh throttle and
    no sleep on a wrong password."""
    for var in SHARE_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gh, "SHARE_FAIL_DELAY", 0)
    gh.SHARE_THROTTLE.reset()
    yield
    gh.SHARE_THROTTLE.reset()


@pytest.fixture
def shared(monkeypatch):
    monkeypatch.setenv(gh.SHARE_MODE_ENV, "1")
    monkeypatch.setenv(gh.SHARE_PASSWORD_ENV, GOOD_PW)


def wizard(runs_dir):
    import gui
    gui.RUNS_DIR = runs_dir
    gui.main()


def boot(runs_dir):
    at = AppTest.from_function(wizard, args=(runs_dir,))
    at.run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    return at


def login(at, password):
    at.text_input(key="share_password_input").set_value(password)
    hits = [b for b in at.button if "Log in" in str(b.label)]
    assert hits, [str(b.label) for b in at.button]
    hits[0].click().run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    return at


def texts(at, kind):
    return [str(e.value) for e in getattr(at, kind)]


def gate_only(at):
    """Nothing but the password form: no sidebar, no run list, no steps."""
    assert not at.sidebar.selectbox and not at.sidebar.radio \
        and not at.sidebar.checkbox and not at.sidebar.button
    assert not at.selectbox and not at.radio and not at.checkbox
    assert [b.label for b in at.button] == ["Log in"]
    assert len(at.text_input) == 1


def seeded_run(runs: Path, name: str) -> Path:
    run = runs / name
    run.mkdir(parents=True)
    (run / "input.xlsx").write_bytes(b"")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["sku", "title", "status"])
    ws.append(["SKU-A", "Samsung 7 kg Washer", "FOUND (amazon)"])
    wb.save(run / "input_filled.xlsx")
    (run / "wordpress_ready.csv").write_text(
        'sku,title\n"WM-SKU-A","Samsung 7 kg Washer"\n', encoding="utf-8")
    (run / "run_meta.json").write_text(json.dumps(
        {"client": "shreejee", "validated": True, "issue_count": 0,
         "built_scope": "confirmed", "built_skus": ["WM-SKU-A"]}),
        encoding="utf-8")
    return run


# ---------------------------------------------------------------- helpers --


def test_share_mode_switch_values():
    for on in ("1", "true", "TRUE", "on", "yes", " On "):
        assert gh.share_mode_on({gh.SHARE_MODE_ENV: on})
    for off in ("", "0", "false", "off", "no", "2"):
        assert not gh.share_mode_on({gh.SHARE_MODE_ENV: off})
    assert not gh.share_mode_on({})


def test_password_problem():
    assert gh.share_password_problem({}) is not None
    short = gh.share_password_problem({gh.SHARE_PASSWORD_ENV: "x" * 11})
    assert short and "x" * 11 not in short          # never echoed
    assert gh.share_password_problem({gh.SHARE_PASSWORD_ENV: "x" * 12}) is None


def test_share_budget():
    assert gh.share_budget({}) == 50
    assert gh.share_budget({gh.SHARE_BUDGET_ENV: "20"}) == 20
    for bad in ("0", "-5", "abc", ""):
        assert gh.share_budget({gh.SHARE_BUDGET_ENV: bad}) == 50


def test_check_share_password():
    assert gh.check_share_password(GOOD_PW, GOOD_PW)
    assert not gh.check_share_password(GOOD_PW + "x", GOOD_PW)
    assert not gh.check_share_password("", GOOD_PW)
    assert not gh.check_share_password("", "")
    assert not gh.check_share_password(None, GOOD_PW)


@pytest.mark.parametrize("flags,cap,want", [
    ([], 50, ["--budget", "50"]),
    (["--budget", "0"], 50, ["--budget", "50"]),          # 0 = unlimited
    (["--budget", "500"], 50, ["--budget", "50"]),
    (["--budget", "10"], 50, ["--budget", "10"]),
    (["--budget=7"], 50, ["--budget", "7"]),
    (["--bud", "900"], 30, ["--budget", "30"]),           # abbreviation
    (["--discover", "--js-fallback", "--force", "--limit", "5"], 50,
     ["--discover", "--limit", "5", "--budget", "50"]),
    (["--js-profile", "C:\\prof", "--delay", "3", "7"], 50,
     ["--delay", "3", "7", "--budget", "50"]),
    (["--js-profile=C:\\prof", "--forc", "--js-f", "--crawl-urls"], 50,
     ["--crawl-urls", "--budget", "50"]),
])
def test_share_cap_flags(flags, cap, want):
    assert gh.share_cap_flags(flags, cap) == want


def test_throttle_locks_after_max_fails_and_unlocks():
    now = [1000.0]
    t = gh.LoginThrottle(max_fails=8, lock_seconds=900, clock=lambda: now[0])
    for _ in range(7):
        t.record_failure()
    assert t.locked_for() == 0
    t.record_failure()
    assert t.locked_for() == pytest.approx(900)
    now[0] += 899
    assert t.locked_for() > 0
    now[0] += 2
    assert t.locked_for() == 0 and t.failures == 0      # lock over: reset


def test_throttle_success_resets_count():
    t = gh.LoginThrottle(max_fails=3, lock_seconds=60, clock=lambda: 0.0)
    t.record_failure()
    t.record_failure()
    t.record_success()
    t.record_failure()
    assert t.locked_for() == 0


# ------------------------------------------------------------- (a) gate --


def test_a_share_mode_without_login_renders_only_the_gate(tmp_path, shared):
    runs = tmp_path / "runs"
    seeded_run(runs, "secret_run")
    at = boot(runs)
    gate_only(at)
    page = " ".join(texts(at, "markdown") + texts(at, "caption")
                    + texts(at, "info") + texts(at, "title"))
    assert "secret_run" not in page


# ---------------------------------------------- (b) wrong pw + lockout --


def test_b_wrong_password_and_lockout(tmp_path, shared):
    runs = tmp_path / "runs"
    seeded_run(runs, "r1")
    at = boot(runs)
    login(at, "not-the-password-at-all")
    assert any("Wrong password" in e for e in texts(at, "error"))
    gate_only(at)
    for _ in range(gh.SHARE_MAX_FAILS - 1):
        login(at, "still-wrong-password")
    assert gh.SHARE_THROTTLE.locked_for() > 0
    assert any("locked" in e for e in texts(at, "error"))
    # the lock is process-wide: a NEW session with the RIGHT password fails
    at2 = boot(runs)
    login(at2, GOOD_PW)
    assert any("locked" in e for e in texts(at2, "error"))
    gate_only(at2)


# ------------------------------------------------------- (c) publish off --


def test_c_publish_step_has_no_controls(tmp_path, shared, monkeypatch):
    monkeypatch.setenv("WC_URL", "https://shreejeeelectronics.in")
    monkeypatch.setenv("WC_KEY", "ck_test")
    monkeypatch.setenv("WC_SECRET", "cs_test")
    runs = tmp_path / "runs"
    seeded_run(runs, "pub")
    at = boot(runs)
    login(at, GOOD_PW)
    assert any(gh.SHARE_BANNER in i for i in texts(at, "info"))
    labels = [str(o) for o in at.sidebar.radio[0].options]
    at.sidebar.radio[0].set_value(labels[4]).run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    buttons = " ".join(str(b.label) for b in at.button)
    for word in ("Preflight", "Dry run", "Commit", "Check", "Publish"):
        assert word not in buttons, buttons
    assert any("Publish is off in share mode" in i for i in texts(at, "info"))
    assert gh.SHARE_BANNER in " ".join(texts(at, "info"))


def test_c_run_publish_refuses(tmp_path, shared, monkeypatch):
    import gui

    def boom(*a, **k):
        raise AssertionError("a subprocess was started in share mode")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    run = seeded_run(tmp_path / "runs", "pub2")
    for extra in (["--preflight"], [], ["--commit", "--status", "publish"]):
        code, out = gui.run_publish(gui.run_paths(run), extra)
        assert code == 4 and "REFUSED" in out
    assert not gui.run_paths(run)["publish_log"].exists()


# ------------------------------------------ (d) delete/archive/map/client --


def test_d_dangerous_functions_refuse(tmp_path, shared):
    import gui
    run = seeded_run(tmp_path / "runs", "keep_me")
    with pytest.raises(gh.ShareModeRefused):
        gui.delete_run(run)
    assert run.is_dir()
    dest = tmp_path / "batches" / "keep_me"
    with pytest.raises(gh.ShareModeRefused):
        gui.archive_run(run, dest)
    assert not dest.exists()
    cmap = tmp_path / "category_map.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["Parent", "Subs"])
    wb.save(cmap)
    before = cmap.read_bytes()
    with pytest.raises(gh.ShareModeRefused):
        gui.backup_and_save_category_map(cmap, None, tmp_path / "bk")
    assert cmap.read_bytes() == before and not (tmp_path / "bk").exists()
    before_env = os.environ.get("PIPELINE_CLIENT")
    with pytest.raises(gh.ShareModeRefused):
        gui.switch_client("shreejee")
    assert os.environ.get("PIPELINE_CLIENT") == before_env


def test_d_controls_hidden_after_login(tmp_path, shared):
    runs = tmp_path / "runs"
    seeded_run(runs, "home_run")
    at = boot(runs)
    login(at, GOOD_PW)
    side_boxes = [str(c.label) for c in at.sidebar.checkbox]
    assert not any("Category map" in c for c in side_boxes), side_boxes
    keys = [s.key for s in at.sidebar.selectbox]
    assert "client_picker" not in keys, keys
    at.sidebar.selectbox(key="run_picker").set_value("🏠 All runs") \
        .run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    assert not any("Manage a run" in str(e.label) for e in at.expander)
    assert not any("Delete" in str(b.label) or "Archive" in str(b.label)
                   for b in at.button)


# ------------------------------------------------------ (e) enricher cap --


def launch_capture(run_dir_str, flags, captured):
    import subprocess as sp
    from pathlib import Path as _P
    import gui

    class _Proc:
        pid = 9999

    def fake_popen(cmd, *a, **k):
        captured.append(list(cmd))
        return _Proc()

    real = sp.Popen
    sp.Popen = fake_popen
    try:
        gui.start_enricher(gui.run_paths(_P(run_dir_str)), list(flags), 3)
    finally:
        sp.Popen = real


@pytest.mark.parametrize("flags,cap,want", [
    (["--discover", "--js-fallback", "--force", "--budget", "500",
      "--js-profile", "prof"], "30", "30"),
    (["--budget", "10"], "30", "10"),
    ([], None, "50"),
])
def test_e_enricher_command_is_capped(tmp_path, shared, monkeypatch,
                                      flags, cap, want):
    if cap:
        monkeypatch.setenv(gh.SHARE_BUDGET_ENV, cap)
    run = tmp_path / "runs" / "enr"
    run.mkdir(parents=True)
    (run / "input.xlsx").write_bytes(b"")
    captured = []
    at = AppTest.from_function(launch_capture,
                               args=(str(run), flags, captured))
    at.run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    assert len(captured) == 1
    cmd = captured[0]
    assert cmd.count("--budget") == 1
    assert cmd[cmd.index("--budget") + 1] == want
    joined = " ".join(cmd)
    assert "--js" not in joined and "--force" not in joined
    meta = json.loads((run / "run_meta.json").read_text(encoding="utf-8"))
    assert "--force" not in meta["enricher_flags"]


# ------------------------------------------------ (f) bad password config --


@pytest.mark.parametrize("password", [None, "short-pw-11"])
def test_f_missing_or_short_password_renders_only_the_error(
        tmp_path, monkeypatch, password):
    monkeypatch.setenv(gh.SHARE_MODE_ENV, "on")
    if password:
        monkeypatch.setenv(gh.SHARE_PASSWORD_ENV, password)
    runs = tmp_path / "runs"
    seeded_run(runs, "r")
    at = boot(runs)
    assert len(at.error) == 1
    assert gh.SHARE_PASSWORD_ENV in str(at.error[0].value)
    if password:
        assert password not in str(at.error[0].value)
    assert not at.button and not at.text_input and not at.selectbox
    assert not at.sidebar.selectbox and not at.radio


# ------------------------------------------------------ (g) env unset --


def test_g_unset_env_leaves_the_gui_unchanged(tmp_path):
    runs = tmp_path / "runs"
    seeded_run(runs, "normal")
    at = boot(runs)
    keys = [s.key for s in at.sidebar.selectbox]
    assert "client_picker" in keys and "run_picker" in keys, keys
    assert any("Category map" in str(c.label) for c in at.sidebar.checkbox)
    assert not any("Log in" in str(b.label) for b in at.button)
    assert not any(gh.SHARE_BANNER in i for i in texts(at, "info"))
    assert len(at.sidebar.radio[0].options) == 5


def test_share_folder_is_never_offered_as_a_run(tmp_path, shared):
    """runs\\_share\\ holds share_info.txt (the password) and the server log:
    the run picker must not list it, nor create run_meta.json in it."""
    runs = tmp_path / "runs"
    seeded_run(runs, "real_run")
    share = runs / "_share"
    share.mkdir()
    (share / "share_info.txt").write_text("password=x", encoding="utf-8")
    at = boot(runs)
    login(at, GOOD_PW)
    options = [str(o) for o in at.sidebar.selectbox(key="run_picker").options]
    assert "real_run" in options and "_share" not in options, options
    assert not (share / "run_meta.json").exists()


def test_step3_offers_the_image_zip_in_share_mode(tmp_path, shared):
    """The person on the other PC has no access to product_images_for_upload:
    step 3 of a built run gives them the CSV AND the images zip. The image
    file does not exist here, so the zip lists it as missing - the button is
    there all the same, and nothing in the run folder is moved or deleted."""
    runs = tmp_path / "runs"
    run = seeded_run(runs, "built")
    (run / "wordpress_ready.csv").write_text(
        'sku,title,Images\n"WM-SKU-A","Samsung 7 kg Washer",'
        '"https://shreejeeelectronics.in/wp-content/uploads/2026/11/'
        'WM-SKU-A_zz-not-on-disk_1.webp"\n', encoding="utf-8")
    before = sorted(p.name for p in run.iterdir())
    at = boot(runs)
    login(at, GOOD_PW)
    labels = [str(o) for o in at.sidebar.radio[0].options]
    at.sidebar.radio[0].set_value(labels[2]).run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    downloads = [str(getattr(d, "proto", d).label)
                 for d in at.get("download_button")]
    assert any("Download images (.zip)" in d for d in downloads), downloads
    assert any("Download CSV" in d for d in downloads), downloads
    assert any("1 missing" in str(c.value) for c in at.caption), \
        [str(c.value) for c in at.caption]
    assert sorted(p.name for p in run.iterdir()) == before


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
