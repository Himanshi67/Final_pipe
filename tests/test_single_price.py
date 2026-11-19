"""[pricing] single_price (owner decision 2026-11-19): ONE price only.

"Keep only one MRP, not more; I don't want MRP and then a sale price; keep it
only as MRP; the higher price will work." With single_price = true the build
output has 'regular price' = the HIGHER of the row's MRP and selling price
(whichever exist) and 'sale price' = empty; no "MRP", "save" or "% off" text
is written; price-band tags use the one price; the validator, the REST
payload (sale_price sent as "") and the stage-1 check follow. Code default
is false, so other clients keep two prices. _filled.xlsx is not changed.

    .venv\\Scripts\\python.exe -m pytest tests\\test_single_price.py -q
"""
import contextlib
import dataclasses
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

import pandas as pd                                # noqa: E402

import app                                         # noqa: E402
import client_config                               # noqa: E402
import validate_csv as V                           # noqa: E402
import wc_publish as W                             # noqa: E402


@contextlib.contextmanager
def pricing(single):
    saved = (app.CLIENT, V.CLIENT, W.CLIENT)
    try:
        for mod in (app, V, W):
            mod.CLIENT = dataclasses.replace(
                mod.CLIENT, pricing=client_config.PricingConfig(single))
        yield
    finally:
        app.CLIENT, V.CLIENT, W.CLIENT = saved


def row(**over):
    base = {"sku": "43FSFHS",
            "title": "Foxsky 109 cm (43 Inch) Full HD Smart LED TV 43FSFHS",
            "categories": "", "Brands": "", "regular price": "45,990",
            "sale price": "28,990", "url_link": "", "image_url": "",
            "specs": ""}
    base.update(over)
    return base


# ------------------------------------------------------------ client config
def test_pricing_table_defaults_to_two_prices():
    assert client_config._load_pricing({}, "t").single_price is False
    assert client_config.PricingConfig().single_price is False


def test_pricing_table_reads_true_and_refuses_junk():
    assert client_config._load_pricing(
        {"pricing": {"single_price": True}}, "t").single_price is True
    with pytest.raises(client_config.ClientConfigError):
        client_config._load_pricing({"pricing": {"single_price": "yes"}}, "t")
    with pytest.raises(client_config.ClientConfigError):
        client_config._load_pricing({"pricing": "on"}, "t")


def test_shreejee_is_single_price():
    assert client_config.load_client("shreejee").pricing.single_price is True


# ------------------------------------------------------------------ app.py
@pytest.mark.parametrize("mrp,sale,want", [
    (45990, 28990, 45990),          # MRP above price: the MRP
    ("", 18999, 18999),             # only a selling price
    (37790, "", 37790),             # only an MRP (haier.com, MOP not used)
    (10000, 12000, 12000),          # a bogus MRP below the price: the higher
    (999.5, "", 999.5),
])
def test_single_price_pair_is_the_higher(mrp, sale, want):
    assert app.single_price_pair(mrp, sale) == (want, "")


def test_single_price_pair_with_no_price():
    assert app.single_price_pair("", "") == ("", "")
    assert app.single_price_pair(None, 0) == ("", "")


def test_row_context_single_price():
    with pricing(True):
        ctx = app.row_context(row())
        assert (ctx["regular price"], ctx["sale price"]) == (45990, "")
        # no MRP is invented in this mode, even when asked
        ctx = app.row_context(row(**{"regular price": ""}), estimate_mrp=True)
        assert (ctx["regular price"], ctx["sale price"]) == (28990, "")


def test_row_context_two_prices_unchanged():
    with pricing(False):
        ctx = app.row_context(row())
        assert (ctx["regular price"], ctx["sale price"]) == (45990.0, 28990.0)


def test_seo_description_quotes_one_price_and_no_saving():
    with pricing(True):
        text = app.seo_description("Foxsky 43 Full HD TV", "Foxsky", "LED",
                                   [], 45990, 28990)
        assert "Price Rs 45,990." in text
        for word in ("MRP", "save", "Save", "% off", "28,990"):
            assert word not in text
    with pricing(False):
        text = app.seo_description("Foxsky 43 Full HD TV", "Foxsky", "LED",
                                   [], 45990, 28990)
        assert "Price Rs 28,990 (MRP Rs 45,990)." in text


