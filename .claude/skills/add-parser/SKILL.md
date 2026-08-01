---
name: add-parser
description: Add a new retailer site parser to sku_enricher.py. Use when the user wants results from a store the enricher doesn't parse yet.
---

# Add a retailer parser

1. **Recon** (web-scout agent): fetch 2 sample product pages from the new site; report
   whether it emits JSON-LD `Product` schema, og: tags, or needs bespoke markup parsing.
2. **Prefer the generic path.** `parse_generic` (sku_enricher.py:624, JSON-LD +
   OpenGraph) already handles schema-compliant sites — then you only need the domain
   ranked and allowed:
   - `PRIORITY_DOMAINS` (:54) / `KNOWN_STORES` (:64) so results rank via `domain_rank`.
   - `is_product_url` (:244) so its product URLs pass the filter (unknown domains
     currently default to True — KI-7 — but add explicit patterns anyway).
3. **Bespoke parser** only if JSON-LD is absent/broken:
   - Template: copy the shape of `parse_croma` (:511).
   - Contract: `parse_<site>(html, soup)` → tuple `(title, mrp, sale, images_list)`;
     empty strings / empty list for anything not found. `extract()` (:654) tops up
     missing fields from `parse_generic` automatically and caps images at
     `MAX_IMAGES = 8`.
   - Register the domain branch in the `extract()` dispatch chain (:657-668).
4. **Verify** (runner agent): scratch input with 2–3 SKUs known to exist on that
   retailer, `--limit` run; confirm FOUND rows cite the new domain and capture
   price + ≥3 images.
5. **/sync-copies**, and add the domain to the site list in docs/PIPELINE_GUIDE.md §3.
