# Final_pipe — Shree Jee catalog pipeline (quickstart)

A batch data pipeline that turns a bare SKU list into live WooCommerce products
on shreejeeelectronics.in.

**No LLM is called anywhere in the product pipeline** — the same SKU always
produces the same output, and that is deliberate. (`blog_automate\` is a
separate, opt-in tool that can use one for prose only; see the bottom.)

```
  SKUs.xlsx
     |  sku_enricher.py      search + scrape + verify        (extract)
     v
  <name>_filled.xlsx  ->  [YOU: review the flagged rows]
     |  app.py  (browser)  or  build_csv.py  (headless)      (transform)
     v
  wordpress_ready_clean_repaired.csv  +  product_images_for_upload\<client>\*.webp
     |  validate_csv.py     QA gate (runs automatically after build_csv.py)
     v
  WooCommerce  <-  Products > Import (by hand)  or  wc_publish.py (REST API)
```

Per-client configuration (`clients\<slug>\`, the publish guards, the credit ledger):
**docs/CLIENT_CONFIG.md**.
Full documentation: **docs/PIPELINE_GUIDE.md** (every flag, every column, where data
goes). Visual overview: **docs/data_flow.svg**. Pre-diagnosed bugs with line anchors:
**docs/KNOWN_ISSUES.md**. Release checklist and the test baseline:
**docs/PRODUCTION_READINESS.md**. Getting past the store's CDN setting:
**docs/HOSTINGER_ACCESS.md**. What else could be automated: **docs/AUTOMATION_OPPORTUNITIES.md**.
The 2026-08-22 GUI refit was designed up front in
**docs/superpowers/specs/2026-08-22-ui-refit.md**. Its four shipped plans are in
`_not_in_use\docs\superpowers\plans\`. Files that no code or doc uses: **_not_in_use/README.md**.
Hardening backlog (48 verified findings): **docs/NEXT_STEPS.md** (local only - not in
the repository). How this is being productized: **docs/PRODUCT_PLAN.md** (local only -
not in the repository). Older narrative state: **docs/HANDOFF.md**
(written 2026-07-28 - predates the GUI refit; trust CLAUDE.md and this file first).

## One-time setup

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Needs **Python 3.11 or newer**. That install also brings in `pytest`, which runs the
quality gate — `.venv\Scripts\python.exe -m pytest tests -q` must be green before any
change ships.

Python is always `.venv\Scripts\python.exe` — never bare `python`, never "activate".

**Clients.** Every store-specific value lives in `clients\<slug>\`:
`client.toml` (store URL, media base, SEO name, the `[sku]` prefix scheme,
`[serper]` budgets), `category_map.xlsx`, `category_seo.xlsx`,
`SubCategory_Attributes_Master.xlsx`, plus a gitignored `.env`, `backups\` and
`credits.json`. `clients\shreejee` is the default client — a NEW client is
`clients\shreejee` copied to `clients\<newslug>`, `client.toml` edited (its
`slug` must equal the folder name), that client's `category_map.xlsx` dropped
in, and a `.env` created with its credentials. Every script then takes
`--client <slug>`; without it the slug comes from `PIPELINE_CLIENT`, then
`shreejee`. Contract: **docs/CLIENT_CONFIG.md**.

Credentials go in `clients\<slug>\.env` (gitignored) or in environment
variables, **never in the repo**. The client's `.env` WINS over the shell env,
so a stale `setx WC_URL` cannot redirect a publish. Open a new terminal after
`setx`, or the value will not be visible.

`.env` grammar: one `KEY=VALUE` per line; `#` comments and blank lines are
ignored; a leading `export ` is tolerated; surrounding single or double quotes
are stripped. Any other line is an error naming its line number.

| Variable | Where to get it | Needed for |
|---|---|---|
| `SERPER_API_KEY` | free at serper.dev | search (falls back to DuckDuckGo without it) |
| `WC_URL` | your store URL | `wc_publish.py`, `wc_seo.py` |
| `WC_KEY` / `WC_SECRET` | WooCommerce > Settings > Advanced > REST API (Read/Write) | products, categories, tags |
| `WP_APP_USER` / `WP_APP_PASSWORD` | WP Users > Profile > Application Passwords | uploading images via the API |

WooCommerce keys authenticate `/wc/v3` only. The Media Library lives at
`/wp/v2`, which is why image upload needs a separate Application Password.
An Application Password is **24 characters in six space-separated groups** —
not your account password — and must be quoted in `setx`.

Serper costs about 8 credits per SKU; the free tier of 2,500/month is roughly
300 SKUs. `--budget N` stops a run once N credits are spent (progress saved,
report written, exit 0, exactly like Ctrl+C); `0` means unlimited and the
default comes from the client's `[serper] default_run_budget`. Spend is tallied
in `clients\<slug>\credits.json`, and the batch report's header prints
`Client:`, `Credits:` and `Stopped by:`.

## Every batch

```
:: 1. put your SKU list in this folder, then fill it in (--client defaults to shreejee):
.venv\Scripts\python.exe sku_enricher.py "WM MODEL LIST FOR WEBSITE.xlsx" --client shreejee --min-price 8000 --delay 5 8 --budget 400

:: 2. QA the enricher output BEFORE reviewing it by hand - this tells you which
::    rows to look at, including two SKUs that resolved to the same listing:
.venv\Scripts\python.exe validate_csv.py "WM MODEL LIST FOR WEBSITE_filled.xlsx"
::    then review the flagged rows (see "Statuses" below)

:: 3. format for WooCommerce - browser:
.venv\Scripts\python.exe -m streamlit run app.py
::    (upload the _filled.xlsx, set the upload month, Generate, download the CSV here)
::    ...or headless, same code path, identical output:
.venv\Scripts\python.exe build_csv.py "WM MODEL LIST FOR WEBSITE_filled.xlsx" --month 2026/07

:: 4. QA before anything goes live. build_csv.py already ran this on the file
::    it just wrote, so this is only needed after the browser route or a hand edit:
.venv\Scripts\python.exe validate_csv.py wordpress_ready_clean_repaired.csv

:: 5a. publish by hand: upload product_images_for_upload\<client>\*.webp to WordPress
::     Media in the SAME month you set above, then WooCommerce > Products > Import
:: 5b. or publish over the API (no media upload, no CSV import):
.venv\Scripts\python.exe wc_publish.py --check
.venv\Scripts\python.exe wc_publish.py --preflight
.venv\Scripts\python.exe wc_publish.py wordpress_ready_clean_repaired.csv
.venv\Scripts\python.exe wc_publish.py wordpress_ready_clean_repaired.csv --commit
```

`wc_publish.py` **never writes without `--commit`**, creates products as drafts
unless `--status publish`, and upserts on SKU so re-running a corrected batch
updates instead of duplicating. Passing `--status` explicitly also applies it to
products that already exist — that is how a reviewed draft goes live.

Before it writes anything it checks the blast radius, and **exit code 4 means
refused — nothing was written**:

- `WC_URL` must be the same store as the client's `[store] url` (a plain dry run
  only warns; `--commit`, `--check` and `--preflight` refuse).
