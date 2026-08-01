# Automation scan — what else could be automated

Scan date 2026-07-25, against the pipeline as it stands after Phase 3 + tags.
Ordered by (value ÷ effort). Nothing here is required; it is a menu.

**Built as of 2026-07-26: 1, 2, 3, 4, 6, 7, 8, 9, 10.**
Still open: 5 (automatic completeness loop), 11 (Serper credit *budgeting* —
the counting half now exists in the batch report), 12 (unattended scheduling,
which stays blocked by KI-15 for as long as publishing needs a hand-copied
cookie).

## Where the manual time actually goes today

    SKUs.xlsx
      -> sku_enricher.py                        automated (CLI)
      -> [YOU] review flagged rows               MANUAL, unavoidable in part
      -> app.py in a browser: upload, pick month, Generate, download   MANUAL GUI
      -> [YOU] upload *.webp to WordPress Media  MANUAL, ~N files per batch
      -> [YOU] WooCommerce > Products > Import   MANUAL
      -> validate_csv.py                         automated, but run by hand

Three of the six steps are hand-work, and two of those (media upload, CSV
import) are pure mechanical repetition.

---

## Tier 1 — biggest wins

### 1. Publish straight to WooCommerce over the REST API — BUILT (`wc_publish.py`)
Replaces BOTH manual steps: no media upload, no CSV import.
**Status:** written and unit-tested offline; blocked from running against the live
store by Hostinger's CDN challenge (KI-15), which 403s every non-browser request.
- `POST /wp-json/wc/v3/products` (upsert keyed on SKU), images uploaded via
  `POST /wp-json/wp/v2/media` and attached by id.
- Idempotent: re-running updates rather than duplicating, so a corrected batch
  can simply be re-pushed.
- Dry-run mode that diffs against what is already live before writing.
- Needs: WooCommerce consumer key/secret. Keep them in env vars, never in the repo.
- **Also unlocks true end-to-end testing** — create one product, verify it, delete it.

### 2. Headless mode for app.py — BUILT (`build_csv.py`)
`app.py` was Streamlit-only, so the formatter could not run unattended and no test
could assert its output. `build_csv.py` imports app.py and calls the same
`process_inventory()`, so the CSV is identical to the one the button produces.
The Streamlit calls inside `process_inventory` now go through `_ui()`, which returns
`st` under Streamlit and a console shim otherwise.

