"""The content firewall and the key-point cap (owner rule 2026-11-19).

Nothing that belongs to the SOURCE RETAILER may reach a field the pipeline
writes: retailer names, seller lines, EMI, bank/card offers, cashback,
coupons, exchange offers, delivery/pincode text, returns, customer care,
badges, ratings, URLs, phones, e-mails. One definition lives in
sku_enricher.py (content_hits / content_ok / spec_row_ok / content_clean);
app.py and validate_csv.py use it. The three description fields show at most
sku_enricher.KEY_POINTS_MAX relevant points.

    .venv\\Scripts\\python.exe -m pytest tests\\test_content_firewall.py -q
"""
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

import app                                         # noqa: E402
import sku_enricher as E                           # noqa: E402
import validate_csv as V                           # noqa: E402

# one sample per forbidden category
FORBIDDEN = {
    "retailer amazon": "Amazon",
    "retailer flipkart": "Flipkart",
    "retailer croma": "Croma",
    "retailer reliance digital": "Reliance Digital",
    "retailer domain": "Amazon.in",
    "marketplace seller": "Cloudtail",
    "sold by": "Sold by RetailNet",
    "fulfilled by": "Fulfilled by partner",
    "emi": "EMI from 999 per month",
    "no cost emi": "No Cost EMI available",
    "bank offer": "Bank Offer on HDFC",
    "card offer": "10% card offer",
    "cashback": "5% Cashback",
    "coupon": "Apply coupon SAVE10",
    "exchange": "Exchange up to 2000 off",
    "delivery": "Free delivery by Tuesday",
    "pincode": "Enter pincode to check",
    "cod": "Cash on Delivery available",
    "returns": "10 Days Return",
    "replacement": "7 days replacement",
    "customer care": "Contact customer care",
    "service centre": "Visit the nearest service centre",
    "badge amazon's choice": "Amazon's Choice",
    "badge best seller": "Best Seller",
    "badge assured": "Flipkart Assured",
    "ratings": "4,512 ratings",
    "reviews": "1,024 reviews",
    "stars": "4.3 out of 5 stars",
    "deal": "Limited time deal",
    "stock": "Only 3 left in stock",
    "url": "https://www.example.com/p/123",
    "bare domain": "see example.com",
    "phone toll-free": "1800 123 4567",
    "phone mobile": "+91 98765 43210",
    "email": "care@example.com",
}


@pytest.mark.parametrize("term", FORBIDDEN.values(), ids=FORBIDDEN.keys())
def test_forbidden_in_label_value_and_title(term):
    assert not E.spec_row_ok(term, "Yes"), "label"
    assert not E.spec_row_ok("Special Feature", f"Includes {term}"), "value"
    assert not E.content_ok(f"Haier 7 kg Washing Machine {term}"), "title"
    assert E.content_hits(term), "content_hits names the term"
    # a bracketed fragment is cut out of a title entirely
    cleaned = E.content_clean(f"Haier 7 kg Fully Automatic Washing Machine ({term})")
    assert E.content_ok(cleaned), cleaned
    assert cleaned.startswith("Haier 7 kg Fully Automatic Washing Machine")


@pytest.mark.parametrize("text", [
    "Amazon Alexa Built-in", "Works with Alexa", "Amazon Fire TV built in",
    "Netflix, Amazon Prime Video, YouTube", "2 Years",
    "2 Years Comprehensive, 10 Years on Compressor", "Ion exchange resin",
    "Copper heat exchanger", "EMI filter", "8901234567890",
])
def test_protected_product_text_survives(text):
    assert E.content_ok(text), E.content_hits(text)


def test_protected_rows_and_brand():
    assert E.spec_row_ok("Warranty", "2 Years")
    assert E.spec_row_ok("Voice Assistant", "Amazon Alexa Built-in")
    assert E.spec_row_ok("Installation Type", "Wall Mounted")
    assert E.spec_row_ok("Manufacturer Warranty", "2 Years")
    assert not E.spec_row_ok("Warranty", "1 Year, call customer care 1800 123 4567")
    # the product's own brand is never a retailer name
    assert E.content_ok("Croma 1.7 L Electric Kettle", brand="Croma")
    assert not E.content_ok("Croma 1.7 L Electric Kettle")
    assert E.content_clean("Croma 1.7 L Electric Kettle - Croma",
                           brand="Croma") == "Croma 1.7 L Electric Kettle"
    assert E.spec_row_ok("Brand", "Croma")          # a Brand row is its own brand
    assert E.content_ok("Amazon Basics 1.5 Ton AC", brand="Amazon Basics")


