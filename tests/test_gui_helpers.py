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
                         raw_sku_for_csv_sku, _sanitized_sku)


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


def test_raw_sku_recoded_prefix_still_maps_back():
    """app.prefixed_sku re-codes a raw sku whose prefix contradicts the
    detected category (P1-13): 'AP-X200' filed under Kitchen Appliances is
    exported as 'KA-X200'. The review sheet still holds the raw form."""
    assert raw_sku_for_csv_sku("KA-X200", ["AP-X200"]) == "AP-X200"
    assert raw_sku_for_csv_sku("KA-X200", ["SA-X200"]) == "SA-X200"
    # the codes must not swallow an unrelated sku
    assert raw_sku_for_csv_sku("KA-X200", ["AP-X201"]) is None
    # and the plain suffix behaviour is unchanged
    assert raw_sku_for_csv_sku("AP-AB-100", ["AB 100"]) == "AB 100"


def test_raw_sku_rules_are_ranked_not_ored():
    """The code-stripped rule is a LAST resort. Flattening the three rules
    into one length-ranked match let it beat a true suffix match and made the
    answer depend on list order - both of which hand a review card, or the
    'Fix in review' jump, somebody else's product."""
    # 'X200' (Home Appliances -> 'HA-X200') and 'AC-X200' on one sheet: the
    # suffix owner wins, whichever order the sheet lists them in
    assert raw_sku_for_csv_sku("HA-X200", ["X200", "AC-X200"]) == "X200"
    assert raw_sku_for_csv_sku("HA-X200", ["AC-X200", "X200"]) == "X200"
    # an exact preimage beats a code-stripped guess, in either order
    assert raw_sku_for_csv_sku("KA-X200", ["AP-X200", "KA-X200"]) == "KA-X200"
    assert raw_sku_for_csv_sku("KA-X200", ["KA-X200", "AP-X200"]) == "KA-X200"


def test_two_coded_raws_over_one_stem_do_not_depend_on_order():
    """Refuted case: 'SA-X200' (Small Appliances, ships as itself) and
    'AP-X200' (Kitchen, re-coded to 'KA-X200') on one sheet. Both reach the
    code-stripped rank, so list order decided it - the 'SA-X200' card wore the
    AP row's validation reasons, and 'Fix in review' on 'KA-X200' jumped the
    operator to 'SA-X200'. The AP- fallback is the code prefixed_sku re-codes
    as a matter of course, so it wins, in either order."""
    assert raw_sku_for_csv_sku("KA-X200", ["SA-X200", "AP-X200"]) == "AP-X200"
    assert raw_sku_for_csv_sku("KA-X200", ["AP-X200", "SA-X200"]) == "AP-X200"
    # two SPECIFIC codes over one stem cannot be told apart from the raw list
    # alone; showing no flags beats showing another product's, in either order
    assert raw_sku_for_csv_sku("KA-X200", ["SA-X200", "EC-X200"]) is None
    assert raw_sku_for_csv_sku("KA-X200", ["EC-X200", "SA-X200"]) is None
    # the single-candidate cases the design pins are untouched
    assert raw_sku_for_csv_sku("KA-X200", ["AP-X200"]) == "AP-X200"
    assert raw_sku_for_csv_sku("KA-X200", ["SA-X200"]) == "SA-X200"
    # and a true suffix owner still outranks both coded raws, either order
    assert raw_sku_for_csv_sku("KA-X200", ["SA-X200", "X200"]) == "X200"
    assert raw_sku_for_csv_sku("KA-X200", ["X200", "SA-X200"]) == "X200"


def test_sanitized_sku_treats_nan_like_app_does():
    """app.compact() reads a NaN cell as empty; str(nan) would have sanitized
    to the literal 'nan' and back-mapped a junk row onto a real one."""
    assert _sanitized_sku(float("nan")) == ""
    assert _sanitized_sku(None) == ""
    assert raw_sku_for_csv_sku("AP-NAN", [float("nan")]) is None


def test_sku_codes_do_not_drift_from_app():
    """gui_helpers deliberately does NOT import app (that would boot Streamlit
    and read the taxonomy workbook in every helper test), so the code list is
    a copy. Import app HERE, inside the one test that has to compare them, so
    the drift is still caught."""
    import app                                   # noqa: E402  (lazy on purpose)
    import gui_helpers
    assert tuple(gui_helpers.SKU_CODES) == tuple(app.SKU_PREFIXES)


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
                 "cached_searches": 0, "cached_pages": 0,
                 "stopped_by_budget": False}


def test_eta_text():
    # 10 of 40 done in 600 s -> 60 s/SKU -> 30 left -> 1800 s
    assert eta_text(10, 40, 1000.0, 1600.0) == "~30 min left"
    assert eta_text(39, 40, 1000.0, 1030.0) == "under a minute left"
    assert eta_text(0, 40, 1000.0, 1600.0) == ""      # nothing done yet
    assert eta_text(40, 40, 1000.0, 1600.0) == ""     # finished
    assert eta_text(10, 0, 1000.0, 1600.0) == ""      # no total known
    assert eta_text(10, 40, None, 1600.0) == ""       # no start time


from gui_helpers import parse_publish_output

DRY_OUT = """DRY RUN: 2 product(s) from wordpress_ready.csv -> https://s.in  (images: url, status: draft for new, unchanged for existing)
  CREATE WM-HW100
    would send 14 fields, 3 image(s)
    ~ would create brand: Haier
  UPDATE AP-X200
    would send 12 fields, 2 image(s)

Dry run complete - nothing was written. Re-run with --commit to apply.
"""

COMMIT_OUT = """PUBLISHING: 2 product(s) from wordpress_ready.csv -> https://s.in  (images: url, status: draft for new, unchanged for existing)
  CREATE WM-HW100
    sending 14 fields, 3 image(s)
    -> id 4211  https://shreejeeelectronics.in/product/haier-hw100
  UPDATE AP-X200
    sending 12 fields, 2 image(s)
    !! HTTP 500 from media endpoint

Done: 1 created, 0 updated, 1 failed.
"""


def test_parse_publish_dry_run():
    rows, totals = parse_publish_output(DRY_OUT)
    assert [(r["action"], r["sku"], r["result"]) for r in rows] == [
        ("create", "WM-HW100", ""), ("update", "AP-X200", "")]
    assert totals == {}


def test_parse_publish_commit():
    rows, totals = parse_publish_output(COMMIT_OUT)
    assert rows[0]["result"] == "ok"
    assert rows[0]["link"] == \
        "https://shreejeeelectronics.in/product/haier-hw100"
    assert rows[1]["result"] == "failed"
    assert rows[1]["error"] == "HTTP 500 from media endpoint"
    assert totals == {"created": 1, "updated": 0, "failed": 1}


def test_parse_publish_ignores_taxonomy_and_noise():
    rows, _ = parse_publish_output(DRY_OUT)
    assert all(r["sku"] not in ("brand:", "Haier") for r in rows)
    assert parse_publish_output("") == ([], {})


from gui_helpers import image_basenames_from_csv, run_stage, sheet_combos


def test_run_stage_ladder():
    f0 = {"input": False, "filled": False, "csv": False}
    assert run_stage({}, f0) == "created"
    assert run_stage({}, {**f0, "input": True}) == "uploaded"
    assert run_stage({}, {**f0, "input": True, "filled": True}) == "enriched"
    assert run_stage({}, {"input": True, "filled": True, "csv": True}) \
        == "built"
    assert run_stage({"validated": True},
                     {"input": True, "filled": True, "csv": True}) \
        == "validated"
    assert run_stage({"validated": True, "published": "2026-08-22T10:00:00"},
                     {"input": True, "filled": True, "csv": True}) \
        == "published"


