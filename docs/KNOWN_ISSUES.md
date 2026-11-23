# Known Issues — pre-diagnosed backlog (sku_enricher.py unless noted)

> **STATUS NOTE (2026-08-11):** several Status lines below are stale. The
> re-verified ledger is **docs/PRODUCTION_READINESS.md** — check there first;
> fixed since this file's last update: KI-7a/b/g (partial), KI-15 stopgap
> (and the CDN block itself is currently OFF), A-4/6/7/8/10/11, A-5 resolved
> by decision. KI-13 operator-deferred.

**Contract:** every entry below is already diagnosed; line anchors were verified
against the code. To fix one: read ONLY the cited lines (±20 context), apply the fix
sketch, run the acceptance test on a SCRATCH COPY (never live spreadsheets), update
the Status line here, then run /sync-copies (the enricher exists in 4 copies).
Do not re-read the whole script to re-diagnose.

Severity: HIGH = silently ruins a batch · MED = wrong-data / data-loss edge case ·
LOW = robustness/efficiency.

---

## KI-1 · HIGH · fixed (2026-07-25)
**What:** `serper_search` never checks `r.status_code`. A dead/expired/over-quota key
returns a JSON error body with no `organic` key → `[]`, which is not `None`, so
`web_search` "trusts Google" and the DuckDuckGo fallback never fires. Every SKU
becomes NOT FOUND with zero error output.
**Where:** fix lives at sku_enricher.py:310-335 (`_serper_failed_warned` + status
guard in `serper_search`); fallback gate in `web_search` :351-355.
**Fix applied:** non-200 → log once per run + `return None`, so `web_search` falls
back to DuckDuckGo. `[]` from a 200 stays trusted — genuine zero results.
**Accept:** with `SERPER_API_KEY` set to garbage, a `--limit 1 --force` run on a scratch
input logs the serper failure and shows DuckDuckGo attempts, not instant NOT FOUND.
With a valid key: behavior unchanged.

## KI-2 · HIGH · fixed (2026-07-25) — added --retry-incomplete
**What:** rows with a `url_link` are skipped unconditionally on resume, so
`FOUND ... - no price shown` / `- only N image(s)` rows can never be re-attempted;
`--retry-missing` only reaches rows with a status and NO url. Only recourse is
`--force` (re-scrapes everything). Observed: only 4-6/43 rows in LED batches ever
captured an MRP.
**Where:** fix lives at sku_enricher.py:1095-1097 (flag), :1137-1149 (skip logic)
**Fix applied:** `--retry-incomplete` re-attempts rows whose status starts with FOUND
and contains " - ", or whose url_link exists but sale price / image_url cell is empty.
Existing cells persist (the writer only overwrites fields the new lookup returned).
**Accept:** on a scratch copy of a `_filled.xlsx`: with the flag, only half-filled rows
are re-attempted and complete FOUND rows are untouched; without the flag, row
selection is identical to before.

## KI-3 · MED · fixed (2026-07-25)
**What:** brand presence check did `brand_n not in title_n + url_n` — the two strings
were concatenated with no separator, so a title ending "…sam" plus a URL starting
"sung…" faked "samsung". Weakened the accessory-rejection chain.
**Where:** fix in `assess` (sku_enricher.py, brand check + the same pattern in the
model-code `core` check).
**Fix applied:** each string is tested on its own — `brand_n not in title_n and
brand_n not in url_n`, and `core in title_n or core in url_n`.
**Accept:** title "foosam" + url "sungbar.com" + brand "samsung" → rejected.

## KI-4 · MED · fixed (2026-07-25)
**What:** when Excel locked the output >5 min, `safe_save` redirected all further saves
to `<stem>_filled_recovery.xlsx`. The NEXT run recomputed `out = <stem>_filled.xlsx`
(stale, pre-lock) and silently re-scraped every row done since the lock.
**Where:** fix in `main()` next to the output-path resolution.
**Fix applied:** if `<stem>_filled_recovery.xlsx` exists and is newer than the normal
output, the run resumes FROM it and writes back to `<stem>_filled.xlsx`, logging both
facts. Self-healing: after that save the normal file is newest again, so the recovery
file stops shadowing without anyone deleting anything.
**Accept:** with both files present and recovery newer, a new run logs the divert and
resumes from recovery instead of re-scraping.

## KI-5 · MED · fixed (2026-07-25)
**What:** output name was unconditionally `<stem>_filled.xlsx`; feeding `LED_filled.xlsx`
back in created `LED_filled_filled.xlsx` and re-scraped from scratch, re-paying every
Serper credit.
**Where:** fix in `main()`, immediately after the input-exists check.
**Fix applied:** an input whose stem contains `_filled` is refused with the command that
was actually meant — `sku_enricher.py "LED.xlsx" --retry-incomplete` — or, if the
original is missing, an instruction to restore it.
**Accept:** running on `LED_filled.xlsx` exits without creating `LED_filled_filled.xlsx`.

