# Shree Jee Catalog Pipeline — Complete Guide

*This folder (`final_pipe`) is the self-contained, final version of the catalog
pipeline. Everything older (surgical_scraper, production engine, sanitizer) is
legacy and lives outside this folder for reference only.*

---

## 1. What this project does

It turns a bare list of SKUs (e.g. `HAIER-WM HW120-DM14F9BKU1`) into a complete,
verified WooCommerce product import for **shreejeeelectronics.in** — with real
titles, sale prices, MRPs, product URLs, 3+ gallery images per product (as WebP
files renamed by SKU), auto-built descriptions, categories, and WooCommerce
attribute columns. Every listing is verified against the model code before it is
trusted, and anything unverifiable is flagged instead of guessed.

## 2. Data flow

```
 YOUR SKU LIST (.xlsx / .csv, a 'sku' column)          <- put it in final_pipe\
        |
        v   sku_enricher.py  (searches Amazon.in, Flipkart, Croma, brand store,
        |                     Reliance Digital + open web; strict verification)
 <name>_filled.xlsx    columns: sku, title, regular price (MRP), sale price,
        |              url_link, image_url (3+ URLs), status
        |
        |   [YOU: review rows whose status is CHECK MANUALLY / CLOSE MATCH /
        |    NOT FOUND / "- only N image(s)" / "- no price shown"]
        v
        v   app.py       (Streamlit browser app - formats + downloads)
        v   build_csv.py (the same formatter, headless - for unattended runs)
 wordpress_ready_clean_repaired.csv   (WooCommerce columns + attributes)
 product_images_for_upload\<client>\*.webp   (SKU_1.webp, SKU_2.webp, ...)
        |
        v   validate_csv.py  (QA gate)
 validation_issues.csv (anything worth fixing before import)
        |
        +-- by hand: upload .webp to WordPress Media in the SAME month chosen
        |            in the app, then WooCommerce > Products > Import the CSV
        |
        +-- or:      wc_publish.py  (REST API - creates/updates products,
        |            uploads the images, sets categories, tags and alt text)
        v
 LIVE PRODUCTS on shreejeeelectronics.in
```

Also see **data_flow.svg** for the visual version.

**The whole chain above, in one app:** `streamlit run gui.py` wraps every
script in this diagram into a single 5-step wizard (see §3). The individual
scripts below still work standalone — useful for headless/scripted runs, or
when only one stage needs redoing.

## 3. The scripts

### Per-client configuration — `client_config.py` + `clients\<slug>\`

Every store-specific value lives in one folder per client, so a second client
cannot leak into or break the first. The full design contract is
**docs/CLIENT_CONFIG.md**; this is the operator's summary.

```
clients\shreejee\
  client.toml                          store URL, media base, SEO site name,
                                       placeholder HTML, taxonomy filenames,
                                       [sku] prefix scheme, [serper] budgets
  category_map.xlsx                    taxonomy (in git)
  category_seo.xlsx                    curated tags + SEO keywords (in git)
  SubCategory_Attributes_Master.xlsx   attributes master: the SOURCE of the filter
                                       groups and the sub-category attribute lists
                                       (app.py reads it; see "Filter groups") (in git)
  backups\                             category-map editor backups (NOT in git)
  .env                                 this client's WC_*/WP_*/SERPER_* (NOT in git)
  credits.json                         serper usage ledger (NOT in git)
```

**Every entry point takes `--client SLUG`** — `sku_enricher.py`, `build_csv.py`,
`validate_csv.py`, `wc_publish.py`, `wc_seo.py`, `wc_checkpoint.py`. Resolution
order is `--client` > the `PIPELINE_CLIENT` environment variable > `shreejee`.
In-process, `app.configure(slug)` and `validate_csv.configure(slug)` reload every
constant for another client. The GUI gets a client selector and the same guards;
see CLIENT_CONFIG.md.

**A new client** = copy `clients\shreejee\` to `clients\<newslug>\`, edit
`client.toml` (`[client] slug` must equal the folder name), drop in that client's
`category_map.xlsx`, and create its `.env`.

**The `[sku]` table is the ONE source of the SKU prefix scheme.** app.py,
validate_csv.py, gui_helpers.py and sku_enricher.py all derive from it instead of
keeping their own copies:

- `[sku.prefix_by_parent]` — the code emitted per taxonomy parent when the
  WooCommerce SKU is built (`Washing Machines = "WM"`, `LED = "TV"`, …).
- `fallback_prefix` (`AP`) — anything with no parent match.
- `never_rename` (`["WM", "WH"]`) — live products already carry these, so
  `app.prefixed_sku` returns such a SKU untouched.
- `[sku.input_codes]` — codes an operator or ERP writes at the FRONT of a raw SKU,
  mapped to a taxonomy hint (`HWD` → Home Appliances / Water Dispensers).
- `strip_codes` — tokens the enricher strips before searching that carry no
  taxonomy hint.

Editing this table is how a new client gets its own prefixes; `[sku]` is required,
so a client cannot silently inherit another's.

**`clients\<slug>\.env` grammar** — parsed by `client_config.load_env`, no
python-dotenv:

```
# comments and blank lines are ignored
WC_URL=https://example.com
export WC_KEY=ck_...            # a leading "export " is tolerated
WC_SECRET="cs_... "             # surrounding ' or " are stripped
SERPER_API_KEY=abc123
```

Any other line is an error naming its line number — a typo in a credentials file
must not silently publish to the wrong store. These values **override** the shell
environment for the scripts that load them (`wc_publish.py`, `wc_seo.py`,
`wc_checkpoint.py`, `sku_enricher.py`), so a stale `setx WC_URL` cannot redirect
a publish.

**`clients\<slug>\credits.json`** — the serper ledger the enricher maintains
(month total, per-run rows, previous months under `history`):

```json
{"month": "2026-09", "serper_calls": 412,
 "runs": [{"id": "20260902-153012", "input": "LIST.xlsx", "started": "...",
           "ended": "...", "serper_calls": 37, "ddg_calls": 2,
           "pages_fetched": 140, "stopped_by": "done"}],
 "history": {"2026-08": 1830}}
