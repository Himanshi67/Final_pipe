# Issues audit — 2026-07-28

> **STATUS NOTE (2026-08-11):** superseded where it disagrees by
> **docs/PRODUCTION_READINESS.md** (every item below re-verified against the
> code on 2026-08-11; A-4/6/7/8/10/11 fixed, A-5 resolved by operator
> decision, A-9 deliberately kept as-is).

A deliberate hunt for defects and risks across `sku_enricher.py`, `app.py`,
`validate_csv.py`, `build_csv.py`, `wc_publish.py`, `wc_seo.py`, and the repo
itself. Everything below was **checked against the running code or the live
store**, not inferred. Where a scan came back clean that is stated too — a
negative result is worth as much as a finding.

Severity: **HIGH** = silently wrong data reaches the storefront ·
**MED** = data loss, wasted work, or a wrong decision by an operator ·
**LOW** = robustness, cost, hygiene.

---

## Fixed during this audit

### A-1 · HIGH · comma-joined URL lists were shredding Amazon URLs — FIXED
**Found by:** noticing one product turned 12 source URLs into 5 files.

Image lists are stored comma-joined, and every consumer split on `,`. But
Amazon rendition modifiers **legitimately contain commas**:

```
https://m.media-amazon.com/images/I/71XA+N8Xj1L._AC_UL165_SR165,165_.jpg
                                                            ^ split here
```

A three-URL list therefore parsed as **eight fragments**, five of them
unusable strings like `165_.jpg` and `85`. Each fragment was then counted as an
image, fetched, and failed — so the row claimed images it never had, and the
failures were swallowed (see A-2).

**Fixed:** `split_image_list()` in all four modules splits only where a comma is
followed by a URL start, with a fallback to plain splitting for non-URL lists.
Verified: the real 3-URL example now yields 3 in every module, was 8.

### A-2 · MED · image download failures were completely silent — FIXED
`download_urls_for_sku` catches every exception per image and appends nothing.
A network failure, a 404, a broken URL — all indistinguishable from "not there".
This is what hid A-1 for so long: the count just quietly dropped.

**Fixed:** rejects are collected and reported by reason. Size rejections were
already named; transport failures now are too.

### A-16 · HIGH · a social share banner was the storefront thumbnail — FIXED
**Found by:** the end-to-end run producing a 1200×628 first image.

`collect_images` puts everything the page *declares* about itself in tier 1 —
JSON-LD, `itemprop`, then `og:image`. On any page without JSON-LD (haier.com,
among others) the `og:image` therefore becomes the first image, and the first
image is the storefront thumbnail.

But `og:image` is a **social share banner**: 1200×628, the product on a coloured
background with marketing text baked in. Measured across the LED batch:
**9 of 37 products led with one.**

**Fixed:** `photos_before_banners()` demotes images at the OpenGraph 1.91:1
ratio behind normally-shaped ones. It **reorders, never drops** — the banner is
still a picture of the product, it just should not lead. 16:9 television shots
(1.78) are deliberately untouched, which the tests assert.
`validate_csv.py` also now flags a banner-shaped *first* image.
**Verified:** the same product went 1200×628 → 1920×1080 as its primary.

### A-17 · MED · site branding was reaching product titles — FIXED
`clean_title` stripped a hardcoded list of store names, so anything nobody
thought of survived. haier.com's `<title>` ends `…H32K82FX (2025)-Haier India`,
and **8 of 43 titles** carried a suffix like it straight onto the storefront.

**Fixed:** a general `SITE_BRAND_SUFFIX` — a short trailing segment, after a
dash or pipe, ending in a site-ish word (`India`, `.in`, `.com`, `Online`,
`Electronics`, `Appliances`). Length-capped and keyword-anchored, and only
applied while ≥20 characters of title survive, so it cannot gut a short name.
**Verified against all 43 real titles:** 8 cleaned, 0 false positives —
`Voltas AC - Made in India` is deliberately left alone because stripping it
would leave too little behind.

### A-3 · LOW · image probing was serial — FIXED
The quality pass measures every gallery image plus up to six rendition
candidates. Done one at a time that is ~18 sequential round trips per listing.

**Fixed:** `probe_many()` runs them in a pool of 8. Measured over the five
test galleries: **8.5s → 4.9s** for 39 images. Also switched the probe off the
shared per-domain session, which was not safe to use from threads.

---

## Open — worth fixing

### A-4 · MED · `validate_csv.py` almost always exits 0
It exits non-zero only on a missing file. A run that finds 23 problems still
exits 0, so it cannot gate anything — a batch script or scheduled run cannot
tell "clean" from "16 rows are wrong".
**Fix:** exit 1 when any row has an issue, with a `--warn-only` escape.
**Why it matters:** the QA gate is currently advisory even when automated.

### A-5 · MED · `AP-` prefix on every non-washer SKU
`prefixed_sku()` gives `WM-` to washing machines, `WH-` to water heaters and
`AP-` to *everything else*, so televisions ship as `AP-SAMSUNG-…`. It is
consistent with the existing catalogue, which is why it was left alone, but it
means the SKU prefix carries no information for most of the range.
**Decision needed** before the catalogue grows further; changing it later
renames live products.

