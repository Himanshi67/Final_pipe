"""Filter groups drive the product attributes (owner rule 2026-11-19).

"The sub-category attribute master sheet is what we use to make the filter
groups. If anything scraped matches these filter groups, we include our
attribute value." Every workbook here is a scratch copy in tmp_path; no test
reads or writes the owner's clients\\shreejee\\*.xlsx except the two that use
the already-loaded real sheets read-only. All HTTP is mocked.
"""
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app                                         # noqa: E402
import validate_csv as V                           # noqa: E402
import wc_publish as W                             # noqa: E402
from gui_helpers import batch_summary              # noqa: E402

HEADER = ("Parent Category", "Sub-Category", "Type", "Size", "Star Rating",
          "Ton", "Capacity", "Storage", "type")
SUBCAT_ROWS = [
    HEADER,
    ("Air Conditioner", "Split AC", "Inverter, Fixed Speed, Hot & Cold", "N/A",
     "2 Star, 3 Star, 5 Star,", "1.0 Ton, 1.5 Ton, 2.0 Ton, 2.5 Ton", "N/A",
     None, None),
    ("Home Appliances", "Fans", "Ceiling, Table",
     "Sweep Size in mm (e.g. 1200mm)", "1 Star to 5 Star", "N/A",
     "Motor Speed (RPM)", None, "non BLDC, BLDC"),
    ("Home Appliances", "Refrigerators", "Single Door, Double Door", "N/A",
     "N/A", "N/A", "150L, 190L, 250L", None, None),
    ("Home Appliances", "Water Purifiers", "Electrical (RO+UV+UF)", "N/A",
     "N/A", "N/A", "Storage Tank Volume in Liters (e.g. 8L)- 6, 7, 8", None,
     "Under Sink, Wall Mounted"),
]
FILTER_ROWS = [
    ("Filter Group", "Filter Values (Options)"),
    ("Brand", "Samsung, LG, etc."),
    ("Price", "Min Price â€” Max Price"),        # mojibake
    ("AC Capacity", "1 Ton, 1.5 Ton, 2 Ton,"),
    ("AC Star Rating", "3 Star, 4 Star, 5 Star"),
    ("AC Technology", "Inverter, Non-Inverter"),
    ("AC Type", "Split, Window"),
    ("Fridge Capacity", "Below 200L, 201L-300L, 300L+"),
    ("Fridge Type", "Single Door, Double Door, Side-by-Side"),
    ("Empty Group", "N/A"),
]


def _book(path, sheets):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(list(row))
    wb.save(path)
    return path


def _client(tmp_path, master=True, catmap_filters=None, catmap_subcat=None):
    """A scratch client: master.xlsx (filter sheet called 'Sheet2', found by
    header) and category_map.xlsx (with 'Filters')."""
    if master:
        _book(tmp_path / "master.xlsx", {
            "Sub-Category Attributes": SUBCAT_ROWS,
            "Sheet1": [("Parent Category", "Sub-Categories")],
            "Sheet2": FILTER_ROWS})
    _book(tmp_path / "category_map.xlsx", {
        "Category Map": [("Parent", "Sub"), ("Air Conditioner", "Split AC")],
        "Sub-Category Attributes": catmap_subcat or SUBCAT_ROWS,
        "Filters": catmap_filters or FILTER_ROWS})
    return SimpleNamespace(slug="scratch", name="Scratch", dir=tmp_path,
                           attributes_master=tmp_path / "master.xlsx",
                           category_map=tmp_path / "category_map.xlsx")


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """app's module tables switched to the scratch client for one test."""
    client = _client(tmp_path)
    monkeypatch.setattr(app, "SUBCATEGORY_ATTRIBUTES_WARNING",
                        app.SUBCATEGORY_ATTRIBUTES_WARNING)
    monkeypatch.setattr(app, "FILTER_GROUPS", app.load_filter_groups(client))
    monkeypatch.setattr(app, "SUBCATEGORY_FILTER_AXES",
                        app.load_subcategory_filter_axes(client))
    monkeypatch.setattr(app, "SUBCATEGORY_ATTRIBUTES",
                        app.load_subcategory_attributes(client))
    return client