```

It is written on every checkpoint save and at exit, with a temp file + replace,
and a ledger that cannot be written warns once instead of taking the run down.
Cache hits never reach serper, so they are never counted.

**Generated per-client folders:** images go to
`product_images_for_upload\<slug>\` and snapshots to `checkpoints\<slug>\`, both
anchored on the repo — not on the directory you happen to run from.

### sku_enricher.py — data collector

**What it does:** for each SKU it extracts the brand + manufacturer model code,
runs site-restricted searches (Amazon.in, Flipkart, Croma, the brand's own
store) plus an open web search, fetches only real product pages, and extracts
title / MRP / sale price / gallery images with site-specific parsers (Amazon,
Flipkart, Croma, Reliance Digital, IFB) plus a generic JSON-LD fallback.

**Image and price capture:** every page is parsed twice - by its site-specific
parser and by the generic JSON-LD/OpenGraph reader - and the two image sets are
merged, so a listing with a full gallery no longer comes back with one picture.
Images are collected in tiers of decreasing trust (the page's own declared
product images first, a page-wide sweep only if those fall short), junk like
logos, app icons and header graphics is filtered out, the same photo at several
sizes is collapsed to its largest version, and MRP is recovered from the word
"MRP" or a strike-through price when the site's own markup hides it.

**Verification (why results are trustworthy):** accessory listings (covers,
stands, "made for...") are rejected; the model code must appear in the listing;
capacity implied by the model (HW90 = 9 kg) must match the listing; wrong model
codes are rejected (colour variants become CLOSE MATCH); off-category results
(TVs, laptops) are never fetched; a result is only FINAL when it has a price
AND 3+ images — otherwise more sources are tried and the row is flagged.

**Statuses:** `FOUND (site)` = safe to use · `... - no price shown` /
`... - only N image(s)` = verified but incomplete, finish by hand ·
`... - unlisted store, verify` = the only match was on a site outside the known
retailer list, so give it a glance · `... - gallery below Npx floor, verify` =
every photo is under the HD floor · `... - gallery matches row N (SKU), verify`
= another row already claimed this exact photo set, so one of the two is the
wrong product · `CLOSE MATCH - CHECK` = variant code,
confirm · `CHECK MANUALLY` = brand-level match only · `NOT FOUND - ...` =
nothing verifiable, and the reason is now spelled out (`all sources blocked`,
`N page(s) checked, none matched`, `no usable search results`).

**When sites block us:** each retailer gets its own cookie session and a
current Chrome fingerprint, rotated on failure. A site that hard-blocks is put
on a short cooldown instead of being retried to death, no single site may use
more than 2 of the 6 page fetches per SKU, and candidates are tried
round-robin across retailers so one hostile site cannot bury a SKU that another
shop has. Rows blocked rather than genuinely missing say so in the status, and
are automatically retried on the next run. For pages that are still blocked, or
whose price is drawn by JavaScript, add `--js-fallback` to open them in a real
headless browser (slower; needs the one-time
`.venv\Scripts\python.exe -m playwright install chromium`, which is skipped
automatically if Google Chrome is installed).

**Best-of-both merge across sources:** brand sites publish the full gallery and
no price; marketplaces publish the price. When the winning source is verified
but incomplete, up to `--merge-budget` (default 2) EXTRA pages are fetched from
the candidates the normal budget never reached, specifically to supply what is
missing — then price, MRP, gallery and specs are merged in from every other
source that verified as the SAME model. A field the winner already has is never
replaced, with two exceptions: the gallery is topped up past the minimum from
other known retailers, and an MRP that fails the sanity check is swapped for a
plausible one. Merged fields are named in the status line, so a reviewer can
see that the price came from a different site than the photos.

**Spec tables:** the retailer's own specification table is scraped into a
`specs` column (`Label: Value | Label: Value`), from schema.org
`additionalProperty` first, then two-column tables and definition lists, then
`Label: Value` bullets. Marketplace bookkeeping (ASIN, rank, reviews, price)
and marketing paragraphs are filtered out. app.py turns these into real
WooCommerce attributes, so a product ships with what its page actually stated
instead of the handful of facts inferable from the title. `--no-specs` skips it.

**Page and search cache:** every successful page fetch and every search result
is stored, gzipped, under `.cache\`. Re-running a finished batch therefore does
no network I/O and spends no Serper credits — which is what makes it worth
re-running an old batch through improved parsers. Failures are never cached (a
transient block must not become permanent) and entries expire after
`--cache-ttl` days (default 14), because a fortnight-old price is not a price.

**Serper budget and the credit ledger.** `--budget N` caps the run at N serper.dev
credits, enforced at two points: once N credits are spent, every further search is
refused before it reaches serper (logged once as `Serper budget of N reached -
remaining searches fall back to DuckDuckGo`, so the row in flight finishes for free),
and after that row the loop logs
`Budget of N serper credit(s) reached - stopping, progress saved.` and takes
the same checkpoint-save + summary + report path as Ctrl+C, so the exit code is 0
and nothing is lost. `0` means unlimited, a negative value is an argparse error, and
the default is the client's `[serper] default_run_budget`. `monthly_budget` is
advisory: when the ledger's month-to-date is already at or over it, the run starts
with one `WARNING` line and does not stop. Spend accumulates in `clients\<slug>\credits.json`
(above). The API key itself is BYOK: the variable NAME comes from `[serper]
api_key_env` (default `SERPER_API_KEY`) and its value from the client's `.env`.

**Tier 0 - URLs without a search (`url_index.py`).** Before any paid search, each
row tries two free sources: (0a) the local URL index - retailer and brand sitemaps
(`url_sources.toml`) stored in `url_index\index.sqlite` and matched on the model code
in the URL slug - and, only when the index verified nothing, (0b) the retailers' own
search endpoints (Reliance Digital, then Croma). Every URL from tier 0 is only a
candidate: it passes the same URL screens and the same strict `assess()` verdict as a
search result. A complete match returns with zero searches spent; a verified but
incomplete one skips the search tiers (exactly as a search-tier match does) and goes
to the donor merge; anything else falls through to serper/DuckDuckGo unchanged. Tier
0 gets its own small fetch budget (3 pages for the index, 3 per site-search endpoint),
so the search tiers keep their full budget. A model code shorter than 6 characters, or
all digits, must also have the brand in the slug or sit on the brand's own site, and
must match as a whole token. At start, sources older than `max_age_days` (default 7)
are refreshed in-process before the first SKU (robots.txt obeyed, incremental by
`<lastmod>` / ETag, a politeness delay before every live request, robots `Crawl-delay`
honoured); a failed source logs one line, keeps its old URLs and is retried after 12 h.
`--discover` uses tier 0; `--crawl-urls` never does. Site-search answers are cached in
`.cache\sitesearch\` like searches. Kill switches: `--no-url-index`, `[url_index]
enabled = false` in the client's `client.toml`, or `PIPELINE_URL_INDEX=off` (the test
suite sets this). The report header gains a `URL index:` line (lookups, candidate URLs,
rows resolved by the index, site-search calls, rows resolved by site search, rows that
still needed a web search); `--cache-info` also prints the index per source.
When tier 0 is on and the enabled index sources hold zero URLs at run start (after
the automatic refresh), the run prints one line:
`!! WARNING: the URL index is EMPTY ... Fix: .venv\Scripts\python.exe url_index.py refresh`.

**How the paid search spends credits (2026-11-19).** Three rules cut serper.dev spend
without lowering the find rate:
- *Incremental queries* (`[serper] query_mode = "incremental"`, the default). In each
  search tier, the queries already in `.cache\` are read first (free). The uncached
  queries then go out one at a time, in their normal order; after each one, the new
  candidates are crawled (the best URL of each domain first, as before). Sending stops
  at the first complete listing, or when the SKU's page-fetch budget (6 pages) is
  spent. A product found by the first query therefore costs 1 credit, not 5. A tier
  whose queries are all cached runs exactly as before, so a warm-cache replay gives
  byte-identical output. `query_mode = "batch"` restores the old behaviour (every
  query of a tier goes out before any page is crawled). `--discover` uses the same
  rule; `--crawl-urls` sends no searches at all.
- *Duplicate queries are skipped.* The `site:croma.com` query is not sent when the
  Croma site search ran for that SKU without an error or a block and returned no
  match. The brand-site `site:` query is not sent when the URL index holds a fresh
  (within `max_age_days`), non-empty source for that brand's own domain and the index
  lookup found no URL on it. If the free source errored, was blocked, was off, was
  stale or empty, or the lookup was cut at its candidate limit, the paid query is
  sent. A query that is already cached is always read, never skipped.
- *Search order* (`[serper] search_order`, or `--search-order` for one run).
  `paid_first` (default): serper.dev, DuckDuckGo only when serper fails.
  `free_first`: DuckDuckGo first, serper.dev only when DuckDuckGo gives no result.
  The cache key is the bare query in both orders, so old cache entries stay valid,
  and the `--budget` gate and the ledger work the same way in both.
- *Engine chain* (`[serper] engines`, KI-36). Default `["serper", "tavily", "serpapi",
  "ddg"]`; `free_first` moves ddg to the front. An engine with no key is skipped; an
  error or a refused budget tries the next engine; an empty answer from a paid engine
  stops the chain. Keys: `TAVILY_API_KEY` (tavily.com, 1,000/month) and `SERPAPI_API_KEY`
  (serpapi.com, 250/month), both free with no card, read like the serper key.
  `--budget` caps serper credits only; the report and the ledger count Tavily and
  SerpApi calls. A row whose every search failed on all engines gets
  `NOT FOUND - search engine failed`, not "no usable search results".

The report's `Credits:` line adds `queries not sent: N (free source said not there)
+ M (stopped early)`, and the console prints the same two counts at the end of a run.

**Batch report:** each run writes `<output>_report.md` next to the workbook —
a header with `Client:`, `Credits:` (this run / month-to-date / budget) and
`Stopped by:` (`done`, `user` or `budget`), then outcome counts, the rows that
still need a human and why, Serper credits spent, cache hits, and which domains
blocked the run. When a `review_metrics.json` sits next to the output (the GUI
wrote it while the sheet was reviewed) the report also carries a `## Review`
section: `Review so far: N min · M min/SKU · needs-human X%`. It describes the whole output
file, not just this run's rows, so a batch resumed across several sessions
still reads as one batch. `--no-report` skips it.

**Where data lives:** the output `<name>_filled.xlsx` appears next to the input
SKU file, wherever that is. The original file is never modified. Progress saves after
every SKU; if the output is open in Excel the script waits instead of crashing.

**Output is app.py-ready:** the filled sheet always carries the eight headers
app.py requires (`sku, title, categories, Brands, regular price, sale price,
url_link, image_url`) plus `specs` and `status`. A `sku` column is added and filled from
whatever your SKU column was called; `categories` and `Brands` are left blank on
purpose, because app.py fills them itself from `category_map.xlsx`. If your input
used names like `MRP`, `price`, `url` or `images`, those headers are renamed to
the canonical ones in the output copy (your original file keeps its names).

