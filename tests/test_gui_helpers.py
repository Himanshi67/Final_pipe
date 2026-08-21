"""Unit tests for gui_helpers.py — pure functions, synthetic frames only."""
import math

import pandas as pd

from gui_helpers import flag_value, sanity_issues


def _df(rows):
    base = {"sku": "S1", "regular price": math.nan,
            "sale price": math.nan, "url_link": ""}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_flag_value_present():
    assert flag_value(["--discover", "--min-price", "8000"], "--min-price") == 8000


def test_flag_value_absent_trailing_or_bad():
    assert flag_value([], "--min-price") == 0
    assert flag_value(["--min-price"], "--min-price") == 0
    assert flag_value(["--min-price", "cheap"], "--min-price") == 0


def test_sale_above_mrp_flagged():
    out = sanity_issues(_df([{"regular price": 100.0, "sale price": 150.0}]))
    assert len(out) == 1 and out[0]["sku"] == "S1"
    assert "above MRP" in out[0]["problem"]


def test_non_positive_prices_flagged():
    out = sanity_issues(_df([{"regular price": 0.0},
                             {"sku": "S2", "sale price": -5.0}]))
    assert {o["sku"] for o in out} == {"S1", "S2"}


def test_below_min_price_flagged_only_when_set():
    rows = [{"sale price": 4000.0}]
    assert sanity_issues(_df(rows)) == []                      # min_price off
    out = sanity_issues(_df(rows), min_price=8000)
    assert len(out) == 1 and "below batch min price" in out[0]["problem"]


def test_bad_url_flagged_empty_url_ignored():
    out = sanity_issues(_df([{"url_link": "www.amazon.in/dp/X"},
                             {"sku": "S2", "url_link": "https://ok.example"},
                             {"sku": "S3", "url_link": ""}]))
    assert len(out) == 1 and out[0]["sku"] == "S1"


def test_clean_row_no_issues():
    out = sanity_issues(_df([{"regular price": 22599.0, "sale price": 18999.0,
                              "url_link": "https://www.croma.com/x"}]),
                        min_price=8000)
    assert out == []


def test_nan_prices_are_not_issues():
    assert sanity_issues(_df([{}]), min_price=8000) == []