# ------------------------------------------------------------------ loader
def test_filter_sheet_found_by_header_and_cleaned(tmp_path):
    groups = app.load_filter_groups(_client(tmp_path))
    assert groups["AC Capacity"] == ["1 Ton", "1.5 Ton", "2 Ton"]   # trailing ,
    assert groups["Brand"] == ["Samsung", "LG"]                     # etc. dropped
    assert groups["Price"] == []                                    # mojibake
    assert groups["Empty Group"] == []                              # N/A
    assert app.clean_filter_cell("1 Star to 5 Star") == [
        "1 Star", "2 Star", "3 Star", "4 Star", "5 Star"]


def test_filter_sheet_falls_back_to_category_map(tmp_path):
    other = list(FILTER_ROWS) + [("Only In Catmap", "A, B")]
    client = _client(tmp_path, master=False, catmap_filters=other)
    assert app.load_filter_groups(client)["Only In Catmap"] == ["A", "B"]
    # a master WITHOUT a filter sheet also falls back
    _book(tmp_path / "master.xlsx", {"Sub-Category Attributes": SUBCAT_ROWS})
    assert "Only In Catmap" in app.load_filter_groups(client)


def test_subcategory_sheet_prefers_master_then_category_map(tmp_path):
    changed = [list(r) for r in SUBCAT_ROWS]
    changed[1][2] = "Catmap Only Type"
    client = _client(tmp_path, catmap_subcat=changed)
    assert app.load_subcategory_attributes(client)["split ac"]["Type"] == [
        "Inverter", "Fixed Speed", "Hot & Cold"]                    # master
    os.remove(tmp_path / "master.xlsx")
    assert app.load_subcategory_attributes(client)["split ac"]["Type"] == [
        "Catmap Only Type"]                                         # fallback


def test_second_type_column_is_its_own_optional_axis(scratch):
    fans = app.SUBCATEGORY_ATTRIBUTES["fans"]
    assert fans[app.SUB_TYPE_AXIS] == ["non BLDC", "BLDC"]
    assert fans["Type"] == ["Ceiling", "Table"]
    # optional: a fan that never states BLDC is not gap-reported for it
    gaps = app.missing_spec_axes("Fans", "Crompton 1200mm 5 Star Ceiling Fan",
                                 "", parent="Home Appliances")
    assert app.SUB_TYPE_AXIS not in gaps
    _parent, axes = app.filter_axes("Water Purifiers")
    assert axes[app.SUB_TYPE_AXIS].values == ("Under Sink", "Wall Mounted")
    plan = {a: d for a, _s, _m, d in app._filter_plan("Fans", "Home Appliances")}
    assert plan[app.SUB_TYPE_AXIS] == "Motor Type"


def test_axis_cells_parse_hints_lists_and_ranges():
    assert app.parse_axis_cell("N/A") is None
    assert app.parse_axis_cell("") is None
    hint = app.parse_axis_cell("Sweep Size in mm (e.g. 1200mm)")
    assert (hint.values, hint.unit, hint.free) == ((), "mm", True)
    tail = app.parse_axis_cell("Storage Tank Volume in Liters (e.g. 8L)- 6, 7, 8")
    assert tail.values == ("6L", "7L", "8L") and not tail.free
    assert app.parse_axis_cell("1 Star to 3 Star").values == (
        "1 Star", "2 Star", "3 Star")
    assert app.parse_axis_cell("Burner Size (Small, Medium, Jumbo)").values == (
        "Small", "Medium", "Jumbo")
    assert app.parse_axis_cell(
        "Electrical (RO+UV+UF), Non-Electrical (Gravity)").values == (
        "Electrical (RO+UV+UF)", "Non-Electrical (Gravity)")
    nothing = app.parse_axis_cell("Room Area Coverage (Sq. Ft.)")
    assert nothing.values == () and not nothing.free


# ------------------------------------------------------------------ snapping
@pytest.mark.parametrize("raw", ["1.5 T", "1.5 TR", "1.5 Ton", "1.5 Tonne",
                                 "1.5 tons", "1.50 ton", "Capacity 1.5-Ton"])