**Commands:**
```
:: normal run (resumes automatically; skips rows already attempted)
.venv\Scripts\python.exe sku_enricher.py "WM MODEL LIST FOR WEBSITE.xlsx" --min-price 8000 --delay 5 8

:: which client's config to use  --client shreejee   (or set PIPELINE_CLIENT)
:: stop after N serper credits   --budget 200        (0 = unlimited)
:: DuckDuckGo before serper      --search-order free_first   (default: client
::                               [serper] search_order = paid_first)
:: quick test on 5 SKUs          --limit 5
:: redo everything               --force
:: re-attempt NOT FOUND rows     --retry-missing
:: re-attempt FOUND rows missing price/images   --retry-incomplete
:: headless browser for blocked / JS-priced pages  --js-fallback [--js-budget 25]
:: keep that browser's cookies between runs         --js-profile DIR
:: test ONE page, touch no spreadsheet             --probe "<product url>"
:: image requirement (default 3) --min-images 3
:: image size floor (px, longest edge, measured)  --min-image-px 1000
:: don't measure images at all (faster, sloppier)  --no-image-check
:: price sanity band             --min-price 8000 --max-price 60000
:: wrong column picked?          --sku-col "Model No"    --sheet "Sheet2"

:: extra fetches to complete a verified row (0 = off)   --merge-budget 2
:: skip the retailer spec table                         --no-specs
:: skip the run summary / put it elsewhere    --no-report | --report PATH

:: --- url index (tier 0: free URL sources before any paid search) ---
:: skip tier 0 for this run       --no-url-index
:: refresh every source now       --refresh-index      (works without an input file)
:: refresh sources older than     --index-max-age 7    (default: client max_age_days)

:: --- cache ---
:: what is cached, how big        --cache-info
:: throw it all away              --cache-clear
:: ignore it for this run         --no-cache
:: fetch fresh but keep the copy  --refresh-cache     (use when prices moved)
:: age at which entries stop counting (0 = never)      --cache-ttl 14
:: keep the cache somewhere else  --cache-dir PATH
```
**`url_index.py` CLI** (the index on its own, no spreadsheet):
```
.venv\Scripts\python.exe url_index.py refresh [--source NAME] [--force]   :: --force ignores lastmod/ETag
.venv\Scripts\python.exe url_index.py stats                              :: sources, URL counts, last refresh
.venv\Scripts\python.exe url_index.py lookup MODEL [--brand B]           :: candidate URLs for one code
```
All three take `--client SLUG` (its `[url_index] disable` / `extra_sources` apply).
Add or switch a source in `url_sources.toml` (`[[source]]` / `[[site_search]]`, each
entry commented with its live-check result).

**Re-running an old batch is now free.** With the cache warm, `--force` replays
the stored pages through the CURRENT parsers instead of re-downloading and
re-paying every Serper credit — so a parser improvement can be applied
retroactively to a finished batch. Outside the cache, `--force` still costs
full price: check `--cache-info` first.

**Two-pass flow — review the links before paying for the full extraction.**
`--discover` (pass 1) finds a candidate product link and photos for each SKU
only — no spec table, no donor-page merges — so it is cheap. Rows it can link
confidently get `NEEDS CRAWL (site)` — a link found, ready for pass 2; rows it
can't get `NEEDS LINK - <why>`, for a human to paste a `url_link` in by hand.
Neither status starts with `FOUND`, on purpose (see below). Once every row has a
link, `--crawl-urls` (pass 2) crawls exactly those links and does **no
searches at all** — it fetches the page you already told it about instead of
asking Serper to find one, so it costs zero Serper credits regardless of
whether that page is cached or fetched fresh.

```
.venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --discover
::   -> open LIST_filled.xlsx, paste a url_link on every NEEDS LINK row
.venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --crawl-urls
```

None of the discovery statuses (`NEEDS CRAWL (site)`, `NEEDS CRAWL - CHECK
LINK (site)`, `NEEDS LINK - <why>`) start with `FOUND`, which is deliberate:
app.py/gui.py/build_csv.py all gate the import on `status.startswith("FOUND")`,
so a half-discovered sheet can never be imported by accident even if you build
the CSV before pass 2 finishes. In the gui the gate applies to BOTH build
scopes — "Confirmed rows only" and "All FOUND rows" — so a reviewer's ✔ cannot
smuggle a non-FOUND row in; a tick or Approve on one is refused and the SKU is
named, and the fix is to correct the url_link and press *Crawl all info*.
`tests\test_two_pass.py` pins that invariant.

**When to use it instead of the normal single-pass run:** a normal run is
fine for a small or well-behaved batch. Reach for two-pass on a large batch,
or one where you expect a lot of manual link-pasting anyway (obscure brands,
BUSY ERP exports) — you pay full search cost only once, on pass 1, and every
later re-crawl (a fixed parser, a corrected link, a retry) is free.

Recommended once: `setx SERPER_API_KEY "<key>"` (free at serper.dev) — Google
search quality instead of DuckDuckGo, and much faster.

### gui.py — the whole pipeline, one wizard (recommended GUI)

**What it does:** a 5-step Streamlit wizard — Upload & Enrich (runs
`sku_enricher.py` as a subprocess with a live log, offers the two-pass toggle),
Review & Confirm (edit price/title/URLs inline, paste links for NEEDS LINK
rows, confirm rows), Build CSV (`app.process_inventory` — same code as
`app.py`/`build_csv.py`), Validate (`validate_csv.validate_export`), and
Publish (`wc_publish.py`, dry-run first, `--commit` gated behind a checkbox).

Everything for one batch lives under `runs\<name>\`: input, `_filled.xlsx`,
enricher log, the CSV, `validation_issues.csv`, review/run state. State is
re-derived from those files, so closing the browser tab mid-enrichment loses
nothing — the enricher checkpoints after every row and reopening the wizard
resumes where it left off. It gets a client selector and the same guards as the
CLI; see docs/CLIENT_CONFIG.md.

**Review states and the approved-only build.** In Step 2 every row is in
exactly one of three states: **approved** (✔ in the table, *Approve & next* in
card view), **skipped** (the *Skip* column in the table, *Skip* plus an
optional reason in card view), or **not reviewed**. Approving clears a skip and
skipping clears an approval; only a FOUND row can be approved, any row can be
skipped. `review_state.json` stores each SKU as `{confirmed, skipped,
skip_reason, fp}`; older files with only `{confirmed, fp}` still load (missing
keys read as not skipped). Step 3 shows "Approved N · Skipped N · Not reviewed
N · Not FOUND N" with the skipped SKUs and their reasons, and its scope ALWAYS
defaults to "Confirmed rows only": with zero approvals it builds nothing and
the Build button stays off until a row is approved. "All FOUND rows" is an
explicit choice that warns it includes unapproved rows. Skipped rows are never
built in either scope, so they never reach the CSV, the images or the store.
When a re-enrichment changes a row, its ✔ is dropped (P3-53) but its skip is
KEPT: an explicit "no" must not turn back into "not reviewed".

**Step 3 downloads.** After a build (and on every later visit to step 3),
step 3 shows "⬇ Download CSV" and "⬇ Download images (.zip)". The zip mirrors
the image URLs: unpack it into `wp-content/uploads/` and every CSV image URL
resolves. See "Image zip (step 3)" under Share mode for the layout.

**Description and short description (review step).** Card review shows a
"Description & short description" panel for the row on screen (the table view has
the same panel for one SKU chosen from a list). It shows the text the CSV WILL
contain for those two WooCommerce fields - `app.description_fields_for_row`, the same
rows `build_descriptions` renders, after the content firewall and `KEY_POINTS_MAX` -
as one **Label: value** line per table row. Delete a line to remove that point, or
edit it. *Save description edits* stores the lines as a per-SKU override in
`runs\<name>\description_overrides.json`; `_filled.xlsx` is not changed. Each line
passes the content firewall when it is saved: a refused line (retailer or seller name,
URL, phone, EMI, offer, delivery, rating) or a line that is not `Label: value` is named
in red and is not saved. Empty text is saved only with *Publish ... EMPTY* ticked. An
edit equal to the generated text is no override. *Reset ... to generated* removes it.
If the row is re-enriched or edited later, the panel says the generated text has
changed; the edit is still used until it is reset. The build (step 3, and
`build_csv.py --description-overrides runs\<name>\description_overrides.json`) writes
the override in place of the generated rows and applies the firewall again.

**Key-features paragraph (review step, 2026-11-20).** The same panel has a third
field, *Key features paragraph*. The build writes it to the CSV column
`configuration` as `<p class="product-key-features">` with one `Label: value.`
sentence per line. The panel shows it as one **Label: value** line per sentence: the
brand row plus at most `KEY_POINTS_MAX` key points. The rules are the same as for the
two other fields: the firewall check at save time and at build time, *Publish ...
EMPTY*, *Reset ... to generated*, and the "generated text has changed" warning. The
edit is stored in `description_overrides.json` under the key `key features`. The
reviewer's lines have no line limit, the same as the two other fields. An older
overrides file without this key builds the paragraph as before.

**Final images (card review, 2026-11-20).** Card review shows each kept photo as the
build will save it: the 1500 x 1500 white canvas, with its final number (`_1` is the
main image, the whitest white-background photo). `app.preview_images_for_row` runs
the build's own image code (`open_source_image`, `square_image`,
`order_square_results`). It writes no file and never moves old images. A photo that
the build refuses shows the source photo and the reason (below the 1000 px floor, or
download failed). A dropped photo shows the source photo with "dropped - not built".
The *keep* checkboxes work as before and write `dropped_images` as before. The
preview downloads each photo once and caches a small copy for one hour. Use the
toggle *Show the final 1500 x 1500 images* to switch the preview off (the source
photos then show with no download). Set `PIPELINE_REVIEW_IMAGE_PREVIEW=off` to make
off the default. The table view does not show the preview.

**Price and attributes (review step, 2026-11-19 late).** A second panel, *Price &
attributes*, sits above the description panel (card view, and in the table view's
one-SKU expander). It shows, for the row on screen only (`app.review_summary`):
- the MRP on the page, the selling price on the page and its kind (`MOP` or `sale
  price`, from the non-published `price_kind` column), the price that will be
  published and the rule in words (`18,590 + 2,000 = 20,590, cap 37,790`, from
  `client_config.one_price`);
- every attribute that will be published, with its source (`specification`,
  `description`, `title`, `brand`, `reviewer`), filter attributes marked ✔, and the
  required attributes still missing as a warning.

Edits are saved in the same `runs\<name>\description_overrides.json`, per SKU, keys
`price` (`values`: only the changed `regular price` / `sale price` and a non-empty
`publish price`) and `attributes` (`set` {name: value}, `remove` [names]).
`_filled.xlsx` is not changed. Refusal rules: a page price that is not a positive
number; a publish price that is not a positive number or is above the MRP in effect;
an attribute value that fails the content firewall (`content_ok` / `spec_row_ok`), is
empty, is a never-a-value word (`Yes`, `No`, `N/A`) or is longer than 40 characters.
A refused entry is named in red and not saved. An empty publish price means no
override. With a publish price the output still has ONE price and an empty sale
price; the screen marks it HAND-ENTERED. A value typed for a filter group goes
through the same snap / new-value rule as a scraped value; a reviewer value that
fills a required attribute clears its `_spec_gaps` entry, and a removed one shows as
missing. *Reset price to the page* / *Reset attributes to generated* remove the
edits. The build (`build_csv.py --description-overrides ...` too) applies them;
without overrides the output is byte-identical. Limit: a changed non-filter
attribute changes the attribute columns, not the description tables (they have
their own editor).

**Review instrumentation.** `runs\<name>\review_state.json` records an `events`
list beside `confirmed` — one entry per approve / unapprove / edit / url_pasted /
drop_image / skip, with the mode (table or card), an ISO timestamp and the wall
seconds since the previous event (capped at 600 s, so an idle gap never counts).
From it the wizard writes `runs\<name>\review_metrics.json` on every save, and
the same one-liner then appears in the GUI, in the archive README's `## Review`
section and in the enricher's report:
`Review so far: N min · M min/SKU · needs-human X%` — how much human time a batch
actually costs, per client.

