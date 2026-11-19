"""Filter groups no sub-category axis feeds (app.DIRECT_FILTER_GROUPS) and
the CSV 'Attribute N global' flag (2026-11-19).

TV Resolution / Display Tech / Refresh Rate, WM Features / Color and Fridge
Defrosting have no column in the sub-category sheet. They are fed from the
scraped spec table, then the title, snapped onto the client's filter sheet.
Every workbook here is a scratch copy in tmp_path; no test touches the
owner's clients\\shreejee\\*.xlsx. All HTTP is mocked.
"""
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

HEADER = ("Parent Category", "Sub-Category", "Type", "Size", "Star Rating",
          "Ton", "Capacity", "Storage", "type")
TV_SIZES = '24", 32", 40", 43", 50", 55", 58", 65", 70", 75"+'
SUBCAT_ROWS = [
    HEADER,
    ("Air Conditioner", "Split AC", "Inverter, Fixed Speed, Hot & Cold", "N/A",
     "2 Star, 3 Star, 4 Star, 5 Star", "1 Ton, 1.5 Ton, 2 Ton", "N/A",
     None, None),
    ("Home Appliances", "Refrigerators",
     "Single Door, Double Door, Side-by-Side, French Door, Multi-Door", "N/A",
     "N/A", "N/A", "150L, 190L, 250L", None, None),
    ("Washing Machines", "Front Load", "Fully Automatic Front Load", "N/A",
     "3 Star, 4 Star, 5 Star", "N/A",
     "Dry Clothes Wash Load Weight in kg (e.g. 8.0kg)", None, None),
    ("Washing Machines", "semi auto", "Semi-Automatic", "N/A",
     "3 Star, 4 Star, 5 Star", "N/A",
     "Dry Clothes Wash Load Weight in kg (e.g. 7.5kg)", None, None),
    ("LED", "QLED,OLED,Qned , Mini LED", "Google TV, Android TV, WebOS, Tizen",
     TV_SIZES, "N/A", "N/A", "N/A", None, None),
]
FILTER_ROWS = [
    ("Filter Group", "Filter Values (Options)"),
    ("Brand", "Samsung, LG, etc."),
    ("AC Capacity", "1 Ton, 1.5 Ton, 2 Ton"),
    ("AC Technology", "Inverter, Non-Inverter"),
    ("AC Type", "Split, Window, Portable"),
    ("AC Mode", "Hot & Cold"),
    ("TV Screen Size", TV_SIZES),
    ("TV Resolution", "HD Ready, Full HD, 4K UHD, 8K"),
    ("TV Display Tech", "LED, QLED, OLED, Mini LED, QNED"),
    ("TV Smart Features", "Google TV, Android TV, WebOS, Tizen"),
    ("TV Refresh Rate", "50Hz, 60Hz, 100Hz, 120Hz, 144Hz"),
    ("WM Function Type", "Fully Automatic Front Load, Fully Automatic Top Load, "
                         "Semi-Automatic"),
    ("WM Washing Capacity", "6kg & below, 6.1-7kg, 7.1-8kg, 8.1kg & above"),
    ("WM Features", "Built-in Heater, Inverter Tech, Steam Wash, Smart Wi-Fi, "
                    "Direct Drive"),
    ("WM Color", "Silver, Grey, White, Black"),
    ("Fridge Type", "Single Door, Double Door, Side-by-Side, French Door, "
                    "Multi-Door"),
    ("Fridge Defrosting", "Frost Free, Direct Cool"),
]
TV = ("QLED,OLED,Qned , Mini LED", "LED")
WM = ("Front Load", "Washing Machines")
FRIDGE = ("Refrigerators", "Home Appliances")


def _book(path, sheets):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(list(row))
    wb.save(path)
    return path


def _client(tmp_path, filter_rows=None):
    rows = filter_rows or FILTER_ROWS
    _book(tmp_path / "master.xlsx", {"Sub-Category Attributes": SUBCAT_ROWS,
                                     "Sheet2": rows})
    _book(tmp_path / "category_map.xlsx", {
        "Category Map": [("Parent", "Sub")],
        "Sub-Category Attributes": SUBCAT_ROWS, "Filters": rows})
    return SimpleNamespace(slug="scratch", name="Scratch", dir=tmp_path,
                           attributes_master=tmp_path / "master.xlsx",
                           category_map=tmp_path / "category_map.xlsx")


def _use(monkeypatch, client):
    monkeypatch.setattr(app, "FILTER_GROUPS", app.load_filter_groups(client))
    monkeypatch.setattr(app, "SUBCATEGORY_FILTER_AXES",
                        app.load_subcategory_filter_axes(client))
    monkeypatch.setattr(app, "SUBCATEGORY_ATTRIBUTES",
                        app.load_subcategory_attributes(client))
    return client


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    return _use(monkeypatch, _client(tmp_path))


