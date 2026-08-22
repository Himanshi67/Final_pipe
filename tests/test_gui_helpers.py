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


from gui_helpers import filter_review_df


def _rdf():
    return pd.DataFrame([
        {"sku": "WM-IFB-01", "title": "IFB 8kg Front Load", "Brands": "IFB"},
        {"sku": "WM-LG-02", "title": "LG 6.5kg Top Load", "Brands": "LG"},
        {"sku": "AC-LG-03", "title": "LG 1.5 Ton Split AC", "Brands": "LG"},
    ])


def test_filter_noop_returns_all_rows():
    assert len(filter_review_df(_rdf())) == 3
    assert len(filter_review_df(_rdf(), "", [])) == 3


def test_filter_query_matches_sku_and_title_case_insensitive():
    assert list(filter_review_df(_rdf(), "ifb")["sku"]) == ["WM-IFB-01"]
    assert list(filter_review_df(_rdf(), "top load")["sku"]) == ["WM-LG-02"]


def test_filter_query_is_regex_safe():
    assert list(filter_review_df(_rdf(), "1.5 Ton")["sku"]) == ["AC-LG-03"]
    assert len(filter_review_df(_rdf(), "1x5 Ton")) == 0   # '.' not a wildcard


def test_filter_by_brand_and_combined():
    assert len(filter_review_df(_rdf(), brands=["LG"])) == 2
    assert list(filter_review_df(_rdf(), "AC", ["LG"])["sku"]) == ["AC-LG-03"]


def test_filter_preserves_original_index():
    out = filter_review_df(_rdf(), brands=["LG"])
    assert list(out.index) == [1, 2]


def test_filter_without_title_column():
    df = pd.DataFrame([{"sku": "X1", "Brands": "LG"}])
    assert len(filter_review_df(df, "x1")) == 1


from gui_helpers import batch_summary


def _result():
    return pd.DataFrame([
        {"sku": "A", "Images": "https://x/1.webp, https://x/2.webp",
         "_spec_gaps": "", "Brands": "LG", "sale price": "22,599",
         "categories": "Home Appliances > Washing Machines"},
        {"sku": "B", "Images": "", "_spec_gaps": "Capacity, RPM",
         "Brands": "", "sale price": "8,999",
         "categories": "Home Appliances > Washing Machines"},
        {"sku": "C", "Images": "https://x/3.webp", "_spec_gaps": "",
         "Brands": "IFB", "sale price": "", "categories": ""},
    ])


def test_batch_summary_counts():
    s = batch_summary(_result())
    assert s["products"] == 3
    assert s["zero_image"] == 1
    assert s["avg_images"] == 1.0          # (2 + 0 + 1) / 3
    assert s["spec_gap_rows"] == 1
    assert s["missing_brand"] == 1


def test_batch_summary_prices_parse_indian_format():
    s = batch_summary(_result())
    assert s["price_min"] == 8999
    assert s["price_max"] == 22599


def test_batch_summary_categories():
    s = batch_summary(_result())
    assert s["category_counts"] == {
        "Home Appliances > Washing Machines": 2, "(none)": 1}


def test_batch_summary_empty_frame():
    s = batch_summary(_result().iloc[0:0])
    assert s["products"] == 0 and s["price_min"] == 0 and s["avg_images"] == 0.0


from gui_helpers import (parse_image_urls, parse_specs_cell,
                         raw_sku_for_csv_sku)


def test_parse_image_urls_basic_and_blank():
    cell = "https://a/1.jpg, https://a/2.jpg,https://a/3.jpg"
    assert parse_image_urls(cell) == [
        "https://a/1.jpg", "https://a/2.jpg", "https://a/3.jpg"]
    assert parse_image_urls("") == []
    assert parse_image_urls(None) == []


def test_parse_image_urls_comma_inside_url_survives():
    cell = "https://a/im,g/1.jpg, https://b/2.jpg"
    assert parse_image_urls(cell) == ["https://a/im,g/1.jpg", "https://b/2.jpg"]


def test_parse_specs_cell_pairs_and_colon_in_value():
    cell = "Capacity: 8 kg | Ratio: 16:9 | Warranty: 2 years"
    assert parse_specs_cell(cell) == [("Capacity", "8 kg"), ("Ratio", "16:9"),
                                      ("Warranty", "2 years")]
    assert parse_specs_cell("") == []
    assert parse_specs_cell("no separator here") == []