**Hand-over pack.** Archiving a run copies its reviewable outputs plus the
original input list into `batches\<name>\` and writes a README with
`## Coverage` (status buckets), `## Review` (the line above) and `## Files`.
That folder is the INTERNAL archive; it is not handed over. The client gets
`batches\<name>.zip`, which is made from a separate folder
`batches\<name>_client\` with ONLY the import CSV, the images it references
and a README (owner rule, 2026-11-19; `validation_issues.csv` is INTERNAL since
the fifth round: it stays next to the CSV and in the internal archive). The client pack
never holds the `_filled.xlsx`, the enricher report, a log or the source list.
`gui_helpers.assemble_pack` refuses (skips, with a printed warning) a member
named `*_filled.xlsx`, `*_report.md`, `*.log` or `validation_issues.csv`, and any text member with a
link to a host that is not the client's store or media host. The README never
prints a site name (`price from a source site`). `build_csv.py --pack`
produces the same client pack from the headless route.

**Command:**
```
.venv\Scripts\python.exe -m streamlit run gui.py
```

`app.py` still exists and still works on its own (just the CSV-build step) —
useful if you already have a `_filled.xlsx` from a script-driven run and only
need the formatter, without the rest of the wizard's state-tracking.

#### Share mode (password-protected GUI)

Share mode lets a second person use the wizard. It asks for a password
before it shows anything. It also turns off the dangerous actions.

**Start:**
```
powershell -ExecutionPolicy Bypass -File tools\share_gui.ps1
```
- The script makes a random 24-character password. To set your own
  password, add `-Password "<at least 12 characters>"`.
- The script binds the GUI to `127.0.0.1` (this PC only), port 8501.
  Other options: `-Port`, `-Budget` (credits per run, default 50) and
  `-MaxUploadMB` (default 20).
- The script writes the URL, the password, the PID and the start time to
  `runs\_share\share_info.txt`. `runs\` is gitignored. The script does not
  print the password. Read it from that file.

**Stop:**
```
powershell -ExecutionPolicy Bypass -File tools\stop_share_gui.ps1
```
The stop script kills the process tree, checks that the port is closed and
deletes `share_info.txt`.

**What is off in share mode:**
- Step 5 Publish: no preflight, no dry run, no commit. `run_publish` refuses.
- Run delete and run archive.
- Category map edits.
- The client selector. The client stays the one the process started with.
- `--js-fallback`, `--js-profile` and `--force`. Every enrichment run gets
  `--budget N`. N is the operator's value or `GUI_SHARE_BUDGET`, whichever
  is smaller.

Upload, enrich, review, build and validate stay available.

**Image zip (step 3):** step 3 shows "⬇ Download images (.zip)" next to
"⬇ Download CSV", in share mode and in normal mode. The zip
(`<csv stem>_images.zip`) holds every image that the CSV `Images` column
names. Each file is at the URL path after `/wp-content/uploads/` (for
example `2026/11/WM-SKU_1.webp`). A URL without that marker keeps its full
path. The zip also holds `image_map.csv` (sku, position, image_url,
zip_path, status = ok / missing / refused) and `README.txt`. A missing file
does not stop the zip; the caption lists it. A URL path with `..` or a
drive letter is refused, and nothing outside the image folder is read. The
button reads only: it moves and deletes nothing. The GUI keeps the zip in
memory and builds it again only when the CSV changes.

**Publish from share mode is off by design.** To put the products on the
store, upload the folders in the zip into `wp-content/uploads/` (keep the
folders). Then do one of these: import the CSV manually in WooCommerce, or
publish from a PC that has the store credentials (`wc_publish.py`).

**Login lock:** after 8 wrong passwords in total (all sessions together),
all logins lock for 15 minutes.

**Environment variables** (the launcher sets them for the GUI process only):
`GUI_SHARE_MODE=1`, `GUI_SHARE_PASSWORD` (12 characters minimum, else the GUI
shows only an error), `GUI_SHARE_BUDGET` (default 50). With none of them set,
the GUI works as before.

**Public access:** the launcher binds to this PC only. It starts no tunnel.
A public address needs a separate tunnel step. The owner starts and stops
that tunnel. Stop the tunnel before you stop the GUI.

#### Password-only mode (`GUI_PASSWORD`, owner decision 2026-11-24)

Password-only mode puts the same password gate in front of the FULL GUI.
After login, nothing is hidden: all 5 steps, the category map editor,
archive/delete, the client selector and the real publish.

- Set `GUI_PASSWORD` (12 characters minimum). A shorter value shows only an
  error, and nothing else renders (fail closed).
- The gate is the same code as share mode (`gui.password_gate()`): the same
  constant-time compare, the same login lock (8 wrong passwords, 15 minutes)
  and the same session flag. `share_mode()` stays False in this mode.
- **Precedence:** when `GUI_SHARE_MODE` and `GUI_PASSWORD` are both set,
  share mode wins (it is more restrictive). The password is then
  `GUI_SHARE_PASSWORD`, and `GUI_PASSWORD` is ignored.
- With neither variable set (local laptop use), there is no gate.

**Step 5 hand-over section (every mode):** the top of step 5 shows
"⬇ Download CSV" and "⬇ Download images (.zip)" (the same buttons as step 3)
and these manual steps:
1. Download the CSV and the image zip.
2. Unzip; upload the folders into `wp-content/uploads/` on the store
   (hosting file manager or FTP), keeping the folders.
3. WooCommerce > Products > Import > choose the CSV.

The existing publish UI follows, unchanged. It stays off in share mode. When
`WC_URL`, `WC_KEY` or `WC_SECRET` is missing, it shows its normal "Missing
credentials" message.

**Category map editor download:** the editor has a "⬇ Download
category_map.xlsx" button (the current file on disk). On a non-Windows host
(Streamlit Cloud, a container) a caption says that a saved edit lives only
on that server until the next restart or redeploy. Download the file and
commit it to GitHub to keep it. Save and backup behaviour is unchanged.

**Streamlit Community Cloud Secrets** (app settings > Secrets, TOML):
- Search keys (recommended in both modes, each on its own line, no leading `#`):
  `SERPER_API_KEY`, `TAVILY_API_KEY`, `SERPAPI_API_KEY`. Without them only
  DuckDuckGo runs, and it often fails from a cloud server.
- Password-only mode: `GUI_PASSWORD`; optional `SERPER_API_KEY`; for the real
  publish also `WC_URL`, `WC_KEY`, `WC_SECRET`, `WP_APP_USER`,
  `WP_APP_PASSWORD`.
- Share mode: as above for share mode (`GUI_SHARE_MODE`,
  `GUI_SHARE_PASSWORD`, optional `GUI_SHARE_BUDGET` and `SERPER_API_KEY`).

**Risk:** in password-only mode, anyone with the password can delete runs,
edit the taxonomy, spend serper credits and publish to the LIVE store (when
the WC credentials are set). Use a long random password, share it only with
trusted people, and rotate it when a person leaves. Runs and taxonomy edits
on Streamlit Cloud are lost on a restart or redeploy.

### app.py — WooCommerce formatter (browser app)