def _fa(where, title, scraped=()):
    sub, parent = where
    hits, unmatched = app.filter_attributes(sub, parent, title, list(scraped))
    return {h.group: h.value for h in hits}, unmatched, hits


# ------------------------------------------------------------ TV Resolution
def test_tv_resolution_from_label_alias_and_highest(scratch):
    got, unmatched, _ = _fa(TV, "Samsung 55 inch Smart TV",
                            [("Resolution", "3840 x 2160 Pixels")])
    assert got["TV Resolution"] == "4K UHD" and unmatched == []
    got, _, _ = _fa(TV, "Sony TV", [("Resolution", "Ultra HD (4K), Full HD")])
    assert got["TV Resolution"] == "4K UHD"            # one value, the highest
    got, _, _ = _fa(TV, "Mi TV", [("Screen Resolution", "1366x768")])
    assert got["TV Resolution"] == "HD Ready"


def test_tv_resolution_from_title_and_unmatched_label(scratch):
    got, unmatched, _ = _fa(TV, "LG 108 cm (43 inch) Full HD LED Smart TV")
    assert got["TV Resolution"] == "Full HD" and unmatched == []
    # owner decision 2026-11-19: an unlisted value is written as scraped
    got, unmatched, _ = _fa(TV, "Some TV", [("Resolution", "1024 x 600")])
    assert got["TV Resolution"] == "1024 x 600"
    assert unmatched == [
        "TV Resolution: '1024 x 600' is a new value, add it to the sheet"]
    # the title never produces a note
    got, unmatched, _ = _fa(TV, "Some 720p TV")
    assert "TV Resolution" not in got and unmatched == []


# ------------------------------------------------------------ TV Display Tech
def test_tv_display_tech_drops_plain_led_when_specific(scratch):
    got, _, _ = _fa(TV, "Samsung 55 inch QLED Smart LED TV")
    assert got["TV Display Tech"] == "QLED"
    got, _, _ = _fa(TV, "TCL 4K Smart LED TV")
    assert got["TV Display Tech"] == "LED"
    got, _, _ = _fa(TV, "TV", [("Display Type", "Mini LED")])
    assert got["TV Display Tech"] == "Mini LED"


def test_tv_display_tech_note_only_from_dedicated_label(scratch):
    _, unmatched, _ = _fa(TV, "TV", [("Panel Type", "VA")])
    assert unmatched == []                            # panel type: no note
    got, unmatched, _ = _fa(TV, "TV", [("Display Technology", "Plasma")])
    assert got["TV Display Tech"] == "Plasma"
    assert unmatched == [
        "TV Display Tech: 'Plasma' is a new value, add it to the sheet"]


# ------------------------------------------------------------ TV Refresh Rate
def test_tv_refresh_rate_exact_hz_only(scratch):
    got, _, hits = _fa(TV, "TV", [("Refresh Rate", "120 Hertz")])
    assert got["TV Refresh Rate"] == "120Hz"
    assert [h.kind for h in hits if h.group == "TV Refresh Rate"] == ["exact"]
    got, _, _ = _fa(TV, "Sony Bravia 4K 100Hz Smart TV")
    assert got["TV Refresh Rate"] == "100Hz"
    # a title number without Hz is not a refresh rate
    got, _, _ = _fa(TV, "Sony Bravia 120 Smart TV")
    assert "TV Refresh Rate" not in got
    # never rounded: 75 Hz is not 60Hz or 100Hz - it is written as 75Hz,
    # spelled like the list ('60Hz'), with a note
    got, unmatched, _ = _fa(TV, "TV", [("Refresh Rate", "75 Hz")])
    assert got["TV Refresh Rate"] == "75Hz"
    assert unmatched == [
        "TV Refresh Rate: '75Hz' is a new value, add it to the sheet"]


# ------------------------------------------------------------ WM Features
def test_wm_features_multi_value_and_aliases(scratch):
    got, unmatched, _ = _fa(WM, "IFB 8 kg Front Load",
                            [("Special Features", "Inbuilt Heater, Steam, "
                              "Wi-Fi enabled, Child Lock")])
    assert got["WM Features"] == "Built-in Heater, Steam Wash, Smart Wi-Fi"
    assert unmatched == []           # a general feature list never gives a note
    got, _, _ = _fa(WM, "LG 8 kg", [("Motor Type", "Inverter Direct Drive")])
    assert got["WM Features"] == "Direct Drive"
    got, _, _ = _fa(WM, "Samsung 7 kg Digital Inverter Front Load")
    assert got["WM Features"] == "Inverter Tech"


