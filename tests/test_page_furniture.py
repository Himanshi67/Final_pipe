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
# digits each sit in their own element. MOP is never read as the MRP; it is
# the SELLING price (owner decision 2026-11-19, revised), via SALE_LABEL_RE.
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


# ------------------------------------------- MOP label = the selling price
def test_mop_label_across_elements_is_the_sale_price_haier():
    assert E.sale_near_label(HAIER_PRICE_BLOCK) == "18,590"


def test_mrp_block_alone_has_no_sale_price():
    mrp_only = HAIER_PRICE_BLOCK.split(" <div class=\"pro_price\">")[1]
    assert "MRP" in mrp_only and "MOP" not in mrp_only
    assert E.sale_near_label(mrp_only) == ""


def test_extract_haier_layout_gives_both_prices():
    page = f"<html><head><title>Haier H32K82FX</title></head><body>{HAIER_PRICE_BLOCK}</body></html>"
    rec = E.extract("https://www.haier.com/in/tvs/h32k82fx.shtml", page)
    assert rec["sale price"] == "18,590"
    assert rec["regular price"] == "37,790"


@pytest.mark.parametrize("html,want", [
    ("MOP: ₹18,590", "18,590"),
    ("M.O.P. <span>Rs. 9,999</span>", "9,999"),
    ("<b>Market Operating Price</b> : <i>₹</i> <span>24,490</span>", "24,490"),
    ("market operating price - INR 12,000", "12,000"),
])
def test_mop_label_layouts(html, want):
    assert E.sale_near_label(html) == want


@pytest.mark.parametrize("html", [
    "Mop: ₹499",                                   # a cleaning mop, not a label
    "Wet mop pad <span>₹ 799</span>",
    "MOP is shown at checkout. Price ₹18,590",     # text between label and number
    "MOP: 18,590",                                 # no currency sign: not taken
    "MOPPING mode ₹ 1,200",                        # label runs into letters
])
def test_mop_label_needs_the_label_and_the_currency(html):
    assert E.sale_near_label(html) == ""


_JSONLD_17999 = ('<script type="application/ld+json">{"@type":"Product",'
                 '"name":"X","offers":{"@type":"Offer","price":"17999",'
                 '"priceCurrency":"INR"}}</script>')


def test_mop_wins_over_the_jsonld_sale_price():
    # owner decision 2026-11-19 (late): a labelled MOP on the page wins
    rec = E.extract("https://www.example.in/p/x", _JSONLD_17999 + HAIER_PRICE_BLOCK)
    assert rec["sale price"] == "18,590"
    assert rec["price_kind"] == E.PRICE_KIND_MOP
    assert rec["regular price"] == "37,790"          # MRP stays the MRP


def test_no_mop_the_jsonld_sale_price_is_used():
    mrp_only = HAIER_PRICE_BLOCK.split(" <div class=\"pro_price\">")[1]
    rec = E.extract("https://www.example.in/p/x",
                    _JSONLD_17999 + ' <div class="pro_price">' + mrp_only)
    assert rec["sale price"].replace(",", "") == "17999"
    assert rec["price_kind"] == E.PRICE_KIND_SALE
    assert rec["regular price"] == "37,790"


def test_no_price_no_price_kind():
    rec = E.extract("https://www.example.in/p/x",
                    "<html><head><title>X</title></head><body>hi</body></html>")
    assert rec["sale price"] == "" and rec["price_kind"] == ""


def test_mop_only_page_is_mop():
    rec = E.extract("https://www.example.in/p/x",
                    "<html><body>MOP: ₹18,590</body></html>")
    assert rec["sale price"] == "18,590" and rec["price_kind"] == "MOP"


def test_cleaning_mop_text_does_not_beat_the_sale_price():
    rec = E.extract("https://www.example.in/p/x",
                    _JSONLD_17999 + "<p>Wet mop pad <span>₹ 799</span></p>")
    assert rec["sale price"].replace(",", "") == "17999"
    assert rec["price_kind"] == E.PRICE_KIND_SALE


def test_merge_carries_the_donor_price_kind():
    best = {"url_link": "https://www.samsung.com/in/x", "sale price": "",
            "image_url": "", "specs": ""}
    donor = {"url_link": "https://www.haier.com/in/x", "sale price": "18,590",
             "regular price": "37,790", "price_kind": "MOP", "image_url": "",
             "specs": "", "page_text": "80 cm (32) HD TV"}
    merged, _donors = E.merge_sources(best, [best, donor])
    assert merged["sale price"] == "18,590" and merged["price_kind"] == "MOP"
    assert merged["page_text"] == "80 cm (32) HD TV"


# ------------------------------------- label/value pair specs (samsung.com)
SAMSUNG_SPEC_BLOCK = """
<div class="pdd32-product-spec__list"><ul>
 <li class="pdd32-product-spec__content-item">
  <p class="pdd32-product-spec__content-item-title">Screen Size</p>
  <p class="pdd32-product-spec__content-item-desc"> 80 cm </p></li>
 <li class="pdd32-product-spec__content-item">
  <p class="pdd32-product-spec__content-item-title">Resolution</p>
  <p class="pdd32-product-spec__content-item-desc">HD (1,366 x 768)</p></li>
 <li class="pdd32-product-spec__content-item">
  <p class="pdd32-product-spec__content-item-title">Beveled Type</p>
  <p class="pdd32-product-spec__content-item-desc">The new Beveled type bezel
   design offers an elegant 45-degree angled inner edge bezel.</p></li>
</ul></div>
<ul class="pdd43-master-compare__list"><li class="x">
  <p class="feature-title">Refresh Rate</p>
  <p class="feature-value">120Hz</p></li></ul>
"""


def test_pair_specs_reads_label_value_siblings():
    rows = dict(E.pair_specs(soup(SAMSUNG_SPEC_BLOCK)))
    assert rows["Screen Size"] == "80 cm"
    assert rows["Resolution"] == "HD (1,366 x 768)"
    assert "Beveled Type" not in rows            # a sentence, not a value
    assert "Refresh Rate" not in rows            # a compare widget


def test_page_text_reads_meta_and_jsonld_description():
    page = ('<html><head><meta name="description" content="80 cm (32) HD '
            'Smart TV"><script type="application/ld+json">{"@type":"Product",'
            '"name":"X","description":"Tizen OS. Buy now on Flipkart"}'
            '</script></head><body></body></html>')
    text = E.page_text(soup(page))
    assert "80 cm (32) HD Smart TV" in text and "Tizen OS" in text
    assert "Flipkart" not in text                # the content firewall