def test_snap_ton_spellings(raw):
    assert app.snap_filter_value(raw, ["1 Ton", "1.5 Ton", "2 Ton"]) == "1.5 Ton"


@pytest.mark.parametrize("raw,allowed,want", [
    ("190 Ltr", ["150L", "190L"], "190L"),
    ("190.0 litres", ["150L", "190L"], "190L"),
    ("7 Kg", ["6 kg", "7 kg"], "7 kg"),
    ("7 kilograms", ["6 kg", "7 kg"], "7 kg"),
    ('43"', ["32 Inch", "43 Inch"], "43 Inch"),
    ("108 cm (43 inches)", ['32"', '43"'], '43"'),
    ("5-star", ["3 Star", "5 Star"], "5 Star"),
    ("Five Star", ["3 Star", "5 Star"], "5 Star"),
    ("750 Watt", ["500W", "750W"], "750W"),
    ("1 TB", ["512GB", "1TB"], "1TB"),
    ("1200 MM", ["600mm", "1200mm"], "1200mm"),
    ("5", ["3 Star", "5 Star"], "5 Star"),          # bare number, axis unit
])
def test_snap_units_and_numbers(raw, allowed, want):
    assert app.snap_filter_value(raw, allowed) == want


def test_snap_never_rounds():
    assert app.snap_filter_value("1.2 Ton", ["1 Ton", "1.5 Ton", "2 Ton"]) is None
    assert app.snap_filter_value("1 Ton", ["1.0 Ton"]) == "1.0 Ton"   # 1 == 1.0
    assert app.snap_filter_value("195 L", ["150L", "190L"]) is None


def test_snap_bands():
    bands = ["Below 200L", "201L-300L", "300L+"]
    assert app.snap_filter_value("190 L", bands) == "Below 200L"
    assert app.snap_filter_value("260 L", bands) == "201L-300L"
    assert app.snap_filter_value("805 L", bands) == "300L+"
    assert app.snap_filter_value("7.5 kg", ["6kg & below", "6.1-7kg",
                                            "7.1-8kg", "8.1kg & above"]) == "7.1-8kg"


def test_snap_text_lists_aliases_and_containment():
    tech = ["Inverter", "Non-Inverter"]
    assert app.snap_filter_value("Inverter", tech) == "Inverter"
    assert app.snap_filter_value("non inverter", tech) == "Non-Inverter"
    assert app.snap_filter_value("Fixed Speed", tech) == "Non-Inverter"
    assert app.snap_filter_value("BLDC Motor", ["non BLDC", "BLDC"]) == "BLDC"
    assert app.snap_filter_value("Non-BLDC", ["non BLDC", "BLDC"]) == "non BLDC"
    wm = ["Fully Automatic Front Load", "Fully Automatic Top Load",
          "Semi-Automatic"]
    assert app.snap_filter_value("Front Load", wm) == "Fully Automatic Front Load"
    assert app.snap_filter_value("Fully-Automatic Front Load", wm) == \
        "Fully Automatic Front Load"
    assert app.snap_filter_value("Twin Tub", wm) == "Semi-Automatic"
    assert app.snap_filter_value("RO+UV+UF", ["RO + UV", "RO + UV + UF"]) == \
        "RO + UV + UF"
    # several values only when the text states each one
    assert app.snap_filter_value("Single Door and Double Door",
                                 ["Single Door", "Double Door"]) == \
        "Single Door, Double Door"
    assert app.snap_filter_value("Doorless", ["Single Door"]) is None
    assert app.snap_filter_value("Splitter", ["Split"]) is None    # whole word


def test_free_form_hint_value_is_normalised():
    assert app.normalise_quantity("Sweep: 1200 MM", "mm") == "1200mm"
    assert app.normalise_quantity("1.50 Litre", "l") == "1.5L"
    assert app.normalise_quantity("no number", "mm") is None


# ------------------------------------------------------------------ fill
AC_TITLE = "LG 1.5 Ton 5 Star Inverter Split AC"


def _labels(rows):
    return [label for label, _value in rows]