def test_raw_sku_untouched_and_prefixed():
    assert raw_sku_for_csv_sku("WM-HW100", ["WM-HW100", "X200"]) == "WM-HW100"
    assert raw_sku_for_csv_sku("AP-X200", ["WM-HW100", "X200"]) == "X200"


def test_raw_sku_sanitized_spaces():
    assert raw_sku_for_csv_sku("AP-AB-100", ["AB 100"]) == "AB 100"


def test_raw_sku_longest_match_wins_and_none():
    assert raw_sku_for_csv_sku("AP-AB-100", ["100", "AB 100"]) == "AB 100"
    assert raw_sku_for_csv_sku("AP-ZZZ", ["AB 100"]) is None


import os


def test_write_edits_back_creates_missing_column(tmp_path):
    import openpyxl
    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    import gui
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["sku", "title"])
    ws.append(["S1", "Old"])
    f = tmp_path / "input_filled.xlsx"
    wb.save(f)
    orig = pd.DataFrame([{"sku": "S1", "title": "Old", "dropped_images": ""}])
    part = pd.DataFrame([{"sku": "S1", "title": "Old",
                          "dropped_images": "https://x/1.jpg"}])
    changed = gui.write_edits_back({"filled": f}, orig, [part])
    assert changed == 1
    ws2 = openpyxl.load_workbook(f).active
    hdrs = [c.value for c in ws2[1]]
    assert "dropped_images" in hdrs
    assert ws2.cell(row=2, column=hdrs.index("dropped_images") + 1
                    ).value == "https://x/1.jpg"


def test_app_filter_dropped_urls():
    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    import app
    urls = ["https://a/1.jpg", "https://a/2.jpg", "https://a/3.jpg"]
    assert app.filter_dropped_urls(urls, "https://a/2.jpg") == [
        "https://a/1.jpg", "https://a/3.jpg"]
    assert app.filter_dropped_urls(urls, " https://a/1.jpg , https://a/3.jpg"
                                   ) == ["https://a/2.jpg"]
    assert app.filter_dropped_urls(urls, "") == urls
    assert app.filter_dropped_urls(urls, None) == urls


from gui_helpers import classify, BADGE, parse_enricher_log, eta_text

SAMPLE_LOG = """
[1] Row 2: WM-HW100
      (cached search)
      (cached page, 214 KB)
    -> FOUND (croma.com)

[2] Row 3: X200
    -> FOUND (amazon.in) - no price shown

[3] Row 4: Y300
    [7 serper.dev call(s) this run so far - the free tier is 2,500/month]
    -> NOT FOUND - nothing matched

[4] Row 5: Z400
    -> NEEDS CRAWL (flipkart.com)
"""


def test_classify_moved_intact():
    assert classify("FOUND (croma.com)") == "found"
    assert classify("FOUND (x) - no price shown") == "review"
    assert classify("NEEDS CRAWL (x)") == "pending"
    assert classify("NEEDS CRAWL - CHECK LINK (x)") == "review"
    assert classify("NEEDS LINK - no url_link in this row") == "failed"
    assert classify("CLOSE MATCH - CHECK (x)") == "review"
    assert classify("SKIPPED - same unit as row 3") == "skipped"
    assert classify("") == "failed"
    assert set(BADGE) == {"found", "review", "failed", "skipped", "pending"}


def test_parse_enricher_log_counts():
    s = parse_enricher_log(SAMPLE_LOG)
    assert s["done"] == 4
    assert s["buckets"] == {"found": 1, "review": 1, "failed": 1,
                            "pending": 1}
    assert s["serper_calls"] == 7
    assert s["cached_searches"] == 1
    assert s["cached_pages"] == 1


def test_parse_enricher_log_empty():
    s = parse_enricher_log("")
    assert s == {"done": 0, "buckets": {}, "serper_calls": 0,
                 "cached_searches": 0, "cached_pages": 0}


def test_eta_text():
    # 10 of 40 done in 600 s -> 60 s/SKU -> 30 left -> 1800 s
    assert eta_text(10, 40, 1000.0, 1600.0) == "~30 min left"
    assert eta_text(39, 40, 1000.0, 1030.0) == "under a minute left"
    assert eta_text(0, 40, 1000.0, 1600.0) == ""      # nothing done yet
    assert eta_text(40, 40, 1000.0, 1600.0) == ""     # finished
    assert eta_text(10, 0, 1000.0, 1600.0) == ""      # no total known
    assert eta_text(10, 40, None, 1600.0) == ""       # no start time