**What it does:** takes the filled file and produces the import CSV: brand +
category detection (Washing Machines > Top load / Front Load / Semi Auto, AC,
LED, etc.), SKU normalization + WM-/WH-/AP- prefixes, duplicate-SKU removal,
description/short-description/configuration HTML, WooCommerce attribute
columns, and downloads every image as `SKU_n.webp` into
`product_images_for_upload\<client>\` (the SKU prefixes come from the client's
`[sku]` table, `WM-`/`WH-` never renamed) — the Images column only lists files
that really downloaded.

**Image layout (owner rule, 2026-11-19):** every image of every product has
the same fixed layout. Each file is exactly `CANVAS_EDGE` x `CANVAS_EDGE`
(1500 x 1500 since the fifth round of 2026-11-19; it was 1200) WebP on a
white canvas. The product is centred with
`MARGIN_PCT` (4%) of white on its limiting side and is never cropped. A source
under `MIN_IMAGE_EDGE` (1000 px) is refused. Upscaling is capped at
`MAX_UPSCALE` (1.15x): a small product that needs more stays centred at that
size on the same canvas (it is not dropped). On the 1500 canvas a source with a
long edge under ~1200 px (white background) or ~1305 px (lifestyle) does not
fill it: a 1000 px source becomes a 1150 px product with more white around it.
The per-product 1500/1200/1000 canvas tiers are gone.

**Old image files leave the upload folder (owner rule, 2026-11-19).** After a
build writes a SKU's images, every other `<sku>_<n>.webp` in
`product_images_for_upload\<client>\` is MOVED (never deleted) to
`_superseded\<YYYY-MM-DD>\` in the same folder - also when the build saved 0
images for that SKU. The SKU is matched exactly (`TV-X_1.webp` is TV-X, never
TV-X2 or TV-X-B). A name already in the target gets a suffix (`TV-X_2-2.webp`).
One line per SKU is printed. Nothing reads `_superseded\`: every consumer opens
images by the names in the CSV.

**White main image:** the FIRST image (`SKU_1.webp`) has a white background.
`app.white_share()` measures the white share of the outer `BORDER_PCT` (2%)
border strip of each SOURCE photo, before it is placed on the white canvas. A
photo is white when that share is at least `WHITE_SHARE` (0.95). Of the white
photos, the one with the HIGHEST share becomes `_1` (a tie goes to the earlier
URL). The other images keep their URL order as `_2.._n`. When no photo is
white, the first URL stays `_1` and `_image_flags` gets
`main image background not white` (validate_csv.py reports a warning).

**Controls in the app:** upload the `_filled.xlsx` directly (Excel accepted);
"Import only verified rows" (on by default — uses the status column);
WordPress uploads folder YYYY/MM (must match the month you'll upload images);
optional "estimate MRP as sale x 1.25" (off by default); optional
**"Add SEO meta columns"** (off by default — see below). After Generate, a
**Quality check** panel runs validate_csv.py on the result and lists anything
that needs a look, so QA is part of generating the file rather than a separate
step to remember.

**Selling price priority (owner decision 2026-11-19, late).** The enricher takes a
labelled MOP on the page (`MOP` / `M.O.P.` / `Market Operating Price` + a currency
sign) as the selling price FIRST; with no MOP it uses the page's sale price
(site parser / JSON-LD / meta). The MRP stays the MRP (guards unchanged:
`bogus_mrp`, foreign-currency strikes ignored). The non-published sheet column
`price_kind` records `MOP` or `sale price` (a donor price carries its own kind).

**One price only (`[pricing]`, owner decisions 2026-11-19).** When a client's
`client.toml` sets `[pricing] single_price = true` (shreejee does; the code default is
false), the CSV has ONE price in `regular price` and `sale price` is empty. Which
price: `basis` and `markup` (`client_config.one_price`). shreejee has `basis =
"lower"`, `markup = 2000`: a row with an MRP and a lower selling price ships the
selling price + 2,000, capped at the MRP (MRP 37,790, price 18,590 -> 20,590); a row
with only one price ships it unchanged. `basis = "higher"` (the code default, with
`markup = 0`) ships the higher of the two. Price-band tags and the SEO description
use that one price; no "MRP", saving or "% off" text is written; "estimate MRP" does
nothing. The CSV writes whole prices as `20590`, not `20590.0`. The `_filled.xlsx`
still records both values. The validator, `wc_publish.py` (sends the CSV price
unchanged and `sale_price: ""`, which clears a live sale price on the products it
writes) and `wc_seo.py` follow. Details: docs/CLIENT_CONFIG.md.

**Gross Weight is not written** (owner decision 2026-11-19): the content firewall's
label deny list refuses it. Product / Item / Net Weight are written as `Weight`.

**TV platform** (owner decision 2026-11-19): the platform (Google TV, Android TV,
WebOS, Tizen, Fire TV, VIDAA) is written when the title or the spec table names one.
A bare "Smart" / "Smart TV" / "Yes" under a platform or OS label is never written -
not as an attribute, not in the description, not in the key features. The tag
"Smart TV" stays.

**Attributes from the retailer's spec table:** when the filled sheet carries a
`specs` column, those facts become real WooCommerce attributes on top of the
ones read from the title. Retailer wording is canonicalised (`Display Size` and
`Screen Size` are one attribute, not two), invoice fields (ASIN, rank, reviews)
are dropped, one row per attribute name, capped at `MAX_ATTRIBUTES` (15) so the
CSV stays a usable width.

**Attribute completeness gate:** the `Sub-Category Attributes` sheet already
declares which axes matter per sub-category (an AC has a Ton, a TV has a Size).
Read as a checklist, it becomes a gate: any axis a product cannot fill from its
spec rows, its description text (`page_text`), its title, a filter attribute or the
sheet's own vocabulary is written to a `_spec_gaps` column, summarised in a warning, and reported by validate_csv.py as
`missing_required_attribute`. It **flags, it does not block** — a missing axis
is often just a terse title, and the human review step decides.

#### Filter groups (owner rule, 2026-11-19)

**Source order (2026-11-19, late).** Every filter group and every required
attribute looks, in this order, in: (1) the scraped specification rows, (2) the
page's description text (the enricher's non-published `page_text` column: JSON-LD
`description` and the meta / og description, each segment through the content
firewall), (3) the product title. The first source that STATES the value wins; each
filter hit records its source (`FilterHit.source`). The existing axis parsers are
used on every source; the title wording is not rewritten. Screen size reads
`80 cm (32)`, `80cm(32)`, `80 cm (32 inch)`, `108 cm (43 inches)`, `32 inch`, `32"`;
the bracketed number is the stated inch size, and a cm figure alone is never
converted. A thousands comma is not a list separator (`HD (1,366 x 768)` snaps to
`HD Ready`). The enricher also reads label/value sibling pairs (samsung.com spec
lists, `pair_specs`; a compare widget and sentence-long values are skipped).

The store's filter groups decide the product attributes. A customer filters
by attribute, so an attribute that feeds a filter must use the filter's own
name and the filter's own spelling of the value.

**Where the sheets are.** The source is the client's attributes master,
`clients\<slug>\SubCategory_Attributes_Master.xlsx` (client.toml
`attributes_master`). It has two sheets that matter:

- `Sub-Category Attributes` - one row per sub-category; the columns `Type`,
  `Size`, `Star Rating`, `Ton`, `Capacity`, `Storage` and a second lower-case
  `type` column (carried as the optional axis `Sub Type`, for example Fans:
  `non BLDC, BLDC` -> attribute `Motor Type`). A cell is a comma list, `N/A`,
  a range (`1 Star to 5 Star`), or a free-text hint
  (`Sweep Size in mm (e.g. 1200mm)`).
- The filter sheet - any sheet whose header starts with `Filter Group`
  (`Sheet2` in the master). One row per store filter group with its values.

`category_map.xlsx` holds copies (`Sub-Category Attributes`, `Filters`). They
are used only when the master is missing or lacks the sheet. The build warns
when the two copies differ. The in-app category editor edits only the FIRST
sheet of `category_map.xlsx` and keeps the other sheets as they are.

**What the build does.** For each axis of the product's sub-category,
`app.filter_attributes` takes the value from the scraped spec table, else
from the title. It snaps the value onto the filter group's list first (that
spelling is written: `1.0 Ton` in the sub-category sheet is written as
`1 Ton`), then onto the sub-category row's list. Units and spellings are
folded (`1.5 T` = `1.5 TR` = `1.5 Tonne`; `190 Ltr` = `190L`; `5-star` =
`5 Star`), and numbers compare as numbers. A value is NEVER rounded:
`1.2 Ton` against `1 / 1.5 / 2 Ton` matches nothing. A filter group whose
values are ranges (`Below 200L`, `6.1-7kg`) gets the range that holds the
value; the exact figure stays as an ordinary attribute. A free-text hint with
an `e.g.` example gets a clean number + unit (`1200mm`).