def test_build_output_single_price(monkeypatch):
    # no image download: the row has no image URLs
    monkeypatch.setattr(app, "download_urls_for_sku",
                        lambda sku, urls, flags=None: [])
    df = pd.DataFrame([row()])
    with pricing(True):
        out = app.process_inventory(df, media_base="https://x.test/m",
                                    seo=True)
    got = out.iloc[0] if hasattr(out, "iloc") else out[0]
    assert got["regular price"] == 45990
    assert got["sale price"] is None or pd.isna(got["sale price"])
    # the price band follows the ONE price (45,990), not the old sale price
    assert "30000 to 50000" in got["Tags"]
    assert "20000 to 30000" not in got["Tags"]
    text = " ".join(str(v) for v in got.values)
    for word in ("% off", " save ", "Save Rs", "MRP Rs"):
        assert word not in text


# --------------------------------------------------------------- validator
def _export(rows):
    return V.validate_export(pd.DataFrame(rows), "single.csv",
                             check_images=False, write_issues=False)


def _csv_row(**over):
    base = {"sku": "TV-FOXSKY-43FSFHS",
            "title": "Foxsky 43 inch Full HD Smart LED TV",
            "categories": "LED", "description": "d", "Images": "",
            "regular price": 45990, "sale price": ""}
    base.update(over)
    return base


def test_validator_single_price_row_is_priced():
    with pricing(True):
        reasons = " ".join(i["reasons"] for i in _export([_csv_row()]))
        assert "missing_price" not in reasons
        assert "sale_price_in_single_price_mode" not in reasons


def test_validator_flags_a_sale_price_in_single_mode():
    with pricing(True):
        reasons = " ".join(i["reasons"] for i in _export(
            [_csv_row(**{"sale price": 28990})]))
        assert "sale_price_in_single_price_mode (28990)" in reasons


def test_validator_price_floor_reads_the_one_price():
    with pricing(True):
        reasons = " ".join(i["reasons"] for i in _export(
            [_csv_row(**{"regular price": 500})]))
        assert "price_too_low_for_category" in reasons
        reasons = " ".join(i["reasons"] for i in _export(
            [_csv_row(**{"regular price": ""})]))
        assert "missing_price" in reasons


def test_stage1_mrp_alone_is_a_price_in_single_mode(tmp_path):
    df = pd.DataFrame([{"sku": "HAIER-H32K82FX",
                        "title": "Haier H32K82FX 80cm (32) Google TV",
                        "status": "FOUND (haier.com) - no price shown",
                        "url_link": "https://www.haier.com/in/x.shtml",
                        "image_url": "a.jpg,b.jpg,c.jpg",
                        "regular price": 37790, "sale price": ""}])
    path = str(tmp_path / "x_filled.xlsx")
    with pricing(True):
        issues = V.validate_filled(df, path) or []
    text = " ".join(str(i) for i in issues)
    assert "verified_but_no_price" not in text
    with pricing(False):
        issues = V.validate_filled(df, path) or []
    assert "verified_but_no_price" in " ".join(str(i) for i in issues)


# -------------------------------------------------------------- wc_publish
def test_payload_single_price_clears_the_sale_price():
    cfg = dataclasses.replace(W.CLIENT,
                              pricing=client_config.PricingConfig(True))
    assert W.payload_prices({"regular price": 45990, "sale price": ""},
                            cfg) == ("45990", "")
    # a stale CSV with both values still ships ONE price, sale_price ""
    assert W.payload_prices({"regular price": 45990, "sale price": 28990},
                            cfg) == ("45990", "")
    assert W.payload_prices({"regular price": "", "sale price": 18999},
                            cfg) == ("18999", "")


def test_payload_two_prices_unchanged():
    cfg = dataclasses.replace(W.CLIENT,
                              pricing=client_config.PricingConfig(False))
    assert W.payload_prices({"regular price": 45990, "sale price": 28990},
                            cfg) == ("45990", "28990")
    assert W.payload_prices({"regular price": "", "sale price": 18999},
                            cfg) == ("18999", "")


def test_dry_run_names_the_sale_price_it_clears():
    payload = {"name": "X", "regular_price": "45990", "sale_price": "",
               "tags": [], "attributes": [], "images": [], "meta_data": []}
    text = W.describe(payload, {"name": "X", "regular_price": "45990",
                                "sale_price": "28990"})
    assert "sale_price 28990 -> -" in text