def test_title_cleaning():
    assert E.content_clean("Amazon.in: Buy LG 1.5 Ton AC Online") == "LG 1.5 Ton AC"
    assert E.content_clean("Samsung 55 inch 4K TV - Sold by Cloudtail") == \
        "Samsung 55 inch 4K TV"
    assert E.content_clean("Haier 7 kg Washing Machine Amazon Exclusive, "
                           "Best Seller") == "Haier 7 kg Washing Machine"
    keep = ("LG 260 L 3 Star Frost Free Refrigerator (GL-S292RPZY, Shiny "
            "Steel, Smart Inverter Compressor)")
    assert E.content_clean(keep) == keep
    assert E.content_clean("Samsung TV, Works with Amazon Alexa") == \
        "Samsung TV, Works with Amazon Alexa"
    # never empty when there was a product name
    assert E.content_clean("Croma") == "Croma"


def _enriched_row():
    return {
        "sku": "LG-GLS292", "Brands": "LG",
        "title": "LG 260 L 3 Star Frost Free Refrigerator GL-S292 | Amazon.in",
        "categories": "", "regular price": "", "sale price": "",
        "url_link": "", "image_url": "",
        "specs": ("Country of Origin: India | Manufacturer: X Industries | "
                  "Customer Care: 1800 123 4567 | Sold by: Cloudtail | "
                  "Colour: Shiny Steel | Warranty: 1 Year"),
    }


def test_already_enriched_row_is_cleaned_at_build_time():
    ctx = app.row_context(_enriched_row())
    assert ctx["title"] == "LG 260 L 3 Star Frost Free Refrigerator GL-S292"
    config, short_desc, long_desc, rows = app.build_descriptions(
        ctx["sku"], ctx["title"], ctx["brand"], ctx["category_family"],
        ctx["scraped"], sub=ctx["sub"], parent=ctx["parent"])
    labels = {label for label, _ in rows}
    for bad in ("Country of Origin", "Manufacturer", "Customer Care", "Sold by"):
        assert bad not in labels
    blob = " ".join([config, short_desc, long_desc]
                    + [f"{a} {b}" for a, b in ctx["dynamic_attrs"]])
    for bad in ("Country of Origin", "India", "X Industries", "1800",
                "Customer Care", "Cloudtail", "Sold by", "Amazon"):
        assert bad not in blob, bad
    assert ("Colour", "Shiny Steel") in rows
    assert ("Warranty", "1 Year") in rows


def _th_labels(html):
    return re.findall(r"<th>(.*?)</th>", html)


def test_at_most_key_points_and_whitelist_order(monkeypatch):
    assert E.KEY_POINTS_MAX == 6
    monkeypatch.setattr(app, "SUBCATEGORY_ATTRIBUTES", {
        app._cat_norm("Test Sub"): {"Star Rating": [], "Capacity": [],
                                    "Type": []}})
    scraped = [("Colour", "White"), ("Configuration", "Top Load"),
               ("Noise Level", "50 dB"), ("Capacity", "8 kg"),
               ("Energy Rating", "5 Star")]
    title = "Zyxo Model ZX100"
    config, short_desc, long_desc, rows = app.build_descriptions(
        "AP-ZX100", title, "Zyxo", "", scraped, sub="Test Sub")
    points = _th_labels(short_desc)[2:]           # after Brand, Product SKU
    whitelisted = [p for p in points
                   if p in ("Energy Rating", "Capacity", "Configuration")]
    # whitelist rows come first, in SHEET order (Star Rating, Capacity, Type)
    assert points[:3] == ["Energy Rating", "Capacity", "Configuration"]
    assert whitelisted == ["Energy Rating", "Capacity", "Configuration"]
    # with a whitelist, other scraped rows stay out of the descriptions...
    for field in (config, short_desc, long_desc):
        assert "Colour" not in field and "Noise Level" not in field
    # ...but still become attributes
    assert ("Colour", "White") in rows and ("Noise Level", "50 dB") in rows
    assert _th_labels(long_desc)[4:] == points


def test_no_whitelist_caps_at_six_and_never_pads():
    scraped = [(f"Extra Spec {n}", f"Value {n}") for n in range(1, 11)]
    config, short_desc, long_desc, _rows = app.build_descriptions(
        "AP-ZX100", "Zyxo Model ZX100", "Zyxo", "", scraped, sub="")
    assert len(_th_labels(short_desc)) - 2 <= E.KEY_POINTS_MAX
    assert len(_th_labels(long_desc)) - 4 <= E.KEY_POINTS_MAX
    assert config.count(":") - 1 <= E.KEY_POINTS_MAX
    # two relevant points -> two points, nothing invented
    _c, short2, long2, _r = app.build_descriptions(
        "AP-ZX100", "Zyxo Model ZX100", "Zyxo", "",
        [("Colour", "Red"), ("Weight", "2 kg")], sub="")
    assert _th_labels(short2) == ["Brand", "Product SKU", "Colour", "Weight"]


def test_invented_mains_row_is_gone():
    _c, short_desc, long_desc, _r = app.build_descriptions(
        "TV-T1", "Samsung 80 cm (32 inches) HD Ready Smart LED TV",
        "Samsung", "LED TV")
    for field in (short_desc, long_desc):
        assert "230 V" not in field and "Operational Grid" not in field