def test_filter_attributes_first_named_by_group_and_not_duplicated(scratch):
    scraped = [("Capacity", "1.5 TR"), ("Energy Rating", "5-star"),
               ("Colour", "White")]
    hits, unmatched = app.filter_attributes("Split AC", "Air Conditioner",
                                            AC_TITLE, scraped)
    assert unmatched == []
    rows = app.product_specs(AC_TITLE, "LG", "Air Conditioner", scraped, hits)
    assert rows[:4] == [("AC Technology", "Inverter"), ("AC Type", "Split"),
                        ("AC Star Rating", "5 Star"), ("AC Capacity", "1.5 Ton")]
    assert ("Brand", "LG") in rows and ("Colour", "White") in rows
    # the scraped rows that BECAME filter attributes are not written again
    assert "Capacity" not in _labels(rows)
    assert "Energy Rating" not in _labels(rows)


def test_filter_sheet_spelling_wins_and_subcategory_values_are_valid(scratch):
    hits, _ = app.filter_attributes("Split AC", "Air Conditioner", "LG AC",
                                    [("Capacity", "1.0 Ton")])
    assert ("AC Capacity", "1 Ton") in [(h.group, h.value) for h in hits]
    hits, _ = app.filter_attributes("Split AC", "Air Conditioner", "LG AC",
                                    [("Capacity", "2.5 Ton")])
    assert ("AC Capacity", "2.5 Ton") in [(h.group, h.value) for h in hits]
    # a value only the sub-category row lists, on a two-group axis
    hits, _ = app.filter_attributes("Split AC", "Air Conditioner",
                                    "LG Hot & Cold Split AC", [])
    assert ("Product Type", "Hot & Cold") in [(h.group, h.value) for h in hits]


def test_unmatched_value_is_written_as_scraped_with_a_note(scratch):
    # owner decision 2026-11-19: never rounded to 1 Ton or 1.5 Ton, never
    # dropped - written as the filter value, with a "new value" note
    scraped = [("Capacity", "1.2 Ton")]
    hits, unmatched = app.filter_attributes("Split AC", "Air Conditioner",
                                            "LG Split AC", scraped)
    assert ("AC Capacity", "1.2 Ton", "new") in [
        (h.group, h.value, h.kind) for h in hits]
    assert unmatched == [
        "AC Capacity: '1.2 Ton' is a new value, add it to the sheet"]
    rows = app.product_specs("LG Split AC", "LG", "Air Conditioner", scraped,
                             hits)
    assert ("AC Capacity", "1.2 Ton") in rows
    assert ("Capacity", "1.2 Ton") not in rows           # the same fact once


def test_nothing_invented_without_spec_or_title_evidence(scratch):
    hits, unmatched = app.filter_attributes("Split AC", "Air Conditioner",
                                            "LG Air Conditioner", [])
    assert hits == [] and unmatched == []
    hits, unmatched = app.filter_attributes("No Such Sub", "Nowhere",
                                            AC_TITLE, [("Capacity", "1.5 Ton")])
    assert hits == [] and unmatched == []


def test_hint_axis_writes_free_form_and_sub_type(scratch):
    hits, _ = app.filter_attributes("Fans", "Home Appliances",
                                    "Crompton 1200 MM BLDC Ceiling Fan", [])
    got = {h.group: (h.value, h.kind) for h in hits}
    assert got["Sweep Size"] == ("1200mm", "free")
    assert got["Motor Type"] == ("BLDC", "text")
    assert got["Product Type"] == ("Ceiling", "text")
    hits, _ = app.filter_attributes("Fans", "Home Appliances",
                                    "Usha non-BLDC table fan", [])
    assert {h.group: h.value for h in hits}["Motor Type"] == "non BLDC"


def test_band_filter_keeps_the_exact_figure(scratch):
    title = "LG 190 L Single Door Refrigerator"
    hits, _ = app.filter_attributes("Refrigerators", "Home Appliances", title,
                                    [("Capacity", "190 L")])
    assert ("Fridge Capacity", "Below 200L") in [(h.group, h.value) for h in hits]
    rows = app.product_specs(title, "LG", "Refrigerators",
                             [("Capacity", "190 L")], hits)
    assert ("Capacity", "190 L") in rows          # a band is not the same fact


