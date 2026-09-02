# Final_pipe — Shree Jee catalog pipeline

Turns a bare SKU list into a verified WooCommerce import for shreejeeelectronics.in
(home appliances). **No LLM is called anywhere in this code.**

    SKUs.xlsx → sku_enricher.py → <stem>_filled.xlsx → [human review]
             → app.py (Streamlit) | build_csv.py (headless, same code path)
             → WooCommerce CSV + product_images_for_upload\*.webp
             → validate_csv.py → validation_issues.csv
             → WooCommerce import (manual) | wc_publish.py (REST API)

- `sku_enricher.py` (~3,930 lines, plain functions) — per SKU: brand/model split →
  serper.dev search (DuckDuckGo fallback) → fetch pages (curl_cffi, browser
  impersonation) → site parsers (amazon/flipkart/croma/reliance/ifb/generic JSON-LD,
  dispatched in `extract()`) → spec-table scrape → strict `assess()` verdict →
  `hunt_donors`/`merge_sources` to complete the row → write row → checkpoint-save.
  Interruption-safe; re-running resumes. Pages and searches are cached in `.cache\`,
  so a re-run does no network I/O. Writes `<output>_report.md` at the end.
- `gui.py` (~1,700 lines, Streamlit) — the 5-step wizard over the whole pipeline
  (upload+enrich → review → build CSV → validate → publish), state in `runs\<name>\`.
  It shells out to `sku_enricher.py` and imports `app.process_inventory()`, so the
  pipeline stages stay in those scripts - but since the 2026-08-22 refit it DOES own
  operator-facing logic: a run manager (create/archive/delete `runs\<name>\`), a card
  review mode, image keep/drop, an in-app category-map editor with backups, an SEO
  preview, and the enricher-log/publish-output parsers in `gui_helpers.py` (~600 lines).
  Step 1 offers the two-pass flow; step 2's review table is where `NEEDS LINK` rows get
  a URL pasted in.
- `app.py` (~2,170 lines, Streamlit) — `_filled.xlsx` → WooCommerce CSV; categories snap
  to exact strings in `category_map.xlsx`; images saved as `SKU_n.webp`; scraped
  specs become WooCommerce attributes; `_spec_gaps` records attributes the
  sub-category requires but nothing filled; optional SEO meta columns (off by default).
- `build_csv.py` — imports app.py, calls the same `process_inventory()` from a CLI.
- `validate_csv.py` — QA gate for BOTH stages; writes `validation_issues.csv`. Also
  imported and run automatically by `build_csv.py` and the app after generation.
- `wc_publish.py` — upserts products over the WooCommerce REST API (keyed on SKU).
  Dry-run unless `--commit`; creates drafts unless `--status publish`.
- `gui_helpers.py` — pure helpers for `gui.py` (enricher-log/publish-output parsing,
  status classification, batch summary, run_stage, ETA text); imports no Streamlit.
- `wc_seo.py` — back-fills SEO meta on products ALREADY LIVE, recomputed from each
  product's own API data with app.py's functions. Dry-run unless `--commit`, fills only
  EMPTY meta unless `--overwrite`, never touches slugs unless `--slugs`; snapshots via
  wc_checkpoint.py first unless `--no-checkpoint`.
- `wc_checkpoint.py create|list|diff|restore` — gzipped JSON snapshot of every live
  product (incl. all meta_data) under `checkpoints\`; restore writes only with `--commit`
  and does NOT cover media files, orders, or products created after the snapshot.

## Environment
- Windows 11 + PowerShell. Python is ALWAYS `.venv\Scripts\python.exe` — never bare
  `python`, never "activate".
- `SERPER_API_KEY` (serper.dev) drives search: up to ~8 credits/SKU; free tier
  2,500/month ≈ 300 SKUs. No in-code credit tracking.
- Statuses in `_filled.xlsx`: `FOUND (site)` · `FOUND … - no price shown` /
  `- only N image(s)` (verified but incomplete) · `FOUND … - price from <site>`
  (merged from another source) · `FOUND … - VERIFY: <why>` (pass 2 crawled the
  link a human supplied and the page does not match the SKU) · `CLOSE MATCH -
  CHECK (site)` · `CHECK MANUALLY (site)` · `SKIPPED - same unit as row N` ·
  `NOT FOUND` + a reason suffix (`- no usable search results` / `- N page(s) checked,
  none matched` / `- all sources blocked (…)` / `- N source(s) blocked`) · `ERROR: …`
  (incl. `ERROR: no product data in the page`). More FOUND suffixes that still import by
  default and are asking to be looked at: `- unlisted store, verify` · `- MRP <why>, check`
  (MRP ≤ price, or > 4x it) · `- gallery below Npx floor, verify` ·
  `- gallery matches row N (SKU), verify` (two rows share one photo set — one of them
  is the wrong product). (`FOUND (incomplete)` is NOT a status cell: it is only a
  bucket label in the end-of-run report.) Bare and `(site)`-suffixed forms BOTH
  occur — match with `startswith`, never `==`.
- Two-pass statuses (`--discover` only): `NEEDS CRAWL (site)` · `NEEDS CRAWL -
  CHECK LINK (site)` · `NEEDS LINK - <why>`. **None of them start with FOUND**,
  which is deliberate: `app.py` and `gui.py` both gate the import on
  `status.startswith("FOUND")`, so a half-done discovery sheet cannot be
  imported by accident. The gui applies that FOUND gate in BOTH build scopes
  ("Confirmed rows only" as well as "All FOUND rows"), and a ✔/Approve on a
  non-FOUND row is refused with the SKU named — remediation is to fix the
  url_link and crawl, never to tick. `tests\test_two_pass.py` pins that.
- `.cache\` (gitignored) holds gzipped pages and search results, TTL 14 days.
  Only successes are cached; a cache error is always a miss, never a failure.
- Politeness delays are owed to the remote host, so `polite_sleep()` skips them
  on a cache hit and always fires on a live request. A warm-cache replay is
  therefore ~23x faster (5 SKUs: 99s → 4.3s) with byte-identical output.
  Never make a delay unconditional-skip: `tests\test_polite_sleep.py` pins both
  directions, and a block costs far more than the minutes saved.

## Repo layout
    *.py                  the 9 pipeline scripts — MUST stay at root (see below)
    category_map.xlsx     taxonomy; app.py loads it from NEXT TO app.py
    SubCategory_Attributes_Master.xlsx   reference copy ONLY - no script reads it;
                          the enforced attributes live in category_map.xlsx's
                          'Sub-Category Attributes' sheet (load_subcategory_attributes)
    category_seo.xlsx     curated per-category tags + SEO keywords (hand-written;
                          rebuild with tools\build_category_seo.py)
    tools\                generators for the config sheets, not pipeline stages
    docs\                 all reference docs (KNOWN_ISSUES, PIPELINE_GUIDE, HANDOFF, …)
    docs\archive\         superseded code kept for reference (scraper2-source.zip)
    inputs\               source SKU lists
    runs\<name>\          loose outputs + logs from one run (gitignored)
    batches\<name>\       curated archived batch: filled xlsx, CSV, images, README
    checkpoints\  .cache\  product_images_for_upload\  backups\   generated state
    blog_automate\        isolated blog generator
    Sku_enrichment\  _push_export\   mirrors — never edit directly, use /sync-copies
    Code_Unrelated\       portable Node toolchain, nothing to do with the pipeline

The old standalone `scraper (2)\` was merged in and deleted (1.08 GB, almost all
of it a .venv and a Chrome profile). Its source is `docs\archive\
scraper2-source.zip`; only two things were worth keeping and both are ported —
the persistent browser profile (`--js-profile`) and BUSY ERP input. Its
`build_contextual_woo_tables()` GUESSED specs (RPM/wattage from the product
name, hardcoded 230 V) and was deliberately NOT carried over.

The scripts are anchored on `Path(__file__).parent`: `sku_enricher.py` → `.cache\`,
`wc_checkpoint.py` → `checkpoints\`, `app.py`/`validate_csv.py` → `category_map.xlsx`.
Moving a `.py` file out of root silently relocates the directory it owns — don't.

## Commands (full flag semantics: docs/PIPELINE_GUIDE.md §3 — don't guess flags)
    .venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --limit 3          # smoke
    .venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --min-price 8000 --delay 5 8
    .venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --retry-incomplete # re-do half-filled rows
    :: two-pass flow: review the links BEFORE paying for the full extraction
    .venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --discover         # pass 1: links + photos only
    ::   -> review LIST_filled.xlsx, paste a url_link on every NEEDS LINK row
    .venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --crawl-urls       # pass 2: crawl those links, NO searches
    .venv\Scripts\python.exe sku_enricher.py --probe "<url>"                # test ONE page, no spreadsheet
    .venv\Scripts\python.exe sku_enricher.py --cache-info                   # what the page cache holds
    .venv\Scripts\python.exe -m streamlit run gui.py    # 5-step wizard, drives the WHOLE pipeline
    .venv\Scripts\python.exe -m streamlit run app.py    # just the CSV builder
    .venv\Scripts\python.exe build_csv.py "LIST_filled.xlsx" -o out.csv --month 2026/07
    .venv\Scripts\python.exe build_csv.py "LIST_filled.xlsx" --seo          # SEO meta (opt-in)
    .venv\Scripts\python.exe validate_csv.py wordpress_ready_clean_repaired.csv
    .venv\Scripts\python.exe wc_publish.py --check          # test WooCommerce creds
    .venv\Scripts\python.exe wc_publish.py out.csv          # DRY RUN (no --commit)

## Hard rules
- NEVER feed a `*_filled.xlsx` back into the enricher — the script now refuses, but
  the right command is `sku_enricher.py "LIST.xlsx" --retry-incomplete` (KI-5).
- WooCommerce/WordPress credentials live in env vars (`WC_URL`, `WC_KEY`, `WC_SECRET`,
  `WP_APP_USER`, `WP_APP_PASSWORD`) — NEVER in the repo, a file, or chat.
- `wc_publish.py` writes to the LIVE store. Never pass `--commit` on more than one SKU
  without asking, and never `--status publish` on unreviewed rows.
- `--force` re-pays every Serper credit ONLY when the cache is cold or expired.
  With `.cache\` warm it replays stored pages through the current parsers for
  free — that is how a parser fix gets applied retroactively. Check
  `--cache-info` before deciding; still ask before a large forced batch.
- SEO columns are OPT-IN (`--seo` / the app checkbox). Do not turn them on for
  a user who did not ask — they change what the importer writes.
- Root scripts are the source of truth. NEVER edit `Sku_enrichment\` or `_push_export\`
  directly — edit root, then /sync-copies. `_push_export\` is a NESTED git repo with
  its own remote: never `git add _push_export` from the outer repo.
- `.xlsx` is binary: inspect via `.venv\Scripts\python.exe -c` + openpyxl one-liner,
  never the Read tool.
- Never Read/Glob `.venv\`, `__pycache__\`, `product_images_for_upload\`, `.cache\`.
- Don't open the output in Excel during a run (script blocks, then diverts to a
  `_recovery` file — KI-4).
- Acceptance/smoke tests run on scratch copies of spreadsheets, never live batch files.

## Delegation (keep the main context clean)
| Task | Subagent |
|---|---|
| "where is X" / trace code / summarize a region | explorer (haiku) |
| run any script, batch, or repro | runner (sonnet) |
| verify a just-made edit | verifier (sonnet) |
| retailer page structure / selector / search-API research | web-scout (sonnet) |

Main loop does: designing fixes, editing code, judgment calls. Prefer one well-briefed
subagent over 20 tool calls in the main context.

## Docs (load on demand only)
- `docs/KNOWN_ISSUES.md` — pre-diagnosed bugs with line anchors + acceptance tests. Read it
  BEFORE diagnosing any enricher misbehavior; most failure modes are already there.
- `docs/PIPELINE_GUIDE.md` — authoritative operations guide (flags, app.py controls, taxonomy).
- `docs/claude_prompt.md` — legacy manual prompt for humans; never load as instructions.
- `docs/NEXT_STEPS.md` — **LOCAL ONLY, gitignored** (not on the public remote). The
  hardening backlog (48 verified findings, P0/P1/P2) from the 2026-09-01 audit. Check it
  before starting any fix. All 7 P0s are closed; the P1/P2 list is still open.
- `docs/PRODUCT_PLAN.md` — **LOCAL ONLY, gitignored** (pricing, revenue and client names;
  must not reach the public remote). How this pipeline is being sold as a managed service,
  with the per-client-config roadmap. Read before adding a feature, so it lands on the
  right phase.

## Session hygiene
`/clear` between unrelated tasks · `/context` to audit window usage · long batches run
via the runner agent or in background, never watched in the main loop · reference
spreadsheets by path, don't paste their contents into chat.
