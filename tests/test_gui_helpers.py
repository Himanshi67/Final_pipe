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
                 "cached_searches": 0, "cached_pages": 0}


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
        ["Kitchen", "Mixer Grinders; Kettles"],
        ["", "orphan sub"],
    ])
    assert sheet_combos(df) == {
        "Home Appliances > Washing Machines",
        "Home Appliances > Refrigerators",
        "Kitchen > Mixer Grinders", "Kitchen > Kettles"}


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