def test_filter_attributes_never_cut_by_the_cap(scratch):
    scraped = [("Capacity", "1.5 Ton")] + [
        (f"Extra Spec {n}", f"Value {n}") for n in range(40)]
    hits, _ = app.filter_attributes("Split AC", "Air Conditioner", AC_TITLE,
                                    scraped)
    rows = app.product_specs(AC_TITLE, "LG", "Air Conditioner", scraped, hits)
    assert len(rows) <= app.MAX_ATTRIBUTES
    assert _labels(rows)[:len(hits)] == [h.group for h in hits]


def test_key_points_rank_filter_attributes_first(scratch):
    scraped = [("Colour", "White"), ("Capacity", "1.5 Ton")]
    hits, _ = app.filter_attributes("Split AC", "Air Conditioner", AC_TITLE,
                                    scraped)
    _config, short_desc, _long, _rows = app.build_descriptions(
        "AC-LG1", AC_TITLE, "LG", "Air Conditioner", scraped, sub="Split AC",
        parent="Air Conditioner", filters=hits)
    import re
    points = re.findall(r"<th>(.*?)</th>", short_desc)[2:]
    assert points[:len(hits)] == [h.group for h in hits]


def test_content_firewall_still_applies(scratch):
    bad = app.FilterHit("AC Type", "Flipkart Assured", "Type",
                        frozenset({"configuration"}), "text")
    rows = app.product_specs(AC_TITLE, "LG", "Air Conditioner", (), [bad])
    assert ("AC Type", "Flipkart Assured") not in rows
    # retailer junk in the spec table never reaches the snapper
    scraped = app.parse_scraped_specs("Capacity: 1.5 Ton Flipkart Assured",
                                      brand="LG")
    hits, _ = app.filter_attributes("Split AC", "Air Conditioner",
                                    "LG Split AC", scraped)
    assert "AC Capacity" not in [h.group for h in hits]


def test_process_inventory_writes_filter_columns_on_real_sheets(monkeypatch):
    """Read-only use of the loaded shreejee sheets: an AC row gets AC filter
    attributes first and a `_filter_unmatched` column."""
    monkeypatch.setattr(app, "download_urls_for_sku",
                        lambda sku, urls, flags=None: [])
    df = pd.DataFrame([{
        "sku": "LG-TS19", "Brands": "LG", "title": AC_TITLE,
        "categories": "Air Conditioner > Split AC", "regular price": "",
        "sale price": "35000", "url_link": "", "image_url": "",
        "specs": "Capacity: 1.5 Ton | Colour: White"}])
    out = app.process_inventory(df)
    assert "_filter_unmatched" in out.columns
    assert list(out.columns).index("_filter_unmatched") == \
        list(out.columns).index("_spec_gaps") + 1
    row = out.iloc[0]
    names = [row[f"Attribute {i} name"] for i in range(1, 5)]
    assert "AC Capacity" in names and "AC Star Rating" in names
    assert row["_filter_unmatched"] == ""


# ------------------------------------------------------------------ publish
class _Resp:
    def __init__(self, data):
        self.status_code, self._data = 200, data
        self.headers = {"Content-Type": "application/json"}
        self.content, self.text = b"{}", ""

    def json(self):
        return self._data


class _Session:
    def __init__(self, attributes):
        self.attributes, self.calls = attributes, []

    def request(self, method, url, auth=None, timeout=None, **kw):
        self.calls.append((method, url.split("/wc/v3/")[1], kw.get("json")))
        if method == "GET":
            return _Resp(self.attributes)
        return _Resp({"id": 99})


def _store(monkeypatch, attributes, allow_writes):
    for key, value in (("WC_URL", "https://store.invalid"), ("WC_KEY", "k"),
                       ("WC_SECRET", "s")):
        monkeypatch.setenv(key, value)
    session = _Session(attributes)
    monkeypatch.setattr(W, "SESSION", session)
    store = W.Store(allow_writes=allow_writes)
    store.filter_groups = {"ac capacity"}
    return store, session


ROW = {"Attribute 1 name": "AC Capacity", "Attribute 1 value(s)": "1.5 Ton",
       "Attribute 1 visible": "1", "Attribute 2 name": "Colour",
       "Attribute 2 value(s)": "White", "Attribute 2 visible": "1"}