def test_image_basenames_from_csv():
    df = pd.DataFrame([
        {"Images": "https://s.in/up/2026/08/WM-HW100_1.webp, "
                   "https://s.in/up/2026/08/WM-HW100_2.webp"},
        {"Images": ""},
        {"Images": "https://s.in/up/2026/08/AP-X200_1.webp"},
    ])
    assert image_basenames_from_csv(df) == [
        "WM-HW100_1.webp", "WM-HW100_2.webp", "AP-X200_1.webp"]
    assert image_basenames_from_csv(pd.DataFrame([{"sku": "x"}])) == []


def test_sheet_combos_split_and_header_skip():
    df = pd.DataFrame([
        ["Parent Category", "Sub Categories"],
        ["Home Appliances", "Washing Machines, Refrigerators"],
        # P2-29: ';' is NOT a separator to app.load_category_map, so this
        # cell holds one sub-category whose name contains a semicolon
        ["Kitchen", "Mixer Grinders; Kettles"],
        ["", "orphan sub"],
    ])
    assert sheet_combos(df) == {
        "Home Appliances > Washing Machines",
        "Home Appliances > Refrigerators",
        "Kitchen > Mixer Grinders; Kettles"}


from gui_helpers import (card_queue_index, found_status,
                         merge_confirm_flags, select_build_rows)


def test_found_status_true_for_every_found_form():
    for s in ("FOUND", "FOUND (amazon.in)",
              "FOUND (amazon.in) - no price shown",
              "FOUND (x) - VERIFY: model differs", "found (x)"):
        assert found_status(s) is True, s


def test_found_status_false_for_everything_else():
    for s in ("NEEDS CRAWL (amazon.in)", "NEEDS CRAWL - CHECK LINK (x)",
              "NEEDS LINK - no usable search results",
              "CLOSE MATCH - CHECK (x)", "CHECK MANUALLY (x)",
              "NOT FOUND - 3 page(s) checked, none matched",
              "ERROR: no product data in the page",
              "SKIPPED - same unit as row 4", "", None):
        assert found_status(s) is False, s


def _orig(rows):
    """review frame as loaded: sku / confirmed (bool) / status."""
    base = {"confirmed": False, "status": "FOUND (amazon.in)"}
    return pd.DataFrame([{**base, **r} for r in rows])


def _part(rows):
    return pd.DataFrame([{"sku": r[0], "confirmed": r[1]} for r in rows])


def test_merge_tick_in_all_survives_untouched_bucket_copy():
    orig = _orig([{"sku": "A"}, {"sku": "B"}])
    all_tab = _part([("A", True), ("B", False)])
    bucket = _part([("A", False)])            # untouched copy, processed last
    flags, rejected = merge_confirm_flags(set(), orig, [all_tab, bucket])
    assert flags["A"] is True and rejected == []


def test_merge_untick_in_all_survives_untouched_bucket_copy():
    orig = _orig([{"sku": "A", "confirmed": True}])
    all_tab = _part([("A", False)])
    bucket = _part([("A", True)])             # still shows the old ✔
    flags, _ = merge_confirm_flags({"A"}, orig, [all_tab, bucket])
    assert flags["A"] is False


def test_merge_later_part_wins_when_both_changed():
    orig = _orig([{"sku": "A"}])
    flags, _ = merge_confirm_flags(set(), orig,
                                   [_part([("A", True)]), _part([("A", True)])])
    assert flags["A"] is True
    orig2 = _orig([{"sku": "A", "confirmed": True}])
    # All ticks it back on, the bucket tab (later) unticks it -> bucket wins
    flags2, _ = merge_confirm_flags({"A"}, orig2,
                                    [_part([("A", True)]), _part([("A", False)])])
    assert flags2["A"] is False


def test_merge_hidden_confirmed_sku_keeps_its_tick():
    orig = _orig([{"sku": "A", "confirmed": True}, {"sku": "B"}])
    flags, _ = merge_confirm_flags({"A"}, orig, [_part([("B", False)])])
    assert flags["A"] is True


def test_merge_rejects_tick_on_non_found_row():
    orig = _orig([{"sku": "A", "status": "NEEDS CRAWL (amazon.in)"},
                  {"sku": "B"}])
    flags, rejected = merge_confirm_flags(
        set(), orig, [_part([("A", True), ("B", True)])])
    assert flags == {"A": False, "B": True}
    assert rejected == ["A"]


def test_merge_does_not_mutate_inputs():
    orig = _orig([{"sku": "A"}])
    part = _part([("A", True)])
    existing = {"B"}
    before = (orig.copy(deep=True), part.copy(deep=True), set(existing))
    merge_confirm_flags(existing, orig, [part])
    pd.testing.assert_frame_equal(orig, before[0])
    pd.testing.assert_frame_equal(part, before[1])
    assert existing == before[2]


def test_merge_ignores_sku_unknown_to_original():
    orig = _orig([{"sku": "A"}])
    flags, rejected = merge_confirm_flags(set(), orig, [_part([("Z", True)])])
    assert flags == {} and rejected == []


def _build_df():
    return pd.DataFrame([
        {"sku": "A", "status": "FOUND (amazon.in)"},
        {"sku": "B", "status": "FOUND (x) - no price shown"},
        {"sku": "C", "status": "NEEDS CRAWL (flipkart.com)"},
        {"sku": "D", "status": "NOT FOUND - nothing matched"},
        {"sku": "E", "status": "CLOSE MATCH - CHECK (croma.com)"},
    ])


def test_select_confirmed_scope_drops_non_found_and_reports_them():
    kept, skipped = select_build_rows(_build_df(), "confirmed",
                                      {"A", "B", "C", "D", "E"})
    assert list(kept["sku"]) == ["A", "B"]
    assert skipped == ["C", "D", "E"]


def test_select_confirmed_scope_drops_unconfirmed_found():
    kept, skipped = select_build_rows(_build_df(), "confirmed", {"A"})
    assert list(kept["sku"]) == ["A"] and skipped == []


def test_select_all_found_scope_keeps_every_found_row():
    kept, skipped = select_build_rows(_build_df(), "all_found", set())
    assert list(kept["sku"]) == ["A", "B"] and skipped == []


def test_select_without_status_column():
    df = pd.DataFrame([{"sku": "A"}, {"sku": "B"}])
    kept, skipped = select_build_rows(df, "all_found", set())
    assert list(kept["sku"]) == ["A", "B"] and skipped == []
    kept, skipped = select_build_rows(df, "confirmed", {"B"})
    assert list(kept["sku"]) == ["B"] and skipped == []


def test_select_returns_a_copy():
    df = _build_df()
    kept, _ = select_build_rows(df, "all_found", set())
    kept.loc[kept.index[0], "sku"] = "CHANGED"
    assert list(df["sku"]) == ["A", "B", "C", "D", "E"]


