"""Per-client configuration - PRODUCT_PLAN Phase 1, item 1.

Pins the contract in docs/CLIENT_CONFIG.md: one place per client for every
store-specific value, `--client <slug>` on every non-GUI entry point, and a
second client that cannot leak into the first.

    .venv\\Scripts\\python.exe tests\\test_client_config.py
    .venv\\Scripts\\python.exe -m pytest tests/test_client_config.py -q
"""
import os
import shutil
import subprocess
import sys
import tempfile
import pathlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

import client_config as C                              # noqa: E402

EXE = str(ROOT / ".venv" / "Scripts" / "python.exe")


# ---------------------------------------------------------------- resolution
def test_resolve_slug_precedence(monkeypatch):
    """explicit > argv --client > $PIPELINE_CLIENT > DEFAULT_CLIENT."""
    monkeypatch.delenv(C.ENV_VAR, raising=False)
    assert C.resolve_slug(argv=[]) == C.DEFAULT_CLIENT

    monkeypatch.setenv(C.ENV_VAR, "fromenv")
    assert C.resolve_slug(argv=[]) == "fromenv"

    # argv beats the environment, in both spellings
    assert C.resolve_slug(argv=["--client", "fromargv"]) == "fromargv"
    assert C.resolve_slug(argv=["--client=fromargv"]) == "fromargv"
    assert C.resolve_slug(argv=["x.xlsx", "--limit", "3",
                                "--client", "fromargv"]) == "fromargv"

    # explicit beats everything
    assert C.resolve_slug(argv=["--client", "fromargv"],
                          explicit="explicit") == "explicit"

    # a bare --client is a typo, not a request for the env/default
    with pytest.raises(C.ClientConfigError):
        C.resolve_slug(argv=["--client"])


def test_load_shreejee_paths_exist():
    cfg = C.load_client("shreejee")
    assert cfg.slug == "shreejee"
    assert cfg.name == "Shree Jee Electronics"
    assert cfg.store_url == "https://shreejeeelectronics.in"
    assert cfg.seo_site_name == "Shree Jee Electronics"
    assert "Shree Jee Electronics" in cfg.placeholder_html
    assert cfg.dir == C.CLIENTS_DIR / "shreejee"
    for path in (cfg.category_map, cfg.category_seo, cfg.attributes_master):
        assert path.is_file(), f"missing taxonomy file: {path}"
    assert cfg.image_dir == C.ROOT / "product_images_for_upload" / "shreejee"
    assert cfg.backups_dir == cfg.dir / "backups"


def test_loading_creates_nothing_on_disk():
    """Loading a config must not mkdir - callers create what they write."""
    cfg = C.load_client("shreejee")
    existed = cfg.backups_dir.exists()
    C.load_client("shreejee")
    assert cfg.backups_dir.exists() == existed


def test_unknown_slug_lists_available_clients():
    with pytest.raises(C.ClientConfigError) as excinfo:
        C.load_client("no-such-store")
    message = str(excinfo.value)
    assert "no-such-store" in message
    assert "shreejee" in message, "the error must name what IS available"


def test_media_base_renders_the_template():
    cfg = C.load_client("shreejee")
    assert cfg.media_base("2026/07") == (
        "https://shreejeeelectronics.in/wp-content/uploads/2026/07")
    # no month = the current one, still the same shape
    now = cfg.media_base()
    assert now.startswith("https://shreejeeelectronics.in/wp-content/uploads/")
    assert len(now.rsplit("/uploads/", 1)[1].split("/")) == 2


def test_available_clients_includes_shreejee():
    assert "shreejee" in C.available_clients()


# ------------------------------------------------------- synthetic clients
SKU_TABLE = """
[sku]
fallback_prefix = "AP"
never_rename = ["WM"]
strip_codes = ["DW"]

[sku.prefix_by_parent]
"Washing Machines" = "WM"
"LED" = "TV"

[sku.input_codes]
REF = ["Home Appliances", "Refrigerators"]
"""

CLIENT_HEAD = """[client]
slug = "{slug}"
name = "Synthetic {slug}"

[store]
url = "https://{slug}.invalid"
"""