def test_publish_uses_the_existing_global_attribute(monkeypatch):
    store, session = _store(monkeypatch, [
        {"id": 7, "name": "AC Capacity", "slug": "pa_ac-capacity"}], False)
    attrs = W.attributes_for(ROW, store)
    assert attrs[0] == {"id": 7, "options": ["1.5 Ton"], "visible": True,
                        "variation": False}
    assert attrs[1]["name"] == "Colour" and "id" not in attrs[1]
    W.attributes_for(ROW, store)                     # cached: one GET per run
    assert [c[0] for c in session.calls] == ["GET"]


def test_publish_dry_run_reports_would_create_and_posts_nothing(monkeypatch, capsys):
    store, session = _store(monkeypatch, [], False)
    attrs = W.attributes_for(ROW, store)
    assert attrs[0]["name"] == "AC Capacity"         # left custom in the dry run
    assert ("global attribute", "AC Capacity") in store.would_create
    assert "would create global attribute AC Capacity" in capsys.readouterr().out
    assert not [c for c in session.calls if c[0] != "GET"]


def test_publish_commit_creates_the_global_attribute_once(monkeypatch):
    store, session = _store(monkeypatch, [], True)
    first = W.attributes_for(ROW, store)
    second = W.attributes_for(ROW, store)
    posts = [c for c in session.calls if c[0] == "POST"]
    assert posts == [("POST", "products/attributes",
                      {"name": "AC Capacity", "type": "select"})]
    assert first[0]["id"] == second[0]["id"] == 99


def test_publish_without_store_keeps_old_payload():
    attrs = W.attributes_for(ROW)
    assert attrs[0] == {"name": "AC Capacity", "options": ["1.5 Ton"],
                        "visible": True, "variation": False}


def test_filter_group_names_come_from_the_sheets():
    names = W.filter_group_names(app.CLIENT)
    assert "AC Capacity" in names and "Fridge Capacity" in names
    assert "Brand" not in names and "Price" not in names


# ------------------------------------------------------------------ validator
def _csv_row(**extra):
    row = {"sku": "AC-LG-TS19", "title": AC_TITLE,
           "Images": "https://x/AC-LG-TS19_1.webp,https://x/AC-LG-TS19_2.webp",
           "description": "<table></table>",
           "categories": "Air Conditioner > Split AC", "regular price": "40000",
           "sale price": "35000", "Brands": "LG", "_spec_gaps": "",
           "_filter_unmatched": ""}
    row.update(extra)
    return row


def _reasons(df, tmp_path):
    issues = V.validate_export(df, str(tmp_path / "x.csv"), check_images=False,
                               write_issues=False)
    return " | ".join(str(i.get("reasons")) for i in issues)


def test_validator_warns_on_a_filter_value_not_in_the_lists(tmp_path):
    bad = pd.DataFrame([_csv_row(**{"Attribute 1 name": "AC Capacity",
                                    "Attribute 1 value(s)": "7 Ton"})])
    text = _reasons(bad, tmp_path)
    assert "warning_filter_value_not_in_sheet (AC Capacity: '7 Ton'" in text
    good = pd.DataFrame([_csv_row(**{"Attribute 1 name": "AC Capacity",
                                     "Attribute 1 value(s)": "2.5 Ton"})])
    assert "filter_value_not_in_sheet" not in _reasons(good, tmp_path)


def test_validator_surfaces_filter_unmatched_as_a_warning(tmp_path):
    df = pd.DataFrame([_csv_row(**{
        "_filter_unmatched": "AC Capacity (Ton): '7 Ton' not in list",
        "Attribute 1 name": "Capacity", "Attribute 1 value(s)": "7 Ton"})])
    text = _reasons(df, tmp_path)
    assert "warning_filter_value_unmatched (AC Capacity (Ton)" in text
    issue = {"reasons": "warning_filter_value_unmatched (x)"}
    assert not V.is_blocking_issue(issue)