def test_merge_is_stable_across_reloads_with_no_new_edits():
    """Save must not make the ✔ oscillate. A genuine two-tab disagreement is
    settled once (the bucket tab wins); after that gui.py rotates the editor
    keys, so every editor reloads the saved value and the next merges see no
    change at all - the flag stays put with no further operator input (P1-8:
    sticky edit state used to flip it False/True on every Save press)."""
    orig = _orig([{"sku": "A", "confirmed": True}])
    flags, _ = merge_confirm_flags(
        {"A"}, orig, [_part([("A", True)]), _part([("A", False)])])
    assert flags["A"] is False
    value = False
    for _ in range(3):                       # three more Save presses
        existing = {k for k, ok in flags.items() if ok}
        orig = _orig([{"sku": "A", "confirmed": value}])
        reloaded = [_part([("A", value)]), _part([("A", value)])]
        flags, _ = merge_confirm_flags(existing, orig, reloaded)
        value = bool(flags.get("A", False))
        assert value is False

    # and the same in the other direction: a tick made in the All tab only
    orig = _orig([{"sku": "A"}])
    flags, _ = merge_confirm_flags(
        set(), orig, [_part([("A", True)]), _part([("A", False)])])
    assert flags["A"] is True
    for _ in range(3):
        existing = {k for k, ok in flags.items() if ok}
        orig = _orig([{"sku": "A", "confirmed": True}])
        reloaded = [_part([("A", True)]), _part([("A", True)])]
        flags, _ = merge_confirm_flags(existing, orig, reloaded)
        assert flags["A"] is True


def test_card_index_clamps_and_floors():
    assert card_queue_index([], 3) == 0
    assert card_queue_index([False, False, False], 99) == 2
    assert card_queue_index([False, False, False], -4) == 0
    assert card_queue_index([False, False, False], 1) == 1


def test_card_index_snaps_off_the_confirmed_tail():
    # approving the LAST unconfirmed card leaves it at position i while
    # A, B, C are still unconfirmed - never strand the operator there (P1-9)
    assert card_queue_index([False, False, False, True], 3) == 0
    # a longer confirmed tail (mixed buckets) behaves the same
    assert card_queue_index([False, False, True, True], 3) == 0
    assert card_queue_index([False, False, True, True], 2) == 0


def test_card_index_stays_put_when_the_card_is_unconfirmed():
    # the happy path: the approved card left position 0, the next one slid in
    assert card_queue_index([False, False, False, True], 0) == 0


def test_card_index_rests_on_a_confirmed_card_once_nothing_is_left():
    assert card_queue_index([True, True, True], 1) == 1
    assert card_queue_index([True, True, True], 9) == 2


# --------------------------------------------------------- P2-27 / P2-34 --

from gui_helpers import (CATEGORY_COMMA_MERGES, eligible_rows,  # noqa: E402
                         split_subcategories)

# verbatim sku_enricher.py end-of-run line (log(f"serper.dev calls this run:
# {RUN_STATS['serper_calls']} " f"(~1 credit each).")) - a 3-SKU smoke run
# never prints the bracketed tally at all, so this is the ONLY evidence.
SHORT_RUN_LOG = """[1] Row 2: WM-A
    -> FOUND (croma.com)
[2] Row 3: WM-B
    -> FOUND (amazon.in)
[3] Row 4: WM-C
    -> NOT FOUND - no usable search results
Done. 3 rows written.
serper.dev calls this run: 13 (~1 credit each).
"""


def test_serper_total_read_from_the_end_of_run_line():
    assert parse_enricher_log(SHORT_RUN_LOG)["serper_calls"] == 13


def test_serper_final_line_beats_a_stale_bracketed_tally():
    log = ("    [20 serper.dev call(s) this run so far - the free tier is "
           "2,500/month]\n"
           "serper.dev calls this run: 31 (~1 credit each).\n")
    assert parse_enricher_log(log)["serper_calls"] == 31


def test_serper_tally_still_wins_while_the_run_is_mid_flight():
    # killed before the summary line: the tally is all there is
    log = "    [7 serper.dev call(s) this run so far - blah]\n"
    assert parse_enricher_log(log)["serper_calls"] == 7


def test_publish_totals_accumulate_across_chunks():
    # gui.run_publish_pinned chunks at 150 SKUs and joins the outputs, so a
    # 300-SKU commit ends with two 'Done:' lines (P2-34)
    out = ("  CREATE AP-1\n    -> id 11  https://x/1\n"
           "Done: 148 created, 0 updated, 2 failed.\n"
           "  CREATE AP-2\n    -> id 12  https://x/2\n"
           "Done: 150 created, 1 updated, 0 failed.\n")
    rows, totals = parse_publish_output(out)
    assert totals == {"created": 298, "updated": 1, "failed": 2}
    assert [r["sku"] for r in rows] == ["AP-1", "AP-2"]


def test_publish_totals_single_chunk_unchanged():
    _, totals = parse_publish_output("Done: 5 created, 2 updated, 1 failed.\n")
    assert totals == {"created": 5, "updated": 2, "failed": 1}


# ------------------------------------------------------------------ P2-29 --

def test_split_subcategories_merges_the_comma_named_category():
    cell = ("Mixer Grinders, Juicers, Mixers & Grinders, Kettles, "
            "Induction Cooktops")
    assert split_subcategories(cell) == [
        "Mixer Grinders", "Juicers, Mixers & Grinders", "Kettles",
        "Induction Cooktops"]


def test_sheet_combos_keeps_the_juicers_row_whole():
    df = pd.DataFrame([
        ["Kitchen Appliances",
         "Mixer Grinders, Juicers, Mixers & Grinders, Kettles"],
    ])
    combos = sheet_combos(df)
    assert "Kitchen Appliances > Juicers, Mixers & Grinders" in combos
    assert "Kitchen Appliances > Juicers" not in combos
    assert "Kitchen Appliances > Mixers & Grinders" not in combos
    assert len(combos) == 3


def test_category_split_does_not_drift_from_app():
    """Same deal as the SKU_CODES guard: gui_helpers copies app's splitter
    (importing app boots Streamlit), so import app HERE and pin the two."""
    import app                                   # noqa: E402  (lazy on purpose)
    import gui_helpers
    assert (tuple(gui_helpers.CATEGORY_COMMA_MERGES)
            == tuple(app.CATEGORY_COMMA_MERGES))
    samples = [
        "Mixer Grinders, Juicers, Mixers & Grinders, Kettles",
        "Washing Machines, Refrigerators",
        "Mixer Grinders; Kettles",
        "Juicers, Mixers & Grinders",
        "",
    ]
    for cell in samples:
        assert (gui_helpers.split_subcategories(cell)
                == app._split_subcategories(cell)), cell


# ------------------------------------------------------------------ P2-26 --

def test_raw_sku_with_a_backslash_maps_back():
    # 'WA70A4022FS\TL' style raws: app.prefixed_sku's whitelist turns the
    # backslash into '-', and _SKU_SANITIZE must do the same
    assert raw_sku_for_csv_sku("WM-A-B", ["A\\B"]) == "A\\B"


# ------------------------------------------------------------------ P2-28 --

ELIGIBLE_ROWS = [
    # sku, url_link, status, sale price, image_url
    ("S1", "", "", "", ""),                                    # untouched
    ("S2", "https://x/2", "FOUND (amazon.in)", "1999", "https://i/2.jpg"),
    ("S3", "https://x/3", "FOUND (croma.com) - no price shown", "",
     "https://i/3.jpg"),                                       # incomplete
    ("S4", "https://x/4", "FOUND (flipkart)", "2999", ""),      # no image
    ("S5", "", "NOT FOUND - no usable search results", "", ""),  # failed
    ("S6", "", "ERROR: all sources blocked (amazon.in)", "", ""),  # blocked
    ("S7", "", "NEEDS LINK - nothing matched", "", ""),
    ("S8", "https://x/8", "NEEDS CRAWL (amazon.in)", "", "https://i/8.jpg"),
]


def _eligible_df():
    return pd.DataFrame(
        [dict(zip(["sku", "url_link", "status", "sale price", "image_url"],
                  r)) for r in ELIGIBLE_ROWS])


