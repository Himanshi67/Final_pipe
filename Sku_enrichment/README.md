# SKU Enricher — setup & usage

Fills product data (title, MRP, sale price, product URL, image URLs) into your Excel by searching Amazon.in → Flipkart → other Indian retailers (Croma, Reliance Digital, Vijay Sales, Tata CLiQ, JioMart, brand sites) for each SKU.

## One-time setup (Windows)

1. Install Python from https://www.python.org/downloads/ — tick **"Add Python to PATH"** during install.
2. Put `sku_enricher.py` and `requirements.txt` in the same folder as your Excel.
3. Open Command Prompt in that folder and run:

```
pip install -r requirements.txt
```

## Run

```
python sku_enricher.py "WM MODEL LIST FOR WEBSITE.xlsx" --min-price 8000
```

`--min-price 8000` is recommended for appliance batches — it instantly rejects accessories (covers, stands) that mention your model code but cost a few hundred rupees.

Useful options:

```
--limit 5                  test run on first 5 unfilled SKUs
--min-price 8000           reject listings cheaper than this (accessory killer)
--max-price 60000          reject listings above this
--min-images 3             required image count; if a verified listing has fewer,
                           other sites are tried and the row is flagged (default 3)
--sku-col "Model No"       if it picks the wrong column (it prints its choice at start)
--sheet "Sheet2"           a specific worksheet
--retry-incomplete         re-attempt FOUND rows missing a price or enough
                           images (their existing good cells are kept)
--force                    redo rows that already have a url_link
--delay 5 10               slower pace if you get blocked
```

## How matching works

For each SKU it searches Amazon.in, Flipkart, the brand's official store (Haier India, Samsung, LG, IFB, Godrej, Whirlpool, Bosch, Panasonic, Voltas), and the open web — **all searches run before anything is accepted**, then candidates are checked in priority order. Only real product pages are fetched (search pages, categories, blogs and PDFs are filtered out), and pages that fail Amazon's bot-check are automatically retried with a different browser fingerprint. A listing is accepted only if it passes every check: no accessory keywords (cover, stand, filter, remote...) or compatibility wording ("made for", "compatible with", "for Haier..."), brand present, price within your bounds, and the exact model code verified in the listing title (or URL, when the title starts with the brand). Unverified candidates additionally face a capacity check (a 9 kg SKU like HW90-... rejects an 8 kg listing) and a category check (a -WM SKU rejects refrigerators).

Site-specific extractors cover Amazon, Flipkart, Croma (full galleries), IFB's own store (price + MRP + full gallery) and Reliance Digital (title, sale price, MRP — but Reliance loads its gallery via script, so usually only the cover image is available and the row is flagged). Amazon, Flipkart AND Croma are all searched explicitly for every SKU, so a gallery-rich source is nearly always in the running. A verified listing is only accepted as final when it's **complete — price present and at least `--min-images` images**; otherwise remaining sites are tried and the best verified hit is kept with an explicit flag ("- no price shown", "- only 1 image(s)").

**Croma price note:** Croma loads prices via script, so raw pages sometimes contain no price (verified). Such rows are flagged, and the completeness rule makes Amazon/Flipkart/Reliance win whenever they carry the product.

## What you get

Creates `<your file>_filled.xlsx` — original untouched. Columns filled: `title`, `regular price` (MRP when shown), `sale price`, `url_link`, `image_url` (up to 8 full-res image links, comma-separated), plus a `status` column:

- **FOUND (amazon.in)** — exact model code verified, accessory checks passed. Safe to use.
- **CLOSE MATCH - CHECK (...)** — same model except trailing characters (e.g. `HWM70-306ES5N1` vs listing `HWM70-306ES5`) — usually the right product in a variant code; confirm before publishing.
- **CHECK MANUALLY (...)** — the model code couldn't be verified on any listing (typical for internal SKUs like `WHIRLPOOL-31631 7.5KG GRY 10YR`); the best brand-led, accessory-clean candidate is recorded. Verify before publishing.
- **NOT FOUND / ERROR** — nothing passed the strict checks; do these by hand or via the Claude prompt (see `claude_prompt.md`). Strict is intentional: a blank cell is safer than a wrong product.

Missing columns are added automatically, so the script works on a bare SKU list too.

## Resume & interruptions

Progress saves after **every SKU**. Ctrl+C anytime; re-running the same command skips every row already attempted (found AND not-found) and continues from the first fresh row. `--retry-missing` re-attempts the NOT FOUND / ERROR rows; `--retry-incomplete` re-attempts FOUND rows that lack a price or enough images; `--force` redoes everything. For 25–100 SKUs expect roughly 30–90 min (deliberate delays avoid blocking). Don't keep the `_filled.xlsx` open in Excel during a run — the script will pause and wait if you do.

## If results degrade (blocking / captchas)

- Increase delay: `--delay 6 12`, and run in batches of 25–50.
- Don't run over VPN or office proxy if possible; a normal broadband IP works best.
- Better search quality (optional): get a free key at https://serper.dev (2,500 free searches), then before running:
  `set SERPER_API_KEY=your_key_here`

## Notes

- SKUs containing the manufacturer model code (e.g. `SAMSUNG-WA70BG4441YYTL`) match near-100%; purely internal codes rely on brand match and land in CHECK MANUALLY.
- Prices change constantly — treat them as a snapshot of the run date.
- Keep volumes modest and delays on; scraping marketplaces sits in a grey zone of their terms of use.