# ------------------------------------------------------------------ tools
def _check_tool():
    spec = importlib.util.spec_from_file_location(
        "check_filter_sheet", ROOT / "tools" / "check_filter_sheet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_check_tool_reports_disagreements_and_copy_drift(tmp_path):
    drift = list(FILTER_ROWS)
    drift[3] = ("AC Capacity", "1 Ton, 1.5 Ton")                 # catmap copy
    client = _client(tmp_path, catmap_filters=drift)
    report = _check_tool().check(client)
    text = "\n".join(report["disagreements"])
    assert "'2.5 Ton' is in the sub-category sheet only" in text
    assert "'Window' is in the filter sheet only" in text
    assert any("'1.0 Ton'" in s and "'1 Ton'" in s for s in report["spelling"])
    assert "Brand" in report["unfed_groups"]
    assert len(report["copy_differences"]) == 1
    assert any("Motor Speed (RPM)" in s for s in report["unparseable"])
    # identical copies -> no drift, and the build warning is empty
    (tmp_path / "same").mkdir()
    same = _client(tmp_path / "same")
    assert app.attribute_sheet_differences(same) == []
    assert app.filter_sheet_warning(same) == ""


def test_batch_summary_counts_filter_unmatched_rows():
    df = pd.DataFrame([
        {"sku": "A", "Images": "", "_spec_gaps": "", "Brands": "LG",
         "sale price": "1", "categories": "", "_filter_unmatched": "x"},
        {"sku": "B", "Images": "", "_spec_gaps": "", "Brands": "LG",
         "sale price": "1", "categories": "", "_filter_unmatched": ""}])
    assert batch_summary(df)["filter_unmatched_rows"] == 1


def test_check_tool_compares_sheet1_with_the_other_lists(tmp_path):
    """Owner decision 5 (2026-11-19): 'Sheet1' (Parent Category |
    Sub-Categories) against the sub-category rows and category_map's list."""
    tool = _check_tool()
    subcat = list(SUBCAT_ROWS) + [
        ("Washing Machines", "Top Load", "N/A", "N/A", "N/A", "N/A", "N/A",
         None, None),
        ("Home Appliances", "Trolley Bag", "N/A", "N/A", "N/A", "N/A", "N/A",
         None, None)]
    sheet1 = [("Parent Category", "Sub-Categories"),
              ("Air Conditioner", "Split AC, Window AC"),
              ("Home Appliances", "Fans , Refrigerators, Water Purifiers, "
                                  "Trolly Bag"),
              ("Washing Machines", "Top load")]
    _book(tmp_path / "master.xlsx", {"Sub-Category Attributes": subcat,
                                     "Sheet1": sheet1, "Sheet2": FILTER_ROWS})
    catmap_list = [("Parent Category", "Sub-Categories"),
                   ("Air Conditioner", "Split AC"),
                   ("Home Appliances", "Fans, Refrigerators, Water Purifiers, "
                                       "Trolly Bag"),
                   ("Washing Machines", "Top Load")]
    _book(tmp_path / "category_map.xlsx", {
        "reorganise": catmap_list, "Sub-Category Attributes": subcat,
        "Filters": FILTER_ROWS})
    client = SimpleNamespace(slug="scratch", name="Scratch", dir=tmp_path,
                             attributes_master=tmp_path / "master.xlsx",
                             category_map=tmp_path / "category_map.xlsx")
    report = tool.check(client)
    text = "\n".join(report["category_list"])
    assert ("Home Appliances / spelling: 'Trolly Bag' (Sheet1) / "
            "'Trolley Bag' (Sub-Category Attributes)") in text
    assert ("Washing Machines / spelling: 'Top load' (Sheet1) / 'Top Load' "
            "(Sub-Category Attributes)") in text
    assert "Air Conditioner / 'Window AC': in Sheet1 only" in text   # catmap
    assert ("vs category_map.xlsx: Washing Machines / spelling: 'Top load' "
            "(Sheet1) / 'Top Load' (category_map.xlsx)") in text
    assert "Fans" not in text                       # spacing is not a difference
    assert tool.summary(report)["category_list"] == len(report["category_list"])
    # the sheets are only read
    assert openpyxl.load_workbook(tmp_path / "master.xlsx")["Sheet1"][
        "B4"].value == "Top load"
