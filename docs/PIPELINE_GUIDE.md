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
  SubCategory_Attributes_Master.xlsx   reference copy, read by no script (in git)
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

:: --- cache ---
:: what is cached, how big        --cache-info
:: throw it all away              --cache-clear
:: ignore it for this run         --no-cache
:: fetch fresh but keep the copy  --refresh-cache     (use when prices moved)
:: age at which entries stop counting (0 = never)      --cache-ttl 14
:: keep the cache somewhere else  --cache-dir PATH
```
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
original input list into `batches\<name>\`, writes a README with `## Coverage`
(status buckets), `## Review` (the line above) and `## Files`, and zips the whole
folder to `batches\<name>.zip` — one file to hand over. `build_csv.py --pack`
produces the same pack from the headless route.

**Command:**
```
.venv\Scripts\python.exe -m streamlit run gui.py
```

`app.py` still exists and still works on its own (just the CSV-build step) —
useful if you already have a `_filled.xlsx` from a script-driven run and only
need the formatter, without the rest of the wizard's state-tracking.

### app.py — WooCommerce formatter (browser app)

**What it does:** takes the filled file and produces the import CSV: brand +
category detection (Washing Machines > Top load / Front Load / Semi Auto, AC,
LED, etc.), SKU normalization + WM-/WH-/AP- prefixes, duplicate-SKU removal,
description/short-description/configuration HTML, WooCommerce attribute
columns, and downloads every image as `SKU_n.webp` into
`product_images_for_upload\<client>\` (the SKU prefixes come from the client's
`[sku]` table, `WM-`/`WH-` never renamed) — the Images column only lists files
that really downloaded.

**Controls in the app:** upload the `_filled.xlsx` directly (Excel accepted);
"Import only verified rows" (on by default — uses the status column);
WordPress uploads folder YYYY/MM (must match the month you'll upload images);
optional "estimate MRP as sale x 1.25" (off by default); optional
**"Add SEO meta columns"** (off by default — see below). After Generate, a
**Quality check** panel runs validate_csv.py on the result and lists anything
that needs a look, so QA is part of generating the file rather than a separate
step to remember.

**Attributes from the retailer's spec table:** when the filled sheet carries a
`specs` column, those facts become real WooCommerce attributes on top of the
ones read from the title. Retailer wording is canonicalised (`Display Size` and
`Screen Size` are one attribute, not two), invoice fields (ASIN, rank, reviews)
are dropped, one row per attribute name, capped at `MAX_ATTRIBUTES` (15) so the
CSV stays a usable width.

**Attribute completeness gate:** the `Sub-Category Attributes` sheet already
declares which axes matter per sub-category (an AC has a Ton, a TV has a Size).
Read as a checklist, it becomes a gate: any axis a product cannot fill from its
title, its specs or the sheet's own vocabulary is written to a `_spec_gaps`
column, summarised in a warning, and reported by validate_csv.py as
`missing_required_attribute`. It **flags, it does not block** — a missing axis
is often just a terse title, and the human review step decides.

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
- **byte-identical galleries across SKUs.** On this file the URL-based
  cross-row checks *cannot* fire, because app.py renames every image to
  `<sku>_<n>.webp` — so the files themselves are compared. Caught on the LED
  batch: `UA32H4500FUXXL` and `UA32H4510FUXXL` shipped the same eight photos,
  meaning one matched the other's listing.
- **price above the category ceiling** as well as below its floor — a bundle,
  a dealer pack, or an MRP read into the price field.
- **`missing_required_attribute`** — the axes `_spec_gaps` recorded.

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
| `--pack` | Also write `<out stem>_pack\` — CSV, `validation_issues.csv`, the images the CSV references, the input list and a README — and zip it. The same deliverable the GUI's Archive button builds |
| `--client SLUG` | Which client's config to use (default: `PIPELINE_CLIENT`, then `shreejee`) |

The QA pass runs by default the moment the CSV is written, so a problem is
reported while you are still looking at the build rather than one step later.

Images are written to `product_images_for_upload\<client>\` **inside the repo**
(anchored on the script, not on the current directory), so the same run from any
folder puts them in the same place.

`--pack` is the hand-over route: it produces one folder plus one
`<out stem>_pack.zip` containing the CSV, the validation issues, every image the
CSV references, the original input list and a README with `## Coverage`,
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
    SubCategory_Attributes_Master.xlsx   REFERENCE COPY ONLY - no script reads it.
                                    The attributes app.py actually enforces live in
                                    the 'Sub-Category Attributes' SHEET of
                                    category_map.xlsx (app.load_subcategory_attributes)
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