def make_client(clients_dir, monkeypatch, body=SKU_TABLE, slug="synth"):
    """A minimal client dir under tmp_path, with CLIENTS_DIR pointed at it.

    Only the taxonomy file's EXISTENCE is checked at load time, so a stub
    stands in for the workbook - these tests are about the toml, and must
    never touch a real batch file.
    """
    target = Path(clients_dir) / slug
    target.mkdir(parents=True, exist_ok=True)
    (target / "category_map.xlsx").write_bytes(b"stub - existence only")
    (target / "client.toml").write_text(
        CLIENT_HEAD.format(slug=slug) + body, encoding="utf-8")
    monkeypatch.setattr(C, "CLIENTS_DIR", Path(clients_dir))
    return target


# ------------------------------------------------- a real second client
@pytest.fixture
def two_clients(monkeypatch):
    """A scratch clients/ dir holding shreejee plus a synthetic second store.

    This is the whole point of item 1: a second client must not be able to
    leak into the first.
    """
    tmp = Path(tempfile.mkdtemp())
    real = C.load_client("shreejee")
    for slug, folder in (("shreejee", real.dir), ("acme", None)):
        target = tmp / slug
        target.mkdir(parents=True)
        if folder is not None:
            shutil.copy2(folder / "client.toml", target / "client.toml")
        for name in ("category_map.xlsx", "category_seo.xlsx",
                     "SubCategory_Attributes_Master.xlsx"):
            shutil.copy2(real.dir / name, target / name)
    (tmp / "acme" / "client.toml").write_text(
        '[client]\n'
        'slug = "acme"\n'
        'name = "Acme Appliances"\n'
        '\n'
        '[store]\n'
        'url = "https://acme.invalid"\n'
        'media_base = "{url}/wp-content/uploads/{yyyy}/{mm}"\n'
        'seo_site_name = "Acme Appliances"\n'
        'placeholder_html = "<p>Acme placeholder.</p>"\n'
        '\n'
        '[taxonomy]\n'
        'category_map = "category_map.xlsx"\n'
        'category_seo = "category_seo.xlsx"\n'
        'attributes_master = "SubCategory_Attributes_Master.xlsx"\n'
        + SKU_TABLE,
        encoding="utf-8")
    monkeypatch.setattr(C, "CLIENTS_DIR", tmp)
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_slug_must_match_the_directory(two_clients):
    (two_clients / "impostor").mkdir()
    shutil.copy2(two_clients / "acme" / "client.toml",
                 two_clients / "impostor" / "client.toml")
    with pytest.raises(C.ClientConfigError):
        C.load_client("impostor")


def test_app_configure_swaps_and_restores_every_derived_constant(two_clients):
    import app

    before = (app.CLIENT.slug, app.MEDIA_BASE, app.SEO_SITE_NAME,
              app.CATEGORY_MAP_FILE, app.IMAGE_DIR)
    try:
        cfg = app.configure("acme")
        assert cfg.slug == "acme"
        assert app.CLIENT.slug == "acme"
        assert app.MEDIA_BASE.startswith("https://acme.invalid/wp-content/uploads/")
        assert app.SEO_SITE_NAME == "Acme Appliances"
        assert app.CATEGORY_MAP_FILE == "category_map.xlsx"
        assert app.IMAGE_DIR.name == "acme"
        # the taxonomy really was re-read, not just re-pointed
        assert app.CATEGORY_COMBOS and app.SUBCATEGORY_ATTRIBUTES
        # ...and the SEO title now signs off with the OTHER store
        assert app.seo_title("Acme 1.5 Ton Split AC").endswith("Acme Appliances")
    finally:
        app.configure("shreejee")

    after = (app.CLIENT.slug, app.MEDIA_BASE, app.SEO_SITE_NAME,
             app.CATEGORY_MAP_FILE, app.IMAGE_DIR)
    assert after == before