@pytest.mark.parametrize("value", ["No", "Not available", "No Wi-Fi",
                                   "Wi-Fi: No", "without heater"])
def test_wm_features_never_written_from_a_negative(scratch, value):
    got, unmatched, _ = _fa(WM, "Bosch 8 kg Front Load",
                            [("Features", value)])
    assert "WM Features" not in got and unmatched == []


# ------------------------------------------------------------ WM Color
def test_wm_color_label_title_alias_and_unmatched(scratch):
    got, _, _ = _fa(WM, "LG 8 kg", [("Colour", "Gray")])
    assert got["WM Color"] == "Grey"
    got, _, _ = _fa(WM, "Whirlpool 7 kg Front Load (White)")
    assert got["WM Color"] == "White"
    got, unmatched, _ = _fa(WM, "LG 8 kg", [("Colour", "Blue")])
    assert got["WM Color"] == "Blue"
    assert unmatched == ["WM Color: 'Blue' is a new value, add it to the sheet"]


# ------------------------------------------------------------ Fridge Defrosting
def test_fridge_defrosting_label_alias_and_title(scratch):
    got, _, _ = _fa(FRIDGE, "LG 260 L", [("Defrost System", "No Frost")])
    assert got["Fridge Defrosting"] == "Frost Free"   # 'No' is part of the name
    got, _, _ = _fa(FRIDGE, "Godrej 190 L Direct Cool Single Door")
    assert got["Fridge Defrosting"] == "Direct Cool"
    got, unmatched, _ = _fa(FRIDGE, "Haier 190 L",
                            [("Defrosting Type", "Auto Defrost")])
    assert got["Fridge Defrosting"] == "Auto Defrost"
    assert unmatched == [
        "Fridge Defrosting: 'Auto Defrost' is a new value, add it to the sheet"]


def test_shared_alias_decisions():
    wm = ["Fully Automatic Front Load", "Fully Automatic Top Load",
          "Semi-Automatic"]
    assert app.snap_filter_value("Fully-Automatic Drum Front Opener", wm) == \
        "Fully Automatic Front Load"
    assert app.snap_filter_value("Top Opener", wm) == "Fully Automatic Top Load"
    wp = ["RO + UV", "UV + UF", "Non-Electrical (Gravity)"]
    assert app.snap_filter_value("Gravity based", wp) == \
        "Non-Electrical (Gravity)"
    assert app.snap_filter_value("Non-Electric", wp) == \
        "Non-Electrical (Gravity)"
    assert app.snap_filter_value("Smart Inverter", ["Inverter Tech"]) == \
        "Inverter Tech"


# ------------------------------------------------------------ general rules
def test_missing_group_is_skipped(tmp_path, monkeypatch):
    rows = [r for r in FILTER_ROWS if r[0] not in ("TV Resolution", "WM Color")]
    _use(monkeypatch, _client(tmp_path, rows))
    got, unmatched, _ = _fa(TV, "LG 4K UHD Smart TV",
                            [("Resolution", "1024 x 600")])
    assert "TV Resolution" not in got and unmatched == []
    got, _, _ = _fa(WM, "LG 8 kg White")
    assert "WM Color" not in got
    assert "TV Resolution" not in app.filter_allowed_for(*TV)
    assert "WM Color" not in app.filter_group_names(_client(tmp_path, rows))


def test_direct_groups_only_for_their_category(scratch):
    got, _, _ = _fa(FRIDGE, "LG 190 L 4K White Steam Refrigerator")
    assert not {"TV Resolution", "WM Color", "WM Features"} & set(got)


def test_direct_groups_are_optional_no_spec_gaps(scratch):
    title = "Samsung 55 inch Google TV"
    gaps = app.missing_spec_axes(TV[0], title, "", (), parent=TV[1])
    assert gaps == []
    for group in ("TV Resolution", "TV Display Tech", "TV Refresh Rate"):
        assert group not in gaps


def test_ac_type_axis_feeds_ac_mode(scratch):
    got, _, _ = _fa(("Split AC", "Air Conditioner"),
                    "LG 1.5 Ton Inverter Hot & Cold Split AC")
    assert got["AC Mode"] == "Hot & Cold"
    assert got["AC Technology"] == "Inverter" and got["AC Type"] == "Split"
    assert "Product Type" not in got


