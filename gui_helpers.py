"""gui_helpers.py - pure, streamlit-free helpers behind gui.py.

Unit-tested in tests/test_gui_helpers.py. Keep streamlit (and app.py)
imports OUT of this module so the tests never boot the app."""

import re

import pandas as pd

_URL_RE = re.compile(r"^https?://", re.I)


def flag_value(flags, name):
    """Integer value following `name` in an argv-style flag list; 0 if the
    flag is absent, last, or its value is not a number."""
    flags = list(flags or [])
    try:
        return int(str(flags[flags.index(name) + 1]))
    except (ValueError, IndexError):
        return 0


def sanity_issues(df, min_price=0):
    """Row-level problems worth fixing BEFORE the CSV build.

    df is the review dataframe from gui.load_filled_df: prices already
    numeric (NaN when missing), url_link a string. Returns
    [{"sku": ..., "problem": ...}, ...] in row order."""
    issues = []
    for _, row in df.iterrows():
        sku = str(row.get("sku", "")).strip()

        def add(problem):
            issues.append({"sku": sku, "problem": problem})

        mrp = row.get("regular price")
        sale = row.get("sale price")
        url = str(row.get("url_link", "") or "").strip()

        if pd.notna(mrp) and mrp <= 0:
            add(f"non-positive MRP ({mrp:.0f})")
        if pd.notna(sale) and sale <= 0:
            add(f"non-positive sale price ({sale:.0f})")
        if pd.notna(mrp) and pd.notna(sale) and mrp > 0 and sale > mrp:
            add(f"sale price {sale:.0f} above MRP {mrp:.0f}")
        if min_price and pd.notna(sale) and 0 < sale < min_price:
            add(f"sale price {sale:.0f} below batch min price {min_price}")
        if url and not _URL_RE.match(url):
            add("url_link is not an http(s) URL")
    return issues


def filter_review_df(df, query="", brands=None):
    """Search + brand filter for the review table. Case-insensitive
    substring match of `query` against sku and title (regex-escaped), then
    an exact Brands membership filter. Original index is preserved so
    edits still map back by SKU."""
    out = df
    q = str(query or "").strip()
    if q:
        title = (out["title"] if "title" in out.columns
                 else pd.Series("", index=out.index))
        hay = (out["sku"].astype(str) + " " + title.astype(str)).str.lower()
        out = out[hay.str.contains(re.escape(q.lower()), na=False)]
    if brands:
        out = out[out["Brands"].astype(str).isin(list(brands))]
    return out


def _price_series(s):
    """'22,599' / '₹22,599' → 22599.0; blanks → NaN. Same stripping the
    review loader applies (gui.load_filled_df)."""
    return pd.to_numeric(
        s.astype(str).str.replace(",", "", regex=False)
         .str.replace("₹", "", regex=False).str.strip(),
        errors="coerce")


def batch_summary(result):
    """Aggregate stats over app.process_inventory() output (string cells).
    Answers 'is this batch import-ready?' at a glance."""
    imgs = result["Images"].astype(str).str.strip().map(
        lambda v: len([u for u in v.split(",") if u.strip()]) if v else 0)
    sale = _price_series(result["sale price"])
    has_price = sale.notna().any()
    return {
        "products": len(result),
        "zero_image": int((imgs == 0).sum()) if len(result) else 0,
        "avg_images": round(float(imgs.mean()), 1) if len(result) else 0.0,
        "spec_gap_rows": int((result["_spec_gaps"].astype(str)
                              .str.strip() != "").sum()),
        "missing_brand": int((result["Brands"].astype(str)
                              .str.strip() == "").sum()),
        "price_min": int(sale.min()) if has_price else 0,
        "price_max": int(sale.max()) if has_price else 0,
        "category_counts": result["categories"].astype(str).str.strip()
                                 .replace("", "(none)")
                                 .value_counts().to_dict(),
    }


# split only at commas that start a new URL - same rule as
# app.split_image_list, so commas inside a URL survive
_IMG_SPLIT_RE = re.compile(r",(?=\s*(?:https?://|//))")

# mirror of app.prefixed_sku's sanitization (app.py:605-612)
_SKU_SANITIZE = (
    (re.compile(r"\s+"), "-"),
    (re.compile(r'[\/:*?"<>|()\[\]{}]+'), "-"),
    (re.compile(r"-{2,}"), "-"),
)


def parse_image_urls(cell):
    """Comma-separated image URLs from a review-sheet cell -> list."""
    text = str(cell or "").strip()
    if not text:
        return []
    return [u.strip() for u in _IMG_SPLIT_RE.split(text) if u.strip()]


def parse_specs_cell(cell):
    """'Label: Value | Label: Value' (sku_enricher.format_specs) ->
    [(label, value)]. format_specs strips ':' and '|' out of labels, so
    splitting on ' | ' then the FIRST ':' is lossless."""
    pairs = []
    for part in str(cell or "").split(" | "):
        if ":" in part:
            label, value = part.split(":", 1)
            if label.strip() and value.strip():
                pairs.append((label.strip(), value.strip()))
    return pairs


def _sanitized_sku(raw):
    s = str(raw or "").strip()
    for rx, rep in _SKU_SANITIZE:
        s = rx.sub(rep, s)
    return s.strip("-")


def raw_sku_for_csv_sku(csv_sku, raw_skus):
    """Map a CSV/validation sku (app.prefixed_sku output) back to the raw
    sku the review sheet holds. The prefixed form is sanitize(raw) with an
    optional 'XX-' category prefix, so match sanitized suffixes; the
    longest raw wins; None when nothing matches."""
    c = str(csv_sku or "").strip()
    best, best_len = None, -1
    for raw in raw_skus:
        s = _sanitized_sku(raw)
        if s and (c == s or c.endswith("-" + s)) and len(s) > best_len:
            best, best_len = raw, len(s)
    return best
