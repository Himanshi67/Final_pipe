# Handover — Final_pipe, state at 2026-07-28

Read this, then `CLAUDE.md`. Load `KNOWN_ISSUES.md` / `ISSUES_AUDIT.md` only
when diagnosing something.

## What this is

A batch data pipeline (ETL) turning a bare SKU list into live WooCommerce
products on **shreejeeelectronics.in** — home appliances and TVs, Shree Jee
Electronics.

**No LLM is called anywhere in the product pipeline.** The same SKU always
produces the same output, and that is the point of it. (`blog_automate\` is a
separate, opt-in tool that may use one for prose only; its *facts* stay
deterministic and are verified against a generated brief.)

```
SKUs.xlsx
  -> sku_enricher.py    search (serper.dev -> DuckDuckGo) + scrape + verify
  -> <stem>_filled.xlsx -> [HUMAN reviews the flagged rows]
  -> app.py (Streamlit) | build_csv.py (headless, same code path)
  -> wordpress_ready_*.csv + product_images_for_upload\*.webp
  -> validate_csv.py    (runs automatically inside build_csv.py)
  -> WooCommerce: Products > Import (manual) | wc_publish.py (REST API)
```

**Every stage is proven end to end.** Latest full run: 14/14 field checks on a
fresh SKU, product created, verified, deleted, store left as found.

## The scripts

| File | Role |
|---|---|
| `sku_enricher.py` | stage 1 — search, scrape, verify, merge, write `_filled.xlsx` |
| `app.py` | stage 2 — Streamlit formatter (categories, tags, specs, images) |
| `build_csv.py` | stage 2 headless — imports app.py, same `process_inventory()` |
| `validate_csv.py` | QA gate for BOTH stages; takes a `_filled.xlsx` or the CSV |
| `wc_publish.py` | stage 3 — upsert to WooCommerce over REST, keyed on SKU |
| `wc_seo.py` | back-fill SEO meta on products already live |
| `wc_checkpoint.py` | snapshot/restore products before a risky bulk change |

## Environment

- Windows 11 + PowerShell. Python is ALWAYS `.venv\Scripts\python.exe`.
- **Env vars are User-scope and NOT inherited by an already-open VS Code.**
  Read them explicitly:
  `[Environment]::GetEnvironmentVariable('WC_KEY','User')`. This bit twice.
- All set and working: `SERPER_API_KEY`, `WC_URL`, `WC_KEY`, `WC_SECRET`,
  `WP_APP_USER`, `WP_APP_PASSWORD`.
- WooCommerce keys authenticate `/wc/v3` only; the Media Library is `/wp/v2`,
  hence the separate WordPress Application Password (24 chars, six groups —
  not the account password, and it must be quoted in `setx`).
- Serper ≈ 8 credits/SKU, free tier 2,500/month. **The page/search cache means
  re-running a finished batch spends nothing** — that is what makes it worth
  replaying old batches through improved parsers (`--force` with a warm cache).
- `.xlsx` is binary — inspect with `python -c` + openpyxl, never the Read tool.
- Never Read/Glob `.venv\`, `__pycache__\`, `product_images_for_upload\`, `.cache\`.

## Repo

- Outer git root is the **parent `Catalogue` folder**, not `Final_pipe`.
- Branch **`main`**, fully pushed to `github.com/atavisticrystal6888/Pipe`
  (**public**). `clean-snapshot` is a stale leftover, safe to delete.
- `_push_export\` is a separate nested repo with its own remote. **Never
  `git add` it from the outer repo** — it records a gitlink no clone can
  resolve. It is gitignored now; commit inside it instead.
- `sku_enricher.py` exists in 4 copies; root is the source of truth, mirrored
  via the sync-copies skill and hash-verified.
- Gitignored on purpose and **only on disk**: `.cache\`, `batches\`, `backups\`,
  `product_images_for_upload\`, `Code_Unrelated\_tools\` (92 MB Node), all
  `*.xlsx` **except** `category_map.xlsx` and `SubCategory_Attributes_Master.xlsx`,
  which are configuration and are committed deliberately.

## Security incident — mostly closed, one step left

`cookie.txt` (a WordPress **admin session**: `wordpress_logged_in_*` +
`wordpress_sec_*`) was committed and pushed to the public repo.

Done: session invalidated by the owner ("Log Out Everywhere Else"); file purged
from history with `git filter-repo`; force-pushed; verified absent from a fresh
clone. No passwords or API keys were ever in the repo — they live in env vars.

**Still open:** GitHub serves the pre-rewrite commit by direct SHA until it
garbage-collects. Ask GitHub Support to GC, or delete and recreate the repo.

Root cause worth remembering: `.gitignore` existed only on the other branch, so
on `main` nothing was ignored and `git add` swept the file in.

## What is on the store

Five LED TVs from the test batch (`batches\test\`, which has a README with ids):

| SKU | id | status |
|---|---|---|
| AP-SAMSUNG-UA32H4560FUXXL | 13844 | **publish** |
| AP-HAIER-LE32A900G | 13791 | draft |
| AP-TCL-32S4K-QLED | 13809 | draft |
| AP-SAMSUNG-UA32T4350BKXXL | 13815 | draft |
| AP-SAMSUNG-UA43F5550FUXXL | 13824 | draft |

## KI-15 — RESOLVED

The store used to sit behind Hostinger's CDN "I'm under attack" mode, which
403'd every non-browser request including `/robots.txt`. **The owner changed the
setting.** `wc_publish.py --check` now works with **no cookie at all**. The
`--cookie-file` stopgap still exists for a store behind a challenge, but is not
needed here. `HOSTINGER_ACCESS.md` documents the setting if it ever returns.

## Recent work (all committed and pushed)

**Quality pass**, driven by profiling the real 43-SKU batch rather than guessing
— it showed 51% of verified rows had no MRP, one title came back in Arabic, and
two SKU pairs shared a gallery:
- `merge_sources()` fills a winner's gaps from other hits that verified as the
  same model, prefers a better-ranked donor, and **names the donor in the
  status** so a reviewer knows the price came from elsewhere.
- Locale guard (rejects non-English pages), MRP sanity, stale-cell clearing.
- `validate_csv.py` now checks the `_filled.xlsx` too — nothing did before — and
  catches cross-row duplicates, price implausibility and panel-class ceilings.

**Image quality**, fixed at source so the `_filled.xlsx` carries good images:
- Galleries are **measured** (64 KB Range read of the header), not guessed.
  Unknown is never treated as small — a CDN refusing a Range must not cost a photo.
- Each CDN is upgraded to its original rendition, **verified by measurement**:
  Amazon `._SL1500_.` → 2392px, `._AC_SX466_.` → 2390px (5×). Tata CLiQ wins
  nothing and is correctly left alone.
- `MIN_IMAGE_EDGE` 500 (skip), `MAX_IMAGE_EDGE` 2400 (downscale only),
  `WEBP_QUALITY` 95.

**Audit** (`ISSUES_AUDIT.md`) — 17 findings, 6 fixed. Highlights:
- Comma-joined URL lists were **shredding Amazon URLs** (their rendition
  modifiers contain commas), turning 3 URLs into 8 fragments.
- Image download failures were **completely silent**, which is what hid it.
- The **og:image share banner was the storefront thumbnail** on 9 of 37
  products — now demoted, never dropped (16:9 TV shots untouched).
- **Site branding in titles** (`…-Haier India`) on 8 of 43 — now stripped by a
  general rule with 0 false positives across all 43.
- `validate_csv.py` now exits 1 when it flags anything (`--warn-only` opts out);
  it always exited 0, so the gate could not gate.

## Open, by priority

1. **GitHub GC** — ask Support, or delete/recreate the repo (see above).
2. **9 products in `LED_filled.xlsx` still have banner primaries and branded
   titles.** Nothing live is affected. Before publishing the rest of that batch,
   re-run the enricher on it — `--force` replays from cache, no credits.
3. **KI-13** — every plain LED TV is filed as **Mini LED** (`app.py`), because
   the LED parent has no plain-LED sub-category. **The owner chose to leave
   it** — do not "fix" it unprompted; it needs a taxonomy decision.
4. **A-5** — `prefixed_sku()` gives `AP-` to everything that is not a washer or
   water heater, so TVs are `AP-…`. Consistent with the live catalogue; changing
   it renames live products. Decision, not a bug.
5. `ISSUES_AUDIT.md` A-6/A-7 — no retry on API calls, sequential media uploads.
6. `AUTOMATION_OPPORTUNITIES.md` — remaining items: 5 (completeness loop),
   11 (credit budgeting), 12 (scheduling).

## Hard rules

- NEVER feed a `*_filled.xlsx` back into the enricher (it refuses; the right
  command is `--retry-incomplete` on the original).
- NEVER `--force` a large batch **with a cold cache** — it re-pays every credit.
  Check `--cache-info` first; warm, it is free.
- NEVER edit `Sku_enrichment\` or `_push_export\` directly — edit root, then sync.
- `wc_publish.py` writes to the LIVE store. Never `--commit` more than one SKU
  without asking; never `--status publish` on unreviewed rows.
- Credentials in env vars only — never in the repo, a file, or chat. Do not
  print more than the first few characters of a secret.
- Acceptance tests run on scratch copies of spreadsheets, never live batch files.
- Run every script from `Final_pipe\` — `product_images_for_upload\` is resolved
  from the current directory.

## Traps that already cost time — don't re-learn these

- **A dry run must write NOTHING.** Twice, assembling a payload wrote as a side
  effect: resolving a tag created it, and building the image list uploaded it.
  If you extend `build_payload`, honour `store.allow_writes`.
- **An empty `images` array on an update DELETES the gallery.** WooCommerce
  reads `[]` as "remove every image". It cost a live product its photos. The
  key is now omitted rather than sent empty.
- **This store caches GET responses** (LiteSpeed). A cached "no such SKU" would
  create a duplicate instead of updating. GETs carry a cache-buster.
- **A browser cookie carries WordPress's session**, and `wordpress_sec_*` makes
  WordPress authenticate by session, find no REST nonce, and return 401 — which
  reads exactly like a bad API key.
- **`shop.haierindia.com` is a JS SPA** returning the same shell for every
  product URL; its "gallery" was nav tiles. Any new domain giving suspiciously
  uniform results deserves the same check.
- **Branch hygiene**: a checkout to a stale branch once emptied the working tree
  of the entire pipeline. Everything was in git; nothing was lost. Check
  `git rev-parse --abbrev-ref HEAD` before concluding a file "disappeared".

## Working style the owner has asked for

- Delegate to subagents to keep the main context clean (`explorer`, `runner`,
  `verifier`, `web-scout` — table in `CLAUDE.md`).
- **Measure, don't guess.** Profile the real batch before "improving" anything;
  capture fixtures and freeze a baseline before editing extraction code. Every
  significant fix in this project came from a measurement, and the two
  regressions that reached the owner came from the times that was skipped.
- Prove claims by measurement, not inference — the 401 above was diagnosed by
  sending three cookie combinations and comparing.
- Report blockers plainly with the exact setting to change, and be explicit
  about what was verified versus what was assumed.
