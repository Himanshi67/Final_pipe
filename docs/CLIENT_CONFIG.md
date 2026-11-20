# Per-client configuration (`clients/<slug>/`) — design contract

PRODUCT_PLAN Phase 1, item 1. Goal: a second client cannot leak into or break the first.
Every store-specific value the pipeline used to hard-code for Shree Jee lives in ONE
place per client, and every entry point takes `--client <slug>`.

## Layout

    clients/
      shreejee/
        client.toml                        the contract below
        category_map.xlsx                  taxonomy (was at repo root)
        category_seo.xlsx                  curated tags + SEO keywords (was at repo root)
        SubCategory_Attributes_Master.xlsx attributes master: SOURCE of the filter groups and
                                           the sub-category attribute lists (app.py reads it;
                                           category_map.xlsx copies are the fallback)
        backups/                           category-map editor backups (gitignored)
        .env                               WC_*/WP_*/SERPER_* for this client (gitignored, Phase 1 item 3)
        credits.json                       serper ledger (gitignored, Phase 1 item 4)

`*.xlsx` stays globally ignored; `.gitignore` un-ignores the three taxonomy files under
`clients/*/`. Root no longer carries any taxonomy file.

## `client.toml`

```toml
[client]
slug = "shreejee"                       # must equal the directory name
name = "Shree Jee Electronics"          # human label (GUI titles, reports)

[store]
url = "https://shreejeeelectronics.in"  # no trailing slash; must equal WC_URL at publish time (item 3)
media_base = "{url}/wp-content/uploads/{yyyy}/{mm}"   # template; {yyyy}/{mm} = the --month value
seo_site_name = "Shree Jee Electronics" # app.SEO_SITE_NAME
placeholder_html = "<p>Premium product available at Shree Jee Electronics. Detailed specifications currently updating.</p>"
                                        # validate_csv.PLACEHOLDER — the EXACT current literal

[taxonomy]
category_map = "category_map.xlsx"      # relative to the client dir
category_seo = "category_seo.xlsx"
attributes_master = "SubCategory_Attributes_Master.xlsx"

# [sku]     — Phase 1 item 2 (single-source prefix scheme) adds this table
# [serper]  — Phase 1 item 4 (BYOK key env name + budget) adds this table
```

Unknown keys are ignored; missing required keys (`client.slug`, `store.url`) raise.

## `client_config.py` (root script — anchored on `Path(__file__).parent` like the others)

```python
DEFAULT_CLIENT = "shreejee"
CLIENTS_DIR = Path(__file__).parent / "clients"

class ClientConfigError(Exception): ...

@dataclass(frozen=True)
class ClientConfig:
    slug: str; name: str; dir: Path
    store_url: str; media_base_template: str; seo_site_name: str; placeholder_html: str
    category_map: Path; category_seo: Path; attributes_master: Path
    image_dir: Path          # <repo>/product_images_for_upload/<slug>
    backups_dir: Path        # <client dir>/backups
    def media_base(self, month: str | None = None) -> str   # month "YYYY/MM", default = today

def available_clients() -> list[str]            # dirs under clients/ whose client.toml LOADS
def broken_clients() -> dict[str, str]          # {slug: why} for the ones that do not
def resolve_slug(argv=None, explicit=None) -> str
    # explicit > "--client X" / "--client=X" in argv (default sys.argv) > env PIPELINE_CLIENT > DEFAULT_CLIENT
def load_client(slug=None, argv=None) -> ClientConfig   # raises ClientConfigError (unknown slug, bad toml, missing files)
def cli_client(parser) -> None                  # adds --client to an argparse parser (help lists the usable clients,
                                                #   then 'broken: <slug> (<why>)'; a bare --client with no slug raises)
```

The GUI resolves its client from `$PIPELINE_CLIENT` ONLY - it calls
`resolve_slug(argv=[])`, because under `streamlit run gui.py` argv holds
Streamlit's own flags, and a stray `--client` there would be Streamlit's.
An unknown value (a typo, a deleted client, a `client.toml` that no longer
loads) is NOT silently replaced by whichever client sorts first: the
selector falls back to `DEFAULT_CLIENT` and shows a sidebar warning naming
the value it ignored, and clients whose toml fails to load are listed,
with the reason, in a caption under the selector instead of being offered.