# A second store with a DIFFERENT prefix scheme: nothing shreejee-shaped in
# it, so a leaked literal ('AP', 'WM-') shows up as a failure here.
ALT_SKU_TABLE = """
[sku]
fallback_prefix = "QQ"
never_rename = ["ZZ"]
strip_codes = ["DW"]

[sku.prefix_by_parent]
"Washing Machines" = "WM"
"LED" = "TV"

[sku.input_codes]
REF = ["Home Appliances", "Refrigerators"]
"""


def test_app_and_validate_configure_swap_the_whole_sku_scheme(two_clients):
    """PRODUCT_PLAN item 2: prefixes come from the client, not from literals.

    prefixed_sku's never-rename guard, its fallback code and validate_csv's
    stripping regex must all follow configure() - and snap back when the
    original client is restored.
    """
    import app
    import validate_csv as V

    zeta = two_clients / "zeta"
    zeta.mkdir()
    for name in ("category_map.xlsx", "category_seo.xlsx",
                 "SubCategory_Attributes_Master.xlsx"):
        shutil.copy2(two_clients / "acme" / name, zeta / name)
    (zeta / "client.toml").write_text(
        CLIENT_HEAD.format(slug="zeta")
        + '\n[taxonomy]\ncategory_map = "category_map.xlsx"\n'
          'category_seo = "category_seo.xlsx"\n'
          'attributes_master = "SubCategory_Attributes_Master.xlsx"\n'
        + ALT_SKU_TABLE, encoding="utf-8")

    before = (dict(app.SKU_PREFIX_BY_PARENT), tuple(app.SKU_PREFIXES),
              dict(app.SKU_PREFIX_CATEGORY), tuple(app.SKU_NEVER_RENAME),
              app.SKU_FALLBACK_PREFIX, V.SKU_PREFIX_RE.pattern)
    try:
        app.configure("zeta")
        V.configure("zeta")
        assert app.SKU_NEVER_RENAME == ("ZZ-",)
        assert app.SKU_FALLBACK_PREFIX == "QQ"
        assert app.SKU_PREFIX_CATEGORY == {
            "REF": ("Home Appliances", "Refrigerators")}
        assert app.prefixed_sku("ZZ-1", "Washing Machines") == "ZZ-1"
        assert app.prefixed_sku("foo", "Nothing") == "QQ-foo"
        # WM- is NOT sacred for this client - only ZZ- is
        assert app.prefixed_sku("WM-1", "LED") == "TV-1"
        assert V.SKU_PREFIX_RE.pattern == "^(WM|TV|QQ)-"
        assert not V.SKU_PREFIX_RE.match("AP-X")
    finally:
        app.configure("shreejee")
        V.configure("shreejee")

    assert app.prefixed_sku("foo", "Nothing") == "AP-foo"
    assert app.prefixed_sku("WM-1", "LED") == "WM-1"      # live SKU, untouched
    assert V.SKU_PREFIX_RE.match("AP-X")
    assert (dict(app.SKU_PREFIX_BY_PARENT), tuple(app.SKU_PREFIXES),
            dict(app.SKU_PREFIX_CATEGORY), tuple(app.SKU_NEVER_RENAME),
            app.SKU_FALLBACK_PREFIX, V.SKU_PREFIX_RE.pattern) == before


def test_validate_csv_configure_swaps_the_placeholder(two_clients):
    import validate_csv as V

    before = (V.CLIENT.slug, V.PLACEHOLDER, V.IMAGE_DIR)
    try:
        V.configure("acme")
        assert V.PLACEHOLDER == "<p>Acme placeholder.</p>"
        assert V.IMAGE_DIR.endswith("acme")
    finally:
        V.configure("shreejee")
    assert (V.CLIENT.slug, V.PLACEHOLDER, V.IMAGE_DIR) == before


# ------------------------------------------------------------- the CLI flag
ENTRY_POINTS = ["build_csv.py", "validate_csv.py", "wc_publish.py",
                "wc_seo.py", "sku_enricher.py"]