def test_validate_csv_reports_retailer_content():
    row = {"title": "Samsung TV", "description": "<p>Sold by Cloudtail</p>",
           "short description": "<table><tr><th>Brand</th><td>Samsung</td>"
                                "</tr></table>",
           "Attribute 1 name": "Warranty",
           "Attribute 1 value(s)": "Call 1800 123 4567"}
    cols = list(row)
    problems = V.content_firewall_problems(row, cols, brand="Samsung")
    assert any(p.startswith("retailer_content_in_field (description:")
               for p in problems)
    assert any("Attribute 1 value(s)" in p and "1800 123 4567" in p
               for p in problems)
    assert len(problems) == 2
    # blocking severity: not a warning_ reason
    assert V.is_blocking_issue({"reasons": problems})
    clean = {"title": "Croma 1.7 L Electric Kettle", "Tags": "Croma, Kettle"}
    assert V.content_firewall_problems(clean, list(clean), brand="Croma") == []


# ---- product size and weight are product facts (owner decision 2026-11-19) --
KEPT_SIZE_WEIGHT = [
    ("Product Dimensions", "45 x 60 x 85 cm", "Dimensions"),
    ("Product Dimensions LxWxH", "45 x 60 x 85 cm", "Dimensions"),
    ("Item Dimensions", "45 x 60 x 85 cm", "Dimensions"),
    ("Dimensions", "45 x 60 x 85 cm", "Dimensions"),
    ("Dimensions (LxWxH)", "45 x 60 x 85 cm", "Dimensions"),
    ("Dimensions (W x H x D)", "60 x 85 x 45 cm", "Dimensions"),
    ("Dimensions in cm", "60 x 85 x 45", "Dimensions"),
    ("Item Weight", "32 kg", "Weight"),
    ("Product Weight", "32 kg", "Weight"),
    ("Net Weight", "32 kg", "Weight"),
    ("Weight", "32 kg", "Weight"),
    ("Weight (kg)", "32", "Weight"),
]
BLOCKED_BOX_ROWS = [
    ("Package Dimensions", "60 x 70 x 95 cm"),
    ("Packaging Dimensions", "60 x 70 x 95 cm"),
    ("Package Weight", "36 kg"),
    ("Packaging Weight", "36 kg"),
    ("Shipping Dimensions", "60 x 70 x 95 cm"),
    ("Shipping Weight", "36 kg"),
    ("Net Quantity", "1 N"),
    ("Included Components", "Washing machine, inlet hose"),
    ("ASIN", "B0ABCDE123"),
]


@pytest.mark.parametrize("label,value,canon", KEPT_SIZE_WEIGHT)
def test_product_size_and_weight_pass_the_firewall(label, value, canon):
    assert E.spec_row_ok(label, value)
    assert E.usable_spec(label, value)
    assert app.canon_spec_label(label) == canon
    rows = app.parse_scraped_specs(f"{label}: {value}")
    assert rows == [(canon, value)]


@pytest.mark.parametrize("label,value", BLOCKED_BOX_ROWS)
def test_box_size_and_weight_stay_blocked(label, value):
    assert not E.spec_row_ok(label, value)
    assert app.parse_scraped_specs(f"{label}: {value}") == []


def test_several_size_labels_give_one_dimensions_row():
    cell = ("Product Dimensions: 45 x 60 x 85 cm | Item Dimensions: 1 x 2 x 3 cm"
            " | Package Dimensions: 9 x 9 x 9 cm | Item Weight: 32 kg"
            " | Shipping Weight: 36 kg")
    assert app.parse_scraped_specs(cell) == [
        ("Dimensions", "45 x 60 x 85 cm"), ("Weight", "32 kg")]


def test_dimension_lookalikes_keep_their_own_label():
    # a different fact, not the product's overall size
    assert app.canon_spec_label("Dimensions Without Stand") == \
        "Dimensions Without Stand"
    assert app.canon_spec_label("Gross Weight") == "Gross Weight"


# ------------------------------------- Gross Weight is not written (2026-11-19)
@pytest.mark.parametrize("label", ["Gross Weight", "gross weight (kg)",
                                   "Gross Weight in Kg", "Weight (Gross)"])
def test_gross_weight_is_refused(label):
    assert not E.spec_row_ok(label, "12.5 kg")
    assert app.parse_scraped_specs(f"{label}: 12.5 kg") == []


def test_product_weights_still_pass_as_weight():
    for label in ("Product Weight", "Item Weight", "Net Weight", "Weight"):
        assert app.parse_scraped_specs(f"{label}: 9.8 kg") == [("Weight", "9.8 kg")]
    cell = "Gross Weight: 12.5 kg | Net Weight: 9.8 kg"
    assert app.parse_scraped_specs(cell) == [("Weight", "9.8 kg")]
    # app.py never names a gross weight 'Weight'
    assert app.canon_spec_label("Gross Weight") != "Weight"