Resolution is deliberately env-first for subprocesses: `gui.py` sets
`PIPELINE_CLIENT=<slug>` in the env it hands to `sku_enricher.py` / `wc_publish.py`, and
sets it in its own `os.environ` BEFORE the first `import app`, so app.py's import-time
constants see the right client.

## What reads it (Phase 1 item 1 scope)

| Consumer | Before | After |
|---|---|---|
| `app.MEDIA_BASE` / `build_media_base` | literal shreejee URL | `CLIENT.media_base(month)` |
| `app.SEO_SITE_NAME` | literal | `CLIENT.seo_site_name` |
| `app.CATEGORY_MAP_CANDIDATES` / `load_category_map` / `load_category_seo` | files next to app.py | `CLIENT.category_map`, `CLIENT.category_seo` |
| `app.load_subcategory_attributes` / `load_subcategory_filter_axes` / `load_filter_groups` (2026-11-19) | `category_map.xlsx` only | `CLIENT.attributes_master` first (its 'Sub-Category Attributes' sheet and the sheet whose header starts with 'Filter Group'), then the `category_map.xlsx` copies ('Sub-Category Attributes', 'Filters'). `app.configure` reloads all three; `tools\check_filter_sheet.py` and a build warning report drift between the two files |
| `wc_publish.attributes_for` (2026-11-19) | every attribute custom | a filter-group attribute is sent as the store's global attribute (`products/attributes`, read once per run; created once only with `--commit`) |
| `app.download_urls_for_sku` image root | `os.getcwd()/product_images_for_upload` | `CLIENT.image_dir` (closes P3-57 for app) |
| `app.configure(slug)` | — | NEW: reloads every constant above for a different client (tests, wc_seo) |
| `build_csv.py` | rebuilds the literal URL | pre-scans `--client` before `import app`; passes `media_base=` from CLIENT |
| `validate_csv.PLACEHOLDER` | literal | from CLIENT (`--client` flag; env fallback). `DEAD_PLACEHOLDERS` stays a module constant: those are app.py's own invented spec VALUES ('Standard Capacity'), identical for every store, so nothing about them is per-client |
| `validate_csv.configure(slug)` | — | NEW: same contract as `app.configure`, so the GUI's one selector drives both |
| `validate_csv.load_valid_categories` | cwd / script dir | `CLIENT.category_map` |
| `validate_csv` image-existence check | bare relative `IMAGE_ROOT` | `CLIENT.image_dir` |
| `wc_publish.py` / `wc_seo.py` | `--client` absent | `--client` accepted; image root = `CLIENT.image_dir`; `wc_seo` calls `app.configure` |
| `gui.py` | "Shree Jee pipeline" titles, `REPO_ROOT/category_map.xlsx`, `REPO_ROOT/backups`, `@st.cache_resource category_options` | client selector in the sidebar (default = env/DEFAULT_CLIENT); titles from `CLIENT.name`; editor + backups under the client dir; cache keyed by slug; `run_meta.json` records `client`; a run created under another client is refused with the slug named |
| `sku_enricher.py` | — | `--client` accepted and recorded in the report header (BYOK key + ledger arrive with item 4) |

## Non-goals (later items)
- SKU prefix tables (item 2), publish blast-radius guard and per-client `.env` (item 3),
  credit ledger (item 4). `.cache/` stays shared on purpose (page cache keyed by URL);
  `checkpoints/` becomes `checkpoints/<slug>/` in item 3 together with the auto-snapshot.

## Item 2 — single-source SKU prefix scheme (`[sku]` table)

Today four copies drift independently: `app.SKU_PREFIX_BY_PARENT`/`SKU_PREFIXES`,
`app.SKU_PREFIX_CATEGORY`, `validate_csv.SKU_PREFIX_RE`, `gui_helpers.SKU_CODES`,
`sku_enricher.CATEGORY_CODES`, plus a literal `('WM-', 'WH-')` never-rename guard inside
`app.prefixed_sku`. One table replaces them:

```toml
# Sub-tables, not inline tables: TOML forbids a newline inside { }, and these
# are far too long for one line. Key order inside a sub-table is preserved.
[sku]
fallback_prefix = "AP"           # anything with no parent match
never_rename = ["WM", "WH"]      # live products carry these - prefixed_sku returns them untouched
strip_codes = ["DW", "LED", "MW"]  # extra tokens the enricher strips before brand/model search, no taxonomy hint

# OUTPUT side: the canonical code emitted per taxonomy parent when building the
# WooCommerce SKU. This order is the order of SkuScheme.prefixes.
[sku.prefix_by_parent]
"Washing Machines" = "WM"
"Water Heaters" = "WH"
"LED" = "TV"
"Air Conditioner" = "AC"
"Cooling & Freezing" = "CF"
"Kitchen Appliances" = "KA"
"Home Appliances" = "HA"
"Small Appliances" = "SA"
"Electronics & Computing" = "EC"

# INPUT side: codes an operator/ERP writes at the front of a raw SKU -> taxonomy hint [parent, sub]
[sku.input_codes]
WM = ["Washing Machines", ""]
WH = ["Water Heaters", "Geysers"]
SM = ["Home Appliances", "sewing machine"]
HWD = ["Home Appliances", "Water Dispensers"]
REF = ["Home Appliances", "Refrigerators"]
FRIDGE = ["Home Appliances", "Refrigerators"]
MWO = ["Kitchen Appliances", "Microwave Ovens"]
IC = ["Kitchen Appliances", "Induction Cooktops"]
ICT = ["Kitchen Appliances", "Induction Cooktops"]
DI = ["Small Appliances", "Electric Iron"]
DF = ["Cooling & Freezing", "Deep Freezers"]
AC = ["Air Conditioner", ""]
TV = ["LED", ""]
```

`ClientConfig.sku: SkuScheme` (frozen dataclass) with the five fields and two derived,
order-preserving tuples:

- `prefixes` = `prefix_by_parent` values + `fallback_prefix` → `app.SKU_PREFIXES`,
  `gui_helpers.SKU_CODES`, `validate_csv.SKU_PREFIX_RE` (built with `re.escape`).