### A-6 · MED · no retry on WooCommerce API calls
`Store.call` raises on the first failure. A transient 502 or a connection reset
mid-batch aborts the row; `wc_publish.py` then continues to the next product,
so a flaky minute can leave a batch half-published.
**Fix:** retry idempotent verbs (GET/PUT) two or three times with backoff.
POST must not be retried blindly — it would duplicate products.

### A-7 · MED · media uploads are sequential
32 images take roughly a minute of wall clock. It no longer risks a cookie
expiry now the CDN challenge is off, but it is the slowest part of publishing
and the uploads are independent.
**Fix:** a small thread pool, as `app.py` already uses for downloads.

### A-8 · LOW · curl_cffi sessions are never closed
`_SESSIONS` holds one Session per domain for the life of the process and
nothing closes them. Harmless on a 43-SKU batch; on a long unattended run
across many domains it leaks sockets.
**Fix:** `atexit` teardown, mirroring the Playwright shutdown that already exists.

### A-9 · LOW · the workbook is rewritten after every SKU
`safe_save()` writes the whole `.xlsx` per row — deliberate, because it is what
makes an interrupted run resumable. It is O(n²) in rows: negligible at 43,
noticeable in the hundreds.
**Fix if it ever bites:** save every N rows plus on exit and on `KeyboardInterrupt`.
Do not "optimise" this without keeping the interrupt-safety.

### A-10 · LOW · `_IMG_DIMS` grows without bound
One entry per probed URL for the process lifetime. ~10k entries on a large
batch, a few MB. Fine today; worth a cap if batches get much larger.

### A-11 · LOW · tiny Amazon nav thumbnails are still collected
The same product had `._SS40_` (40px) and `._AC_UL165_` URLs in its gallery —
navigation strip thumbnails, not product photos. The new measurement pass drops
them, but only after fetching each one.
**Fix:** treat `_SS<n>_` / `_UL<n>_` under ~200 as junk at collection time and
save the probe.

---

## Checked and clean

These were scanned specifically and found **not** to be problems:

- **Division by zero** — every `reg / sale`, `latin / len(letters)` and
  `max/min` ratio is guarded (`sale > 0`, a length check, or `min(...) and`).
- **Missing HTTP timeouts** — every `requests`/`curl_cffi` call passes one; the
  initial grep hits were all continuation lines.
- **Mutable default arguments** — none.
- **Credentials in code** — none. `WC_KEY`, `WC_SECRET`, `SERPER_API_KEY`,
  `WP_APP_PASSWORD` are read from the environment only, and a scan of the full
  pushed git tree found no key material. This is why the `cookie.txt` incident
  was survivable.
- **Dry-run write safety** — a `DryStore` test asserts zero non-GET calls, after
  two real incidents (taxonomy terms, then media uploads).
- **Thread safety of the new probe memo** — dict assignment is atomic in
  CPython; the worst case is probing one URL twice.

---

## Repo and operational risks

### A-12 · HIGH · a live admin session reached a public repo — CONTAINED
`cookie.txt` (WordPress `wordpress_logged_in_*` + `wordpress_sec_*`) was
committed and pushed to a public GitHub repo. Session invalidated by the owner;
file purged from history with `git filter-repo` and force-pushed; verified
absent from a fresh clone.
**Still open:** GitHub serves the pre-rewrite commit by direct SHA until it
garbage-collects. Ask GitHub Support to GC, or delete and recreate the repo.
**Root cause:** `.gitignore` existed only on the working branch, so on `main`
nothing was ignored and `git add` swept the file in. Now committed on `main`.

### A-13 · MED · two branches had diverged into "code" and "data"
`clean-snapshot` carried every script; `main` carried the batch artefacts and
cache. A branch switch emptied the working tree of the entire pipeline.
**Resolved:** consolidated onto `main`; `clean-snapshot` is now redundant and
can be deleted once you are confident.

### A-14 · MED · 100 MB Node distribution in the public repo
`Code_Unrelated/_tools/node-v24.18.0-win-x64/node.exe` is 88 MB and trips
GitHub's file-size warning on every push. Unrelated to this pipeline.
**Fix:** untrack and gitignore, as was done for `.cache/` and `batches/`.

### A-15 · LOW · the `_push_export` mirror can drift silently
Four copies of `sku_enricher.py` are kept in sync by hand via a skill, verified
by hashing. Nothing *enforces* it, so an edit to the wrong copy is possible.
**Fix if it ever bites:** a pre-commit hook comparing hashes.

---

## Deliberately not "fixed"

- **KI-13, plain LED TVs categorised as Mini LED.** Needs a taxonomy decision;
  the owner chose to leave it.
- **The broad `except Exception` blocks in the enricher (22 of them).** Each one
  is a deliberate "a single bad page must not kill a 43-SKU batch". They are a
  smell in general and correct here. The ones that mattered — silent image
  failures — are fixed above.
- **Money handled as `float`.** Prices are parsed to float and re-formatted as
  strings for WooCommerce. Wrong in principle for currency; harmless here
  because nothing does arithmetic on them beyond a ratio comparison.