The snapped value becomes an attribute NAMED BY THE FILTER GROUP
(`AC Capacity: 1.5 Ton`). Filter attributes come FIRST in the attribute list
and the 15-attribute cap never cuts them. The scraped row they came from is not
written again under its retailer label. An axis that no filter-sheet group
covers is named from the axis and its unit (`Energy Rating`, `Product Type`,
`Power Rating`, `Sweep Size`, ...). Nothing is invented: no stated value, no
filter attribute.

The axis -> filter group table is `app.FILTER_AXIS_GROUPS` (AC Ton -> AC
Capacity, AC Star Rating -> AC Star Rating, AC Type -> AC Technology + AC Type,
LED Size/Capacity -> TV Screen Size, LED Type -> TV Smart Features, Washing
Machines Type -> WM Function Type and Capacity -> WM Washing Capacity,
Refrigerators Type/Capacity -> Fridge Type/Fridge Capacity, Water Purifiers
Type/Capacity -> WP Technology/WP Storage Capacity). The AC Type axis also
feeds AC Mode (Hot & Cold).

**Directly-fed groups (2026-11-19).** Some groups have no sub-category column.
`app.DIRECT_FILTER_GROUPS` feeds them from the scraped spec table (named
labels first), then from the title. The value is snapped onto the client's
filter-sheet list with the same rules: TV Resolution (one value, the highest
stated), TV Display Tech (plain LED is dropped when QLED/OLED/Mini LED/QNED is
also stated), TV Refresh Rate (a number with Hz only, exact), WM Features
(several values; a negative such as `No` or `without heater` is never
written), WM Color, Fridge Defrosting. These groups are optional: they never
add to `_spec_gaps`. A dedicated label (for example `Resolution`, `Colour`)
whose value does not snap is written as scraped, with a "new value" note in
`_filter_unmatched` (see below). The title and the general feature lists never
give a new text value. A group that is not in the client's
filter sheet is skipped. Brand, Price, Availability, Discount and Fridge Price
Range are computed by the store and are never written.

**`Attribute N global`.** The CSV writes `1` only for filter attributes and `0`
for every other attribute, so scraped spec labels stay custom attributes and
do not become global taxonomies. `wc_publish.py` keeps a `0` row custom even
when its name is a filter-group name.

**New values (owner decision, 2026-11-19).** A scraped value is never replaced,
rounded or dropped because the list does not hold it. Only its spelling is made
regular. A stated value that is in neither list is WRITTEN as the filter
attribute (first in order, `Attribute N global` = 1):

- Numeric group: a number in the axis unit (the scraped label first, else the
  title) is written as number + the list's own suffix. `0.8 Ton` gives
  `AC Capacity: 0.8 Ton`; `1.8 T` gives `1.8 Ton`; `24 inch` against a list
  that spells `32"` gives `24"`.
- Band group (WM Washing Capacity, Fridge Capacity, WP Storage Capacity,
  `75"+`): a value inside a band writes the band label, and the exact scraped
  row stays as its own attribute (`Capacity: 7 kg` next to
  `WM Washing Capacity: 6.1-7kg`). A value outside every band is written exact.
- Text group: a value under a label dedicated to the group (never the title,
  never a general feature list, never the general `Configuration` label) is
  written as scraped, when it passes the content firewall and has at most 40
  characters. In a multi-value cell, the matched parts get the sheet spelling
  and the other parts stay as scraped.
- Never a value: `Yes`, `No`, `NA`, `N/A`, `Not available`, a negative
  (`without heater`), and for TV Smart Features a bare `Smart TV`. The
  platform (Android TV, Google TV, WebOS, Tizen, or another named platform
  under `Smart Platform` / `Operating System`) is written only when the page
  names one.

**`_filter_unmatched`.** Each written new value gets the note
`AC Capacity: '0.8 Ton' is a new value, add it to the sheet`. A stated value
that cannot be written (no unit, fails the firewall, too long) gets the note
`... not in list`. validate_csv.py gives a WARNING for each, never a block:
`warning_filter_value_not_in_sheet` for a written value that is in neither
list, `warning_filter_value_unmatched` for the other notes. `_spec_gaps` is
unchanged: a required axis with no value at all.

**How to add a value.** Open the attributes master. Add the value to the filter
group in the filter sheet (this spelling is the one written), and to the
sub-category row if it belongs to one sub-category. Keep the
`category_map.xlsx` copies the same, then rebuild the CSV.

**The check tool.** It is read-only:

    .venv\Scripts\python.exe tools\check_filter_sheet.py --client shreejee

It prints the values that are in one sheet and not the other (per axis), the
same values spelled two ways, cells that give no value list, the filter groups
nothing feeds (only the store-computed ones should remain), and every difference between the master and the
category_map.xlsx copies.

**REST publish.** `wc_publish.py` sends an attribute whose name is a filter
group as the store's GLOBAL attribute (`{id, options}`), so the filter widgets
see it. It reads `products/attributes` once per run. A missing global attribute
is printed as "would create global attribute <name>" in a dry run and is
created once with `--commit`. All other attributes are sent as before.

**Content firewall (owner rule, 2026-11-19):** nothing that belongs to the
SOURCE RETAILER goes into a field we write: name, description, short
description, key features, attributes, SEO title/description, tags. The
firewall removes retailer and marketplace-seller names, "sold by" /
"fulfilled by" text, EMI, bank and card offers, cashback, coupons, exchange
offers, delivery and pincode text, return and replacement policy, the
retailer's warranty-service and customer-care text, badges ("Amazon's
Choice", "Best Seller", "Flipkart Assured"), ratings and review counts, URLs,
phone numbers and e-mail addresses. The ONE definition is in `sku_enricher.py`
(`RETAILER_NAMES`, `SPEC_LABEL_DENY_RE`, `COMMERCE_RE`, `SERVICE_RE`,
`URL_RE`, `EMAIL_RE`, `PHONE_RE`, and the functions `content_hits`,
`content_ok`, `spec_row_ok`, `content_clean`). The enricher applies it while
it scrapes (`usable_spec`). `app.py` applies it again at build time, so an
already-enriched `_filled.xlsx` is cleaned without a re-scrape: spec rows
(`parse_scraped_specs`, `product_specs`), the title (`content_clean`, once,
in `row_context`), and each rendered description row. The product's own brand
is never a retailer name (a Croma-brand kettle keeps "Croma"). Genuine
product features stay: "Amazon Alexa built-in", "Works with Alexa", "Amazon
Fire TV", "Prime Video". A `Warranty` row stays when its value is a duration
("2 Years Comprehensive") and goes when it holds service or customer-care
text. validate_csv.py runs the same check over every written field
(`retailer_content_in_field`, blocking).

**Key points (owner rule, 2026-11-19):** the key-features paragraph, the short
description and the long description each show at most `KEY_POINTS_MAX` (6)
relevant spec points. The Brand / Product SKU / Title / Categories template
rows do not count. Order: (1) rows whose label is on the sub-category's
attribute whitelist (`Sub-Category Attributes` sheet), in sheet order;
(2) the core rows read from the title (capacity, screen, resolution, energy,
power, ...); (3) other scraped rows, which are left out when the sub-category
has a whitelist. Fewer relevant points means fewer points — never padded.
The attributes are not capped by this (still `MAX_ATTRIBUTES`, 15). No row is
invented: the old fixed "Standard Operational Grid: 230 V / 50 Hz" row is
removed.

**SEO meta (OFF by default).** Meta title and description are not WooCommerce
fields; they belong to Yoast or Rank Math and live as WordPress post meta. With
the checkbox on (or `build_csv.py --seo`) four columns are added:

| Column | Contents |
|---|---|
| `slug` | Readable URL, model code appended so variants cannot collide |
| `Meta: _yoast_wpseo_title` | <= 60 chars, store name appended when it fits |
| `Meta: _yoast_wpseo_metadesc` | <= 155 chars: title, price/MRP, top specs |
| `Meta: _yoast_wpseo_focuskw` | `brand model`, lowercase |

`--seo-plugin rankmath` writes `Meta: rank_math_*` instead; `both` writes both
sets. The `Meta: ` prefix is what tells the WooCommerce CSV importer to store a
column as post meta, so no plugin-specific tooling is needed; `wc_publish.py`
forwards the same keys through the API's `meta_data`. Everything is derived
from the row — title, category, specs, the two prices — so it is deterministic
and contains no claim the data does not support. **`slug` is the one column the
CSV importer cannot use** (it has no slug field); it applies on the API route
only. Image alt text is likewise attachment-level, so it is set by
`wc_publish.py` at upload time and cannot ride the CSV at all.

**Category taxonomy:** the `categories` column is snapped to the EXACT strings
(case- and space-sensitive) in **`clients\<slug>\category_map.xlsx`** — e.g. `Washing Machines > Top load`, `Washing Machines > semi auto`.
Edit that Excel to change the taxonomy; no code changes needed. Categories the
app cannot map are left as detected and listed in a warning; validate_csv.py
also flags any `category_not_in_map` rows. Note: "Juicers, Mixers & Grinders"
is treated as ONE category name, and "electric kettle" maps to Small
Appliances while plain "kettle" maps to Kitchen Appliances > Kettles.