def test_direct_hits_downstream_order_dedup_validator_names(scratch):
    scraped = [("Resolution", "4K Ultra HD"), ("Colour", "Black"),
               ("Refresh Rate", "120 Hz")]
    title = "Samsung 55 inch QLED Smart TV"
    hits, _ = app.filter_attributes(TV[0], TV[1], title, scraped)
    rows = app.product_specs(title, "Samsung", "", scraped, hits)
    labels = [label for label, _v in rows]
    assert labels[:len(hits)] == [h.group for h in hits]      # filters first
    assert "Resolution" not in labels and "Refresh Rate" not in labels
    assert ("Colour", "Black") in rows                 # not a TV filter group
    allowed = app.filter_allowed_for(*TV)
    assert allowed["TV Resolution"] == {"hd ready", "full hd", "4k uhd", "8k"}
    names = app.filter_group_names(scratch)
    for group in ("TV Resolution", "TV Display Tech", "TV Refresh Rate",
                  "WM Features", "WM Color", "Fridge Defrosting", "AC Mode"):
        assert group in names
    assert "Brand" not in names


def test_direct_source_row_with_more_facts_is_kept(scratch):
    scraped = [("Special Features", "Steam Wash, Child Lock")]
    hits, _ = app.filter_attributes(WM[0], WM[1], "LG 8 kg", scraped)
    rows = app.product_specs("LG 8 kg", "LG", "", scraped, hits)
    assert ("WM Features", "Steam Wash") in rows
    assert ("Special Features", "Steam Wash, Child Lock") in rows


def test_validator_checks_direct_group_values(tmp_path, monkeypatch):
    client = _use(monkeypatch, _client(tmp_path))
    monkeypatch.setattr(V, "_FILTER_APP", [app])
    monkeypatch.setattr(app, "CLIENT", SimpleNamespace(slug=V.CLIENT.slug))
    row = {"categories": "LED > QLED,OLED,Qned , Mini LED",
           "Attribute 1 name": "TV Refresh Rate",
           "Attribute 1 value(s)": "75Hz",
           "Attribute 2 name": "TV Resolution",
           "Attribute 2 value(s)": "4K UHD"}
    problems = V.filter_value_problems(
        row, ["Attribute 1 name", "Attribute 2 name"], row["categories"])
    assert problems == [f"{V.WARNING_PREFIX}filter_value_not_in_sheet "
                        "(TV Refresh Rate: '75Hz' is a new value, add it to "
                        "the filter sheet)"]
    assert client


# ------------------------------------------------------------ global flag
def test_csv_global_flag_only_on_filter_attributes(scratch, monkeypatch):
    monkeypatch.setattr(app, "download_urls_for_sku",
                        lambda sku, urls, flags=None: [])
    df = pd.DataFrame([{
        "sku": "LG-TV55", "Brands": "LG",
        "title": "LG 55 inch 4K UHD OLED Smart TV",
        "categories": "LED > QLED,OLED,Qned , Mini LED", "regular price": "",
        "sale price": "95000", "url_link": "", "image_url": "",
        "specs": "Refresh Rate: 120 Hz | Colour: Black | Sound Output: 40 W"}])
    out = app.process_inventory(df)
    row = out.iloc[0]
    flags = {}
    i = 1
    while f"Attribute {i} name" in out.columns and \
            str(row[f"Attribute {i} name"]) not in ("", "nan"):
        flags[row[f"Attribute {i} name"]] = row[f"Attribute {i} global"]
        i += 1
    assert flags["TV Resolution"] == "1" and flags["TV Display Tech"] == "1"
    assert flags["TV Refresh Rate"] == "1"
    assert flags["Brand"] == "0" and flags["Colour"] == "0"
    assert set(flags.values()) == {"0", "1"}
    filters = {"TV Resolution", "TV Display Tech", "TV Refresh Rate",
               "TV Screen Size"}
    assert all((v == "1") == (k in filters) for k, v in flags.items())


def test_publish_keeps_a_global_0_row_custom():
    class _Store:
        def filter_group_set(self):
            return {"capacity", "ac capacity"}

        def global_attribute_id(self, name):
            return 7

    row = {"Attribute 1 name": "AC Capacity", "Attribute 1 value(s)": "1.5 Ton",
           "Attribute 1 visible": "1", "Attribute 1 global": "1",
           "Attribute 2 name": "Capacity", "Attribute 2 value(s)": "1.2 Ton",
           "Attribute 2 visible": "1", "Attribute 2 global": "0"}
    attrs = W.attributes_for(row, _Store())
    assert attrs[0]["id"] == 7
    assert attrs[1]["name"] == "Capacity" and "id" not in attrs[1]
    # an old CSV without the column: judged by the name alone
    del row["Attribute 2 global"]
    assert W.attributes_for(row, _Store())[1]["id"] == 7