- `enricher_codes` = `input_codes` keys ∪ `prefixes` ∪ `strip_codes` → `sku_enricher.CATEGORY_CODES`.
  (Must equal today's 22-entry tuple as a SET; the drift-guard test pins that.)

`app.SKU_PREFIX_BY_PARENT` / `SKU_PREFIX_CATEGORY` become views of the table;
`prefixed_sku` reads `never_rename` instead of the literal. `gui_helpers` may import
`client_config` (stdlib only) but still never `app` or streamlit. Validation at load:
every `never_rename` code, every `prefix_by_parent` value and `fallback_prefix`
is a 2-3 char `[A-Z]+` token; `input_codes` keys and `strip_codes` are `[A-Z]{2,}`
(input codes may be longer - `FRIDGE`); `fallback_prefix` is not a `prefix_by_parent`
value; codes are unique (no two parents share one, no code is in both `input_codes`
and `strip_codes`). `[sku]` is REQUIRED - a client that states no scheme would
silently inherit another's prefixes. `[serper]` is optional (every key defaults).
`ClientConfig.credits_file` / `.env_file` are `clients/<slug>/credits.json` and
`.env` - paths only, never created at load.

## Item 3 — publish blast-radius guard (`wc_publish.py`, `wc_checkpoint.py`, `gui.py`)

Nothing of this exists today (recon 2026-09-02). New exit code `EXIT_REFUSED = 4`:
a guard tripped and NOTHING was written (distinct from 2 row-failed / 3 edge-blocked).
`gui.py` mirrors it as `PUBLISH_EXIT_REFUSED`.

1. **Per-client `.env`** — `client_config.load_env(cfg) -> dict` parses
   `clients/<slug>/.env` (`KEY=VALUE` lines, `#` comments, optional quotes; no
   python-dotenv) and `client_config.apply_env(cfg)` injects it into `os.environ`,
   **overriding** the process env for those keys (a stale shell export must never win over
   the client's own file). Called at the top of `main()` in `wc_publish.py`, `wc_seo.py`,
   `wc_checkpoint.py`, `sku_enricher.py`, and at import in `blog_automate/blog_publish.py`,
   after `--client` is resolved - so `env_or_die` and every direct `os.environ["WC_URL"]`
   read see the client's values unchanged. `gui.py` never injects into its own process
   env (one process serves every client); it reads `load_env(cfg)` for the presence
   check and passes `PIPELINE_CLIENT` to subprocesses, which load the file themselves.
2. **Store-URL check** — `client_config.same_store(url_a, url_b)` compares
   scheme-less, `www.`-less, trailing-slash-less hosts case-insensitively.
   `wc_publish.store_mismatch(cfg)` returns a message naming BOTH hosts when
   `WC_URL` ≠ `cfg.store_url`, else None. It is a **hard stop (exit 4, refused
   before the `Store` is even constructed) only where a write can happen or where
   the operator asked for a verdict** — `--commit` (including `--delete-sku`),
   `--check`, `--preflight`, every `wc_seo.py` / `wc_checkpoint.py` action, and
   `gui.step_publish`, which shows the refusal before any button renders. A plain
   CLI dry run instead prints ONE loud `WARNING:` line naming both hosts and
   proceeds: a dry run cannot write, and the offline test-suite deliberately runs
   dry runs against `store.invalid`, which matches no client. An empty `WC_URL` is
   not a mismatch — `env_or_die` owns that message.
3. **`--max-commit N`** (default 25) — in `--commit` mode, if the rows that would be
   written after `--sku`/`--limit` filtering exceed N, refuse with exit 4 and say
   "pass --max-commit <n> to allow it". Dry runs are never capped. `--delete-sku` = 1.
4. **`review_signoff.csv`** — `--commit --status publish` and `--commit --delete-sku`
   require `review_signoff.csv` next to the CSV with columns `sku,reviewer,signed_at`;
   every SKU about to go live / be deleted must be listed, else exit 4 naming the first
   missing SKU. `gui.py` writes that file from `confirmed_set(P)` (reviewer =
   `os.getlogin()` or `USERNAME`, ISO timestamp) at the moment the operator arms a
   go-live commit, so live status is reachable only for human-confirmed rows.
5. **Auto snapshot** — before the first write of any `--commit` run,
   `wc_checkpoint.create(store, label=f"before-publish-{slug}")` unless `--no-checkpoint`;
   the name is printed and recorded in the publish log. `wc_checkpoint.CHECKPOINT_DIR`
   becomes `<repo>/checkpoints/<slug>/` (`--client` added to its parser; legacy files at
   `checkpoints/*.json.gz` are still listed under a "(unscoped)" label).
6. **`--preflight`** — `store.check()` plus `looks_like_challenge` on the raw response,
   `media_creds()` presence, and the store-URL match; prints one line per check, exits
   0 / 3 (challenge) / 4 (mismatch or missing creds). `gui.py` gets a "Preflight" button
   in Step 5 that must pass before the dry-run button is enabled in that session.

## Item 4 — credit ledger + hard stop (`sku_enricher.py`, `gui.py`)

```toml
[serper]
api_key_env = "SERPER_API_KEY"   # BYOK: the env var (in the client's .env) that holds the key
monthly_budget = 2500            # informational (free tier); the GUI shows month-to-date against it
default_run_budget = 0           # 0 = unlimited; the GUI pre-fills --budget with it
query_mode = "incremental"       # or "batch" (the behaviour before 2026-11-19)
search_order = "paid_first"      # or "free_first"; --search-order overrides it per run
```

- **`query_mode`** (default `"incremental"`). Per search tier, the cached queries are
  read first (free); the uncached ones go out one at a time, each followed by a crawl,
  and sending stops at the first complete listing or when the SKU's page-fetch budget
  is spent. A wholly cached tier takes the batch path, so a warm-cache replay is
  byte-identical in both modes. `"batch"` sends every query of a tier before any crawl.
  Another value raises `ClientConfigError`.
- **`search_order`** (default `"paid_first"`). `"free_first"` asks DuckDuckGo first and
  serper only when DuckDuckGo returns no result. The `--search-order
  {paid_first,free_first}` flag of `sku_enricher.py` overrides it for one run. The
  search cache key is the bare query in both orders (old entries stay valid); the
  `--budget` gate and the ledger behave the same in both. Another value raises
  `ClientConfigError`.
- **Duplicate-query skip** (always on, both modes): an UNCACHED `site:croma.com` query
  is not sent when the Croma site search answered for that SKU (no error, no block)
  with no match; an UNCACHED brand-site `site:` query is not sent when the URL index
  has a fresh, non-empty source on the brand's own domain and the lookup found nothing
  there. Counted in `RUN_STATS["queries_skipped_dup"]`; queries the incremental mode
  never sent are in `RUN_STATS["queries_skipped_early"]`. Both appear on the report's
  `Credits:` line.
- `serper_search` reads `os.environ.get(SERPER_KEY_ENV)` where `SERPER_KEY_ENV` is a
  module variable set from the client in `main()` (default `SERPER_API_KEY`, so
  `--probe` and tests behave as before).
- **`--budget N`** (non-negative int, default from `[serper] default_run_budget`; a
  negative value is refused by argparse, exit 2 — it used to clamp to 0, i.e. unlimited).
  N is a CEILING on credits, not a between-rows poll: one SKU fires 3-5 tier-1 queries,
  so checking only after the row overshot by up to a row's worth. Enforcement is in two
  places:
  - `web_search`, before it calls `serper_search`: while `SERPER_BUDGET` is set and
    `RUN_STATS["serper_calls"] >= SERPER_BUDGET`, serper is skipped, logged ONCE as
    `Serper budget of N reached - remaining searches fall back to DuckDuckGo`, and the
    existing DuckDuckGo fallback finishes the row for free.
  - the main loop, right where the one-shot 2000-call warning lives: log `Budget of N
    serper credit(s) reached - stopping, progress saved.` and `break` into the SAME
    `safe_save` + summary + report path the KeyboardInterrupt uses.

  So the Nth credit is the last one spent, and the run stops after the row in which it
  was spent. Exit code 0 (checkpoint-safe); the report and the ledger say
  `stopped_by = "budget"`.
- `[serper] monthly_budget` stays ADVISORY (it is not a stop). When the ledger's
  month-to-date is already at or over it, `main()` logs one line at startup —
  `Month-to-date serper credits (M) already at or over the monthly budget (B) -
  consider --budget` — and the run proceeds.
- **Ledger** `clients/<slug>/credits.json` (gitignored), written with tmp + `os.replace`:
  ```json
  {"month": "2026-09", "serper_calls": 412,
   "runs": [{"id": "20260902-153012-9184", "input": "LIST.xlsx", "started": "...", "ended": "...",
             "serper_calls": 37, "ddg_calls": 2, "pages_fetched": 140, "stopped_by": "done"}],
   "history": {"2026-08": 1830}}
  ```
  `sku_enricher.Ledger.update(run_entry)` upserts the current run by `id` and recomputes
  the month total; called on every checkpoint save and at exit (so a crash loses at most
  one checkpoint interval). Month rollover moves the old total into `history`.
  Two enrichers can run for one client at once, so: the run id is
  `%Y%m%d-%H%M%S-<pid>` (a bare second-resolution stamp let a same-second run REPLACE
  another by upsert), `update()` re-reads the file from disk immediately before merging
  its own entry (rewriting from the copy loaded at startup dropped the other run), and
  the scratch file is `credits.json.<pid>.tmp` before `os.replace`.
  Cache hits do not count (they never reach `serper_search`), which is what "credits
  actually spent" means.
- `write_report` gains `Client: <slug>` and `Credits this run / month-to-date / budget`.
- `gui.py` Step 1: a "Serper budget for this run" number input (pre-filled from the
  client) → `--budget`; the enricher panel shows `month-to-date / monthly_budget` from
  the ledger and surfaces the budget-stop line (`gui_helpers.parse_enricher_log` learns
  the new line).

## Tier 0 URL sources (`[url_index]` table, `url_index.py`, `sku_enricher.py`)

```toml
[url_index]
enabled = true          # false = straight to serper/DuckDuckGo, as before
max_age_days = 7        # sources older than this refresh at enricher start (>= 0)
disable = []            # source and site-search names to switch off for this client
# [[url_index.extra_sources]]       # same keys as [[source]] in url_sources.toml
# name = "mydealer"
# root = "https://www.example.com/sitemap.xml"
# url_pattern = '/product/'
```

- Optional; every key defaults (`client_config.UrlIndexConfig`, loaded by
  `_load_url_index` exactly like `[serper]`). A bad type raises `ClientConfigError`.
- The shared defaults live in the root `url_sources.toml` (`[[source]]` sitemaps,
  `[[site_search]]` endpoints). An extra source with the same name as a shared one
  replaces it. A name in `disable` drops it from BOTH lists.
- The index file `url_index\index.sqlite` is SHARED across clients (retailer URLs are
  not client data); only the selection of sources is per client. It is gitignored.
- `--no-url-index` and `PIPELINE_URL_INDEX=off` override `enabled = true` for one run.
- A keyed site search (`key_page` + `key_inputs`) reads its keys from the page's hidden
  inputs at run time, once per run, in memory only: they are never committed, cached
  (the cache key is `<name>:<code>`), logged or reported.

## One price only (`[pricing]` table, owner decisions 2026-11-19)

```toml
[pricing]
single_price = true     # shreejee: ONE price. Code default (table absent): false
basis = "lower"         # shreejee. Code default: "higher"
markup = 2000           # rupees, >= 0. shreejee: 2000. Code default: 0
```

- Optional; loaded by `client_config._load_pricing` into `ClientConfig.pricing`
  (`PricingConfig(single_price, basis, markup)`). `single_price` not `true`/`false`,
  `basis` not `"higher"`/`"lower"`, or `markup` not a number >= 0 raises
  `ClientConfigError`.
- `single_price = false` (default): two prices. `regular price` = MRP (struck
  through), `sale price` = the selling price; an MRP at or below the price is dropped.
  `basis` and `markup` are not used.
- `single_price = true`: the BUILD OUTPUT has one price, in `regular price`;
  `sale price` = empty. `_filled.xlsx` is not changed: the enricher still records both.
  The one price is `client_config.one_price(mrp, selling, pricing)` - the ONLY
  definition of the rule; app.py, validate_csv.py, wc_publish.py and wc_seo.py call it:
  - only one price on the row (or both equal): that price, UNCHANGED (no markup);
  - two different prices, `basis = "higher"`: the higher one (the first rule);
  - two different prices, `basis = "lower"`: the lower one + `markup`, capped at the
    higher one (never above the MRP). Example: MRP 37,790, price 18,590 -> 20,590.
- Defaults: `basis = "higher"`, `markup = 0`. A client that sets only
  `single_price = true` keeps the first rule (the higher price); a new rule is never
  applied to a client that did not ask for it.
- The rule is idempotent: a one-price pair `(20590, "")` returns 20590, so a second
  normalisation (seo_description, wc_publish) never adds a second markup.
- What follows the switch:
  - `app.row_context` (`single_price_pair`): the two prices; `--estimate-mrp` and the
    GUI "Estimate missing MRP" box do nothing (the box is disabled).
  - `app.build_tags`: the price-band tag uses the one price.
  - `app.seo_description` (CSV `--seo`, GUI SEO preview, `wc_seo.py`): "Price Rs N." only.
    No "MRP", no saving, no "% off".
  - `app.write_csv` (every CSV writer): prices are written as whole numbers
    (`"20590"`, not `"20590.0"`).
  - `validate_csv.py` export check: the one price drives `missing_price` and the
    category floor / ceiling / TV price checks; the MRP-ratio checks are off (no pair);
    a non-empty `sale price` is `sale_price_in_single_price_mode` (BLOCKING, a stale
    or hand-edited CSV). Stage-1 check (`_filled.xlsx`): an MRP alone is a price (no
    `verified_but_no_price`), the TV price check reads `one_price()`, and the
    MRP-ratio checks STAY (a wrong MRP can become, or cap, the price in this mode).
  - `wc_publish.payload_prices`: a CSV from the build has one price and sends it
    UNCHANGED as `regular_price`; only a stale CSV with both values is recomputed by
    `one_price()`. `sale_price` is SENT as `""`. On an update this CLEARS a sale price
    the live product had - only for the products in that CSV. The dry run names it
    (`sale_price 28990 -> -`).
  - `wc_seo.py`: a live product with only `regular_price` is quoted unchanged; a stale
    live product with both prices is quoted by `one_price()`.

## Item 5 — review instrumentation (`gui.py` step_review / card_review, `gui_helpers.py`)

`review_state.json` gains an `events` list beside `confirmed`:
`{"sku": "...", "action": "approve|unapprove|edit|url_pasted|drop_image|skip",
  "mode": "table|card", "at": "<iso>", "secs": 12.4}`.

- `secs` = wall seconds since the previous review event in this session (or since Step 2
  was opened: `review_clock_<run>` in session_state, reset on every event). Each event
  bills at most 600 s of the gap before it - the cap bounds what one idle stretch can
  add, it does not detect idleness, so up to 600 s of a coffee break IS billed to the
  next event. Table saves attribute the elapsed time equally to the rows changed in
  that save; card Approve / Save-and-next / Skip each log one event.
- `gui_helpers.review_metrics(state, df) -> dict`: `review_minutes`, `rows_touched`,
  `min_per_sku`, `needs_human_pct` = rows that need human attention - a hands-on
  review action (edit / url_pasted / drop_image) OR a status in the review/failed
  bucket - over all rows. The bucket half is a property of the SHEET, counted whether
  or not anyone opened the row, so this is not a measure of review time; a run nobody
  has reviewed can already report a non-zero percentage. Plus a per-action count.
  Pure; tested.
- The GUI writes `runs/<name>/review_metrics.json` from it on every save AND refreshes
  it when Step 2 loads (`gui.refresh_review_metrics`), so a re-enrichment that rewrote
  statuses cannot leave a stale percentage behind; it is never created for a run with
  no review events. The archive
  README and `sku_enricher.write_report` (when a `review_metrics.json` sits next to the
  output, i.e. on a retry run) print `Review: N min · M min/SKU · needs-human X%`.

## Item 6 — deliverable pack (`gui.archive_run`, `build_csv.py --pack`)

- `gui_helpers.coverage_summary(df) -> dict`: rows; FOUND clean; FOUND incomplete by
  suffix; CLOSE MATCH; CHECK MANUALLY; NOT FOUND by reason suffix; ERROR; SKIPPED; the
  two-pass statuses. `gui_helpers.render_batch_readme(meta, coverage, review, images,
  validation) -> str` renders the README (one renderer for both entry points).
- `archive_run` additionally copies the client's original input list (under its
  original name) and writes the README with `## Coverage` and `## Review` sections into
  `batches/<name>/` - the INTERNAL archive. The zip handed over, `batches/<name>.zip`,
  is made from a separate `batches/<name>_client/` folder: CSV, referenced images and a
  README only (`validation_issues.csv` is internal since 2026-11-19, fifth round).
- `build_csv.py --pack` writes `<out dir>/<stem>_pack/` (CSV, referenced images,
  README from the same renderer; no `validation_issues.csv`) and zips it. A repo file, never a published page.
- Owner rule 2026-11-19: a client pack never holds `*_filled.xlsx`, `*_report.md`,
  `*.log`, `validation_issues.csv` or a text file that links to a host other than the client's store / media
  host. `gui_helpers.assemble_pack` refuses such members (`internal=True` only for the
  operator's archive) and `remove_refused` checks the finished folder before the zip.