def test_eligible_plain_run_takes_fresh_and_blocked_rows_only():
    # S1 (untouched) + S6 (blocked, transient) - S5/S7 were tried and failed
    assert eligible_rows(_eligible_df(), []) == 2


def test_eligible_crawl_urls_counts_linked_non_found_rows():
    # S8 only: S2/S3/S4 already start with FOUND, the rest have no link
    assert eligible_rows(_eligible_df(), ["--crawl-urls"]) == 1


def test_eligible_retry_incomplete_counts_half_filled_rows():
    # every linked row that is short of something - S3 (' - ' suffix, no
    # price), S4 (no image_url), S8 (linked but never crawled) - plus the
    # rows a PLAIN run would have taken anyway (S1 untouched, S6 blocked).
    # S2 is complete and stays skipped.
    assert eligible_rows(_eligible_df(), ["--retry-incomplete"]) == 5


def test_eligible_retry_missing_counts_failed_unlinked_rows():
    # S1, S5, S6, S7 - every unlinked row, tried or not
    assert eligible_rows(_eligible_df(), ["--retry-missing"]) == 4


def test_eligible_discover_counts_unlinked_untried_rows():
    # S1 only: pass 1 skips anything with a link OR any status at all - the
    # blocked-row exemption lives in the plain branch, not this one
    assert eligible_rows(_eligible_df(), ["--discover"]) == 1


def test_eligible_force_and_limit():
    df = _eligible_df()
    # --force ignores every skip rule (bar crawl-urls' missing link)
    assert eligible_rows(df, ["--force"]) == len(ELIGIBLE_ROWS)
    assert eligible_rows(df, ["--force", "--crawl-urls"]) == 4
    # --limit caps it the way the enricher's `done >= args.limit` break does
    assert eligible_rows(df, ["--force", "--limit", "3"]) == 3
    assert eligible_rows(df, ["--limit", "9"]) == 2


def test_eligible_tolerates_a_sheet_read_as_strings():
    # pd.read_excel(..., dtype=str) gives NaN for every blank cell
    df = pd.DataFrame([{"sku": "S1", "url_link": float("nan"),
                        "status": float("nan"), "sale price": float("nan"),
                        "image_url": float("nan")},
                       {"sku": float("nan"), "url_link": float("nan"),
                        "status": float("nan"), "sale price": float("nan"),
                        "image_url": float("nan")}])
    assert eligible_rows(df, []) == 1


from gui_helpers import limit_from_flags                       # noqa: E402


def test_limit_from_flags_reads_the_cap_without_a_sheet():
    # the whole point: knowable from argv alone, so gui.eligible_total can
    # apply it on a first launch when <stem>_filled.xlsx does not exist yet
    assert limit_from_flags(["--limit", "5"]) == 5
    assert limit_from_flags(["--discover", "--limit", "3", "--force"]) == 3


def test_limit_from_flags_is_none_when_there_is_no_usable_cap():
    for flags in ([], None, ["--force"], ["--limit"], ["--limit", "all"],
                  ["--limit", "0"], ["--limit", "-2"]):
        assert limit_from_flags(flags) is None, flags


from gui_helpers import csv_signature                          # noqa: E402


def test_csv_signature_is_none_when_the_file_is_missing(tmp_path):
    assert csv_signature(tmp_path / "wordpress_ready.csv") is None
    # a directory is not a readable CSV either, but it must not raise
    assert csv_signature(tmp_path) is not None                 # stat works
    assert csv_signature(tmp_path / "a" / "b.csv") is None     # missing parent


def test_csv_signature_is_stable_while_the_file_is_untouched(tmp_path):
    p = tmp_path / "wordpress_ready.csv"
    p.write_text("sku,title\nWM-A,one\n", encoding="utf-8")
    first = csv_signature(p)
    assert first and csv_signature(p) == first


def test_csv_signature_changes_when_the_csv_is_rebuilt(tmp_path):
    """The gate this backs: Step 3 rebuilding with a wider scope must make
    the signature stored at dry-run time stop matching."""
    import os as _os

    tick = [0]

    def _write(text):
        """Write, then stamp mtime a whole second past the previous write.

        Three back-to-back writes can land inside ONE filesystem mtime tick
        on Windows, and the third rewrite below is deliberately the same
        LENGTH as the first - so without this the signature could legitimately
        repeat and the test flapped. The stamps are absolute and increasing,
        not 'current + 1s', so no two writes can collide however fast the box
        is. The gap is real and documented in csv_signature(); it just isn't
        what this test is about."""
        p.write_text(text, encoding="utf-8")
        tick[0] += 1
        stamp = (1_700_000_000 + tick[0]) * 10**9
        _os.utime(p, ns=(stamp, stamp))

    p = tmp_path / "wordpress_ready.csv"
    _write("sku,title\nWM-A,one\nRF-B,two\n")
    two_rows = csv_signature(p)
    _write("sku,title\nWM-A,one\nRF-B,two\nAC-C,three\n")
    assert csv_signature(p) != two_rows
    # and back to the same row count but edited content: same SIZE as the
    # first write, so only mtime can separate them - which is the point
    _write("sku,title\nWM-A,ONE\nRF-B,two\n")
    assert csv_signature(p) != two_rows


def test_csv_signature_pairs_mtime_and_size(tmp_path):
    """Both halves are present and load-bearing. A same-length rewrite is
    caught by mtime_ns alone, which on Windows can reuse a tick for two
    back-to-back writes - so this asserts the FORM (mtime:size) rather than
    racing the clock; the real gap is documented in csv_signature."""
    p = tmp_path / "wordpress_ready.csv"
    p.write_text("sku,title\nWM-A,one\n", encoding="utf-8")
    mtime, _, size = csv_signature(p).partition(":")
    import os
    stat = os.stat(p)
    assert mtime == str(stat.st_mtime_ns) and size == str(stat.st_size)


# ------------------------------------------------------------------ P3-53 --

from gui_helpers import (confirmed_from_state,                 # noqa: E402
                         row_fingerprint, stale_confirmations)

FP_ROW = {"sku": "WM-A", "title": "LG 7 kg Washer", "regular price": "25,000",
          "sale price": 22599, "url_link": "https://x.in/a",
          "image_url": "https://x.in/a1.jpg,https://x.in/a2.jpg",
          "status": "FOUND (amazon) - no price shown"}


def test_row_fingerprint_is_stable_and_ignores_other_columns():
    base = row_fingerprint(FP_ROW)
    assert base == row_fingerprint(dict(FP_ROW))
    # status/categories/specs are NOT part of the identity: --retry-incomplete
    # flips the status suffix on a row it did not otherwise change
    assert row_fingerprint({**FP_ROW, "status": "FOUND (amazon)",
                            "specs": "Type: Front Load"}) == base
    # both readers of the sheet must agree: dtype=str + to_numeric (step 2)
    # and plain read_excel (step 3) render the same cell differently
    assert row_fingerprint({**FP_ROW, "regular price": 25000.0,
                            "sale price": "22,599"}) == base
    # a missing column counts as blank, not as a KeyError
    thin = {k: v for k, v in FP_ROW.items() if k != "image_url"}
    assert row_fingerprint(thin) == row_fingerprint({**FP_ROW,
                                                     "image_url": ""})


def test_row_fingerprint_changes_on_every_watched_field():
    base = row_fingerprint(FP_ROW)
    for col, new in (("title", "LG 7 kg Washer FHM1207"),
                     ("regular price", 26000),
                     ("sale price", ""),
                     ("url_link", "https://x.in/b"),
                     ("image_url", "https://x.in/a1.jpg")):
        assert row_fingerprint({**FP_ROW, col: new}) != base, col


