# Handover — SEO back-fill, parser optimisation, checkpoints, blog automation

Session of 2026-07-28/29. Read `CLAUDE.md` first, then this. `HANDOFF.md` is
the older, broader state document; where the two disagree, this one is newer.

---

## 1. The one thing to know

**1,279 live products had SEO meta written to them. It is fully reversible:**

```
.venv\Scripts\python.exe wc_checkpoint.py restore 20260729-103354-before-seo-reconstructed --commit
```

Drop `--commit` for a dry run. Verified by diffing the before/after checkpoints
offline: exactly six meta keys differ (`rank_math_title`, `_yoast_wpseo_title`,
`rank_math_description`, `_yoast_wpseo_metadesc`, and the two focus-keyword
keys). **Zero non-meta fields changed** — no names, prices, slugs, SKUs,
descriptions or categories were touched.

---

## 2. State changes to the store

| What | Result |
|---|---|
| SEO meta written | 1,279 products, 0 failures (3 transient drops swept up) |
| Meta titles | 1,279 · all ≤ 60 chars |
| Meta descriptions | 1,278 · all ≤ 155 chars |
| Focus keywords | 150 set; the rest **deliberately suppressed** |
| Slugs | **unchanged** — live URLs never rewritten |
| Hand-written meta | **kept** — only empty fields were filled |

Focus keywords are suppressed when more than 3 products would share one
(`MAX_SHARED_FOCUS` in `wc_seo.py`). A phrase 60 products compete for is worse
than no phrase.

### Two open decisions — yours, not mine

1. **Yoast AND Rank Math are both active.** Product pages therefore emit
   duplicate `description`, `og:description` and `canonical` tags. This
   **predates this work**. Both key sets are written so the meta is correct
   whichever survives — but disabling one is worth more than anything here.
2. **Purge the LiteSpeed cache.** The storefront serves stale tags until TTL
   expiry. `X-LiteSpeed-Cache: hit` is why the first verification looked like
   a no-op until the cache was bypassed with `?cb=<n>`.

---

## 3. KI-15 is CLOSED

The Hostinger CDN "under attack" challenge is **off**. `wc_publish.py --check`
connects with no cookie. `cookie.txt`, `--cookie-file` and `--user-agent` are
obsolete for normal use — the code paths remain for if it is ever re-enabled.

---

## 4. New tools

### `wc_checkpoint.py` — snapshot and roll back products

```
wc_checkpoint.py create --label before-x   # snapshot every product
wc_checkpoint.py list                       # PARTIAL captures are flagged
wc_checkpoint.py diff latest                # what changed since
wc_checkpoint.py restore latest             # dry run
wc_checkpoint.py restore latest --commit    # apply
```

`--fields meta_data` (default) rolls back plugin/SEO meta only; `--fields all`
also covers names, prices, descriptions, categories, tags, attributes, images.

**Covers:** product fields + full `meta_data`.
**Does NOT cover:** media library *files*, orders, customers, pages/posts,
plugin & theme settings, products created after (reported, never deleted) or
deleted after (reported, not recreated). It is a product-state checkpoint, not
a site backup — use Hostinger's snapshot for that.

`wc_seo.py --commit` now **auto-creates a checkpoint before its first write**
and refuses to run if that fails (`--no-checkpoint` overrides).

> **Ignore the 3-product checkpoints** in `checkpoints/`. They are partial
> captures and `list` now flags them. The only real restore points are the two
> **1,279-product** ones.

### `wc_seo.py` — SEO back-fill for products already live

```
wc_seo.py                                   # DRY RUN, whole catalogue
wc_seo.py --plugin both --commit            # write Yoast + Rank Math keys
wc_seo.py --sku ABC --commit                # one product
wc_seo.py --overwrite --commit              # replace hand-written meta (off by default)
wc_seo.py --slugs --commit                  # rewrite live URLs (off by default — breaks links)
```

Derives meta from each product's own live data using the same deterministic
functions `app.py` uses. No LLM.

### `blog_automate/` — hybrid blog generation (isolated)

```
blog_run.py roundup    --source out.csv --category LED --limit 5
blog_run.py comparison --source out.csv --sku A --sku B --llm
blog_run.py guide      --source out.csv --category "Washing Machines"
```

The design: `blog_brief.py` turns catalogue data into a brief of **facts**
(deterministic). `blog_render.py` renders it with templates — **this is the
default and needs no LLM**. `--llm` adds `blog_write.py`, where Claude sees the
brief and *nothing else* and writes prose only. `blog_verify.py` then rejects
any number, model code or brand not traceable to the brief, plus a list of
unsupportable claims. A failure discards the prose and falls back to templates.
`blog_publish.py` posts to WordPress as **draft** unless `--status publish`.

**Untested against the live API** — there is no `ANTHROPIC_API_KEY` set and no
`ant` CLI. The deterministic path is proven end-to-end; the `--llm` path is
tested only in shape. Without a key it falls back cleanly and says so.

---

## 5. Parser optimisation — `sku_enricher.py`

**2.03× faster (279 ms → 137 ms per page), output byte-identical.**

Measured by loading the pre- and post-commit versions in one process and
running both over the 189 real pages in `.cache/`; all six extracted fields
matched on all 189. Individual wins, each A/B'd before applying:

| Change | Gain |
|---|---|
| `collect_images` tier 1: five document scans → one | −82% |
| srcset selection: document-wide CSS selector → one pass | −95% |
| `mrp_from_strike`: seven attribute selectors → `strike_elements()` | −96% |
| `table_specs`/`bullet_specs`: `select()` → `find_all()` | −66% / −68% |
| `MRP_LABEL_RE`: `Was` was matching inside "**Was**hing" | −21% |
| `extract_specs`: sources were eager, so early-exit never fired | −15% |

The `Was`/`Washing` fix is **performance only** — the tag-group guard already
prevented a wrong MRP. (An earlier note in this session called it a correctness
bug; that was wrong.)

Also: `wc_publish.Store.call` now retries transport failures (4 attempts,
exponential backoff). This host drops connections — 5 occurrences in one day,
one of which killed a whole checkpoint capture mid-way. HTTP errors are **not**
retried; a 404 means the same thing every time.

---

## 6. Defects the live run exposed (all fixed)

1. **26% of products crashed** (335/1,279). WooCommerce meta values can be
   lists/dicts (`_elementor_page_assets: []`); `app.compact()` calls
   `pd.isna()`, which returns an *array* for those and raises. Guarded by
   `wc_seo.meta_text()`. → 0 failures.
2. `&amp;` leaking into focus keywords — WordPress returns names HTML-escaped.
   Fixed by `unescape_text()`.
3. 67 products sharing `voltas air conditioner`. Now spec tokens are folded in
   for products with no SKU, and over-shared keywords are dropped.
4. My own first fix for (3) produced `voltas prism1` from "PRISM1.25 Ton" —
   caught by a test and fixed with a lookahead.

---

## 7. Tests — ACTION NEEDED (still open, re-checked 2026-09-02)

**287 assertions across 6 suites, all passing at the time — but ACTION NEEDED:
those six files were never moved into the repo and are gone.** They were written
in that session's scratchpad (since cleared), so the assertions below survive
only as this list:

```
LOST (session scratchpad, never committed - none of these exist in tests\)
  test_enricher_new.py   61   cache, spec tables, merge, batch report
  test_app_new.py        67   scraped specs, spec-gap gate, SEO pass
  test_wc_seo.py         36   plan/fetch/safety defaults
  test_wc_seo_fixes.py   34   the four defects above
  test_blog.py           62   brief, render, and the verifier's failure modes
  test_integration.py    27   cache short-circuit, price hunt, meta passthrough
```

Plus benchmarks: `ab_commit.py` (old vs new parser), `bench_parse.py`,
`ab_opt.py`, `ab_images.py`, `make_pre_seo_checkpoint.py`.

The repo's own suite (`git ls-files tests`) covers different ground —
`test_bugfixes.py`, `test_two_pass.py`, `test_polite_sleep.py`,
`test_category_coverage.py`, `test_hd_images.py`, `test_crawl_findings.py`,
`test_break_handler.py`, `test_blog_automate.py`, plus the untracked
`test_gui_*.py` — so `wc_seo.py` and the SEO pass described here have **no
committed coverage**. Write what you need before changing them; the benchmark
scripts above are gone too.

---

## 8. Environment / gotchas

- Python is **always** `.venv\Scripts\python.exe`.
- Env vars are **User-scope and not inherited** by an already-open VS Code.
  Read them explicitly in PowerShell:
  `[Environment]::GetEnvironmentVariable('WC_KEY','User')`. This bit twice.
- `anthropic` 0.120.0 was installed into `.venv` for `blog_automate --llm`.
  No `ANTHROPIC_API_KEY` is set.
- `.cache/` holds 411 entries (189 pages, 222 searches, 22.9 MB). A re-run of
  a finished batch does **no network I/O** — this is what makes replaying old
  batches through improved parsers free.
- **The repo moved several times mid-session.** Work lives on `main` now;
  `clean-snapshot` is the older branch that held the pipeline while `main`
  didn't. If files appear to vanish, check the other branch before assuming
  data loss — that happened once this session and nothing was actually lost.
- Enricher mirrors (`Sku_enrichment/`, `_push_export/`) are currently **in
  sync**. Re-run `/sync-copies` after any enricher edit.

---

## 9. Still open

1. **Purge LiteSpeed cache** so the SEO meta is actually served.
2. **Decide Yoast vs Rank Math** and disable the other.
3. **Rewrite the six lost test suites** — the scratchpad they lived in is gone
   and none of them reached `tests\` (§7).
4. `blog_automate --llm` unproven against the real API (needs a key).
5. `AUTOMATION_OPPORTUNITIES.md` items 5, 11, 12 remain unbuilt
   (auto-completeness loop, Serper credit tracking, unattended scheduling).
6. Weekly scheduled SEO refresh was discussed but **not built** — `wc_seo.py`
   is the engine for it; it just needs a scheduler.

---

## 10. Hard rules (unchanged, still apply)

- Never feed a `*_filled.xlsx` back into the enricher — use `--retry-incomplete`.
- Never `--force` a large batch without asking (re-pays Serper credits when the
  cache is cold).
- Credentials in env vars only — never in the repo, a file, or chat.
- `wc_publish.py` / `wc_seo.py` write to the LIVE store. Dry-run first, always.
- Never edit `Sku_enrichment/` or `_push_export/` directly — edit root, then sync.
- `.xlsx` is binary: inspect with `python -c` + openpyxl, never the Read tool.
- Acceptance tests run on scratch copies, never live batch files.
