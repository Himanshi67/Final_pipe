# Reusable Claude prompt — SKU product-data lookup

Paste this into Claude (Cowork) along with your Excel/CSV. Use it for full batches, or just for the rows the script marked NOT FOUND / CHECK MANUALLY.

---

Attached is my product file. For every row with a SKU but an empty `url_link`:

1. Identify the manufacturer model code inside the SKU (the alphanumeric code after the brand, e.g. `SAMSUNG-WA70BG4441YYTL` → `WA70BG4441YYTL`) and search the web for it.
2. Pick the listing in this priority: amazon.in → flipkart.com → Croma / Reliance Digital / Vijay Sales / Tata CLiQ / JioMart / official brand store. Open the product page and confirm the model code (or exact product) appears in the listing title or URL before trusting it.
   **Strict rule:** the listing must BE the product, not an accessory that mentions it — reject covers, stands, filters, remotes, spare parts, and anything worded "made for / compatible with / for [brand]", even if the title contains the exact model code. Sanity-check the price against the product category (a ₹749 result for a washing machine is an accessory).
3. Fill these columns exactly:
   - **title** — the listing title, copied verbatim
   - **regular price** — MRP if the page shows one (numbers only, Indian commas, no ₹)
   - **sale price** — current selling price (same format)
   - **url_link** — clean product-page URL
   - **image_url** — all product gallery images at full resolution, comma-separated in one cell
4. If no verifiable match exists, write `NOT FOUND` in a status column and leave the rest blank — never guess or substitute a similar model.

Work in batches of 10, saving the file after each batch so progress is never lost. When finished, give me the updated file plus a short summary: found on Amazon / found on Flipkart / found elsewhere / not found.

---

**Tip:** for a quick single lookup, use:
"Find [SKU] on amazon.in or flipkart — give me title, sale price, MRP, product URL and all gallery image URLs, verified against the model code."
