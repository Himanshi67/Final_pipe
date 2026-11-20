"""Per-client configuration (PRODUCT_PLAN Phase 1, item 1).

Every store-specific value the pipeline used to hard-code for Shree Jee lives
in ONE place per client: `clients/<slug>/client.toml` plus the taxonomy
workbooks next to it. Every entry point takes `--client <slug>`.

Design contract: docs/CLIENT_CONFIG.md. Anchored on `Path(__file__).parent`
like the other root scripts, so `clients/` is found however the process was
started (KI: moving a .py out of root silently relocates the dir it owns).

Resolution order is deliberately env-first for subprocesses:

    explicit > "--client X" / "--client=X" in argv > $PIPELINE_CLIENT > DEFAULT_CLIENT

gui.py sets PIPELINE_CLIENT in the environment it hands to sku_enricher.py /
wc_publish.py, and in its own os.environ BEFORE the first `import app`, so
app.py's import-time constants see the right client.

Loading NEVER creates a directory as a side effect - a caller that writes
(image downloads, the category-map editor's backups) does its own mkdir.
The same goes for `credits_file` / `env_file`: they are paths, nothing more.

Public API:

    DEFAULT_CLIENT · ROOT · CLIENTS_DIR · ENV_VAR · IMAGE_ROOT_NAME
    ClientConfigError
    ClientConfig(slug, name, dir, store_url, media_base_template,
                 seo_site_name, placeholder_html, category_map, category_seo,
                 attributes_master, image_dir, backups_dir, sku, serper,
                 url_index)
        .media_base(month=None) -> str
        .credits_file -> Path            clients/<slug>/credits.json
        .env_file     -> Path            clients/<slug>/.env
    SkuScheme(prefix_by_parent, fallback_prefix, never_rename, input_codes,
              strip_codes)                          [sku]    - item 2
        .prefixes        -> tuple  parent codes in TOML order + fallback
        .enricher_codes  -> tuple  input_codes + prefixes + strip_codes
    SerperConfig(api_key_env, monthly_budget, default_run_budget,
                 query_mode, search_order)          [serper] - item 4
    UrlIndexConfig(enabled, max_age_days, extra_sources, disable)
                                                    [url_index] - tier 0
    PricingConfig(single_price, basis, markup)      [pricing] - one price only
    one_price(regular, sale, pricing)               the one-price rule
    available_clients() · broken_clients() · resolve_slug(argv, explicit)
    load_client(slug, argv)
    load_env(cfg) -> dict                           clients/<slug>/.env
    apply_env(cfg, environ=None) -> dict            overrides existing keys
    store_host(url) -> str · same_store(a, b) -> bool        - item 3
    cli_client(parser)
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    import tomllib                       # Python 3.11+
except ModuleNotFoundError:              # pragma: no cover - 3.10 and older
    import tomli as tomllib              # type: ignore[no-redef]


DEFAULT_CLIENT = "shreejee"
ROOT = Path(__file__).resolve().parent
CLIENTS_DIR = ROOT / "clients"
IMAGE_ROOT_NAME = "product_images_for_upload"
ENV_VAR = "PIPELINE_CLIENT"


class ClientConfigError(Exception):
    """Unknown slug, unreadable client.toml, or a taxonomy file that is gone."""


# --------------------------------------------------------------- [sku]
# Output-side codes (what gets written into a WooCommerce SKU) are short and
# uniform; input-side codes are whatever an operator or an ERP types in front
# of a raw SKU, so they may be longer ("FRIDGE").
_OUT_CODE_RE = re.compile(r"^[A-Z]{2,3}$")
_IN_CODE_RE = re.compile(r"^[A-Z]{2,}$")


@dataclass(frozen=True)
class SkuScheme:
    """The single source of truth for SKU prefixes (PRODUCT_PLAN item 2).

    Replaces app.SKU_PREFIX_BY_PARENT / SKU_PREFIXES / SKU_PREFIX_CATEGORY,
    validate_csv.SKU_PREFIX_RE, gui_helpers.SKU_CODES and
    sku_enricher.CATEGORY_CODES, which used to drift independently.
    """
    prefix_by_parent: dict          # taxonomy parent -> emitted code
    fallback_prefix: str            # anything with no parent match
    never_rename: tuple             # codes already live: prefixed_sku leaves them
    input_codes: dict               # code -> (parent, sub) taxonomy hint
    strip_codes: tuple              # tokens stripped before search, no hint

    @property
    def prefixes(self) -> tuple:
        """Every code the builder may emit: parents in TOML order + fallback."""
        out = []
        for code in list(self.prefix_by_parent.values()) + [self.fallback_prefix]:
            if code and code not in out:
                out.append(code)
        return tuple(out)

    @property
    def enricher_codes(self) -> tuple:
        """Every token the enricher recognises at the front of a raw SKU."""
        out = []
        for code in (list(self.input_codes) + list(self.prefixes)
                     + list(self.strip_codes)):
            if code and code not in out:
                out.append(code)
        return tuple(out)


@dataclass(frozen=True)
class SerperConfig:
    """[serper] - BYOK key env var and the credit budgets (item 4)."""
    api_key_env: str = "SERPER_API_KEY"
    monthly_budget: int = 2500          # informational (free tier)
    default_run_budget: int = 0         # 0 = unlimited
    # "incremental": send a tier's uncached queries one at a time and stop
    # once a complete listing is verified; "batch": send the whole tier first
    query_mode: str = "incremental"
    # "paid_first": serper, DuckDuckGo only as a fallback; "free_first":
    # DuckDuckGo first, serper only when DuckDuckGo gives nothing
    search_order: str = "paid_first"


SERPER_QUERY_MODES = ("incremental", "batch")
SERPER_SEARCH_ORDERS = ("paid_first", "free_first")


@dataclass(frozen=True)
class UrlIndexConfig:
    """[url_index] - tier 0 of the URL search (url_index.py): the sitemap
    index and retailer site search that run before any paid search."""
    enabled: bool = True
    max_age_days: float = 7.0          # auto-refresh sources older than this
    extra_sources: tuple = ()          # [[url_index.extra_sources]] tables (dicts)
    disable: tuple = ()                # source / site-search names switched off


@dataclass(frozen=True)
class PricingConfig:
    """[pricing] - how the build output states a product's price.

    single_price = false (default): 'regular price' is the MRP (struck
    through) and 'sale price' the selling price, as scraped.
    single_price = true: ONE price only, in 'regular price'; 'sale price' =
    empty. No discount, "save" or "% off" text is written. The enricher's
    _filled.xlsx still records both values; only the build output is
    single-price. Which price: see `one_price()`.
      basis = "higher" (code default): the higher of MRP and selling price.
      basis = "lower": the lower price + `markup` rupees, capped at the
        higher price. A row with only one price ships it unchanged.
      markup (code default 0): rupees added to the lower price.
    The defaults keep a client that sets only `single_price = true` on the
    behaviour it had before basis/markup existed.
    """
    single_price: bool = False
    basis: str = "higher"
    markup: float = 0


PRICE_BASES = ("higher", "lower")


@dataclass(frozen=True)
class ClientConfig:
    slug: str
    name: str
    dir: Path
    store_url: str
    media_base_template: str
    seo_site_name: str
    placeholder_html: str
    category_map: Path
    category_seo: Path
    attributes_master: Path
    image_dir: Path
    backups_dir: Path
    sku: SkuScheme
    serper: SerperConfig
    url_index: UrlIndexConfig = field(default_factory=UrlIndexConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)

    @property
    def credits_file(self) -> Path:
        """clients/<slug>/credits.json - the credit ledger (item 4). Never
        created here; sku_enricher.Ledger writes it (tmp + os.replace)."""
        return self.dir / "credits.json"

    @property
    def env_file(self) -> Path:
        """clients/<slug>/.env - this client's credentials (item 3). A path
        only: load_env() reads it if it happens to exist."""
        return self.dir / ".env"

    def media_base(self, month=None) -> str:
        """Render `media_base_template` for a 'YYYY/MM' month (default: now).

        A blank/None month means the current month, which is what every entry
        point did before this existed.
        """
        text = str(month or "").strip().strip("/")
        if text:
            parts = [p for p in text.replace("-", "/").split("/") if p]
        else:
            parts = []
        if len(parts) >= 2:
            yyyy, mm = parts[0], parts[1]
        else:
            now = datetime.now()
            yyyy, mm = f"{now:%Y}", f"{now:%m}"
        return (self.media_base_template
                .replace("{url}", self.store_url.rstrip("/"))
                .replace("{yyyy}", yyyy)
                .replace("{mm}", mm))


_SCANNING = False


def _scan_clients():
    """(usable slugs, {broken slug: why}) for every dir under clients/.

    A client.toml that EXISTS but does not load is worse than useless in a
    picker: it is offered, chosen, and only then blows up. So the scan
    actually loads each one and splits the two groups. load_client() only
    calls back in here when the toml is MISSING - which this scan never
    asks it about - and `_SCANNING` closes that door anyway."""
    global _SCANNING
    try:
        entries = sorted(p for p in CLIENTS_DIR.iterdir() if p.is_dir())
    except (OSError, AttributeError):
        return [], {}
    if _SCANNING:                       # re-entrant call: names only
        return [p.name for p in entries
                if (p / "client.toml").is_file()], {}
    good, broken = [], {}
    _SCANNING = True
    try:
        for entry in entries:
            if not (entry / "client.toml").is_file():
                continue
            try:
                load_client(entry.name)
            except ClientConfigError as exc:
                broken[entry.name] = str(exc)
            except Exception as exc:    # a corrupt workbook, a bad mode...
                broken[entry.name] = f"{type(exc).__name__}: {exc}"
            else:
                good.append(entry.name)
    finally:
        _SCANNING = False
    return good, broken


def available_clients():
    """Slugs under clients/ whose client.toml loads. Broken ones are NOT
    listed - see broken_clients() for those, with the reason."""
    return _scan_clients()[0]


def broken_clients():
    """{slug: why} for every clients/<slug>/client.toml that fails to load."""
    return _scan_clients()[1]


def _argv_client(argv):
    """'--client X' or '--client=X', last one wins (argparse semantics).

    A `--client` with no slug after it (`--client --limit 5`) is a typo, not
    a request for the default: it raises rather than silently falling
    through to $PIPELINE_CLIENT, which is what argparse would do too."""
    found = None
    items = list(argv or [])
    for i, arg in enumerate(items):
        if arg == "--client":
            candidate = items[i + 1] if i + 1 < len(items) else ""
            if not candidate or candidate.startswith("-"):
                raise ClientConfigError(
                    "--client needs a value: a client slug must follow it "
                    f"(available: {', '.join(available_clients()) or '(none)'}).")
            found = candidate
        elif arg.startswith("--client="):
            found = arg.split("=", 1)[1]
    return (found or "").strip() or None


def resolve_slug(argv=None, explicit=None):
    """explicit > argv --client > $PIPELINE_CLIENT > DEFAULT_CLIENT."""
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    from_argv = _argv_client(sys.argv[1:] if argv is None else argv)
    if from_argv:
        return from_argv
    from_env = (os.environ.get(ENV_VAR) or "").strip()
    if from_env:
        return from_env
    return DEFAULT_CLIENT


def _require(table, path, what):
    node = table
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            raise ClientConfigError(
                f"{what}: required key '{path}' is missing from client.toml")
        node = node[key]
    text = str(node or "").strip()
    if not text:
        raise ClientConfigError(
            f"{what}: required key '{path}' is empty in client.toml")
    return text


def _code_list(raw, what, where):
    """A TOML array of code strings -> tuple, uppercased and stripped."""
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise ClientConfigError(
            f"{where}: [sku] {what} must be a list of codes, got {raw!r}")
    return tuple(str(item or "").strip().upper() for item in raw)


def _check_code(code, what, where, pattern=_OUT_CODE_RE):
    if not code or not pattern.match(code):
        shape = ("2-3 uppercase letters" if pattern is _OUT_CODE_RE
                 else "uppercase letters only")
        raise ClientConfigError(
            f"{where}: [sku] {what} '{code}' is not a valid code "
            f"({shape}).")
    return code


def _load_sku_scheme(data, where) -> SkuScheme:
    """Parse and validate the [sku] table. Required: a client that does not
    state its scheme would silently inherit someone else's prefixes."""
    table = data.get("sku")
    if not isinstance(table, dict) or not table:
        raise ClientConfigError(
            f"{where}: client.toml has no [sku] table. Every client must "
            f"state its own prefix scheme (prefix_by_parent, fallback_prefix, "
            f"never_rename, input_codes, strip_codes) - see "
            f"docs/CLIENT_CONFIG.md item 2.")

    raw_parents = table.get("prefix_by_parent")
    if not isinstance(raw_parents, dict) or not raw_parents:
        raise ClientConfigError(
            f"{where}: [sku] prefix_by_parent must be a non-empty "
            f"parent -> code table.")

    prefix_by_parent = {}
    seen = {}
    for parent, code in raw_parents.items():
        parent = str(parent or "").strip()
        code = str(code or "").strip().upper()
        _check_code(code, f"prefix_by_parent['{parent}']", where)
        if code in seen:
            raise ClientConfigError(
                f"{where}: [sku] prefix code '{code}' is used by two parents "
                f"('{seen[code]}' and '{parent}'). Codes must be unique - a "
                f"duplicate makes the SKU ambiguous.")
        seen[code] = parent
        prefix_by_parent[parent] = code

    fallback = str(table.get("fallback_prefix") or "").strip().upper()
    _check_code(fallback, "fallback_prefix", where)
    if fallback in seen:
        raise ClientConfigError(
            f"{where}: [sku] fallback_prefix '{fallback}' is already the code "
            f"for parent '{seen[fallback]}'. The fallback must be distinct, "
            f"or an unmapped product looks like a mapped one.")

    never_rename = _code_list(table.get("never_rename"), "never_rename", where)
    for code in never_rename:
        _check_code(code, "never_rename", where)
    if len(set(never_rename)) != len(never_rename):
        raise ClientConfigError(
            f"{where}: [sku] never_rename lists a code twice: {never_rename}")

    raw_inputs = table.get("input_codes") or {}
    if not isinstance(raw_inputs, dict):
        raise ClientConfigError(
            f"{where}: [sku] input_codes must be a code -> [parent, sub] table.")
    input_codes = {}
    for code, hint in raw_inputs.items():
        code = str(code or "").strip().upper()
        _check_code(code, "input_codes", where, _IN_CODE_RE)
        if code in input_codes:
            raise ClientConfigError(
                f"{where}: [sku] input_codes lists '{code}' twice.")
        if isinstance(hint, str) or not isinstance(hint, (list, tuple)):
            raise ClientConfigError(
                f"{where}: [sku] input_codes['{code}'] must be a "
                f"[parent, sub] pair, got {hint!r}")
        pair = [str(x or "").strip() for x in hint] + ["", ""]
        input_codes[code] = (pair[0], pair[1])

    strip_codes = _code_list(table.get("strip_codes"), "strip_codes", where)
    for code in strip_codes:
        _check_code(code, "strip_codes", where, _IN_CODE_RE)
    if len(set(strip_codes)) != len(strip_codes):
        raise ClientConfigError(
            f"{where}: [sku] strip_codes lists a code twice: {strip_codes}")
    for code in strip_codes:
        if code in input_codes:
            raise ClientConfigError(
                f"{where}: [sku] '{code}' is in both input_codes and "
                f"strip_codes. A code either carries a taxonomy hint or it "
                f"does not - pick one.")

    return SkuScheme(
        prefix_by_parent=prefix_by_parent,
        fallback_prefix=fallback,
        never_rename=never_rename,
        input_codes=input_codes,
        strip_codes=strip_codes,
    )