def test_row_fingerprint_treats_blanks_alike():
    blank = row_fingerprint({"sku": "X"})
    assert blank == row_fingerprint({"sku": "X", "title": float("nan"),
                                     "regular price": None,
                                     "sale price": "  ", "url_link": "",
                                     "image_url": float("nan")})


def test_confirmed_from_state_accepts_both_formats():
    assert confirmed_from_state({}) == set()
    assert confirmed_from_state({"confirmed": {"A": True, "B": False}}) \
        == {"A"}
    assert confirmed_from_state({"confirmed": {
        "A": {"confirmed": True, "fp": "abc"},
        "B": {"confirmed": False, "fp": "def"},
        "C": True}}) == {"A", "C"}


def test_stale_confirmations_spots_a_re_enriched_row():
    df = pd.DataFrame([FP_ROW, {**FP_ROW, "sku": "WM-B"}])
    fp = row_fingerprint(FP_ROW)
    state = {"confirmed": {"WM-A": {"confirmed": True, "fp": fp},
                           "WM-B": {"confirmed": True, "fp": fp}}}
    assert stale_confirmations(state, df) == set()
    # --retry-incomplete rewrote WM-A's price and gallery
    df2 = pd.DataFrame([{**FP_ROW, "sale price": 19999,
                         "image_url": "https://x.in/new1.jpg"},
                        {**FP_ROW, "sku": "WM-B"}])
    assert stale_confirmations(state, df2) == {"WM-A"}


def test_stale_confirmations_never_touches_legacy_or_missing_rows():
    df = pd.DataFrame([{**FP_ROW, "sale price": 1}])
    state = {"confirmed": {
        "WM-A": True,                              # legacy bool: no fp
        "WM-B": {"confirmed": True, "fp": None},   # saved without a frame
        "WM-C": {"confirmed": True, "fp": "deadbeef"},   # not on the sheet
        "WM-D": {"confirmed": False, "fp": "deadbeef"}}}  # not confirmed
    assert stale_confirmations(state, df) == set()
    assert stale_confirmations(state, None) == set()
    assert stale_confirmations(state, pd.DataFrame([{"x": 1}])) == set()


# ------------------------------------------------------------------ P3-54 --

def test_run_stage_demotes_a_publish_from_an_earlier_build():
    """A rebuild rewrites wordpress_ready.csv but leaves published/
    published_live behind, so the Home table kept calling the new CSV live."""
    files = {"input": True, "filled": True, "csv": True}
    meta = {"validated": True, "published": "2026-08-22T10:00:00",
            "published_csv": "111:222"}
    assert run_stage(meta, files, "111:222") == "published"
    assert run_stage(meta, files, "999:888") == "validated"
    assert run_stage({k: v for k, v in meta.items() if k != "validated"},
                     files, "999:888") == "built"
    # legacy meta (no published_csv) and callers that pass no signature keep
    # the old answer - never demote real history on missing data
    assert run_stage({"validated": True, "published": "2026-08-22T10:00:00"},
                     files, "999:888") == "published"
    assert run_stage(meta, files) == "published"


# ----------------------------------------- Phase 1 item 5: review metrics --

from gui_helpers import (append_events, clock_elapsed,  # noqa: E402
                         review_caption, review_event, review_metrics)


def _sheet(rows):
    """A minimal review sheet: sku + status is all review_metrics reads."""
    return pd.DataFrame([{"sku": s, "status": st} for s, st in rows])


def test_review_event_shape():
    e = review_event("WM-A", "approve", "card", 12.44, at="2026-09-02T10:00")
    assert e == {"sku": "WM-A", "action": "approve", "mode": "card",
                 "at": "2026-09-02T10:00", "secs": 12.4}
    # a stamp is generated when none is passed, and junk/negative time is 0
    live = review_event(1, "skip", "table", None)
    assert live["sku"] == "1" and live["secs"] == 0.0 and live["at"]
    assert review_event("A", "edit", "table", -9)["secs"] == 0.0
    assert review_event("A", "edit", "table", "nope")["secs"] == 0.0


def test_clock_elapsed_caps_an_idle_gap():
    st = {"clock": 1000.0}
    assert clock_elapsed(st, "clock", 1012.5) == 12.5
    assert clock_elapsed(st, "clock", 1000.0 + 4000) == 600.0     # capped
    assert clock_elapsed(st, "clock", 1000.0 + 4000, cap=60) == 60.0
    # no stamp, junk stamp, or a clock that went backwards: never negative
    assert clock_elapsed({}, "clock", 1000.0) == 0.0
    assert clock_elapsed({"clock": "soon"}, "clock", 1000.0) == 0.0
    assert clock_elapsed(st, "clock", 900.0) == 0.0


def test_append_events_creates_and_grows_the_list():
    state = {"confirmed": {"WM-A": {"confirmed": True, "fp": "x"}}}
    append_events(state, [review_event("WM-A", "approve", "table", 5)])
    assert [e["action"] for e in state["events"]] == ["approve"]
    append_events(state, [review_event("WM-B", "skip", "card", 1)])
    assert len(state["events"]) == 2
    # the confirmed map is untouched, so the file format stays readable
    assert state["confirmed"] == {"WM-A": {"confirmed": True, "fp": "x"}}
    assert append_events({"events": "junk"}, [])["events"] == []


def test_review_metrics_counts_time_rows_and_actions():
    state = {"events": [
        review_event("WM-A", "approve", "table", 30),
        review_event("WM-A", "edit", "table", 30),
        review_event("WM-B", "url_pasted", "table", 60),
        review_event("WM-C", "skip", "card", 60)]}
    df = _sheet([("WM-A", "FOUND (amazon)"),
                 ("WM-B", "NEEDS LINK - nothing found"),
                 ("WM-C", "FOUND (croma)"),
                 ("WM-D", "FOUND (flipkart)")])
    m = review_metrics(state, df)
    assert m["review_minutes"] == 3.0            # 180 s
    assert m["rows_touched"] == 3
    assert m["min_per_sku"] == 1.0
    assert m["actions"] == {"approve": 1, "edit": 1, "url_pasted": 1,
                            "skip": 1}
    # WM-A (edit) and WM-B (url_pasted + failed bucket) needed a human;
    # WM-C was only skipped and WM-D never looked at -> 2 of 4
    assert m["needs_human_pct"] == 50.0


def test_review_metrics_needs_human_includes_the_review_bucket():
    """A row nobody has touched still counts if the enricher left it in the
    review/failed bucket - that is work a human is going to have to do."""
    df = _sheet([("WM-A", "FOUND (amazon)"),
                 ("WM-B", "FOUND (flipkart) - only 1 image(s)"),
                 ("WM-C", "CLOSE MATCH - CHECK (croma)"),
                 ("WM-D", "NOT FOUND")])
    m = review_metrics({"events": []}, df)
    assert m["needs_human_pct"] == 75.0
    assert m["rows_touched"] == 0 and m["min_per_sku"] == 0.0


def test_review_metrics_on_a_legacy_state_returns_zeros():
    """review_state.json written before item 5 has no 'events' key at all."""
    m = review_metrics({"confirmed": {"WM-A": True}},
                       _sheet([("WM-A", "FOUND (amazon)")]))
    assert m == {"review_minutes": 0.0, "rows_touched": 0, "min_per_sku": 0.0,
                 "needs_human_pct": 0.0, "actions": {}}
    # and neither a missing state nor a missing sheet may raise
    assert review_metrics(None, None)["needs_human_pct"] == 0.0
    assert review_metrics({}, pd.DataFrame())["review_minutes"] == 0.0
    assert review_metrics({"events": [None, "junk", {}]},
                          None)["rows_touched"] == 0