**Where data lives:** images land in `product_images_for_upload\<client>\`
inside the repo, whatever directory you started the app from. Download the
generated CSV wherever you like; keep it next to the images folder's repo root
if you plan to publish from the CLI.

**Command:**
```
.venv\Scripts\python.exe -m streamlit run app.py
```

### validate_csv.py — QA gate (both stages)

**It takes either file.** Given a `_filled.xlsx` it runs stage-1 checks on the
enricher's output; given the CSV it runs the import checks. Run it on the filled
sheet *before* reviewing by hand — it tells you which rows deserve attention.

```
.venv\Scripts\python.exe validate_csv.py "LIST_filled.xlsx"     # stage 1
.venv\Scripts\python.exe validate_csv.py wordpress_ready_clean_repaired.csv
```

Stage-1 checks: verified-but-no-price, verified-but-thin-gallery, MRP at/below
the price or implausibly far above it (`MRP_RATIO_LIMIT` 4x, tightened to 3x by
`mrp_ratio_limit()` for TV/LED titles), a panel-class price ceiling (a HD Ready TV priced
like a 4K one), titles not in English or missing the SKU's brand, unlisted-store
sources, and **cross-row duplicates** — two SKUs sharing a source URL, a first
image, a whole gallery, ANY single image, or a title, which is the classic
wrong-match signature and no per-row check can see it.

Stage-2 checks (the generated CSV): missing sku/title/category/description/
price, fewer than 3 images, image URLs whose local `.webp` file is missing
(broken images after import), duplicate SKUs, MRP <= sale price, front-load
titles labelled Top Load, plus:

- **image files that are not product photos** — the local `.webp` is opened and
  anything under 400 px on its long edge or beyond a 3:1 aspect is flagged.
  Caught on the LED batch: a 3x1 pixel, a 94x46 and a 60x60 icon shipped as
  product images.
- **`warning_image_not_house_canvas`** — a final image file that is not exactly
  `CANVAS_EDGE` x `CANVAS_EDGE` (1500 x 1500). A warning, not a block.
- **`retailer_content_in_field`** (blocking) — the content firewall found
  retailer-owned text in the name, a description, key features, tags, SEO meta
  or an attribute. The reason names the field and the matched term.
- **byte-identical galleries across SKUs.** On this file the URL-based
  cross-row checks *cannot* fire, because app.py renames every image to
  `<sku>_<n>.webp` — so the files themselves are compared. Caught on the LED
  batch: `UA32H4500FUXXL` and `UA32H4510FUXXL` shipped the same eight photos,
  meaning one matched the other's listing.
- **price above the category ceiling** as well as below its floor — a bundle,
  a dealer pack, or an MRP read into the price field.
- **`missing_required_attribute`** — the axes `_spec_gaps` recorded.
- **`warning_filter_value_unmatched`** — a `_filter_unmatched` note for a
  stated filter value that could not be written (see "Filter groups").
- **`warning_filter_value_not_in_sheet`** — a filter attribute (`global` = 1)
  holds a value that neither the filter sheet nor the sub-category row lists:
  a new value, written as scraped. Add it to the sheet. Non-blocking.

Prints a summary and writes `validation_issues.csv`. It also runs
**automatically** at the end of `build_csv.py` and in the app after Generate.

**Where data lives:** the image-existence check reads
`product_images_for_upload\<client>\` inside the repo, so the directory you run
from no longer matters. `validation_issues.csv` is written next to the file
being checked.

**Command:**
```
.venv\Scripts\python.exe validate_csv.py wordpress_ready_clean_repaired.csv
.venv\Scripts\python.exe validate_csv.py out.csv --client shreejee --warn-only
```

`--warn-only` reports without failing (exit 0 even when rows are flagged);
without it the exit code is 1 whenever anything is flagged, so it can gate a
script. `--client` picks the placeholder text, the category map and the image
folder.

### build_csv.py — the formatter without the browser

**What it does:** imports `app.py` and calls the same `process_inventory()`, so the
CSV and the `.webp` files are identical to what the Generate button produces. Exists so
a batch can run unattended and so the formatter can be asserted on by tests.

**Commands:**
```
.venv\Scripts\python.exe build_csv.py "LED_filled.xlsx"
.venv\Scripts\python.exe build_csv.py "LED_filled.xlsx" -o out.csv --month 2026/08
.venv\Scripts\python.exe build_csv.py "LED_filled.xlsx" --all-rows --limit 5
```

| Flag | Meaning |
|---|---|
| `-o, --out` | Output CSV (default `wordpress_ready_clean_repaired.csv`) |
| `--month YYYY/MM` | Uploads folder the image URLs will point at (default: this month) |
| `--all-rows` | Include unverified rows; default is FOUND rows only |
| `--estimate-mrp` | Fill a missing MRP with sale x 1.25 (off by default) |
| `--limit N` | First N rows only — for smoke tests |
| `--seo` | Add the SEO meta columns (OFF by default) |
| `--seo-plugin` | `yoast` (default) · `rankmath` · `both` |
| `--no-validate` | Skip the QA pass that otherwise runs on the file just written |
| `--description-overrides JSON` | The reviewer's edits per SKU: description / short description / key-features paragraph (`configuration`), page MRP / selling price / publish price, attributes (`runs\<name>\description_overrides.json`, written by the GUI review step). Default: none - every field is generated |
| `--pack` | Also write `<out stem>_pack\` — CSV, the images the CSV references and a README (never the `_filled.xlsx`, the report or a log) — and zip it. The same client pack the GUI's Archive button zips |
| `--client SLUG` | Which client's config to use (default: `PIPELINE_CLIENT`, then `shreejee`) |

The QA pass runs by default the moment the CSV is written, so a problem is
reported while you are still looking at the build rather than one step later.

Images are written to `product_images_for_upload\<client>\` **inside the repo**
(anchored on the script, not on the current directory), so the same run from any
folder puts them in the same place.

`--pack` is the hand-over route: it produces one folder plus one
`<out stem>_pack.zip` containing the CSV, the validation issues, every image the
CSV references and a README (no `_filled.xlsx`: it holds source links) with `## Coverage`,
`## Review` and `## Files` sections. The README is rendered by the same
`gui_helpers.render_batch_readme` the GUI archive uses, so the two routes cannot
drift.

### wc_publish.py — publish over the WooCommerce REST API

**What it does:** upserts products keyed on SKU, so re-running a corrected batch
updates rather than duplicates. Creates missing categories and tags, uploads the local
`.webp` files and sets their alt text, and writes attributes and prices. Replaces both
manual steps (Media upload + Products > Import).

**Post meta and slug.** Any column named `Meta: <key>` is forwarded as a
`meta_data` entry with the prefix stripped — the same convention the WooCommerce
CSV importer uses, so a CSV built with `--seo` publishes its Yoast or Rank Math
fields on either route with no plugin-specific code. A `slug` column is sent as
the product slug; that one has no CSV-importer equivalent, so this route is the
only way to set it. Columns absent = nothing extra sent, so a CSV built without
`--seo` produces exactly the payload it always did.

**Credentials — `clients\<slug>\.env` or environment variables, never in the
repo.** The client's `.env` is loaded first and OVERRIDES the shell environment
for these keys:

| Variable | Where it comes from | Needed for |
|---|---|---|
| `WC_URL` | your store URL | everything |
| `WC_KEY` / `WC_SECRET` | WooCommerce > Settings > Advanced > REST API (Read/Write) | products, categories, tags |
| `WP_APP_USER` / `WP_APP_PASSWORD` | WP Users > Profile > Application Passwords | uploading images |

WooCommerce keys authenticate `/wc/v3` only; the Media Library lives at `/wp/v2`, which
is why image upload needs a separate Application Password.

**Commands:**
```
.venv\Scripts\python.exe wc_publish.py --check                      # test credentials
.venv\Scripts\python.exe wc_publish.py --preflight                  # every guard, no write
.venv\Scripts\python.exe wc_publish.py out.csv                      # DRY RUN
.venv\Scripts\python.exe wc_publish.py out.csv --sku AP-XYZ --commit # one product
.venv\Scripts\python.exe wc_publish.py out.csv --commit --status publish
.venv\Scripts\python.exe wc_publish.py out.csv --commit --max-commit 50
.venv\Scripts\python.exe wc_publish.py --delete-sku AP-XYZ --commit  # clean up a test
```

**Nothing is written without `--commit`**, and new products are created as `draft`
unless `--status publish` is given. An update never flips a live product back to draft.