@pytest.mark.parametrize("script", ENTRY_POINTS)
def test_entry_point_accepts_client_and_help(script):
    """Every non-GUI entry point takes --client. No network: --help only."""
    proc = subprocess.run(
        [EXE, str(ROOT / script), "--client", "shreejee", "--help"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=180,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert proc.returncode == 0, (
        f"{script} --client shreejee --help exited {proc.returncode}: "
        f"{proc.stderr[-500:]}")
    assert "--client" in (proc.stdout + proc.stderr), (
        f"{script} --help does not document --client")


def test_no_taxonomy_file_left_at_the_repo_root():
    """The move is the config: root must carry no per-client taxonomy."""
    for name in ("category_map.xlsx", "category_seo.xlsx",
                 "SubCategory_Attributes_Master.xlsx"):
        assert not (ROOT / name).exists(), (
            f"{name} is back at the repo root - it belongs in clients/<slug>/")


# ------------------------------------------------------- [sku] (item 2)
def test_sku_scheme_matches_todays_tables():
    """The one table must reproduce the copies it replaces, exactly."""
    sku = C.load_client("shreejee").sku
    assert sku.prefixes == ("WM", "WH", "TV", "AC", "CF", "KA", "HA", "SA",
                            "EC", "AP")
    assert set(sku.enricher_codes) == {
        "WM", "TV", "AC", "REF", "FRIDGE", "MWO", "DW", "WH", "GWH", "SM", "HWD",
        "IC", "ICT", "DI", "DF", "LED", "MW", "CF", "KA", "HA", "SA", "EC",
        "AP"}, "sku_enricher.CATEGORY_CODES drifted from the [sku] table"
    assert len(sku.enricher_codes) == len(set(sku.enricher_codes)), \
        "enricher_codes must be de-duplicated"
    assert sku.input_codes["REF"] == ("Home Appliances", "Refrigerators")
    assert sku.never_rename == ("WM", "WH")
    assert sku.fallback_prefix == "AP"
    assert sku.prefix_by_parent["Washing Machines"] == "WM"
    assert sku.strip_codes == ("DW", "LED", "MW")


def test_sku_scheme_rejects_a_bad_code(tmp_path, monkeypatch):
    body = SKU_TABLE.replace('"LED" = "TV"', '"LED" = "television"')
    make_client(tmp_path, monkeypatch, body)
    with pytest.raises(C.ClientConfigError) as excinfo:
        C.load_client("synth")
    assert "TELEVISION" in str(excinfo.value).upper()


def test_sku_scheme_rejects_a_duplicate_code(tmp_path, monkeypatch):
    body = SKU_TABLE.replace('"LED" = "TV"', '"LED" = "WM"')
    make_client(tmp_path, monkeypatch, body)
    with pytest.raises(C.ClientConfigError) as excinfo:
        C.load_client("synth")
    message = str(excinfo.value)
    assert "WM" in message and "LED" in message


def test_sku_scheme_rejects_fallback_colliding_with_a_parent(tmp_path,
                                                             monkeypatch):
    body = SKU_TABLE.replace('fallback_prefix = "AP"',
                             'fallback_prefix = "WM"')
    make_client(tmp_path, monkeypatch, body)
    with pytest.raises(C.ClientConfigError) as excinfo:
        C.load_client("synth")
    assert "WM" in str(excinfo.value)


def test_missing_sku_table_is_refused(tmp_path, monkeypatch):
    """No [sku] = the client would silently inherit someone else's prefixes."""
    make_client(tmp_path, monkeypatch, body="")
    with pytest.raises(C.ClientConfigError) as excinfo:
        C.load_client("synth")
    assert "[sku]" in str(excinfo.value)


def test_strip_code_cannot_also_be_an_input_code(tmp_path, monkeypatch):
    body = SKU_TABLE.replace('strip_codes = ["DW"]', 'strip_codes = ["REF"]')
    make_client(tmp_path, monkeypatch, body)
    with pytest.raises(C.ClientConfigError) as excinfo:
        C.load_client("synth")
    assert "REF" in str(excinfo.value)


# ---------------------------------------------------- [serper] (item 4)
def test_serper_defaults_when_the_table_is_absent(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    serper = C.load_client("synth").serper
    assert serper.api_key_env == "SERPER_API_KEY"
    assert serper.monthly_budget == 2500
    assert serper.default_run_budget == 0


def test_serper_values_come_from_the_toml(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch, SKU_TABLE + """
[serper]
api_key_env = "ACME_SERPER_KEY"
monthly_budget = 10000
default_run_budget = 150
""")
    serper = C.load_client("synth").serper
    assert serper.api_key_env == "ACME_SERPER_KEY"
    assert serper.monthly_budget == 10000
    assert serper.default_run_budget == 150


def test_shreejee_serper_table_is_the_documented_one():
    serper = C.load_client("shreejee").serper
    assert (serper.api_key_env, serper.monthly_budget,
            serper.default_run_budget) == ("SERPER_API_KEY", 2500, 0)
    # KI-36: the engine chain and the two fallback key env names
    assert serper.engines == ("serper", "tavily", "serpapi", "ddg")
    assert (serper.tavily_api_key_env, serper.serpapi_api_key_env) == (
        "TAVILY_API_KEY", "SERPAPI_API_KEY")


def test_serper_engine_defaults_when_absent(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    serper = C.load_client("synth").serper
    assert serper.engines == ("serper", "tavily", "serpapi", "ddg")
    assert serper.tavily_api_key_env == "TAVILY_API_KEY"
    assert serper.serpapi_api_key_env == "SERPAPI_API_KEY"


def test_serper_engines_come_from_the_toml(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch, SKU_TABLE + """
[serper]
engines = ["Tavily", "ddg"]
tavily_api_key_env = "ACME_TAVILY"
serpapi_api_key_env = "ACME_SERPAPI"
""")
    serper = C.load_client("synth").serper
    assert serper.engines == ("tavily", "ddg")
    assert serper.tavily_api_key_env == "ACME_TAVILY"
    assert serper.serpapi_api_key_env == "ACME_SERPAPI"


@pytest.mark.parametrize("value", ['["serper", "bing"]', "[]", "5"])
def test_unknown_or_empty_engine_list_is_refused(tmp_path, monkeypatch, value):
    make_client(tmp_path, monkeypatch, SKU_TABLE + f"""
[serper]
engines = {value}
""")
    with pytest.raises(C.ClientConfigError) as excinfo:
        C.load_client("synth")
    assert "engines" in str(excinfo.value)


def test_credits_and_env_are_paths_only(tmp_path, monkeypatch):
    cfg_dir = make_client(tmp_path, monkeypatch)
    cfg = C.load_client("synth")
    assert cfg.credits_file == cfg_dir / "credits.json"
    assert cfg.env_file == cfg_dir / ".env"
    assert not cfg.credits_file.exists() and not cfg.env_file.exists(), \
        "loading a config must not create the ledger or the .env"


# ------------------------------------------------ per-client .env (item 3)
def test_load_env_parses_quotes_export_and_comments(tmp_path, monkeypatch):
    cfg_dir = make_client(tmp_path, monkeypatch)
    (cfg_dir / ".env").write_text(
        "# a comment\n"
        "\n"
        "WC_URL=https://synth.invalid\n"
        '  WC_KEY = "ck_quoted"  \n'
        "WC_SECRET='cs_single'\n"
        "export WP_APP_USER=exported\n"
        "EMPTY=\n",
        encoding="utf-8")
    values = C.load_env(C.load_client("synth"))
    assert values == {"WC_URL": "https://synth.invalid",
                      "WC_KEY": "ck_quoted",
                      "WC_SECRET": "cs_single",
                      "WP_APP_USER": "exported",
                      "EMPTY": ""}


def test_load_env_missing_file_is_empty(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    assert C.load_env(C.load_client("synth")) == {}


def test_load_env_malformed_line_names_the_line_number(tmp_path, monkeypatch):
    cfg_dir = make_client(tmp_path, monkeypatch)
    (cfg_dir / ".env").write_text(
        "WC_URL=https://synth.invalid\n"
        "this line has no equals sign\n",
        encoding="utf-8")
    with pytest.raises(C.ClientConfigError) as excinfo:
        C.load_env(C.load_client("synth"))
    assert "line 2" in str(excinfo.value)


def test_apply_env_overrides_an_existing_key(tmp_path, monkeypatch):
    """A stale shell export must never beat the client's own file."""
    cfg_dir = make_client(tmp_path, monkeypatch)
    (cfg_dir / ".env").write_text(
        "WC_URL=https://synth.invalid\nWC_KEY=ck_new\n", encoding="utf-8")
    environ = {"WC_URL": "https://STALE.invalid", "KEEP": "untouched"}
    applied = C.apply_env(C.load_client("synth"), environ=environ)
    assert applied == {"WC_URL": "https://synth.invalid", "WC_KEY": "ck_new"}
    assert environ["WC_URL"] == "https://synth.invalid"
    assert environ["KEEP"] == "untouched"
    assert os.environ.get("WC_KEY") != "ck_new", \
        "apply_env with an explicit mapping must not touch os.environ"


# ------------------------------------------- store-URL comparison (item 3)
@pytest.mark.parametrize("a, b, expected", [
    ("https://www.Shop.in/", "http://shop.in", True),
    ("https://shreejeeelectronics.in", "shreejeeelectronics.in/", True),
    ("https://shop.in/wp-json/wc/v3?x=1", "https://www.shop.in", True),
    ("  https://SHOP.in  ", "https://shop.in", True),
    ("https://shop.in", "https://other.in", False),
    ("https://shop.in", "https://staging.shop.in", False),
    ("", "https://shop.in", False),
    ("", "", False),
    (None, None, False),
])
def test_same_store_truth_table(a, b, expected):
    assert C.same_store(a, b) is expected


def test_store_host_strips_scheme_www_and_path():
    assert C.store_host("https://www.Shop.in/wp-json/wc/v3/") == "shop.in"
    assert C.store_host("") == ""


# ------------------------------------------------------------------ runner
if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-header",
                                  "-p", "no:cacheprovider"]))


# ---------------------------------------- a broken client.toml is not offered

def _break_client(clients_dir, slug="nosku"):
    """A client dir whose client.toml EXISTS but cannot be parsed."""
    bad = pathlib.Path(clients_dir) / slug
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "client.toml").write_text("this is not = = toml\n",
                                     encoding="utf-8")
    return bad


def test_available_clients_hides_a_broken_toml_and_broken_clients_names_it(
        tmp_path, monkeypatch):
    """A toml that exists but does not load must not be offered as a choice:
    it is picked, and only THEN blows up. It is reported instead."""
    make_client(tmp_path, monkeypatch)                  # 'synth' loads
    _break_client(tmp_path)
    assert C.available_clients() == ["synth"], C.available_clients()
    broken = C.broken_clients()
    assert list(broken) == ["nosku"], broken
    assert "nosku" in broken["nosku"], broken["nosku"]


def test_cli_client_help_lists_usable_and_broken_clients(tmp_path,
                                                         monkeypatch):
    """--client help names the broken ones too, with the reason - otherwise
    their absence reads as 'no such client' and the slug gets retyped."""
    import argparse
    make_client(tmp_path, monkeypatch)
    _break_client(tmp_path)
    parser = argparse.ArgumentParser()
    C.cli_client(parser)
    text = " ".join(parser.format_help().split())       # argparse wraps
    assert "Available: synth" in text, text
    assert "broken: nosku" in text, text


def test_a_bare_client_flag_raises_instead_of_falling_through(monkeypatch):
    """'--client --limit 5' is a typo. Falling through to $PIPELINE_CLIENT
    would silently build for a different store."""
    monkeypatch.setenv(C.ENV_VAR, "fromenv")
    with pytest.raises(C.ClientConfigError) as exc:
        C.resolve_slug(argv=["--client"])
    assert "--client needs a value" in str(exc.value), str(exc.value)
    with pytest.raises(C.ClientConfigError):
        C.resolve_slug(argv=["--client", "--limit", "3"])
    # an explicit slug still short-circuits before argv is looked at
    assert C.resolve_slug(argv=["--client"], explicit="synth") == "synth"