## KI-6 · MED · fixed (2026-07-25)
**What:** column aliases map pre-existing user columns `price`→sale price,
`link`/`website`→url_link, `image`→image_url. A sheet whose `price` column actually
holds MRP (or `link` something unrelated) got silently overwritten, AND the output then
lacked the canonical header app.py requires.
**Where:** fix in `map_columns` (sku_enricher.py ~:1031-1040)
**Fix applied:** a column bound through a non-canonical alias has its header renamed to
the canonical name and the rename is logged. No duplication, no data loss — the output
is a separate workbook, the input is never modified.
**Accept:** a sheet headed `MRP`/`price`/`url`/`images` comes out with `regular price`/
`sale price`/`url_link`/`image_url`, and each rename appears in the startup log.

## KI-8 · HIGH · fixed (2026-07-25)
**What:** app.py hard-requires 8 exact-string headers and raises
`ValueError: Missing required columns` otherwise, but the enricher never wrote `sku`
(it left the input's own header, e.g. `Item Details`), `categories`, or `Brands`. Every
batch needed manual column surgery between stage 1 and stage 2.
**Where:** contract at app.py:12; fix at sku_enricher.py `APP_EXTRA_COLS` (~:129) and
`ensure_app_columns` (~:1046), called from `main()`.
**Fix applied:** missing `sku`/`categories`/`Brands` headers are appended and the
detected SKU column is mirrored into `sku`. `categories`/`Brands` stay blank by design —
app.py's `infer_brand`/`detect_category` fill them from `category_map.xlsx`.
**Accept:** after any run, the filled sheet contains all 8 headers from
app.py:REQUIRED_INPUT_COLUMNS and `sku` is populated on every data row.

## KI-9 · HIGH · fixed (2026-07-25) — extraction quality (Phase 2)
**What:** pages with a full gallery and a visible price yielded 1 image and/or no price.
**Causes (all fixed):** `extract()` gated the generic top-up on `if not t or not s`, so a
parser returning title+price never topped up images, and `i or i2` replaced instead of
merging (primary cause); `u.startswith("http")` dropped `//cdn/...` and `/media/...`;
`split("?")[0]` collapsed galleries keyed by query string; `clean_price` took the first
number run anywhere ("1 offer from ₹22,599" → `1`, a WRONG price); `offer_price` read
`offers[0]` only; `jsonld_products` was LIFO so `@graph` products came out reversed;
croma never set MRP; flipkart depended on rotating obfuscated classes.
**Fix applied:** new helpers `absolutize_url`, `looks_like_product_image` (junk guard),
`image_size_hint`, `image_key`/`dedupe_images` (size-variant collapse + larger-rendition
preference + same-directory anchoring), tiered `collect_images`, context-aware
`clean_price`/`parse_amount`/`price_candidates`, `mrp_near_label`/`mrp_from_strike`
(the word "MRP" doesn't rotate like class names), `main_product`, `offer_prices`,
`rec_score`. `extract()` now always tops up and CONCATENATES images, site-parser first.
**Verified:** 10 HTML fixtures captured before the change with a frozen baseline.
Titles and first images unchanged, image counts only increased
(haier 1→8, vijaysales 1→8, tatacliq 0→8), new MRPs found, 45 helper unit assertions.
**Note:** `collect_images` is deliberately tiered — a page-wide `<img>` sweep only runs
when the declared sources yield fewer than MIN_IMAGES, otherwise related products and
page furniture leak into `image_url`. Keep that ordering if you extend it.

## KI-11 · HIGH · fixed (2026-07-25) — Phase 2 follow-up regressions
Two regressions introduced by KI-9, both reported from real batches:
1. **Random sites won.** `close_hits[0]`/`weak_hits[0]` respected the `domain_rank`
   candidate order; replacing them with `max(..., key=rec_score)` ranked purely on
   completeness, so a thin aggregator with a big gallery beat Croma or Amazon. Fixed by
   `domain_quality()` (2 = priority marketplace, 1 = known/brand store, 0 = unknown) and
   making `rec_score` return `(domain_quality, completeness)` — source first, always. A
   complete hit from an unknown domain no longer short-circuits the search, and its
   status gains " - unlisted store, verify".
2. **Image quality dropped.** The widened net admitted 36-60px icons and thumbnails as
   gallery images. Fixed by `is_thumbnail()` + a sort that puts full-size photos first
   (demote, never drop — so being aggressive is safe), and by making `image_size_hint`
   **context-aware**: a number only counts as pixels in a real size shape
   (`1200x1200`, `320Wx357H`, `._SL1500_.`, `_1200.jpg`, `/416/416/`, `?width=`).
   Matching bare digits read sizes out of hashes (`ur558uB41` → 558) and uuids
   (`7ea9b3744b37` → 3744).
**Also added:** `upsize_image_url()` strips resize params on Magento-style
`/media/catalog/product/` URLs — measured 34.9 KB vs 14.2 KB on vijaysales. Deliberately
scoped: tatacliq 404s on any upsize (verified), and a broken URL loses the image entirely.
**Accept:** helper unit tables assert a real retailer outranks a rich unknown site, that
thumbnails sort last, and that hash/uuid digits are not read as sizes.

## KI-10 · HIGH · fixed (2026-07-25) — blocking / "bot-check page - retrying..." (Phase 3)
**What:** repeated bot-check retries, and blocked SKUs indistinguishable from genuinely
absent ones (both ended as a bare `NOT FOUND`).
**Root cause:** `IMPERSONATE` was `("chrome","chrome120","chrome110")` — those advertise
2023 browsers, which in 2026 is itself the block trigger. Confirmed by probe: the same
Amazon URL that failed all 4 attempts before now succeeds on the FIRST attempt with
chrome146/145/142/136/131.
**Also fixed:** no cookie persistence (now one `creq.Session()` per domain); no `Referer`
(now sent with a matching `Sec-Fetch-Site`; nothing else is overridden, because curl_cffi's
impersonation already sends a consistent `Accept`/`Sec-Ch-Ua`/`Sec-Fetch-*` set and
hand-setting them breaks that consistency); silent failures (`fetch` now returns
`(html, reason)` and every path logs); `looks_blocked` now returns a reason and covers
Cloudflare, PerimeterX, DataDome, Imperva and Akamai, scanning head+tail rather than the
first 20 KB; amazon monopolising the budget (now `MAX_PAGES_PER_DOMAIN = 2` plus
round-robin candidate ordering); endless retrying of a hostile domain (now a
`DOMAIN_COOLDOWN_SKUS = 3` circuit breaker, and an early exit after 2 hard blocks).
**Statuses** now say why: `NOT FOUND - all sources blocked (amazon.in http 503)`,
`NOT FOUND - N page(s) checked, none matched`, `NOT FOUND - no usable search results`.
The resume gate treats a `blocked` status as transient so those rows retry automatically —
without that, the richer status would have made them HARDER to retry than a bare NOT FOUND.
**Playwright** `--js-fallback` (+ `--js-budget`) added for pages that are still blocked or
that render price in JS. Off by default, one browser per run, `atexit` teardown,
`KeyboardInterrupt` re-raised so Ctrl+C still works, and a missing install logs an
actionable message instead of crashing the batch (verified).
**Verified:** `--probe` against httpbin `/status/403` (2 BLOCKED lines then early exit),
`/status/404` (one line, no retries), `/headers` (short-body path); live Amazon and Croma
probes both succeed first try; a 6-SKU live batch.

## KI-12 · HIGH · fixed (2026-07-25) — SPA shells scraped as if they were products
**What:** `shop.haierindia.com` is a client-side-rendered SPA. Every product URL returns
the SAME shell: the homepage `<title>`, no og:image, no JSON-LD, and a "Shop by category"
strip of 7 nav tiles plus a cart icon. Those tiles satisfied MIN_IMAGES, so the page
looked like a successful extraction — this is the "only 1 image for the Haier product"
report (after junk URLs were filtered, one mis-resolved icon survived).
**Where:** `collect_images` tier 2 swept every `<img>` regardless of what the element
said about itself.
**Fix applied:** `junk_img_element()` now rejects an `<img>` whose own `alt`/`class`/`id`
admits it is furniture (`category-img`, `logo`, `nav`, `banner`, `icon`, ...), plus URL
patterns for `/subcategory/` and storefront UI icons (cart, wishlist, account, ...).
`shop.haierindia.com` now yields 0 images instead of 8 wrong ones, and is listed in
`JS_PRICE_DOMAINS` so `--js-fallback` renders it properly.
**Accept:** probe `shop.haierindia.com/products/...` → 0 images; `haier.com` and
`amazon.in` probes unchanged at 8.

## KI-13 · MED · open (deferred by the user, 2026-07-25) — every plain LED TV is "Mini LED"
**What:** `app.py:234` reads
`if has('mini led') or word('led') or word('tv') or has('television'): return 'LED', 'Mini LED'`
so ANY television that is not QLED/OLED/QNED is filed under the premium **Mini LED**
sub-category. That is most of the catalogue — the whole LED batch is HD/FHD sets.
**Why it is still open:** the `LED` parent in `category_map.xlsx` has no plain-LED
sub-category (only QLED, OLED, Qned, Mini LED), so fixing it needs a taxonomy decision.
Offered on 2026-07-25; the user chose to leave the behaviour as-is for now.
**Fix when wanted:** add e.g. `LED TV` to the LED row of the taxonomy sheet, create the
matching WooCommerce category, and split the `app.py:234` branch so `Mini LED` requires
the literal phrase.

## KI-14 · HIGH · fixed (2026-07-26)
**What:** every product that is not a washing machine or water heater gets filler
specs and a washing-machine attribute name. Real output for a Samsung 32" TV:
`Storage Capacity: Standard Capacity`, `Configuration: Appliance Utility Core`,
`Power Rating: Standard Power Rating`, and an attribute literally called
**`WM Function Type`** (WM = washing machine) — all of it visible on the storefront
product page and in the WooCommerce attribute filters.
**Where:** app.py `capacity_label`/`capacity_value`/`configuration_text`/`wattage` all
default to a "Standard …" string instead of omitting the row; the attribute name comes
from `attribute_name_map` in `parse_dynamic_attributes_from_html` (~:596), which maps
`'configuration' -> 'WM Function Type'` unconditionally.
**Why it matters:** the LED batch alone is 43 SKUs, and the same applies to every AC,
refrigerator and microwave. `validate_csv.py` passes the file as import-ready, so
nothing catches it before it goes live.
**Fix applied:** new `product_specs(title, brand, category)` builds the rows from
`spec_tags` plus the category-specific readers, and **omits any row whose value could
not be read** — the unknown-spec decision went to "omit", never a placeholder.
Every `Standard …` / `Appliance Utility Core` / `1 Cooking Zone` default became `''`,
and `capacity_label` learned `LED -> Screen Size`. `build_descriptions` now returns the
spec rows as its 4th value and `process_inventory` uses them directly, so
`parse_dynamic_attributes_from_html` — the regex round-trip through app.py's own HTML,
and the only source of the `WM ` names — is gone.
**Before / after** on the real test SKU (Samsung 32" HD Smart LED TV):
`Storage Capacity: Standard Capacity`, `WM Function Type: Appliance Utility Core`,
`Power Rating: Standard Power Rating` → `Screen Size: 32 Inch`, `Resolution: HD Ready`,
`Smart Platform: Smart TV`.
**Verified:** 61 assertions covering washing machines, water heaters, microwaves, TVs
and a title with nothing readable (which now yields Brand only, not a table of
placeholders), plus a check that no banned wording appears in any generated field.
**Accept:** a TV row exports no `WM *` attribute and no `Standard …` placeholder value.
**Follow-up fixed (2026-11-19):** `build_descriptions` still wrote one invented row
into every long description, `Standard Operational Grid: 230 V / 50 Hz`. It is removed;
`tests\test_content_firewall.py::test_invented_mains_row_is_gone` pins it.

## KI-15 · HIGH · open — infrastructure, not code
**What:** `https://shreejeeelectronics.in` sits behind Hostinger's CDN JS challenge
("Checking your browser before accessing…", `Server: hcdn`). EVERY non-browser request
gets HTTP 403 — `/wp-json/wc/v3/*`, `/?rest_route=`, `/wp-login.php`, even
`/robots.txt`. Verified 2026-07-25 with valid WooCommerce keys; the keys are never
reached, so this is not an auth problem.
**Blocks:** `wc_publish.py` entirely (the whole publish stage of the pipeline).
**Also worth knowing:** Hostinger's own documentation says this mode blocks search
engine bots, so while it is on, crawlers get challenged too.
**Fix:** hPanel → Websites → Dashboard → CDN → **Manage** → Security → move the level
off "I'm under attack". hPanel is a SEPARATE login from WordPress
(hpanel.hostinger.com, hosting-account credentials); WordPress admin cannot reach it,
so this may need whoever owns the hosting account.
**Stopgap (added 2026-07-26):** `wc_publish.py --cookie-file cookie.txt` presents the
clearance cookie a real browser already earned. `looks_like_challenge()` detects the
interstitial and prints both routes instead of dumping HTML. The cookie is bound to
that browser and IP and expires, so it is a stopgap, not a fix.
**Note:** the manual route is unaffected — a browser passes the challenge, so Media
upload + Products > Import work normally. Only the automation is blocked.
**Accept:** `.venv\Scripts\python.exe wc_publish.py --check` prints the store version
instead of an HTML challenge page.

**Two traps the cookie stopgap exposed (both fixed, both cost a debugging round):**
1. A browser cookie carries WordPress's own session. With `wordpress_sec_*` present,
   WordPress authenticates by session, finds no REST nonce and returns **401
   `woocommerce_rest_cannot_view`** — which reads exactly like a bad consumer key.
   Measured: clearance cookie alone → 200; clearance + `wordpress_sec_*` → 401.
   `strip_wp_session()` now removes them.
2. The clearance cookie is refreshed by the CDN via `Set-Cookie`. Pinning it as a
   fixed header discarded every refresh and expired mid-run; it now seeds the
   session cookie jar. Even so it lasts minutes, not hours — expect to re-copy it.

## KI-16 · HIGH · fixed (2026-07-26) — a dry run wrote to the live store
**What:** `wc_publish.py` promises "nothing is written without `--commit`". It wrote
anyway, in two places, because assembling a payload performed writes as a side effect:
- `_term()` created a taxonomy term whenever the lookup missed — a dry run created
  **6 product tags** on the live store;
- `images_for(mode="upload")` uploaded to the Media Library while building the payload
  — a dry run uploaded **8 images**.
**Where:** `Store._term`, `Store.category_id`, `images_for`, `build_payload`.
**Fix applied:** `Store(allow_writes=...)` is set from `--commit`. Without it a missing
term prints `~ would create …` and resolves to `None`, `tag_ids` drops the `None`s,
`category_id` returns `None` and `build_payload` sends no categories rather than
`[{"id": null}]`; `images_for` reports `N local .webp file(s) ready - NOT uploaded`
and uploads nothing.
**Cleanup:** all 6 tags and all 8 media items created by the offending runs were
deleted; the store was verified back to its prior state.
**Accept:** a dry run against a store missing every tag creates none of them, and a
`DryStore` records zero non-GET calls.

## KI-7 · partially closed by Phase 3
Silent exception swallowing in `fetch`, the short-body blind spot, and the 20 KB
`looks_blocked` scan limit are all fixed. Serper credit tracking is closed too
(2026-09-02): `sku_enricher.Ledger` writes `clients\<slug>\credits.json` and
`--budget N` stops a run at N credits on the checkpoint-safe path. Still open: no
caching of search results or fetched HTML, `is_product_url` defaults True for unknown
domains (mitigated — `domain_quality` now ranks those last), and the O(n²) workbook
rewrite.

## KI-7 · LOW · open (grouped — robustness / efficiency)
- `fetch` swallows every exception silently (:391-392); pages <3,000 chars are rejected
  indistinguishably from failures (:386).
- `ddg_search`: 2 attempts, hardcoded 30 s sleep (:337-347).
- `is_product_url` defaults True for unknown domains (:261) — landing pages burn the
  fetch budget (FETCH_BUDGET=6).
- Whole workbook rewritten after every SKU (:1177) — O(n²) I/O on big sheets.
- No caching of search results / fetched HTML; `--force` re-pays every credit and fetch.
- ~~No Serper credit tracking~~ — CLOSED 2026-09-02. `Ledger` tallies every run into
  `clients\<slug>\credits.json` (month total + per-run rows + `history`), the report
  header prints `Credits:` and `Stopped by:`, and `--budget N` (default from the
  client's `[serper] default_run_budget`) hard-stops a run before it overspends.
- `looks_blocked` scans only the first 20,000 chars (:369).
- `ensure_writable` checks the Excel lock once at t=0 only (:1116).
- validate_csv.py:176 — substring removal of the title from joined text can corrupt
  the comparison when the title appears inside cats/desc.

---

## Found by the mixed-category test of 2026-11-20

The test enriched 9 models (3 air conditioners, 3 washing machines, 3
refrigerators) with `--search-order free_first --budget 60`. It used 0 serper
credits. Run folder: `runs\mixed-2026-11-20\` (`enrich.log`, `mixed_filled.xlsx`,
`mixed_filled_report.md`). Line anchors are from 2026-11-20. Check them before a fix.

## KI-17 · HIGH · fixed (2026-11-23) — junk images pass the image filter
**Fix (2026-11-23):** one commented KI-17 group in `IMAGE_JUNK_RE`: `energy[-_]?label`,
`/gnb/`, `[-_/]mega\d+(?:[-_.?#]|$)`, `[-_/]explore[-_]`, `get-inspired`, `/og\?`,
`/etc\.clientlibs/`, `/pictograms?/`, `[-_/]og[-_]tag`, `trustmark`,
`lg\.com/.*/features?/`, `watermark`, `opengraph-image`, `twitter-image`. The last
eight came from the replays: each removed junk image let the next one into the freed
slot (see KI-32). Tests: `tests\test_junk_images_ki17.py`. A sweep of 611 URLs in 24
`*_filled.xlsx` files drops only the 14 evidence URLs. The replay of
`runs\mixed-2026-11-20\` (`runs\ki17-replay6\`, Serper blocked, 0 credits) writes
none of them. Not caught: the bajajfinserv.in partner logo `91mobiles.png` (KI-33).
**Symptom:** banners, energy labels and text cards reach the `image_url` cell as
product photos.
**Evidence:** SAMSUNG-AR50H19D1PHNNA: all 8 images are samsung.com energy labels
(`.../energylabel/in-energylabel-product-ar50h19d1phnna-*`). HAIER-HRD-2561SGSA-N:
the 3 images are bajajfinserv.in banners (`mega1`, `mega2`, `BM_Explore_tractors`).
LG-US-Q18ENXE (a split AC): image 3 is the lg.com menu banner
`/gnb/windowsac-644x328.jpg`. VOLTAS BEKO-WFL7012VTMP: the only image is a
generated text card (`og?t=...&k=Review`) from bestpurifier.in.
PANASONIC-CW-XU185CGT: image 2 is a campaign tile (`.../get-inspired-section/.../Higlish.jpg`).
**Likely cause:** `IMAGE_JUNK_RE` (sku_enricher.py:772-784) and `JUNK_IMG_ATTR_RE`
(:789-791) do not know these patterns: `energylabel`, `/gnb/` (global navigation
bar), `mega\d`, `explore_`, `get-inspired`, and generated `og?` image URLs.
**Build check (2026-11-20):** the build's 1000 px floor dropped the bajajfinserv.in
banners and the lg.com banner (both rows then have 0 images). It WROTE 3 Samsung
energy labels, the Panasonic campaign tile and the Voltas Beko text card as product
images (`_1` of the last two is not white).
**Accept:** a replay of the run (warm `.cache\`, `--force`, scratch copy) writes
none of the URLs above. The good photos of the same rows stay. A unit test pins
each pattern.

## KI-18 · HIGH · fixed (2026-11-23) — a different model's photo is the only image
**Fix (2026-11-23):** `model_core` / `foreign_model_image` / `drop_foreign_model_images`
(before `status_for`), called from `finalize_gallery(rec, model)` and on each strong
donor in `hunt_donors`. Rules:
- The row's model core is the leading letters plus the first digit run. A core under 4
  characters is not judged.
- An image is kept when its file name holds the core, so shared-prefix variants stay.
- Hash-named CDNs are not judged: amazon, flixcart, shopify `/cdn/shop/` and jiostore.
- Before the scan, the check removes size tokens (`1200Wx1200H`, `1600x1062`, `1000px`),
  years, uuids and stop words.
- Codes are found in single segments, and in a pair only after a 2-3 letter non-word
  prefix (`GL-D201ASCU`).
- A row left with 0 images gets `- only 0 image(s)`.

Tests: `tests\test_wrong_model_images_ki18.py`. Two review rounds found these false
positives, and each is now pinned as a keep case: Reliance `…-1200Wx1200H` names that
could empty a correct gallery, `SKU12345`, `lg-1500` and `hero-e1234`. The sweep of 728
URL/row pairs drops only the GL-D201ASCU image. LG-GLD2256ZARS now keeps its own 2
photos, and its status flags the floor. Known false negative: a short Samsung core
(`RT28`) keeps sibling models.
**Symptom:** LG-GLD2256ZARS (a 210 L single-door refrigerator) has one image, and it
shows another model.
**Evidence:** the image path is
`.../gl-d201ascu_asczebn_eail_in_c/gallery/GL-D201ASCU-Refrigerators-Front-View-D-01-v.jpg`
on the lg.com page of GLD2256ZARS. The poorvika.com images were below the 1000 px floor
and were not donated.
**Likely cause:** the lg.com image collection (`extract()` / `parse_generic`, sku_enricher.py
:2680 and :2975) accepts any gallery image on the page. No check compares a model code
in the image file name with the row's model code.
**Accept:** an image whose file name holds a model code that is not the row's model
code (and is not a shared-prefix variant) is dropped. LG-GLD2256ZARS then has 0 images
from lg.com and the status says so.

## KI-19 · MED · open (found 2026-11-20) — a foreign model code in the title is not flagged
**Symptom:** HAIER-HRF-3782BNSA-P is a clean FOUND, but the title names another code.
**Evidence:** title "Haier 328 L, Frost Free Refrigerator (HHRF-3782BIS-P)"; status
`FOUND (haier.com)`.
**Likely cause:** `assess()` returns "strong" at sku_enricher.py:3206-3208 when the
model code is in the URL and the title starts with the brand. The different-model
check (:3228-3236) runs only after that return, so it never sees the title code.
**Accept:** a page whose URL holds the model code but whose title names a different,
non-variant code gets a VERIFY or CLOSE MATCH status, not a clean FOUND.

## KI-20 · MED · open (found 2026-11-20) — exact official URL gives CLOSE MATCH and no price
**Symptom:** SAMSUNG-AR50H19D1PHNNA is `CLOSE MATCH - CHECK (samsung.com)` and has no
price, although tier 0 found the exact official page.
**Evidence:** URL `https://www.samsung.com/in/air-conditioners/split-ac/bespoke-ai-inverter-split-ac-5-00-kw-3-star-2026-ar50h19d1phnna/`
holds the full model code. Title "Online Bespoke AI Inverter Split AC 3 Star 5.0 kW 2026"
holds no model code and does not start with "Samsung". Price cells are empty.
**Likely cause:** the URL vouches only when `starts_with_brand` is true (sku_enricher.py
:3206-3207). The title has no model token, so the verdict falls to the trailing-characters
branch (:3237-3239, the core code is in the URL) and returns "close". The samsung.com
parser reads no price from this page (cause not confirmed: page price is maybe loaded by JavaScript).
**Accept:** an official brand-domain URL that holds the full model code gives a
"strong" verdict even when the title omits the brand. A `--probe` of the URL shows a
price, or the status says "no price shown".

## KI-21 · MED · open (found 2026-11-20) — panasonic.com spec scrape returns site navigation
**Symptom:** PANASONIC-CW-XU185CGT has no real spec rows, no price, no tonnage and no
star rating.
**Evidence:** the `specs` cell is "Find Your Nearest Dealers: ... | Product Catalogues: ...".
`page_text` is 76 characters.
**Likely cause:** the generic spec-table scrape (`parse_generic`, sku_enricher.py:2680)
takes the first label/value block on the page, which is the footer navigation. It has
no panasonic.com parser.
**Accept:** a `--probe` of the panasonic.com page gives spec rows with the capacity
(tonnage) and the star rating, and no navigation text.

## KI-22 · MED · open (found 2026-11-20) — reliancedigital.in FOUND with 0 spec rows
**Symptom:** SAMSUNG-WA80F10B3NTL is a clean FOUND with an empty `specs` cell.
**Evidence:** `FOUND (reliancedigital.in)`, 0 spec rows, `page_text` 131 characters.
**Likely cause:** `parse_reliance` (sku_enricher.py:2612) reads the title, price and
images but not the specification block of the current page layout.
**Accept:** a `--probe` of the reliancedigital.in URL gives spec rows (capacity, load
type, star rating).

## KI-23 · MED · open (found 2026-11-20) — review text passes the content firewall
**Symptom:** spec rows of VOLTAS BEKO-WFL7012VTMP hold review opinions.
**Evidence:** rows such as "Service network: good" and "✓ Front-load drum: the gentlest
wash..." from bestpurifier.in.
**Likely cause:** `content_ok` / `content_clean` (sku_enricher.py:686 and :706) block
retailer, offer and contact text, but not review wording or check-mark bullets.
**Accept:** spec rows with review wording (good, best, gentlest, a leading check mark)
do not reach the sheet or the CSV. A unit test pins the two rows above.

## KI-24 · MED · open (found 2026-11-20) — sizing specs in the title but not in the spec rows
**Symptom:** the spec rows miss the main sizing value that the title states.
**Evidence:** no tonnage row on both split ACs (LG-US-Q18ENXE "(1.5)",
SAMSUNG-AR50H19D1PHNNA "5.0 kW"); no litres or door-type row on LG-GLD2256ZARS
("210L Single Door"); no star row on GODREJ-WSEDGE-ULT-80-5.0-DB2M-CSRD ("5 Star").
**Likely cause:** the site parsers copy only the page's spec table. The enricher
does not add a sizing row from the title. `app.py` reads the title as a third source
for filter groups, so the CSV can still be right; check the build before a fix.
**Accept:** for each row above, the built CSV has the tonnage / capacity / door type /
star rating attribute. If the CSV already has it, close this entry as "no defect".
**Build check (2026-11-20, `runs\mixed-2026-11-20\build\mixed.csv`):** the title
fills the gap for 3 rows: LG-US-Q18ENXE `AC Capacity=1.5 Ton`, LG-GLD2256ZARS
`Fridge Type=Single Door` + `Capacity=210 L`, GODREJ `Energy Rating=5 Star`. Still
open for SAMSUNG-AR50H19D1PHNNA (no `AC Capacity`, see KI-29).

## KI-25 · LOW · open (found 2026-11-20) — free_first sends no serper query after poor DuckDuckGo results
**Symptom:** with `--search-order free_first`, a poor DuckDuckGo answer stops the
paid search.
**Evidence:** SAMSUNG-AR50H19D1PHNNA: `"AR50H19D1PHNNA" site:flipkart.com` returned a
jeans page; `site:samsung.com` returned a user manual, a help page and a community post.
The run sent 0 serper queries.
**Likely cause:** `web_search` (sku_enricher.py:1429-1433) returns any non-empty
DuckDuckGo result list and caches it; serper is asked only when DuckDuckGo gives nothing.
**Accept:** in free_first mode, a DuckDuckGo result list with no URL on the queried
site (for a `site:` query) counts as empty, and serper is asked within the budget.

## Found by the build of the mixed-category rows (2026-11-20)

Command: `build_csv.py runs\mixed-2026-11-20\build\mixed_filled.xlsx -o
runs\mixed-2026-11-20\build\mixed.csv --client <scratch copy of shreejee> --all-rows`,
then `validate_csv.py` on the CSV. Output: `runs\mixed-2026-11-20\build\` (`mixed.csv`,
`build.log`, `validate.log`, `validation_issues.csv`). The scratch client and its
images were deleted after the run.

## KI-26 · MED · fixed (2026-11-24, validate_csv.py) — every washing machine is flagged as a pre-KI-14 CSV
**Fix (2026-11-24):** `WM Function Type` and `WM Washing Capacity` moved from
`DEAD_PLACEHOLDERS` to `DEAD_PLACEHOLDER_VALUES`. These strings are flagged only when they
are an attribute VALUE (an exact cell or a comma item), never when they are an attribute
name or a description label. The mixed build CSV went from 3 flags to 0. Tests:
`tests\test_build_fixes_ki26_28.py`.
**Symptom:** all 3 washing-machine rows get `placeholder_spec_text (WM Function Type) -
rebuild this CSV, it predates the KI-14 fix`. The CSV is new.
**Evidence:** `validation_issues.csv` rows of WM-SAMSUNG-WA80F10B3NTL,
WM-VOLTAS-BEKO-WFL7012VTMP and WM-GODREJ-WSEDGE-ULT-80-5.0-DB2M-CSRD.
**Likely cause:** `DEAD_PLACEHOLDERS` (validate_csv.py:196-199) lists `WM Function Type`
and `WM Washing Capacity`. Since 2026-11-19 these are the store's real filter-group
names (filter sheet; `app.FILTER_AXIS_GROUPS`, app.py:2033-2034). The check at
validate_csv.py:1105-1109 searches the whole row text.
**Accept:** a washing-machine row built today has no `placeholder_spec_text` issue.
A row with `Standard Capacity` or `Appliance Utility Core` is still flagged.

## KI-27 · MED · fixed (2026-11-24, app.py) — a semi-automatic washer is also "Fully Automatic Top Load"
**Fix (2026-11-24):** `_text_hits` drops an ALIAS hit that directly follows (with only
whitespace between them) a hit of another value in the same group. The Godrej
semi-auto row now gives `Semi-Automatic` only. "Fully Automatic Top Load" and a bare
"Top Load" are unchanged. Tests: `tests\test_build_fixes_ki26_28.py`.
**Symptom:** GODREJ-WSEDGE-ULT-80-5.0-DB2M-CSRD gets
`WM Function Type=Fully Automatic Top Load, Semi-Automatic`. The product is semi-automatic only.
**Evidence:** title "... Semi-Automatic Top Load ..."; `categories` is
`Washing Machines > semi auto` (correct).
**Likely cause:** `FILTER_VALUE_ALIASES` (app.py:1601-1602) maps the bare phrase
`top load` to `Fully Automatic Top Load`. `_text_hits` (app.py:1735-1753) drops a hit
only when it sits inside a longer hit, and "semi automatic" and "top load" do not overlap.
**Accept:** "Semi-Automatic Top Load" gives `Semi-Automatic` only. "Fully Automatic Top
Load" and a bare "Top Load" on a fully automatic product still give `Fully Automatic Top Load`.

## KI-28 · MED · fixed (2026-11-24, app.py) — "2 Way Air Swing" becomes Power Rating 2 W
**Fix (2026-11-24):** `wattage()` uses `(\d+\s*(?:Watts|Watt|W))\b`. LG-US-Q18ENXE has no
Power Rating now. "1200 W", "1200W" and "1200 Watts" still match. `wattage()` is the only
wattage regex, and `_QUANTITY_RE` already has a boundary.
**Symptom:** LG-US-Q18ENXE has the attribute `Power Rating=2 W`.
**Evidence:** title "... 2 Way Air Swing, ADC Sensor, 4.4 kW ...".
**Likely cause:** `wattage()` (app.py:773) matches `\d+\s*(?:W|Watt|Watts)` with no
word boundary after the unit, so "2 W" in "2 Way" matches. Called from app.py:2871.
**Accept:** "2 Way Air Swing" gives no Power Rating. "1200 W" and "1200 Watts" still give one.

## KI-29 · MED · open (found 2026-11-20, app.py) — a scraped value outside the filter list leaves the filter empty and no gap
**Symptom:** two rows have no value for a required filter, and `_spec_gaps` is empty.
**Evidence:** SAMSUNG-AR50H19D1PHNNA: `_filter_unmatched` = `AC Capacity (Ton):
'17061 Btu/h' not in list`; no `AC Capacity` attribute; the title says "5.0 kW".
HAIER-HRF-3782BNSA-P (a top-mount, two-door refrigerator): `_filter_unmatched` =
`Fridge Type (Type): 'Top Mount Frost Free (FF)' not in list`; no `Fridge Type` attribute.
**Likely cause:** the `Ton` axis has no unit (app.py:2027), so a Btu/h capacity is a
candidate; nothing converts kW or Btu/h to tons (the owner rule says a scraped value
is never rounded, so a conversion needs an owner decision). `FILTER_VALUE_ALIASES`
(app.py:1597) has no `top mount` -> `Double Door` alias. `_spec_gaps` seems to count an
unmatched value as filled (not confirmed).
**Accept:** each row either gets the filter value (after an owner decision on the
conversion and the alias) or lists the axis in `_spec_gaps`.

## KI-30 · LOW · open (found 2026-11-20) — lg.com spec labels keep section prefixes and lose units
**Symptom:** attribute names such as `GENERAL - Product Type`, `Energy Saving - Energy
Grade` and `BASIC SPEC - ENERGY`; the value `GENERAL - Cooling Capacity=5000` has no unit.
**Evidence:** LG-US-Q18ENXE and LG-GLD2256ZARS in `mixed.csv`. The sheet row says
`GENERAL - Cooling Capacity Max(W): 5000`. `Energy Grade=3 Star` repeats `AC Star Rating`.
**Likely cause:** the lg.com spec scrape in sku_enricher.py writes "SECTION - label"
(parser not confirmed), and the app.py label clean-up drops the "(W)" unit with the
label suffix.
**Accept:** lg.com rows give plain labels (`Product Type`, `Cooling Capacity`) and the
value keeps its unit (`5000 W`).

## KI-31 · LOW · open (found 2026-11-20, app.py) — split inverter AC filed as "Inverter AC"; brand "Voltas" for Voltas Beko
**Symptom:** SAMSUNG-AR50H19D1PHNNA ("Inverter Split AC") gets `Air Conditioner >
Inverter AC`; LG-US-Q18ENXE (a split AC) gets `Air Conditioner > Split AC`.
VOLTAS BEKO-WFL7012VTMP gets the brand `Voltas`.
**Likely cause:** the taxonomy has `Split AC` and `Inverter AC` as sibling
sub-categories, and `detect_category` tests inverter first (app.py:575-579). The
brand split keeps the first word of a two-word brand (not confirmed where).
**Accept:** the owner decides which sub-category an inverter split AC belongs to (or
adds the technology as a filter only), and the detector follows it. "Voltas Beko"
stays one brand.

## KI-32 · MED · open (found 2026-11-23) — removed junk is replaced by the next junk image
**Symptom:** in the KI-17 replays, each junk image that the filter removed freed a slot,
and lower-ranked junk filled it. Examples: samsung.com site icons after the energy
labels went, lg.com feature tiles after the pictograms went, and a page change on the
Haier row. HAIER-HRD-2561SGSA-N moved from bajajfinserv.in (2 images left) to
arihantelectronics.org (6 other-model photos), and after KI-18 to industrybuying.com
(watermark and share cards).
**Likely cause:** the winner choice and `hunt_donors` reward a page for its image COUNT.
The 1000 px floor drops real sub-floor gallery photos (LG-US-Q18ENXE: 7 gallery photos
under 1000 px, so only a dimension drawing stays).
**Accept:** removing a junk image never changes the winning page to one with fewer
real photos of the row's model. A page's image score counts only images that pass the
junk and KI-18 checks. Needs an owner decision if it touches the floor.

## KI-33 · LOW · open (found 2026-11-23) — partner logo passes as a product image
**Symptom:** HAIER-HRD-2561SGSA-N keeps `mc.bajajfinserv.in/.../dynamic-pdp/91mobiles.png`
(a partner logo) as image 2.
**Likely cause:** the URL has no generic junk word, and the logo is a brand name.
**Accept:** the logo is dropped (by size, aspect ratio or a bajajfinserv.in
`/dynamic-pdp/` rule) without dropping real product photos. Until then, the reviewer
removes it in card review.