**Blast-radius guards — exit code 4 means REFUSED and nothing was written**
(distinct from 2 = some rows failed, 3 = the store's edge blocked us):

| Guard | What it does |
|---|---|
| Store URL | `WC_URL` must be the same store as the client's `[store] url` (compared without scheme, `www.` or trailing slash). Hard stop on `--commit`, `--check` and `--preflight`; a plain dry run prints one loud `WARNING:` naming both hosts and carries on, because a dry run cannot write |
| `--max-commit N` | Default 25. A `--commit` that would write more products than this is refused, and the message names the flag to raise it. Dry runs are never capped; `--delete-sku` counts as 1 |
| `review_signoff.csv` | `--commit --status publish` and `--commit --delete-sku` require this file next to the CSV, with columns `sku,reviewer,signed_at`, listing every SKU about to go live or be deleted. A missing SKU is refused by name. The GUI writes the file from the confirmed rows when the operator arms a go-live commit |
| Auto snapshot | Before the first write of any `--commit` run, a `before-publish-<slug>` checkpoint is taken and its name printed. `--no-checkpoint` skips it — that is a run you cannot roll back |
| `--preflight` | Runs the store-URL check, `store.check()`, the media credentials and the sign-off file, prints one line per check and exits without writing: 0 = pass, 3 = edge challenge, 4 = mismatch or missing credentials |

**Getting through a CDN browser challenge.** If the store answers with
"Checking your browser before accessing…" instead of JSON, the script says so and
prints both routes out (it does not dump raw HTML at you):

| Flag | Meaning |
|---|---|
| `--limit N` | At most N products — for smoke tests |
| `--client SLUG` | Which client's config and `.env` to use (default: `PIPELINE_CLIENT`, then `shreejee`) |
| `--images auto\|upload\|url\|skip` | `auto` (default) uploads the local `.webp` when `WP_APP_*` is set, else trusts the Images column |
| `--cookie-file PATH` | file holding your browser's `Cookie` header — preferred, keeps it out of shell history |
| `--cookie VALUE` | the cookie value inline |
| `--user-agent UA` | must match the browser that earned the cookie, or the clearance will not be honoured |

Read from `WC_COOKIE` / `WC_USER_AGENT` if the flags are omitted. `cookie.txt` is
gitignored; it is a live session credential.

> **Currently blocked (KI-15).** Hostinger's CDN "I'm under attack" mode returns HTTP
> 403 to every non-browser request to the site — including `/robots.txt` — so the API
> is unreachable no matter how valid the keys are. Proper fix: hPanel (separate login
> from WordPress, hpanel.hostinger.com) > Websites > Dashboard > CDN > Manage >
> Security. Stopgap if that panel is out of reach: `--cookie-file` above.

### wc_seo.py - back-fill SEO meta on products ALREADY LIVE

`build_csv.py --seo` covers products this pipeline is about to publish; this covers
the ones already on the storefront. Dry run unless `--commit`.

    .venv\Scripts\python.exe wc_seo.py --report            :: what would change
    .venv\Scripts\python.exe wc_seo.py --sku TV-... --commit

Flags: `--commit` `--sku` `--limit` `--plugin` `--overwrite` `--slugs` `--report`
`--no-checkpoint` `--checkpoint-label` `--cookie-file` `--cookie` `--user-agent`
`--client`. The store-URL guard applies here too: a mismatch between `WC_URL` and
the client's `[store] url` refuses with exit 4.
It snapshots via wc_checkpoint.py first unless `--no-checkpoint`; `--slugs` rewrites
permalinks, which breaks existing links - do not use it on an indexed store.

### wc_checkpoint.py - snapshot the store, and put it back

Takes a restorable copy of the products before a risky bulk change.

    .venv\Scripts\python.exe wc_checkpoint.py create --label before-seo
    .venv\Scripts\python.exe wc_checkpoint.py list --client shreejee
    .venv\Scripts\python.exe wc_checkpoint.py restore 20260901-1204-before-seo --commit

Positional `action` + `name`; flags `--label` `--commit` `--fields` `--sku` `--limit`
`--cookie-file` `--cookie` `--user-agent` `--client` `--force-store`. Every snapshot
records the store host and client slug it was taken from; `restore` (and `diff`,
read-only) compares them with the target store and REFUSES with exit 4 when they
differ or when the snapshot predates the stamping — `--force-store` overrides, and
prints why. Snapshots land in
`checkpoints\<slug>\` (anchored on the script's own folder); snapshots taken before
checkpoints were split per client still sit in `checkpoints\` and `list` shows them
under an "(unscoped)" label. Restore is a dry run unless `--commit`, and the
store-URL guard refuses a mismatched store with exit 4.

`wc_publish.py --commit` calls `create` for you (label `before-publish-<slug>`)
unless `--no-checkpoint` is given.

`--label` is **create-only** — it names a NEW snapshot `<timestamp>-<label>`. `restore`
ignores it and restores its positional `name`, which defaults to `latest` (the
last file by sort), so `restore --label before-seo --commit` would quietly put the
NEWEST snapshot back, not the labelled one. Run `list` and pass the file name.

## 4. Features at a glance

Verified-only product data (model-code matching, accessory rejection, capacity
and category guards) · MRP + sale price capture · 3+ image standard with
honest flags · resume/interrupt-safe long runs · Excel-lock protection ·
status-aware import (unverified rows excluded by default) · SKU normalization
and dedupe · WebP conversion with SKU-based names · WooCommerce attributes ·
QA gate matching the site's real failure modes · reusable Claude prompt
(`claude_prompt.md`) for finishing NOT FOUND rows by hand.

## 5. Folder contents

| File | Role |
|---|---|
| `sku_enricher.py` | Step 1 — fill SKU list from marketplaces |
| `gui.py` | Recommended — 5-step wizard driving the whole pipeline (browser) |
| `app.py` | Step 3 — WooCommerce CSV + WebP images (browser, standalone; step 2 is the human review of `_filled.xlsx`) |
| `build_csv.py` | Step 3 headless — same formatter, no browser |
| `validate_csv.py` | Step 4 — QA before import |
| `wc_publish.py` | Step 5 — publish to WooCommerce over the REST API |
| `wc_seo.py` | SEO meta back-fill on products ALREADY live (§3) |
| `wc_checkpoint.py` | Snapshot / restore live product meta (§3) |
| `docs\AUTOMATION_OPPORTUNITIES.md` | What else could be automated, ranked |
| `docs\KNOWN_ISSUES.md` | Diagnosed bugs with line anchors and acceptance tests |
| `client_config.py` | Loads `clients\<slug>\client.toml` + `.env` — every per-client value and path (§3) |
| `clients\<slug>\category_map.xlsx` | Category taxonomy — output categories use these exact strings |
| `docs\CLIENT_CONFIG.md` | The per-client design contract: layout, `client.toml`, publish guards, credit ledger, pack |
| `requirements.txt` | All dependencies for every root script |
| `README.md` | Quickstart commands |
| `docs\PIPELINE_GUIDE.md` | This document |
| `docs\data_flow.svg` | Visual pipeline diagram (open in a browser) |
| `docs\claude_prompt.md` | Manual-lookup prompt for flagged rows |

Generated at runtime: `<name>_filled.xlsx`,
`product_images_for_upload\<client>\`, `wordpress_ready_clean_repaired.csv`,
`validation_issues.csv`, `clients\<client>\credits.json`,
`runs\<name>\review_metrics.json`.

### Folder layout

```
Final_pipe\
  *.py  *.md  requirements.txt      the pipeline and its docs (in git)
  clients\<slug>\                   ALL per-client config (§3). No taxonomy file
                                    is at the repo root any more:
    client.toml                     store URL, [sku] scheme, [serper] budgets
    category_map.xlsx               taxonomy - CONFIG, kept in git on purpose
    category_seo.xlsx               curated tags + SEO keywords (in git)
    SubCategory_Attributes_Master.xlsx   attributes master - app.py READS it: its
                                    'Sub-Category Attributes' sheet and its filter
                                    sheet drive the filter attributes and
                                    `_spec_gaps`. category_map.xlsx holds copies of
                                    both sheets and is only the fallback.
    backups\ .env credits.json      editor backups, credentials, ledger (NOT in git)
  <name>.xlsx  <name>_filled.xlsx   the batch you are working on
  product_images_for_upload\<slug>\ images for the CURRENT batch, per client
  checkpoints\<slug>\               store snapshots, per client (NOT in git)
  batches\<name>-<YYYY-MM>\         finished batches, archived (NOT in git)
                                    + batches\<name>.zip, the one-file hand-over
  .claude\                          agents and skills
  Sku_enrichment\                   enricher-only mirror - CODE ONLY, no data
  _push_export\                     nested export repo, own remote
```

Two working rules that follow from this:

- **`product_images_for_upload\<slug>\` is resolved from the repo**, not from the
  current directory, so `build_csv.py`, `validate_csv.py` and `wc_publish.py`
  agree on it wherever you run them. To work on an archived batch again, copy its
  `images\` back into that folder first.
- **Archive a batch when it is published**, into `batches\<name>-<YYYY-MM>\`.
  Keep the `.xlsx` inputs in the root so the batch stays resumable, and leave a
  short README in the archive saying what is in it.

## 6. First-time setup

```
cd D:\Dhruv-Personal\Catalogue\Final_pipe   :: or wherever you cloned it -
                                            :: every command below runs from the repo root
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
setx SERPER_API_KEY "your_key_here"
```
(Open a NEW Command Prompt after `setx` so the key is picked up. Per client, the
key can instead live in `clients\<slug>\.env` as `SERPER_API_KEY=...`, under
whatever name `[serper] api_key_env` gives — that file wins over the shell.)

`clients\shreejee\` ships with the repo and is the default client. Set up a
second one by copying that folder, editing `client.toml` and dropping in its own
`category_map.xlsx` (§3).
