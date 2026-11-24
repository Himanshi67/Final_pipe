"""Password-only mode (GUI_PASSWORD): the share-mode gate in front of the FULL
GUI, plus the step 5 hand-over section and the category map download.

No network, no subprocess, no store. Every AppTest run uses a scratch
RUNS_DIR.

    .venv\\Scripts\\python.exe -m pytest tests\\test_password_mode.py -q
"""

import json
import os
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

GOOD_PW = "password-only-mode-secret"
SHARE_PW = "correct-horse-battery-staple"
GATE_VARS = (gh.SHARE_MODE_ENV, gh.SHARE_PASSWORD_ENV, gh.SHARE_BUDGET_ENV,
             gh.PASSWORD_ENV)


# --------------------------------------------------------------- fixtures --


@pytest.fixture(autouse=True)
def clean_gate_env(monkeypatch):
    for var in GATE_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gh, "SHARE_FAIL_DELAY", 0)
    gh.SHARE_THROTTLE.reset()
    yield
    gh.SHARE_THROTTLE.reset()


@pytest.fixture
def pw_only(monkeypatch):
    monkeypatch.setenv(gh.PASSWORD_ENV, GOOD_PW)


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


def downloads(at):
    return [str(getattr(d, "proto", d).label)
            for d in at.get("download_button")]


def gate_only(at):
    assert not at.sidebar.selectbox and not at.sidebar.radio \
        and not at.sidebar.checkbox and not at.sidebar.button
    assert not at.selectbox and not at.radio and not at.checkbox
    assert [b.label for b in at.button] == ["Log in"]
    assert len(at.text_input) == 1


def seeded_run(runs: Path, name: str, images: bool = False) -> Path:
    run = runs / name
    run.mkdir(parents=True)
    (run / "input.xlsx").write_bytes(b"")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["sku", "title", "status"])
    ws.append(["SKU-A", "Samsung 7 kg Washer", "FOUND (amazon)"])
    wb.save(run / "input_filled.xlsx")
    if images:
        csv = ('sku,title,Images\n"WM-SKU-A","Samsung 7 kg Washer",'
               '"https://shreejeeelectronics.in/wp-content/uploads/2026/11/'
               'WM-SKU-A_zz-not-on-disk_1.webp"\n')
    else:
        csv = 'sku,title\n"WM-SKU-A","Samsung 7 kg Washer"\n'
    (run / "wordpress_ready.csv").write_text(csv, encoding="utf-8")
    (run / "run_meta.json").write_text(json.dumps(
        {"client": "shreejee", "validated": True, "issue_count": 0,
         "built_scope": "confirmed", "built_skus": ["WM-SKU-A"]}),
        encoding="utf-8")
    return run


def open_step(at, index):
    labels = [str(o) for o in at.sidebar.radio[0].options]
    at.sidebar.radio[0].set_value(labels[index]).run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    return at


# ---------------------------------------------------------------- helpers --


def test_mode_helpers():
    assert not gh.gate_on({})
    assert not gh.password_mode_on({})
    env = {gh.PASSWORD_ENV: GOOD_PW}
    assert gh.password_mode_on(env) and gh.gate_on(env)
    assert not gh.share_mode_on(env)
    assert gh.gate_password(env) == GOOD_PW
    assert gh.gate_password_problem(env) is None
    short = gh.gate_password_problem({gh.PASSWORD_ENV: "x" * 11})
    assert short and gh.PASSWORD_ENV in short and "x" * 11 not in short
    both = {gh.PASSWORD_ENV: GOOD_PW, gh.SHARE_MODE_ENV: "1",
            gh.SHARE_PASSWORD_ENV: SHARE_PW}
    assert gh.share_mode_on(both) and not gh.password_mode_on(both)
    assert gh.gate_password(both) == SHARE_PW
    # share mode wins even when its own password is missing: fail closed
    no_share_pw = {gh.PASSWORD_ENV: GOOD_PW, gh.SHARE_MODE_ENV: "1"}
    assert gh.gate_password_problem(no_share_pw) is not None
    assert gh.gate_password(no_share_pw) == ""


# ------------------------------------------------------------------- gate --


def test_gate_shows_and_nothing_else_renders(tmp_path, pw_only):
    runs = tmp_path / "runs"
    seeded_run(runs, "secret_run")
    at = boot(runs)
    gate_only(at)
    page = " ".join(texts(at, "markdown") + texts(at, "caption")
                    + texts(at, "info") + texts(at, "title"))
    assert "secret_run" not in page


def test_wrong_password_is_refused(tmp_path, pw_only):
    runs = tmp_path / "runs"
    seeded_run(runs, "r1")
    at = boot(runs)
    login(at, "not-the-password-at-all")
    assert any("Wrong password" in e for e in texts(at, "error"))
    gate_only(at)


