"""Pins the 2026-11-20 optimisation pass: each cache or shortcut returns
exactly what the slow path returned.

    sku_enricher.first_meta       == soup.select_one('meta[attr="value"]')
    sku_enricher.jsonld_products  same result on a reused soup
    sku_enricher.content_hits     memoised, but every call gets its own list
    app._allowed_entry            memoised with typed keys (1 != 1.0)
    app.compact                   str fast path == the general path
    gui_helpers.stale_confirmations  only ticked rows are hashed, same answer
"""
import os
import re
import sys
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

import app                      # noqa: E402
import gui_helpers              # noqa: E402
import sku_enricher as E        # noqa: E402

HTML = """<html><head>
<META PROPERTY="og:title" content="First title">
<meta property="og:title" content="Second title">
<meta property="og:image" content="">
<meta property="og:image" content="https://x.test/a.jpg">
<meta itemprop="price" content="1,999">
<meta name="description" content="Desc">
<meta property="OG:DESCRIPTION" content="case differs">
<script type="application/ld+json">{"@type": "Product", "name": "P",
 "offers": {"price": "10"}}</script>
</head><body><meta property="product:price:amount" content="5"></body></html>"""


def test_first_meta_matches_select_one():
    soup = BeautifulSoup(HTML, "lxml")
    for attr, value in (("property", "og:title"), ("property", "og:image"),
                        ("itemprop", "price"), ("name", "description"),
                        ("property", "og:description"),
                        ("property", "product:price:amount"),
                        ("property", "missing")):
        want = soup.select_one(f'meta[{attr}="{value}"]')
        assert E.first_meta(soup, attr, value) is want, (attr, value)


def test_jsonld_products_same_on_reused_soup():
    soup = BeautifulSoup(HTML, "lxml")
    first = E.jsonld_products(soup)
    again = E.jsonld_products(soup)
    assert first == again and first and first is not again
    fresh = E.jsonld_products(BeautifulSoup(HTML, "lxml"))
    assert fresh == first


def test_content_hits_memo_hands_out_new_lists():
    text = "Sold by Cloudtail, free delivery"
    a = E.content_hits(text)
    a.append("mutated")
    b = E.content_hits(text)
    assert "mutated" not in b and b
    for t, brand in ((text, ""), ("Samsung TV on amazon.in", "Samsung"),
                     ("Call 1800 123 4567", "")):
        assert E.content_hits(t, brand=brand) == list(
            E._content_hits_for.__wrapped__(t, E._alnum(brand)))
    assert E.content_hits("   ") == []


def test_allowed_entry_cache_is_typed_and_equal():
    for value in ("Below 1 Ton", "1.5 Ton", "1 - 2 Ton", "5 Star", "Inverter",
                  1, 1.0, "43 Inch", "Above 100000"):
        assert app._allowed_entry(value) == app._classify_allowed(value)
    assert app._allowed_entry(1)[-1] == "1"
    assert app._allowed_entry(1.0)[-1] == "1.0"


def test_compact_fast_path():
    for value in ("  a \n b\t", "", "x", None, float("nan"), 12, 1.5):
        slow = ("" if value is None or pd.isna(value)
                else re.sub(r"\s+", " ", str(value)).strip())  # the old body
        assert app.compact(value) == slow


def test_gui_sheet_cache_reloads_after_a_save(tmp_path):
    import openpyxl
    import gui
    path = tmp_path / "x_filled.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["sku", "title", "status"])
    wb.active.append(["A", "old", "FOUND"])
    wb.save(path)
    P = {"filled": path}
    first = gui.load_filled_df(P)
    first.loc[0, "title"] = "caller mutation"      # must not leak into cache
    assert gui.load_filled_df(P).loc[0, "title"] == "old"
    wb = openpyxl.load_workbook(path)
    wb.active.cell(row=2, column=2, value="new title, longer")
    wb.save(path)
    assert gui.load_filled_df(P).loc[0, "title"] == "new title, longer"


def test_stale_confirmations_only_hashes_ticked_rows():
    df = pd.DataFrame([{"sku": "A", "title": "t1"}, {"sku": "B", "title": "t2"},
                       {"sku": "A", "title": "dup later row"}])
    fp_a = gui_helpers.row_fingerprint(df.iloc[0])
    state = {"confirmed": {
        "A": {"confirmed": True, "fp": fp_a},              # unchanged
        "B": {"confirmed": True, "fp": "old"},             # changed -> stale
        "C": {"confirmed": True, "fp": "x"},               # not on the sheet
        "D": {"confirmed": True, "skipped": True, "fp": "x"},
        "E": True}}
    assert gui_helpers.stale_confirmations(state, df) == {"B"}
    assert gui_helpers.stale_confirmations({"confirmed": {}}, df) == set()
