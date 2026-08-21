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