def _load_serper(data, where) -> SerperConfig:
    """Parse the optional [serper] table; every key has a working default."""
    table = data.get("serper") or {}
    if not isinstance(table, dict):
        raise ClientConfigError(f"{where}: [serper] must be a table.")
    defaults = SerperConfig()

    def _int(key, default):
        raw = table.get(key, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ClientConfigError(
                f"{where}: [serper] {key} must be a whole number, "
                f"got {raw!r}") from None
        if value < 0:
            raise ClientConfigError(
                f"{where}: [serper] {key} must not be negative, got {value}")
        return value

    def _choice(key, default, allowed):
        raw = str(table.get(key, default) or default).strip().lower()
        if raw not in allowed:
            raise ClientConfigError(
                f"{where}: [serper] {key} must be one of "
                f"{', '.join(allowed)}, got {table.get(key)!r}")
        return raw

    return SerperConfig(
        api_key_env=(str(table.get("api_key_env") or "").strip()
                     or defaults.api_key_env),
        monthly_budget=_int("monthly_budget", defaults.monthly_budget),
        default_run_budget=_int("default_run_budget",
                                defaults.default_run_budget),
        query_mode=_choice("query_mode", defaults.query_mode,
                           SERPER_QUERY_MODES),
        search_order=_choice("search_order", defaults.search_order,
                             SERPER_SEARCH_ORDERS),
    )


def _load_url_index(data, where) -> UrlIndexConfig:
    """Parse the optional [url_index] table; every key has a working default.
    Source entries are only shape-checked here - url_index.load_config()
    validates their keys when it builds the source list."""
    table = data.get("url_index") or {}
    if not isinstance(table, dict):
        raise ClientConfigError(f"{where}: [url_index] must be a table.")
    defaults = UrlIndexConfig()
    enabled = table.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        raise ClientConfigError(
            f"{where}: [url_index] enabled must be true or false, got {enabled!r}")
    raw_age = table.get("max_age_days", defaults.max_age_days)
    try:
        max_age = float(raw_age)
    except (TypeError, ValueError):
        raise ClientConfigError(
            f"{where}: [url_index] max_age_days must be a number, "
            f"got {raw_age!r}") from None
    if max_age < 0:
        raise ClientConfigError(
            f"{where}: [url_index] max_age_days must not be negative, got {max_age}")
    extra = table.get("extra_sources", [])
    if not isinstance(extra, list) or not all(
            isinstance(e, dict) and str(e.get("name") or "").strip() for e in extra):
        raise ClientConfigError(
            f"{where}: [url_index] extra_sources must be a list of tables, "
            f"each with a name.")
    disable = table.get("disable", [])
    if not isinstance(disable, list) or not all(isinstance(d, str) for d in disable):
        raise ClientConfigError(
            f"{where}: [url_index] disable must be a list of source names.")
    return UrlIndexConfig(
        enabled=enabled,
        max_age_days=max_age,
        extra_sources=tuple(dict(e) for e in extra),
        disable=tuple(d.strip() for d in disable if d.strip()),
    )


def _load_pricing(data, where) -> PricingConfig:
    """Parse the optional [pricing] table; every key has a working default."""
    table = data.get("pricing") or {}
    if not isinstance(table, dict):
        raise ClientConfigError(f"{where}: [pricing] must be a table.")
    default = PricingConfig()
    single = table.get("single_price", default.single_price)
    if not isinstance(single, bool):
        raise ClientConfigError(
            f"{where}: [pricing] single_price must be true or false, "
            f"got {single!r}")
    basis = table.get("basis", default.basis)
    if basis not in PRICE_BASES:
        raise ClientConfigError(
            f"{where}: [pricing] basis must be one of "
            f"{', '.join(repr(b) for b in PRICE_BASES)}, got {basis!r}")
    markup = table.get("markup", default.markup)
    if (isinstance(markup, bool) or not isinstance(markup, (int, float))
            or markup < 0):
        raise ClientConfigError(
            f"{where}: [pricing] markup must be a number >= 0 (rupees), "
            f"got {markup!r}")
    return PricingConfig(single_price=single, basis=basis, markup=markup)


def _positive_number(raw):
    """float(raw) when raw is a number > 0, else None ('' / None / text)."""
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        number = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:          # NaN or not positive
        return None
    return number


def one_price(regular_price, sale_price, pricing=None):
    """The ONE price a single-price build ships, from a row's MRP
    (`regular_price`) and selling price (`sale_price`). None when neither is a
    positive number. This is the ONLY definition of the rule: app.py,
    validate_csv.py, wc_publish.py and wc_seo.py all call it.

    - Only one price (or both equal): that price, unchanged - no markup.
    - Two different prices, basis "higher": the higher one.
    - Two different prices, basis "lower": the lower one + `markup`, capped at
      the higher one (never above the MRP).
    """
    pricing = pricing if pricing is not None else PricingConfig()
    values = [v for v in (_positive_number(regular_price),
                          _positive_number(sale_price)) if v is not None]
    if not values:
        return None
    high, low = max(values), min(values)
    if high == low:
        return high
    if pricing.basis == "lower":
        return min(low + float(pricing.markup or 0), high)
    return high


def load_client(slug=None, argv=None) -> ClientConfig:
    """Read clients/<slug>/client.toml. Raises ClientConfigError, never
    half-loads, and does not create anything on disk."""
    slug = resolve_slug(argv=argv, explicit=slug)
    client_dir = CLIENTS_DIR / slug
    toml_path = client_dir / "client.toml"
    if not toml_path.is_file():
        known = ", ".join(available_clients()) or "(none)"
        raise ClientConfigError(
            f"Unknown client '{slug}': no {toml_path}. "
            f"Available clients: {known}. "
            f"Add one as a directory under {CLIENTS_DIR}.")
    try:
        with open(toml_path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:
        raise ClientConfigError(
            f"Could not read {toml_path}: {type(exc).__name__}: {exc}") from exc

    where = f"client '{slug}'"
    declared = _require(data, "client.slug", where)
    if declared != slug:
        raise ClientConfigError(
            f"{toml_path}: client.slug is '{declared}' but the directory is "
            f"'{slug}'. They must match, or a --client that names the folder "
            f"would load someone else's store.")
    store_url = _require(data, "store.url", where).rstrip("/")

    client = data.get("client", {})
    store = data.get("store", {})
    taxonomy = data.get("taxonomy", {})

    def _tax(key, default):
        return client_dir / str(taxonomy.get(key) or default).strip()

    category_map = _tax("category_map", "category_map.xlsx")
    category_seo = _tax("category_seo", "category_seo.xlsx")
    attributes_master = _tax("attributes_master",
                             "SubCategory_Attributes_Master.xlsx")
    if not category_map.is_file():
        raise ClientConfigError(
            f"{where}: taxonomy file not found: {category_map}. app.py cannot "
            f"map a single category without it.")

    return ClientConfig(
        slug=slug,
        name=str(client.get("name") or slug).strip(),
        dir=client_dir,
        store_url=store_url,
        media_base_template=str(
            store.get("media_base")
            or "{url}/wp-content/uploads/{yyyy}/{mm}").strip(),
        seo_site_name=str(store.get("seo_site_name")
                          or client.get("name") or slug).strip(),
        placeholder_html=str(store.get("placeholder_html") or "").strip(),
        category_map=category_map,
        category_seo=category_seo,
        attributes_master=attributes_master,
        image_dir=ROOT / IMAGE_ROOT_NAME / slug,
        backups_dir=client_dir / "backups",
        sku=_load_sku_scheme(data, where),
        serper=_load_serper(data, where),
        url_index=_load_url_index(data, where),
        pricing=_load_pricing(data, where),
    )


# ------------------------------------------------------ per-client .env
def load_env(cfg) -> dict:
    """Parse clients/<slug>/.env into a dict (no python-dotenv).

    KEY=VALUE per line; `#` comments and blank lines ignored; a leading
    `export ` tolerated; surrounding single or double quotes stripped;
    whitespace stripped around key and value. A line that is neither blank,
    a comment, nor KEY=VALUE is an error naming its number - a typo in a
    credentials file must not silently publish to the wrong store.
    Missing file -> {}.
    """
    path = Path(cfg.env_file)
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ClientConfigError(f"Could not read {path}: {exc}") from exc

    out = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            raise ClientConfigError(
                f"{path} line {number}: expected KEY=VALUE, got {raw.strip()!r}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ClientConfigError(
                f"{path} line {number}: empty key before '='")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def apply_env(cfg, environ=None) -> dict:
    """Inject load_env(cfg) into os.environ (or `environ`), OVERRIDING.

    A stale `set WC_URL=...` in the operator's shell must never win over the
    client's own file - that is exactly how a publish lands on the wrong
    store. Returns the dict that was applied.
    """
    values = load_env(cfg)
    target = os.environ if environ is None else environ
    for key, value in values.items():
        target[key] = value
    return values


# -------------------------------------------------- store-URL comparison
def store_host(url) -> str:
    """Bare comparable host: no scheme, no `www.`, no path/query, no slash."""
    text = str(url or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.lstrip("/")
    for sep in ("/", "?", "#"):
        text = text.split(sep, 1)[0]
    if "@" in text:                      # user:pass@host
        text = text.rsplit("@", 1)[1]
    text = text.strip().strip(".")
    if text.startswith("www."):
        text = text[4:]
    return text


def same_store(a, b) -> bool:
    """True when two URLs name the same WooCommerce store. Empty -> False."""
    host_a, host_b = store_host(a), store_host(b)
    return bool(host_a) and host_a == host_b


def cli_client(parser):
    """Add --client to an argparse parser."""
    usable, broken = _scan_clients()
    known = ", ".join(usable) or DEFAULT_CLIENT
    # a broken client is named too, with the reason - otherwise its absence
    # from the list reads as "no such client" and the operator retypes the
    # slug instead of fixing the toml
    bad = ("  broken: "
           + "; ".join(f"{slug} ({why})" for slug, why in broken.items())
           if broken else "")
    parser.add_argument(
        "--client", default=None, metavar="SLUG",
        help=f"Which client's config to use (clients/<slug>/client.toml). "
             f"Available: {known}.{bad} Falls back to ${ENV_VAR}, "
             f"then '{DEFAULT_CLIENT}'.")