### 3. Cache fetched pages and search results — BUILT
Every successful page fetch and every search result is stored gzipped under
`.cache\`, keyed by URL / query. `fetch()` is now a cached front door to
`fetch_live()`, and `web_search()` sits in front of both providers.
- **Re-parsing a past batch with improved parsers now costs nothing** — the big
  one. `--force` replays stored HTML through the current parsers instead of
  re-downloading and re-paying every Serper credit, so a parser fix can finally
  be applied retroactively to rows already collected.
- Interrupted batches resume without refetching; JS-rendered pages are cached too,
  so an expensive browser round-trip is paid once.
- The stored pages are ready-made test fixtures.

Deliberate limits: **failures are never cached** (a transient 403 must not become
permanent), entries expire after `--cache-ttl` days (default 14, because a
fortnight-old price is not a price), and any cache error is treated as a miss —
it can slow a batch down, never fail one.

Flags: `--cache-info` · `--cache-clear` · `--no-cache` · `--refresh-cache`
(fetch fresh, still store) · `--cache-ttl DAYS` · `--cache-dir PATH`.

---

## Tier 2 — quality, mostly cheap

### 4. Best-of-both merge across sources — BUILT (`merge_sources`)
Brand sites give great titles and galleries but no price; marketplaces give price.
One source used to win the whole row and its gaps were never filled — measured on
the 43-SKU LED batch: **51% of verified rows had no MRP and 9 had no price at all**.
`merge_sources()` now fills the winner's missing fields from the other hits that
passed `assess()` as *strong* for the same SKU (so the model code was verified on
both sides), prefers a better-ranked donor, refuses a donor MRP that fails
`bogus_mrp()`, never overwrites a value the winner already has, and **names the
donor in the status** so the reviewer knows the price came from elsewhere.

**Extended 2026-07-26.** `merge_sources` could only ever merge what had already
been fetched, and the main loop spends its `FETCH_BUDGET` looking for a *winner*
rather than for the missing half of one — so a brand site that won on title and
gallery was still written out as "no price shown" while a marketplace listing
for the very same model sat unfetched in the candidate list. `hunt_donors()`
now spends up to `--merge-budget` (default 2) extra fetches, one per domain and
marketplaces first, purely to complete a row that is already verified. Donors
are held to the same `assess() == "strong"` bar, so a different product can
never donate a price. Also added: the gallery is topped up past the minimum to
`MAX_IMAGES` from other known retailers (a brand site routinely has eight photos
where the marketplace that won on price had two), a present-but-bogus MRP is
replaced rather than kept, and spec tables are merged the same way.
Nothing is spent when there is no verified winner to complete.

### 5. Automatic completeness loop
Run `--retry-incomplete` repeatedly until a pass yields no improvement, instead of
the operator noticing and re-running it.

### 6. Stronger QA gates in validate_csv.py — BUILT
Added on top of the original per-row checks:
- **the enricher's own output is now validated too.** `validate_csv.py <filled.xlsx>`
  runs stage-1 checks; previously nothing checked that file at all, which is where
  a wrong match is cheapest to catch — one spreadsheet cell versus a live product.
- **cross-row duplicate detection** (same source URL / same first image / identical
  gallery / same title across different SKUs) — the classic wrong-match signature,
  invisible to any per-row check. It found two SKU pairs sharing a full gallery in
  the LED batch.
- **price plausibility**: a floor per category (a ₹749 "washing machine" is an
  accessory), an MRP more than 4× the price, and a panel-class ceiling — a HD Ready
  TV priced like a 4K one. That last caught a 32" HD Ready set at ₹34,590.
- **title sanity**: not-in-English (a brand site's Arabic locale), suspiciously
  short, or missing the brand the SKU names.
- **stale-placeholder detection** so a CSV predating the KI-14 fix cannot be
  imported unnoticed.

**Completed 2026-07-26** — the two remaining bullets, plus two more the work turned up:
- **image resolution / aspect sanity.** The local `.webp` is opened (header only,
  so it stays cheap) and anything under 400 px on its long edge or beyond 3:1 is
  flagged. Found immediately on the LED batch: a **3x1 pixel**, a 94x46 and a
  60x60 icon had shipped as product images for `AP-HAIER-H32K82FX`.
- **byte-identical galleries across SKUs.** The URL-based cross-row checks turn
  out to be *structurally unable to fire* on the generated CSV, because app.py
  renames every image to `<sku>_<n>.webp` — SKU-derived URLs can never collide
  however wrong the match is. `duplicate_image_content()` compares the files
  instead, keyed on (size, hash of the first 64 KB). Found immediately:
  `UA32H4500FUXXL` and `UA32H4510FUXXL` shipped the same eight photos, so one of
  them matched the other's listing.
- **partial gallery overlap** (`shares_an_image_with`) for the stage-1 sheet,
  where the source URLs are still real. It flagged 8 rows of the LED batch that
  the first-image and whole-gallery checks missed.
- **price ceiling per category**, the other end of the existing floor.
- **`missing_required_attribute`**, reporting item 7's `_spec_gaps`.
- **runs automatically after generation** — `build_csv.py` validates the CSV it
  just wrote (`--no-validate` opts out) and the Streamlit app shows a Quality
  check panel after Generate. `validate_export()` was made importable for this.
- the issue summary now groups cross-row reasons instead of printing one line
  per partner SKU.

### 7. WooCommerce attributes from the taxonomy sheet — MOSTLY DONE (KI-14)
The fragile round-trip is gone: `product_specs()` builds the rows as data and
`build_descriptions()` returns them, so attributes are no longer recovered by
regexing app.py's own HTML. Specs that cannot be read are omitted instead of
being filled with placeholders.

**Completed 2026-07-26 — the sheet is now a completeness gate.**
`missing_spec_axes()` reads `Sub-Category Attributes` as a checklist rather than
a tag source. For each axis the sub-category declares, a product satisfies it by
naming one of the sheet's own vocabulary values in its title, by carrying the
measurement the axis names, by having the axis in its scraped spec table, or —
for `Type` — by resolving to a real configuration. Anything left over goes into
a `_spec_gaps` column, is summarised in a warning at the end of the build, and
is reported by `validate_csv.py` as `missing_required_attribute`.

It **flags, it does not block**: a missing axis is usually just a terse title,
and the human review step is where that gets decided. On the LED batch it
immediately marked three TVs as missing `Star Rating` / `Type` / `Size`.

And per the note below, the spec values no longer come from the title only —
item 9 feeds this gate directly.

---

## Tier 3 — nice to have

### 8. Fuller SEO pass — BUILT 2026-07-26, OPT-IN (off by default)
Turned on with the app's "Add SEO meta columns" checkbox or `build_csv.py --seo`.
While it is off the output is exactly what it was — the columns are simply absent.

| Column | Contents |
|---|---|
| `slug` | Readable URL from the title, model code appended so variants cannot collide, trimmed on word boundaries to 70 chars |
| `Meta: _yoast_wpseo_title` | <= 60 chars; store name appended only when it fits, dropped before the product name is mangled |
| `Meta: _yoast_wpseo_metadesc` | <= 155 chars, assembled BY PRIORITY (title, then price/MRP, then top specs, then store) with progressively shorter variants per slot, so the price is never crowded out by a third spec and the text never stops mid-clause |
| `Meta: _yoast_wpseo_focuskw` | `brand model`, lowercase; falls back to `brand category` when the SKU carries no model code |

`--seo-plugin rankmath` writes `Meta: rank_math_*`; `both` writes both sets.
`wc_publish.py` now forwards **any** `Meta: <key>` column into `meta_data` with
the prefix stripped — the CSV importer's own convention — so neither route needs
plugin-specific code, and it sends `slug` as a real field.

Everything is derived from the row (title, category, specs, the two prices):
deterministic, and containing no claim the data does not support — no "free
delivery", no "best price". **The pipeline stays LLM-free.**

**`slug` is the one column the CSV importer cannot use** (it has no slug field),
so it applies on the API route only. Image alt text is the same story in reverse
and is covered below.

Original analysis, still accurate:
**Are meta title / meta description required by WooCommerce? No.** WooCommerce core
has no such fields; a product needs only name, SKU and price. They belong to an SEO
plugin (Yoast or Rank Math), which stores them as ordinary WordPress post meta.

**Can they be bulk imported the way products are? Yes, both routes:**
- *CSV importer* — add columns named exactly `Meta: _yoast_wpseo_title` and
  `Meta: _yoast_wpseo_metadesc` (Rank Math: `Meta: rank_math_title` /
  `Meta: rank_math_description`). The `Meta: ` prefix is what tells the WooCommerce
  importer to write post meta, so no plugin-specific tooling is needed.
- *REST API* — the same keys inside the `meta_data` array `wc_publish.py` already
  sends. Adding two entries would be a few lines.

**Image alt text is different** and is the one piece the product CSV cannot carry:
alt lives on the *attachment*, not the product (`_wp_attachment_image_alt`). It has to
be set when the file is uploaded. `wc_publish.py` already does this — it sets
`alt` on every image it attaches — so alt text is solved on the API route and
unsolvable on the CSV route. One more reason to prefer publishing over importing.

(Focus keyword and slug generation, listed here as remaining, are now done.)

### 9. Spec tables from retailer pages — BUILT
`extract_specs()` reads the product's own specification table into a `specs`
column, `Label: Value | Label: Value`, in descending order of trust:
schema.org `additionalProperty` → two-column tables and definition lists →
`Label: Value` bullets. First writer of a label wins.

Filtered out: marketplace bookkeeping (ASIN, best-sellers rank, reviews, price,
packer), marketing paragraphs, values containing markup, and labels that are not
words. Capped at `MAX_SPECS` (24). `--no-specs` turns it off; `--probe` prints
what it found.

app.py canonicalises retailer wording (`Display Size` and `Screen Size` become
one attribute, not two), drops anything invoice-shaped, enforces one row per
attribute name, and caps the total at 15 so the CSV stays a usable width.
Title-derived rows still come first — a scraped row only fills a label nothing
else supplied, so KI-14's "never invent a spec" rule holds and normalised
category labels are never displaced.

Measured: a Haier 43" TV went from 5 attributes to 7, gaining `Warranty` and
`Refresh Rate` — facts its page stated and the title never could. It also feeds
item 7's completeness gate directly.

### 10. Batch report — BUILT
Each run writes `<output>_report.md` beside the workbook: outcome counts
bucketed by status, how many verified rows still lack a price or images, the
rows that need a human **and why**, Serper calls made (≈1 credit each), searches
and pages served from cache, current cache size, and which domains blocked the
run and how often. It ends with the exact next commands.

Built from the SHEET rather than from this run's rows, so a batch resumed across
several sessions still reads as one batch. `--no-report` / `--report PATH`.
Writing it can never fail a run whose data is already saved.

### 11. Serper credit tracking — HALF BUILT
The counting half is done: `RUN_STATS["serper_calls"]` is incremented per API
call and reported both on stdout and in the batch report, alongside how many
searches the cache served (each one a credit not spent). Still missing: the
pre-flight "this batch will cost ~N credits, you have ~M left". The API exposes
no balance endpoint, so that needs a locally tracked running total against a
user-supplied monthly quota.

### 12. Unattended scheduling
Once app.py has a headless mode and publishing is API-driven, a whole batch can
run on a schedule with a report at the end.

Both prerequisites now exist (item 2, item 1) and item 10 supplies the report,
so the only thing still in the way is **KI-15**: while publishing needs a
hand-copied browser cookie that expires in minutes, nothing downstream of
`build_csv.py` can run unattended. Enrich + build + validate could be scheduled
today; publish cannot.

---

## Deliberately NOT recommended

- **Auto-publishing unreviewed rows.** `CLOSE MATCH`, `CHECK MANUALLY` and
  `unlisted store` exist precisely because the match is uncertain. Automating the
  review away would put wrong products on the storefront.
- **LLM-generated product descriptions.** This pipeline is deliberately
  LLM-free and deterministic; the same SKU always produces the same output. That
  property is worth more than prettier copy.
- **Scraping harder.** The politeness delays and per-domain caps are what keep the
  scraper working at all. Phase 3 showed the fix for blocking was a *current*
  browser fingerprint, not more aggression.
