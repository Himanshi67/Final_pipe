# Final_pipe — Shree Jee catalog pipeline

Turns a bare SKU list into a verified WooCommerce import for shreejeeelectronics.in
(home appliances). **No LLM is called anywhere in this code.**

    SKUs.xlsx → sku_enricher.py → <stem>_filled.xlsx → [human review]
             → app.py (Streamlit) | build_csv.py (headless, same code path)
             → WooCommerce CSV + product_images_for_upload\*.webp
             → validate_csv.py → validation_issues.csv
             → WooCommerce import (manual) | wc_publish.py (REST API)

- `sku_enricher.py` (~2,700 lines, plain functions) — per SKU: brand/model split →
  serper.dev search (DuckDuckGo fallback) → fetch pages (curl_cffi, browser
  impersonation) → site parsers (amazon/flipkart/croma/reliance/ifb/generic JSON-LD,
  dispatched in `extract()`) → spec-table scrape → strict `assess()` verdict →
  `hunt_donors`/`merge_sources` to complete the row → write row → checkpoint-save.
  Interruption-safe; re-running resumes. Pages and searches are cached in `.cache\`,
  so a re-run does no network I/O. Writes `<output>_report.md` at the end.
- `app.py` (~1,300 lines, Streamlit) — `_filled.xlsx` → WooCommerce CSV; categories snap
  to exact strings in `category_map.xlsx`; images saved as `SKU_n.webp`; scraped
  specs become WooCommerce attributes; `_spec_gaps` records attributes the
  sub-category requires but nothing filled; optional SEO meta columns (off by default).
- `build_csv.py` — imports app.py, calls the same `process_inventory()` from a CLI.
- `validate_csv.py` — QA gate for BOTH stages; writes `validation_issues.csv`. Also
  imported and run automatically by `build_csv.py` and the app after generation.
- `wc_publish.py` — upserts products over the WooCommerce REST API (keyed on SKU).
  Dry-run unless `--commit`; creates drafts unless `--status publish`.

## Environment
- Windows 11 + PowerShell. Python is ALWAYS `.venv\Scripts\python.exe` — never bare
  `python`, never "activate".
- `SERPER_API_KEY` (serper.dev) drives search: up to ~8 credits/SKU; free tier
  2,500/month ≈ 300 SKUs. No in-code credit tracking.
- Statuses in `_filled.xlsx`: `FOUND (site)` · `FOUND … - no price shown` /
  `- only N image(s)` (verified but incomplete) · `FOUND … - price from <site>`
  (merged from another source) · `CLOSE MATCH - CHECK` · `CHECK MANUALLY` ·
  `NOT FOUND` · `ERROR: …`
- `.cache\` (gitignored) holds gzipped pages and search results, TTL 14 days.
  Only successes are cached; a cache error is always a miss, never a failure.

## Commands (full flag semantics: PIPELINE_GUIDE.md §3 — don't guess flags)
    .venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --limit 3          # smoke
    .venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --min-price 8000 --delay 5 8
    .venv\Scripts\python.exe sku_enricher.py "LIST.xlsx" --retry-incomplete # re-do half-filled rows
    .venv\Scripts\python.exe sku_enricher.py --probe "<url>"                # test ONE page, no spreadsheet
    .venv\Scripts\python.exe sku_enricher.py --cache-info                   # what the page cache holds
    .venv\Scripts\python.exe -m streamlit run app.py
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
- `KNOWN_ISSUES.md` — pre-diagnosed bugs with line anchors + acceptance tests. Read it
  BEFORE diagnosing any enricher misbehavior; most failure modes are already there.
- `PIPELINE_GUIDE.md` — authoritative operations guide (flags, app.py controls, taxonomy).
- `claude_prompt.md` — legacy manual prompt for humans; never load as instructions.

## Session hygiene
`/clear` between unrelated tasks · `/context` to audit window usage · long batches run
via the runner agent or in background, never watched in the main loop · reference
spreadsheets by path, don't paste their contents into chat.