def test_after_login_nothing_is_hidden(tmp_path, pw_only):
    import gui
    runs = tmp_path / "runs"
    seeded_run(runs, "home_run")
    at = boot(runs)
    login(at, GOOD_PW)
    assert not gui.share_mode()
    assert not any(gh.SHARE_BANNER in i for i in texts(at, "info"))
    assert any("Category map" in str(c.label) for c in at.sidebar.checkbox)
    keys = [s.key for s in at.sidebar.selectbox]
    assert "client_picker" in keys and "run_picker" in keys, keys
    at.sidebar.selectbox(key="run_picker").set_value("🏠 All runs") \
        .run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    assert any("Manage a run" in str(e.label) for e in at.expander)


@pytest.mark.parametrize("password", ["short-pw-11", "x"])
def test_short_password_fails_closed(tmp_path, monkeypatch, password):
    monkeypatch.setenv(gh.PASSWORD_ENV, password)
    runs = tmp_path / "runs"
    seeded_run(runs, "r")
    at = boot(runs)
    assert len(at.error) == 1
    assert gh.PASSWORD_ENV in str(at.error[0].value)
    assert password not in str(at.error[0].value)
    assert not at.button and not at.text_input and not at.selectbox
    assert not at.sidebar.selectbox and not at.radio


def test_both_set_gives_share_mode(tmp_path, monkeypatch):
    import gui
    monkeypatch.setenv(gh.PASSWORD_ENV, GOOD_PW)
    monkeypatch.setenv(gh.SHARE_MODE_ENV, "1")
    monkeypatch.setenv(gh.SHARE_PASSWORD_ENV, SHARE_PW)
    runs = tmp_path / "runs"
    seeded_run(runs, "r")
    at = boot(runs)
    gate_only(at)
    login(at, GOOD_PW)              # GUI_PASSWORD is NOT the password here
    assert any("Wrong password" in e for e in texts(at, "error"))
    gate_only(at)
    login(at, SHARE_PW)
    assert gui.share_mode()
    assert any(gh.SHARE_BANNER in i for i in texts(at, "info"))
    assert not any("Category map" in str(c.label)
                   for c in at.sidebar.checkbox)
    assert "client_picker" not in [s.key for s in at.sidebar.selectbox]


def test_neither_set_has_no_gate(tmp_path):
    runs = tmp_path / "runs"
    seeded_run(runs, "normal")
    at = boot(runs)
    assert not any("Log in" in str(b.label) for b in at.button)
    keys = [s.key for s in at.sidebar.selectbox]
    assert "client_picker" in keys and "run_picker" in keys, keys


# ------------------------------------------------------ step 5 hand-over --


def test_step5_shows_handover_and_missing_creds_message(tmp_path, pw_only,
                                                         monkeypatch):
    import gui
    for var in ("WC_URL", "WC_KEY", "WC_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gui, "client_env", lambda cfg: {})
    runs = tmp_path / "runs"
    run = seeded_run(runs, "built", images=True)
    before = sorted(p.name for p in run.iterdir())
    at = boot(runs)
    login(at, GOOD_PW)
    open_step(at, 4)
    got = downloads(at)
    assert any("Download CSV" in d for d in got), got
    assert any("Download images (.zip)" in d for d in got), got
    md = " ".join(texts(at, "markdown"))
    assert "wp-content/uploads/" in md and "Products > Import" in md, md
    assert any("Missing credentials" in e for e in texts(at, "error"))
    assert sorted(p.name for p in run.iterdir()) == before


def test_step5_handover_in_share_mode_publish_stays_off(tmp_path,
                                                        monkeypatch):
    monkeypatch.setenv(gh.SHARE_MODE_ENV, "1")
    monkeypatch.setenv(gh.SHARE_PASSWORD_ENV, SHARE_PW)
    runs = tmp_path / "runs"
    seeded_run(runs, "built", images=True)
    at = boot(runs)
    login(at, SHARE_PW)
    open_step(at, 4)
    got = downloads(at)
    assert any("Download CSV" in d for d in got), got
    assert any("Publish is off in share mode" in i for i in texts(at, "info"))
    buttons = " ".join(str(b.label) for b in at.button)
    for word in ("Preflight", "Dry run", "Commit", "Check"):
        assert word not in buttons, buttons


def test_handover_without_csv_shows_info(tmp_path):
    """handover_section on a run with no CSV: an info line, no buttons."""
    run = tmp_path / "runs" / "empty"
    run.mkdir(parents=True)

    def page(run_dir):
        import gui
        from pathlib import Path as _P
        gui.handover_section(gui.run_paths(_P(run_dir)))

    at = AppTest.from_function(page, args=(str(run),))
    at.run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    assert any("No CSV yet" in i for i in texts(at, "info"))
    assert not downloads(at)


# ------------------------------------------------ category map download --


def test_category_map_editor_has_download(tmp_path, pw_only):
    runs = tmp_path / "runs"
    seeded_run(runs, "r")
    at = boot(runs)
    login(at, GOOD_PW)
    box = next(c for c in at.sidebar.checkbox
               if "Category map" in str(c.label))
    box.check().run(timeout=60)
    assert not at.exception, [e.value for e in at.exception]
    got = downloads(at)
    assert any("Download category_map.xlsx" in d for d in got), got


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