def test_review_caption_wording_and_empty_case():
    m = review_metrics({"events": [review_event("WM-A", "edit", "card", 90)]},
                       _sheet([("WM-A", "FOUND (amazon)")]))
    assert review_caption(m) == \
        "Review so far: 1.5 min · 1.5 min/SKU · needs-human 100.0%"
    assert review_caption({}) == "" and review_caption(None) == ""


# --------------------------------------------- item 6: deliverable pack --
from gui_helpers import (assemble_pack, coverage_summary,  # noqa: E402
                         coverage_total, render_batch_readme)

# every bucket coverage_summary knows, once each (plus a second 'only N
# image(s)' so the digit normalisation has something to collapse)
COVERAGE_STATUSES = [
    ("A1", "FOUND (amazon.in)"),
    ("A2", "FOUND"),                                   # bare form
    ("B1", "FOUND (croma.com) - only 2 image(s)"),
    ("B2", "FOUND (reliancedigital.in) - only 5 image(s)"),
    ("B3", "FOUND (amazon.in) - price from flipkart.com"),
    ("C1", "CLOSE MATCH - CHECK (croma.com)"),
    ("D1", "CHECK MANUALLY (flipkart.com)"),
    ("E1", "NOT FOUND - 3 page(s) checked, none matched"),
    ("E2", "NOT FOUND - 7 page(s) checked, none matched"),
    ("E3", "NOT FOUND"),                               # no reason suffix
    ("F1", "ERROR: no product data in the page"),
    ("G1", "SKIPPED - same unit as row 4"),
    ("H1", "NEEDS CRAWL (amazon.in)"),
    ("H2", "NEEDS CRAWL - CHECK LINK (croma.com)"),
    ("I1", "NEEDS LINK - no usable search results"),
    ("J1", ""),                                        # never processed
    ("K1", "WHAT IS THIS"),                            # unknown shape
]


def _cov_sheet(rows=None):
    return pd.DataFrame([{"sku": s, "status": st}
                         for s, st in (rows or COVERAGE_STATUSES)])


def test_coverage_summary_buckets_every_status_shape():
    cov = coverage_summary(_cov_sheet())
    assert cov["rows"] == len(COVERAGE_STATUSES)
    assert cov["found_clean"] == 2                     # bare AND (site) form
    assert cov["close_match"] == 1 and cov["check_manually"] == 1
    assert cov["error"] == 1 and cov["skipped"] == 1
    assert cov["needs_crawl"] == 2 and cov["needs_link"] == 1
    assert cov["blank"] == 1 and cov["other"] == 1


def test_coverage_summary_normalises_digits_in_the_breakdowns():
    """'only 2 image(s)' and 'only 5 image(s)' are ONE reason, not two -
    otherwise the Coverage table grows a row per SKU."""
    cov = coverage_summary(_cov_sheet())
    assert cov["found_incomplete"] == {"only N image(s)": 2,
                                       "price from flipkart.com": 1}
    assert cov["not_found"] == {"N page(s) checked, none matched": 2,
                                "unspecified": 1}


def test_coverage_summary_counts_always_add_up_to_rows():
    """The promise the README's table relies on: no row is dropped or
    double-counted, whatever the status says."""
    for rows in (COVERAGE_STATUSES, [("X", "FOUND - a - b - c")],
                 [("Y", None)], [("Z", "  ")]):
        cov = coverage_summary(_cov_sheet(rows))
        assert coverage_total(cov) == cov["rows"] == len(rows), cov


def test_coverage_summary_survives_a_sheet_with_no_status_column():
    cov = coverage_summary(pd.DataFrame([{"sku": "A"}, {"sku": "B"}]))
    assert cov["rows"] == 2 and cov["blank"] == 2
    assert coverage_summary(None)["rows"] == 0


def test_render_batch_readme_has_every_section_and_the_old_lines():
    cov = coverage_summary(_cov_sheet())
    text = render_batch_readme(
        "batch-1",
        {"original_name": "LIST.xlsx", "total_rows": 17,
         "built_scope": "confirmed", "built_skus": ["A1", "A2"],
         "issue_count": 3, "published": "2026-08-30T09:00:00",
         "published_live": True, "published_stale": True,
         "archived_from": "runs\\batch-1",
         "files": ["wordpress_ready.csv", "images", "README.md"]},
        cov,
        review_metrics={"review_minutes": 12.0, "min_per_sku": 1.5,
                        "needs_human_pct": 40.0},
        images={"count": 6, "missing": 2})
    # the lines that were there before item 6 must survive verbatim
    assert text.startswith("# Batch batch-1\n\n")
    assert "from runs\\batch-1." in text
    assert "- Source list: LIST.xlsx\n" in text
    assert "- Rows enriched: 17\n" in text
    assert "- Built scope: confirmed (2 SKUs)\n" in text
    assert "- Validation issues: 3\n" in text
    assert "- Published: 2026-08-30T09:00:00 (LIVE)" in text
    assert "for a PREVIOUS build" in text
    # ... and the new ones
    assert "- Images: 6 file(s) copied, 2 referenced but MISSING" in text
    for section in ("## Coverage", "## Review", "## Files"):
        assert f"\n{section}\n" in text, section
    assert "| **Rows** | **17** |" in text
    assert "| FOUND (clean) | 2 |" in text
    assert "↳ only N image(s) | 2 |" in text          # indented sub-row
    assert "↳ N page(s) checked, none matched | 2 |" in text
    assert "Review so far: 12.0 min · 1.5 min/SKU · needs-human 40.0%" in text
    assert "- `wordpress_ready.csv` - the WooCommerce import file" in text


def test_render_batch_readme_without_a_run_or_review_or_images():
    text = render_batch_readme("out", {}, coverage_summary(_cov_sheet()),
                               source_name="LED.xlsx")
    assert "Packed " in text and "build_csv.py --pack" in text
    assert "- Source list: LED.xlsx\n" in text
    assert "- Rows enriched: ?\n" in text
    assert "- Published: never\n" in text              # no (LIVE), no note
    assert "- Images: 0 file(s) copied\n" in text      # no MISSING clause
    assert "no review metrics recorded" in text
    assert "- (nothing was copied)" in text


def test_render_batch_readme_takes_the_validation_count_from_the_caller():
    """build_csv --pack knows the issue count from the pass it just ran;
    there is no run_meta.json to read it out of."""
    text = render_batch_readme("out", {}, {"rows": 1},
                               validation={"issue_count": 0})
    assert "- Validation issues: 0\n" in text


def test_render_batch_readme_renders_notes_as_bullets():
    text = render_batch_readme("out", {}, {"rows": 0},
                               notes=["the CSV could not be read (ParserError)",
                                      "\n- pre-formatted note\n"])
    assert "- the CSV could not be read (ParserError)\n" in text
    assert "- pre-formatted note\n" in text


def test_assemble_pack_copies_files_images_and_counts_the_missing(tmp_path):
    src = tmp_path / "src"
    (src / "img").mkdir(parents=True)
    (src / "out.csv").write_text(
        'sku,Images\nA,"https://cdn.test/A_1.webp,https://cdn.test/A_2.webp"\n'
        "B,https://cdn.test/GONE_1.webp\n", encoding="utf-8")
    (src / "LIST.xlsx").write_bytes(b"xl")
    for name in ("A_1.webp", "A_2.webp"):              # GONE_1 is NOT there
        (src / "img" / name).write_bytes(b"RIFF----WEBPVP8 ")

    dest = tmp_path / "pack"
    res = assemble_pack(dest, {"out.csv": src / "out.csv",
                               "CLIENT LIST.xlsx": src / "LIST.xlsx",
                               "missing.md": src / "nope.md"},
                        src / "img", src / "out.csv",
                        allowed_hosts={"cdn.test"})
    assert res["files"] == 2                           # nope.md skipped
    assert res["images"] == 2 and res["missing"] == 1  # never raises
    assert res["copied"] == 4 and res["csv_error"] is None
    assert (dest / "CLIENT LIST.xlsx").exists()        # renamed on the way in
    assert sorted(p.name for p in (dest / "images").iterdir()) == [
        "A_1.webp", "A_2.webp"]
    assert (src / "out.csv").exists()                  # copies only