- `--commit` writes at most `--max-commit` products (default 25). Dry runs are
  never capped.
- `--commit --status publish` and `--commit --delete-sku` need
  `review_signoff.csv` (`sku,reviewer,signed_at`) next to the CSV, listing every
  SKU going live. The GUI writes it when you arm a go-live commit.
- Every `--commit` takes a `before-publish-<slug>` checkpoint first, unless you
  pass `--no-checkpoint` — which means no way back.
- `--preflight` runs the store-URL, API, media-credential and sign-off checks and
  exits without writing (0 pass, 3 edge challenge, 4 refused).

## Image quality

Product photos are the one thing a customer judges instantly, so the pipeline is
opinionated about them.

It is fixed **at source** in `sku_enricher.py`, so the `_filled.xlsx` itself
carries good images and everything downstream inherits them:

- **Galleries are measured, not guessed.** A 64 KB Range request reads each
  file's header, so the row's image count only includes photos that really are
  usable. Anything that cannot be measured is *kept* — a CDN refusing a Range
  request must not cost you a good photo.
- **Each CDN is upgraded to its original rendition, verified.** The rules are
  tried against the first image of a gallery and whichever measurably returns
  the largest file is applied to the rest. Measured live:

| Source | Captured | After |
|---|---|---|
| Amazon `._SL1500_.` | 1500×927 | **2392px** |
| Amazon `._AC_SX466_.` | 466×298 | **2390px** (5×) |
| Tata CLiQ `320Wx357H` | 320×357 | unchanged — it 404s on upsize, so it is left alone |
| haier.com | 1920×1080 | already the original |

```
.venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --min-image-px 500
.venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --no-image-check   # skip it
```

And again at download time in `app.py`:

| Rule | Value | Why |
|---|---|---|
| Ask the CDN for its largest rendition | — | belt and braces: an older `_filled.xlsx` captured before the enricher learned this still gets full resolution, with no re-scrape and no credits |
| `WEBP_QUALITY` | 95 | WooCommerce re-encodes its own thumbnails from this master, so softness compounds |
| `MAX_IMAGE_EDGE` | 2400, **downscale only** | never upscale — enlarging invents detail and looks worse than the original |
| `MIN_IMAGE_EDGE` | 1000, skipped + named | raised from 500: a 500 px master goes soft in the zoom/lightbox; one image fewer beats a blurry one |
| `GOOD_IMAGE_PX` | 1000, QA flag | usable but soft; reported by `validate_csv.py`, never silently shipped |

The rewrite runs at **download** time as well as scrape time, so an existing
`_filled.xlsx` gets full resolution without re-scraping a page or spending a
credit — just re-run `build_csv.py` on it.

Scoped to Amazon hosts and Magento paths on purpose: Tata CLiQ 404s without its
resize params, and a broken URL costs the image entirely.

## Statuses in the _filled.xlsx

| Status | Meaning |
|---|---|
| `FOUND (site)` | verified and complete — these go live automatically |
| `FOUND ... - price from <other site>` | merged: the photos and the price came from different sources, both verified as the same model |
| `FOUND ... - no price shown` / `- only N image(s)` | verified but incomplete; retry with `--retry-incomplete` |
| `FOUND ... - unlisted store, verify` | matched on a domain we do not recognise — eyeball it |
| `FOUND ... - MRP <reason>, check` | the MRP failed a sanity test (at/below price, or >4× it) |
| `FOUND ... - gallery below Npx floor, verify` | every photo is smaller than the HD floor; the build may drop them |
| `FOUND ... - gallery matches row N (SKU), verify` | another row already claimed this exact photo set — one of the two is the wrong product |
| `CLOSE MATCH - CHECK` / `CHECK MANUALLY` | your review list |
| `NOT FOUND - no usable search results` | search returned nothing worth fetching |
| `NOT FOUND - all sources blocked (...)` | the sites blocked us; retried automatically next run |
| `NOT FOUND - N source(s) blocked, ...` | some sites blocked us and the rest did not match; retried next run |
| `NOT FOUND - N page(s) checked, none matched` | genuinely not found; needs manual research |
| `ERROR: ...` | something broke on that row; the rest of the batch continued |

## Useful flags

```
:: re-do only the half-filled rows, keeping the good cells
.venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --retry-incomplete

:: re-attempt previous NOT FOUND / ERROR rows
.venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --retry-missing

:: a site is blocking us, or its price only appears in the browser
.venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --js-fallback

:: test one product page without touching any spreadsheet
.venv\Scripts\python.exe sku_enricher.py --probe "<product url>"

:: build only the first few rows, for a smoke test
.venv\Scripts\python.exe build_csv.py "LIST_filled.xlsx" --limit 3

:: add SEO meta title / description / focus keyword / slug (OFF by default)
.venv\Scripts\python.exe build_csv.py "LIST_filled.xlsx" --seo
.venv\Scripts\python.exe build_csv.py "LIST_filled.xlsx" --seo --seo-plugin rankmath

:: back-fill SEO meta on products ALREADY live (including pre-pipeline ones)
.venv\Scripts\python.exe wc_seo.py                    # DRY RUN
.venv\Scripts\python.exe wc_seo.py --commit           # fills only what is EMPTY
.venv\Scripts\python.exe wc_seo.py --overwrite --commit

:: stop a run once N serper credits are spent (progress saved, report written)
.venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --budget 200

:: build the hand-over pack (CSV, issues, images, input list, README) and zip it
.venv\Scripts\python.exe build_csv.py "LIST_filled.xlsx" --pack

:: check the store, API, media creds and sign-off file without writing anything
.venv\Scripts\python.exe wc_publish.py --preflight

:: publish or clean up a single product
.venv\Scripts\python.exe wc_publish.py out.csv --sku AP-XYZ --commit
.venv\Scripts\python.exe wc_publish.py out.csv --sku AP-XYZ --status publish --commit
.venv\Scripts\python.exe wc_publish.py --delete-sku AP-XYZ --commit

:: what the cache holds / re-run an old batch through improved parsers for free
.venv\Scripts\python.exe sku_enricher.py --cache-info
.venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --force      # replays cached pages
.venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --refresh-cache  # prices moved
```

`wc_seo.py` never changes the **slug** of a live product unless you pass
`--slugs`. Changing a slug changes the URL: existing links break and whatever
ranking that URL had is discarded. Free on a new product, a decision on a live one.

Every run writes `<LIST>_filled_report.md` beside the workbook: what was found,
which rows still need a human and why, Serper credits spent, and which domains
blocked the run. `--no-report` turns it off.

## Rules of thumb

