---
name: run-batch
description: Run a full SKU enrichment batch end-to-end - pre-flight checks, smoke run, full run, post-run triage and handoff to app.py. Use when the user says "run the batch", "enrich this file", or drops a new SKU list.
---

# Run an enrichment batch

## 1. Pre-flight
- Input exists, is `.xlsx`/`.csv`, and is NOT already a `*_filled.xlsx` (never feed
  output back in — KI-5).
- **Which client?** Confirm the slug and pass `--client <slug>` on every command
  (or set `PIPELINE_CLIENT`); without it the run silently uses `shreejee`. The
  taxonomy, the image folder, the serper key and the budget all come from
  `clients\<slug>\` — see docs/CLIENT_CONFIG.md.
- Row count via openpyxl one-liner → credit math: rows × ~8 Serper credits vs the
  client's `[serper] monthly_budget` (2,500/month on the free tier), minus what
  `clients\<slug>\credits.json` already records for this month. Warn the user if
  the batch may exhaust the quota.
- **Set `--budget N`** for anything but a small batch: the run stops at N credits
  with progress saved and the report written (exit 0, same path as Ctrl+C), which
  is how an overspend is prevented rather than discovered afterwards. `0` =
  unlimited; the default is the client's `[serper] default_run_budget`.
- Serper key present? It comes from the variable named by `[serper] api_key_env`
  (default `SERPER_API_KEY`), read from `clients\<slug>\.env` first and the shell
  environment second. Liveness-test with ONE cheap call — must print 200:

      .venv\Scripts\python.exe -c "import os; from curl_cffi import requests as r; print(r.post('https://google.serper.dev/search', headers={'X-API-KEY': os.environ.get('SERPER_API_KEY',''), 'Content-Type':'application/json'}, json={'q':'test','num':1}, timeout=20).status_code)"

- Ask for the price band when the category is obvious (e.g. `--min-price 8000` for
  washing machines) — it kills accessory false-positives.

## 2. Smoke
Delegate to the **runner** agent: `--limit 3` on the real input. Expect ≥1 FOUND.
Instant all-NOT-FOUND → stop and re-check the key (KI-1).

## 3. Full run
Runner agent, in background:
`sku_enricher.py "<file>" --client <slug> --budget N --min-price N --delay 5 8`.
Checkpoints after every SKU; safe to interrupt. Do NOT stream the log into the main
context.

## 4. Post-run triage
- Status counts + list of CHECK MANUALLY / CLOSE MATCH / NOT FOUND rows
  (openpyxl one-liner via runner).
- NOT FOUND rows → offer a `--retry-missing` re-run.
- FOUND-but-incomplete rows (no price / few images) → `--retry-incomplete`
  (see /retry-incomplete for files made by older builds).
- Tell the user which rows need human review before stage 2.

## 5. Handoff (the "movement" downstream)
User runs `.venv\Scripts\python.exe -m streamlit run app.py`, uploads the
`_filled.xlsx`, sets the WordPress upload month, downloads the CSV into this folder →
`validate_csv.py` → upload `product_images_for_upload\<slug>\*.webp` to WordPress Media
(same month) → WooCommerce > Products > Import. Details: docs/PIPELINE_GUIDE.md §2–3.
