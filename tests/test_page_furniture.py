"""Website furniture must never become a product fact (smoke 2026-11-19).

samsung.com/in TV pages carry two pieces of page furniture that reached the
output as facts:
  * a buy-back popup table whose HEADING row 'Model Name | Purchase Price'
    became the spec 'Model Name: Purchase Price' (CSV attribute
    'Model = Purchase Price');
  * a hidden UK template '<del class="was-text">£2,399.00</del>' that
    mrp_from_strike() took as the MRP of a 32-inch TV.

    .venv\\Scripts\\python.exe -m pytest tests\\test_page_furniture.py -q
"""
import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sku_enricher as E                           # noqa: E402


def soup(html):
    return BeautifulSoup(html, "lxml")


SAMSUNG_POPUP = """
<html><body>
<table class="assured-buyback-popup__info">
  <thead><tr><th scope="col">Model Name</th><th scope="col">Purchase Price</th></tr></thead>
  <tbody><tr><td></td><td></td></tr></tbody>
</table>
<div class="pdd16-step-buying__header-price-save">
  <del class="was-text">£2,399.00</del><span class="sale-text">save $ £400.00</span>
</div>
</body></html>"""


# ---------------------------------------------------------------- spec table

def test_header_row_in_thead_is_not_a_spec():
    assert E.table_specs(soup(SAMSUNG_POPUP)) == []


def test_all_th_row_outside_thead_is_not_a_spec():
    html = "<table><tr><th>Feature</th><th>Detail</th></tr>" \
           "<tr><td>Screen Size</td><td>80 cm</td></tr></table>"
    assert E.table_specs(soup(html)) == [("Screen Size", "80 cm")]


@pytest.mark.parametrize("row", [
    "<tr><th>Resolution</th><td>1366 x 768</td></tr>",
    "<tr><td>Resolution</td><td>1366 x 768</td></tr>",
])
def test_real_spec_rows_still_pass(row):
    assert E.table_specs(soup(f"<table>{row}</table>")) == [("Resolution", "1366 x 768")]


def test_extract_on_samsung_furniture_writes_no_spec_and_no_price():
    rec = E.extract("https://www.samsung.com/in/tvs/hd-tv/x-ua32h4500fuxxl/",
                    SAMSUNG_POPUP)
    assert rec["specs"] == ""
    assert rec["regular price"] == ""
    assert rec["sale price"] == ""


# ---------------------------------------------------- value is a UI label

@pytest.mark.parametrize("value", [
    "Purchase Price", "Offer Price", "price", "MRP", "M.R.P.", "Selling Price",
    "Buy-back Price", "EMI Options", "Offers",
    "Buy Now", "Add to Cart", "Check Pincode", "View Offers", "Out of Stock",
])
def test_firewall_rejects_ui_label_as_value(value):
    assert not E.spec_row_ok("Model Name", value)
    assert not E.usable_spec("Model Name", value)


@pytest.mark.parametrize("label,value", [
    ("Model Name", "UA32H4500FUXXL"),
    ("Maintenance", "Low Cost Maintenance"),
    ("Type", "HD READY"),
    ("Warranty", "1 Year Comprehensive"),
])
def test_firewall_keeps_real_values(label, value):
    assert E.spec_row_ok(label, value)


# ------------------------------------------------------- strike-through MRP

def test_foreign_currency_strike_is_not_mrp():
    assert E.mrp_from_strike(soup(SAMSUNG_POPUP)) == ""


@pytest.mark.parametrize("text", ["$499.00", "€1,299", "USD 899", "GBP 2,399"])
def test_other_foreign_currencies_rejected(text):
    assert E.mrp_from_strike(soup(f"<del>{text}</del>")) == ""


@pytest.mark.parametrize("text,want", [("₹37,790", "37,790"), ("Rs. 24,990", "24,990"),
                                       ("24,990", "24,990")])
def test_rupee_and_bare_strike_prices_still_read(text, want):
    assert E.mrp_from_strike(soup(f"<del>{text}</del>")) == want


def test_rupee_strike_after_foreign_one_is_used():
    html = "<del>£2,399.00</del><span class='old-price'>₹ 31,900</span>"
    assert E.mrp_from_strike(soup(html)) == "31,900"


# ------------------------------------------- MRP label split across elements
# haier.com (cached H32K82FX page, 2026-11-19): label, ':' + currency sign and
# digits each sit in their own element. MOP is NOT a price (owner decision):
# it must not be read as the MRP and is not mapped to the selling price.
HAIER_PRICE_BLOCK = """
<div class="pro_price">
    <div class="detail_inlineblock">
        <span>MOP</span>
    </div>
    <div class="detail_inlineblock">
        <span>: ₹</span>
    </div>
    <div class="detail_inlineblock ">
        <span class="js_price">18590</span>
    </div>
</div>
 <div class="pro_price">
    <div class="detail_inlineblock">
        <span>MRP</span>
    </div>
    <div class="detail_inlineblock">
        <span>: ₹</span>
    </div>
    <div class="detail_inlineblock ">
        <span class="js_mop">37790</span>
    </div>
</div>
"""


def test_mrp_label_across_elements_haier():
    assert E.mrp_near_label(HAIER_PRICE_BLOCK) == "37,790"


def test_mop_alone_is_not_an_mrp():
    mop_only = HAIER_PRICE_BLOCK.split(" <div class=\"pro_price\">")[0]
    assert "MOP" in mop_only and "MRP" not in mop_only
    assert E.mrp_near_label(mop_only) == ""


@pytest.mark.parametrize("html,want", [
    ("MRP: ₹24,990", "24,990"),                       # old layouts still read
    ("M.R.P. (Incl. of all taxes) <span>₹ 31,900</span>", "31,900"),
    ("<b>MRP</b> - <i>Rs.</i> <span>12,499</span>", "12,499"),
])
def test_mrp_label_layouts(html, want):
    assert E.mrp_near_label(html) == want


def test_mrp_label_does_not_jump_over_text():
    # words between the label and the number: not this label's price
    assert E.mrp_near_label("MRP is shown at checkout. Price ₹18,590") == ""