- **Never feed a `_filled.xlsx` back into the enricher.** It is the enricher's own
  output; re-running on it used to build `_filled_filled.xlsx` and re-pay every
  search credit. The script now refuses and prints the command you meant.
- `--force` is much cheaper than it used to be: with the cache warm it replays
  stored pages through the current parsers instead of re-downloading and
  re-paying credits. Check `--cache-info` first — if the cache is cold or
  expired, `--force` still costs full price on every row.
- Never open the `_filled.xlsx` in Excel while the enricher is running — it will
  pause and wait for you to close it, then divert to a `_recovery` file.
- The `_filled.xlsx` already has every column app.py needs (`sku`, `categories`,
  `Brands` included). Upload it as-is; no column editing required.
- Only `FOUND` rows are exported by default. `--all-rows` (headless) or the
  checkbox (browser) includes the rest.
- Set the upload month to the month you will **actually** upload the images in —
  it is baked into the image URLs.
- Every folder a script owns is anchored on the script's own location, so it no
  longer matters which directory you run from: images go to
  `product_images_for_upload\<client>\`, checkpoints to `checkpoints\<client>\`,
  and the taxonomy comes from `clients\<client>\`.
- When a batch is published, move its outputs to `batches\<name>\` and leave the
  `.xlsx` inputs in the root so it stays resumable. Archived batches are not in
  git — that folder is the only copy.
- `clients\<slug>\category_map.xlsx`, `category_seo.xlsx` and
  `SubCategory_Attributes_Master.xlsx` **are** in git, unlike every other
  `.xlsx`. They are configuration: without the taxonomy, app.py cannot map a
  single category. `SubCategory_Attributes_Master.xlsx` is a REFERENCE COPY that
  no script reads — the attributes actually enforced live in the
  `Sub-Category Attributes` sheet of `category_map.xlsx`
  (`app.load_subcategory_attributes`), so edit them there. The client's `.env`,
  `backups\` and `credits.json` are NOT in git.

## Known blocker — the store is behind a browser challenge

shreejeeelectronics.in sits behind Hostinger's CDN "I'm under attack" mode,
which returns HTTP 403 to every non-browser request — including `/robots.txt`,
so search-engine crawlers get challenged too. The keys are never reached, so
this is **not** an auth problem, and `wc_publish.py` says so rather than dumping
raw HTML at you.

**Proper fix** — hPanel (a *separate* login from WordPress, at
hpanel.hostinger.com, using the hosting account credentials):
**Websites > Dashboard > CDN > Manage > Security**, move the level off
"I'm under attack".

**Stopgap** — borrow the clearance cookie your own browser already earned:

1. open `https://shreejeeelectronics.in/robots.txt` in Chrome; you should see
   plain text, not "Checking your browser"
2. F12 → Network → untick **Preserve log** → Ctrl+R
3. click the request and **check its Status is 200** — copying from the
   challenge response gives the pre-clearance token, which looks identical and
   never works
4. copy the entire `Cookie` value into `cookie.txt`, and the `User-Agent` too

```
.venv\Scripts\python.exe wc_publish.py --check --cookie-file cookie.txt --user-agent "<paste>"
```

> **Measured reality, 2026-07-26.** Across six attempts the cookie worked three
> times and each clearance lasted **minutes** — one died 90 seconds in, part way
> through uploading a 5-product batch's images. It is fine for a handful of
> products and **will not carry a 43-product batch**. For a real batch, either
> get the CDN setting changed or use the manual route.

`cookie.txt` is gitignored — it is a live session credential, treat it like a
password. Prefer `--cookie-file` over `--cookie`: a value pasted on the command
line lands in your shell history.

**The manual route is unaffected** — your browser passes the challenge, so
Media upload + WooCommerce > Products > Import work today.

Details in docs/KNOWN_ISSUES.md (KI-15).

## What is on the store right now

The five-product test batch lives in `batches\test\` (CSV, images, stage-1
input, and a README with every id). As of the last run, all five exist as
**drafts** carrying the full-resolution images, except one product still on its
older images, and `AP-SAMSUNG-UA32H4560FUXXL` (id 13844) awaiting publication.
`batches\test\README.md` is the authority — it is updated after every push.

## blog_automate\ — separate, opt-in

A hybrid blog generator for the same store. Kept isolated: nothing in this
folder imports it, and it borrows only `wc_publish.py`'s session handling.

The product pipeline stays LLM-free. In `blog_automate` the **facts** are still
deterministic — `blog_brief.py` builds every fact a post may state, as data —
and only the *prose* is optionally generated. `blog_verify.py` then requires
every number and product name in that prose to trace back to the brief, or the
post is held. See `blog_automate\README.md`.
