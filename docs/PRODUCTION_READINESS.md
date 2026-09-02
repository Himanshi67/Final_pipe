# Production readiness — audited 2026-08-11

Full-repo debug pass: every documented issue re-verified against current code,
all test suites run, new UI suite added, end-to-end smoke on scratch data.
This file supersedes the STATUS columns in ISSUES_AUDIT.md / KNOWN_ISSUES.md
where they disagree — those docs were last audited 2026-07-28 and several
"open" items have since been fixed.

## Test baseline — the gate (collected 2026-09-02; last full run: 169 passed, 0 failed, 0 skipped)

The whole suite is one command, and it makes no network call and touches no
live store: `.venv\Scripts\python.exe -m pytest tests -q --no-header -p
no:cacheprovider` (the `gate` skill runs it after a `py_compile` of the nine
pipeline scripts). `pytest` 9.x IS a dependency now and lives in `.venv`;
`tests\conftest.py` supplies what the runner needs — the repo root on
`sys.path`, `STREAMLIT_SERVER_HEADLESS=true`, a `tmp` fixture for the GUI
suite's signature, and a `collect_ignore` for the three `qa_*.py` operator
scripts. 122 tests collect across nine files:

| Suite | Tests collected | Standalone script too |
|---|---|---|
| tests\test_blog_automate.py | 14 | yes |
| tests\test_break_handler.py (needs an attached console, else skips) | 2 | yes |
| tests\test_bugfixes.py | 30 | yes |
| tests\test_category_coverage.py | 1 | yes |
| tests\test_crawl_findings.py | 4 | yes |
| tests\test_gui_helpers.py | 76 | **no — pytest only** |
| tests\test_gui_runmgr.py (archive_run + category-map editor, real functions) | 4 | yes |
| tests\test_gui_ui.py (Streamlit AppTest, headless) | 21 | yes |
| tests\test_hd_images.py | 3 | yes |
| tests\test_polite_sleep.py | 7 | yes |
| tests\test_two_pass.py | 7 | yes |
| **total** | **169** | |

"Tests collected" counts pytest test *functions*. Every suite except
test_gui_helpers.py still carries an `if __name__ == "__main__"` runner
(`.venv\Scripts\python.exe tests\test_bugfixes.py`), and that mode reports a
finer-grained tally — one line per assertion — which is why this file used to
record e.g. "98 passed" against the 23 functions pytest sees in
test_bugfixes.py. Both modes exercise the same code; the pytest run is the gate.

The GUI suite covers: boot, run creation, step gating/locks, per-step render
against seeded artifacts, status bucketing, Indian-format price parsing.

Outside the gate: `tests\qa_pipeline.py`, `tests\qa_cli.py` and
`tests\qa_deep.py` are operator runbooks rather than unit tests — they shell
out to the real scripts and want a live network and Serper credits, so they are
run deliberately, never as part of the gate; `conftest.py` keeps pytest from
collecting them. They no longer need a hardcoded absolute repo path or the
gitignored `inputs\LED.xlsx` — each resolves its own root and builds an inline
SKU fixture.

## End-to-end smoke (2026-08-11, scratch data, no writes)

2 real SKUs from the latest batch, warm cache (1 serper credit spent):
enricher → 2/2 FOUND · build_csv → 2 products, 2 with images, no warnings ·
validate_csv → 0 issues, exit 0 · `wc_publish.py --check` → connected
(WooCommerce 10.8.1 / WP 7.0.3, media creds OK) · dry run → both products
would CREATE as drafts with prices, attributes and 8 images each. Nothing
committed. One observation: spec/attribute coverage varies by category
(15 attrs vs 1 on the dispenser) — data coverage, not a defect.

## Issue ledger (re-verified against code, not the docs)

### Fixed before this audit (docs still said open)
- **A-4** validate_csv exits 1 on issues, `--warn-only` escape (validate_csv.py:782)
- **A-6** WooCommerce calls retry with exponential backoff (wc_publish.py:221)
- **A-8** curl_cffi sessions closed via atexit (sku_enricher.py:1617)
- **KI-7a** fetch() logs exception class instead of swallowing (sku_enricher.py:1399)
- **KI-7g** looks_blocked scans head+tail 150K/20K (sku_enricher.py:1063) — title
  scan still 20KB-limited, acceptable
- **KI-15 stopgap** `--cookie-file` + strip_wp_session wired (wc_publish.py:90,596)

### Fixed in this audit (2026-08-11)
- **A-7** media uploads parallelised, order-preserving 4-thread pool
  (wc_publish.py `images_for`) — was ~1 min per 32 images, sequential
- **validate_csv:671** front/top-load check no longer corrupts the comparison
  blob via `str.replace`; checks categories+description directly

### Open — needs a DECISION from the operator, not code
- **KI-15 (infrastructure): NOT currently blocking.** Verified live
  2026-08-11: `wc_publish.py --check` reached the store with no cookie
  (WooCommerce 10.8.1 / WordPress 7.0.3) and a 2-SKU dry run assembled
  correctly. If Hostinger's "I'm under attack" mode is ever re-enabled the
  403s return; the `--cookie-file` workaround stays wired for that day.