def test_assemble_pack_reports_an_unreadable_csv_and_skips_images(tmp_path):
    """P2-25 again, on the shared routine: a 0-byte CSV costs the images
    folder, not the pack."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "out.csv").write_bytes(b"")
    dest = tmp_path / "pack"
    res = assemble_pack(dest, {"out.csv": src / "out.csv"}, src, src / "out.csv")
    assert res["csv_error"] == "EmptyDataError"
    assert res["files"] == 1 and res["images"] == 0
    assert not (dest / "images").exists()


def test_assemble_pack_makes_no_empty_images_folder(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "out.csv").write_text("sku,Images\nA,\n", encoding="utf-8")
    dest = tmp_path / "pack"
    res = assemble_pack(dest, {"out.csv": src / "out.csv"}, src, src / "out.csv")
    assert res["images"] == 0 and not (dest / "images").exists()
    # and a pack with no CSV at all is a no-op for images, not a crash
    assert assemble_pack(tmp_path / "p2", {}, src, None)["copied"] == 0


# ------------------------------------------- per-client SKU codes (item 2) --

def test_configure_sku_codes_rebinds_the_codes_and_both_regexes():
    """gui_helpers derives SKU_CODES from the client's [sku] table, so the
    review back-map must follow the sidebar's Client selector rather than
    staying on the first client's prefixes for the rest of the session.
    Restores shreejee's scheme: every other test here (and the
    app.SKU_PREFIXES drift guard) assumes it."""
    import client_config
    import gui_helpers

    before = tuple(gui_helpers.SKU_CODES)
    try:
        gui_helpers.configure_sku_codes(("ZZ", "QQ", "XX"))
        assert gui_helpers.SKU_CODES == ("ZZ", "QQ", "XX")
        # the leading-code stripper follows...
        assert gui_helpers._strip_code("ZZ-1234") == "1234"
        # ...and so does the "this raw states a category" regex, whose
        # exception is the LAST code (the fallback), not a literal 'AP'
        assert gui_helpers._SPECIFIC_CODE_RE.match("QQ-1234")
        assert not gui_helpers._SPECIFIC_CODE_RE.match("XX-1234")
        # a code from the previous client means nothing any more
        assert gui_helpers._strip_code("WM-1234") == "WM-1234"
    finally:
        gui_helpers.configure_sku_codes(
            client_config.load_client("shreejee").sku.prefixes)
    assert tuple(gui_helpers.SKU_CODES) == before


def test_sku_codes_come_from_the_client_config():
    """The import-time binding is the client's own table, not a literal."""
    import client_config
    import gui_helpers
    assert tuple(gui_helpers.SKU_CODES) == tuple(
        client_config.load_client("shreejee").sku.prefixes)


BUDGET_LOG = """\
[1] Row 2: WM-100
    -> FOUND (amazon)
[2] Row 3: WM-101
    -> FOUND (croma)
Budget of 5 serper credit(s) reached - stopping, progress saved.
Done. 2 rows in 12s
"""


def test_parse_enricher_log_flags_the_budget_stop():
    """Item 4: --budget exits 0 with the sheet saved, so nothing else in the
    log says the batch is unfinished. The panel needs this flag to say so."""
    stats = parse_enricher_log(BUDGET_LOG)
    assert stats["stopped_by_budget"] is True
    assert stats["done"] == 2
    other = parse_enricher_log(BUDGET_LOG.replace("Budget of 5", "Budget was 5"))
    assert other["stopped_by_budget"] is False


# ------------------------------------------- review states (approve/skip) --
from gui_helpers import (APPROVE, CLEAR, SKIP,                  # noqa: E402
                         merge_review_flags, review_counts,
                         review_counts_text, review_decision, review_entries,
                         skipped_from_state)


def test_three_state_rule_approve_and_skip_clear_each_other():
    conf, skips = review_decision(set(), {}, "A", SKIP, "  wrong model ")
    assert conf == set() and skips == {"A": "wrong model"}
    conf, skips = review_decision(conf, skips, "A", APPROVE)
    assert conf == {"A"} and skips == {}
    conf, skips = review_decision(conf, skips, "A", SKIP)
    assert conf == set() and skips == {"A": ""}
    conf, skips = review_decision(conf, skips, "A", CLEAR)
    assert conf == set() and skips == {}


def test_review_decision_does_not_mutate_inputs():
    conf, skips = {"A"}, {"B": "x"}
    review_decision(conf, skips, "A", SKIP)
    review_decision(conf, skips, "B", APPROVE)
    assert conf == {"A"} and skips == {"B": "x"}
    try:
        review_decision(conf, skips, "A", "maybe")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown decision must raise")


def test_review_entries_store_one_state_per_sku_and_skip_wins():
    old = {"A": {"confirmed": True, "fp": "old-a"}}
    out = review_entries(old, {"A": True, "B": True, "C": False},
                         {"B": "bad price", "D": ""}, {"B": "fp-b"})
    assert out["A"] == {"confirmed": True, "skipped": False,
                        "skip_reason": "", "fp": "old-a"}
    # B is both ticked and skipped in the input: the skip wins
    assert out["B"] == {"confirmed": False, "skipped": True,
                        "skip_reason": "bad price", "fp": "fp-b"}
    assert out["C"]["confirmed"] is False and out["C"]["skipped"] is False
    # a skip the flag map never mentions still gets its entry
    assert out["D"]["skipped"] is True and out["D"]["confirmed"] is False
    state = {"confirmed": out}
    assert confirmed_from_state(state) == {"A"}
    assert skipped_from_state(state) == {"B": "bad price", "D": ""}


def test_old_review_state_files_load_without_skip_keys():
    state = {"confirmed": {"A": {"confirmed": True, "fp": "abc"},
                           "B": {"confirmed": False, "fp": None},
                           "C": True, "D": False}}
    assert confirmed_from_state(state) == {"A", "C"}
    assert skipped_from_state(state) == {}
    assert skipped_from_state({}) == {}
    # a corrupt entry saying both: read as skipped only, never buildable
    both = {"confirmed": {"E": {"confirmed": True, "skipped": True}}}
    assert confirmed_from_state(both) == set()
    assert skipped_from_state(both) == {"E": ""}


def test_select_build_rows_excludes_skipped_rows_in_both_scopes():
    df = _build_df()
    kept, refused = select_build_rows(df, "confirmed", {"A", "B"},
                                      {"B": "dup"})
    assert list(kept["sku"]) == ["A"] and refused == []
    kept, refused = select_build_rows(df, "all_found", set(), {"B": "dup"})
    assert list(kept["sku"]) == ["A"] and refused == []
    # a set works as the skip argument too, and a skipped non-FOUND row is
    # not reported as a refused tick
    kept, refused = select_build_rows(df, "confirmed", {"A", "C"}, {"C"})
    assert list(kept["sku"]) == ["A"] and refused == []
    nostatus = pd.DataFrame([{"sku": "A"}, {"sku": "B"}])
    kept, _ = select_build_rows(nostatus, "all_found", set(), {"A"})
    assert list(kept["sku"]) == ["B"]