- **KI-13 — operator decided 2026-08-11: keep as-is.** Plain LED TVs stay in
  "Mini LED" until a plain-LED bucket is wanted; the fix (add "LED TV" to
  category_map.xlsx + require the literal phrase in app.py) is trivial when
  that day comes.
- **A-5 — RESOLVED 2026-08-11 (operator decision): per-category prefixes.**
  `app.SKU_PREFIX_BY_PARENT` now maps every taxonomy parent (WM/WH kept for
  live products; TV/AC/CF/KA/HA/SA/EC new; AP- fallback for unknown parents).
  validate_csv's prefix regex and the enricher's CATEGORY_CODES extended to
  match; pinned by tests\test_bugfixes.py test_19. Products already published
  as AP-… (drafts) get the new SKU on their next publish.

### Fixed in this audit, round 2 (2026-08-11)
- **A-10** `_IMG_DIMS` memo now clears at 20 000 entries — bounded memory,
  worst case is a re-probe.
- **A-11** Amazon nav thumbnails (`._SS40_` etc.) dropped at collection time
  when every size token is < 200 px — no wasted probe per thumb. hiRes URLs
  come first, so real gallery images are never tiny-only.
- **KI-7b** ddg_search cool-down is 30 s only for rate-limit exceptions,
  10 s for any other hiccup.

### Review round (2026-08-11, 7-angle adversarial review of the day's commits)
All fixed and pinned by tests:
- `prefixed_sku` was being fed `family_for()` output, so all six FAMILY_BY_SUB
  product families (microwaves, kettles, irons, dispensers…) silently fell to
  AP- — now takes the parent; stale/contradicting prefixes are re-coded
  (WM-/WH- never renamed: live products).
- `expected_caps` unit bail-out fired on Samsung H-/LG V-series model codes,
  blinding the capacity guard for major washer brands — now bails only on
  glued, terminal unit letters.
- `parse_amazon`'s tiny-thumbnail filter could empty a sparse page's gallery
  whose only reference was a small rendition — now keeps candidates when
  filtering would leave zero (upgrade pipeline recovers them).
- GUI publish gating: pre-scope-tracking CSVs are refused (was warn-and-arm-
  all); the CSV on disk is verified against `built_skus`; the >200-SKU case
  chunks `--sku` pinning instead of silently dropping it.
- GUI `pid_alive` identifies the enricher by process CREATION TIME vs the
  stored launch timestamp (a syscall) instead of spawning `tasklist` on
  every 2-second tick; tasklist remains a memoized fallback for old metas.
- Scope radio uses stable value keys; DDG rate limits matched in the message
  too; `_IMG_DIMS` evicts half instead of clearing; `media_creds()` is the
  single (stripped) reader of WP_APP_*; validate_csv's prefix regex is a
  module constant cross-pinned against `app.SKU_PREFIXES` by test_19.

Accepted risks (reviewed, deliberate):
- Parallel media uploads share the requests Session (cookie-jar races are
  theoretical; auth is basic, not cookie-based) and can orphan Media Library
  items when a gallery fails mid-upload — `find_media`'s name+size dedupe
  reclaims them on the retry.
- **Migration note:** products already published as AP-… (drafts only — live
  products are WM-/WH- and keep their SKUs) get a new prefixed SKU on their
  next publish; wc_publish upserts by SKU, so the old AP- DRAFT remains as a
  duplicate. Clean up with `wc_publish.py --delete-sku AP-… --commit` when
  republishing an old batch.
- Terminal V-series codes (LG "P75VBAZ") still skip the capacity guess —
  over-bailing is the safe direction (a wrong expected capacity rejects real
  pages; a skipped guess only weakens one check).

### Open — code hardening, deliberately deferred
- **A-9** whole-workbook save after every SKU (O(n²), deliberate for
  interrupt-safety). If large batches get slow: save every N rows + on exit +
  on Ctrl+C.
- **KI-7c** `is_product_url` defaults True for unknown domains (:875) —
  INTENTIONAL, do not "fix": unlisted stores are parsed via generic JSON-LD,
  and a False default would drop every donor from a store the allowlist has
  never heard of.
- **KI-7h** Excel lock checked once at start only (:3019) — KI-4 recovery
  file mitigates.

## Go-live checklist
1. Env: `SERPER_API_KEY`, `WC_URL`, `WC_KEY`, `WC_SECRET`, `WP_APP_USER`,
   `WP_APP_PASSWORD` set in the shell that launches the pipeline/GUI.
2. Hostinger CDN security mode OFF (or a fresh `cookie.txt` per session).
3. `wc_publish.py --check` prints the store version — the acceptance test
   for KI-15.
4. Enrich → review in the GUI (`streamlit run gui.py`) → build CSV from
   confirmed rows → validation shows 0 issues (exit 0) → dry-run → commit
   as drafts → spot-check drafts in wp-admin → `--status publish`.
5. Serper budget: ~8 credits/SKU worst case; free tier ≈ 300 SKUs/month.
   Warm cache replays are free — check `--cache-info` before `--force`.