def test_confirmed_scope_with_zero_approvals_builds_nothing():
    kept, refused = select_build_rows(_build_df(), "confirmed", set())
    assert kept.empty and refused == []
    kept, _ = select_build_rows(_build_df(), "confirmed", {"A"}, {"A"})
    assert kept.empty


def _rev_orig(rows):
    base = {"confirmed": False, "skipped": False,
            "status": "FOUND (amazon.in)"}
    return pd.DataFrame([{**base, **r} for r in rows])


def _rev_part(rows):
    return pd.DataFrame([{"sku": s, "confirmed": c, "skipped": k}
                         for s, c, k in rows])


def test_merge_review_flags_three_state_rule():
    orig = _rev_orig([{"sku": "A", "confirmed": True}, {"sku": "B"},
                      {"sku": "C", "skipped": True},
                      {"sku": "D", "status": "NOT FOUND"}])
    # A: approved -> skip ticked (approval cleared)
    # B: both ticked in one save -> skipped (safe reading)
    # C: skipped -> ✔ ticked (skip cleared)
    # D: non-FOUND -> skip allowed
    part = _rev_part([("A", True, True), ("B", True, True),
                      ("C", True, True), ("D", False, True)])
    # C's skip column is unchanged (still True) so only its ✔ is an edit
    flags, skips, rejected = merge_review_flags({"A"}, {"C": "old"}, orig,
                                                [part])
    assert flags["A"] is False and "A" in skips
    assert flags["B"] is False and "B" in skips
    assert flags["C"] is True and "C" not in skips
    assert "D" in skips and rejected == []


def test_merge_review_flags_untick_skip_and_untouched_copies():
    orig = _rev_orig([{"sku": "A", "skipped": True}, {"sku": "B"}])
    all_tab = _rev_part([("A", False, False), ("B", False, False)])
    bucket = _rev_part([("A", False, True)])     # untouched copy, later
    flags, skips, _ = merge_review_flags(set(), {"A": "why"}, orig,
                                         [all_tab, bucket])
    assert skips == {} and not flags.get("A")
    # an old skip with no edit stays, and a legacy tick on it does not win
    flags, skips, _ = merge_review_flags({"B"}, {"B": "x"},
                                         _rev_orig([{"sku": "B",
                                                     "skipped": True}]), [])
    assert skips == {"B": "x"} and flags["B"] is False


def test_review_counts_put_every_sku_in_one_bucket():
    df = pd.concat([_build_df(), _build_df().head(1)])   # duplicate A row
    c = review_counts(df, {"A", "C"}, {"B": "x", "D": ""})
    # A approved; B skipped; D skipped; C non-FOUND (stray tick); E non-FOUND
    assert c == {"approved": 1, "skipped": 2, "not_reviewed": 0,
                 "not_found": 2}
    assert review_counts_text(c) == \
        "Approved 1 · Skipped 2 · Not reviewed 0 · Not FOUND 2"
    c2 = review_counts(_build_df(), set(), {})
    assert c2["not_reviewed"] == 2 and sum(c2.values()) == 5


def test_a_changed_row_keeps_its_skip_but_loses_its_tick():
    """The stale-fingerprint rule: a tick on a re-enriched row is dropped
    (P3-53), a SKIP is kept - dropping it would turn an explicit 'no' into
    'not reviewed', which the 'All FOUND rows' scope would then build."""
    fp = row_fingerprint(FP_ROW)
    state = {"confirmed": {
        "WM-A": {"confirmed": False, "skipped": True, "skip_reason": "x",
                 "fp": fp},
        "WM-B": {"confirmed": True, "skipped": False, "skip_reason": "",
                 "fp": fp}}}
    changed = {**FP_ROW, "sale price": 19999}
    df = pd.DataFrame([changed, {**changed, "sku": "WM-B"}])
    assert stale_confirmations(state, df) == {"WM-B"}
    assert skipped_from_state(state) == {"WM-A": "x"}
    kept, _ = select_build_rows(df, "all_found", set(),
                                skipped_from_state(state))
    assert list(kept["sku"]) == ["WM-B"]


# ------------------------------- client pack guard (owner rule 2026-11-19) --
from gui_helpers import (foreign_hosts, remove_refused,  # noqa: E402
                         scan_pack_folder, scrub_sites)


def _guard_src(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "out.csv").write_text(
        "sku,Images\nA,https://shop.test/wp-content/uploads/A_1.webp\n",
        encoding="utf-8")
    (src / "LIST_filled.xlsx").write_bytes(b"xl")
    (src / "LIST_filled_report.md").write_text("# report", encoding="utf-8")
    (src / "enricher.log").write_text("row 2", encoding="utf-8")
    (src / "leak.csv").write_text(
        "sku,url_link\nA,https://www.samsung.com/in/tv/x\n", encoding="utf-8")
    (src / "leak.md").write_text("see www.haier.com/in", encoding="utf-8")
    return src


def test_assemble_pack_refuses_internal_files_and_source_links(tmp_path,
                                                               capsys):
    src = _guard_src(tmp_path)
    dest = tmp_path / "pack"
    files = {p.name: p for p in src.iterdir()}
    res = assemble_pack(dest, files, src, src / "out.csv",
                        allowed_hosts={"shop.test"})
    assert sorted(p.name for p in dest.iterdir() if p.is_file()) == ["out.csv"]
    refused = " | ".join(res["refused"])
    for name in ("LIST_filled.xlsx", "LIST_filled_report.md", "enricher.log",
                 "leak.csv", "leak.md"):
        assert name in refused
    assert "samsung.com" in refused and "haier.com" in refused
    assert "REFUSED" in capsys.readouterr().out          # visible warning


def test_assemble_pack_internal_archive_keeps_everything(tmp_path):
    src = _guard_src(tmp_path)
    dest = tmp_path / "archive"
    res = assemble_pack(dest, {p.name: p for p in src.iterdir()}, src,
                        src / "out.csv", internal=True)
    assert res["refused"] == [] and (dest / "LIST_filled.xlsx").exists()


def test_foreign_hosts_allows_own_host_and_subdomains():
    text = ("https://shop.test/a.webp //cdn.shop.test/b.webp "
            "http://www.shop.test/c https://img.samsung.com/x www.haier.com")
    assert foreign_hosts(text, {"shop.test"}) == ["img.samsung.com",
                                                  "haier.com"]
    assert foreign_hosts("no links, 0.8 Ton, 4.5 kg", {"shop.test"}) == []


def test_scan_and_remove_refused_on_a_finished_folder(tmp_path, capsys):
    dest = tmp_path / "pack"
    dest.mkdir()
    (dest / "README.md").write_text("from https://www.flipkart.com/x",
                                    encoding="utf-8")
    (dest / "out.csv").write_text("sku\nA\n", encoding="utf-8")
    assert len(scan_pack_folder(dest, {"shop.test"})) == 1
    remove_refused(dest, {"shop.test"})
    assert sorted(p.name for p in dest.iterdir()) == ["out.csv"]
    assert "REFUSED" in capsys.readouterr().out


def test_readme_never_prints_a_site_name():
    cov = coverage_summary(_cov_sheet())
    text = render_batch_readme("b", {}, cov)
    for site in ("flipkart", "amazon", "croma", "reliancedigital"):
        assert site not in text.lower(), site
    assert "price from a source site" in text
    assert scrub_sites("all sources blocked (amazon.in, croma.com)") == \
        "all sources blocked (source sites)"
