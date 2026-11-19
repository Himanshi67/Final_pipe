"""Owner decision 1 of 2026-11-19: a scraped filter value is never replaced,
rounded or dropped because the filter list does not hold it. Only its
spelling is made regular. Decision 3: 'Smart TV' is not a TV platform.

Rules a-g of the fourth-round brief. Every workbook is a scratch copy in
tmp_path; no test touches clients\\shreejee\\*.xlsx. All HTTP is mocked.
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
TV_SIZES = '32", 43", 50", 55", 65", 75"+'
SUBCAT_ROWS = [
    HEADER,
    ("Air Conditioner", "Split AC", "Inverter, Fixed Speed, Hot & Cold", "N/A",
     "2 Star, 3 Star, 4 Star, 5 Star",
     "1 Ton, 1.2 Ton, 1.5 Ton, 2 Ton, 2.5 Ton, 3 Ton", "N/A", None, None),
    ("Home Appliances", "Refrigerators",
     "Single Door, Double Door, Side-by-Side, French Door, Multi-Door", "N/A",
     "N/A", "N/A", "150L, 190L, 250L", None, None),
    ("Washing Machines", "Front Load", "Fully Automatic Front Load", "N/A",
     "3 Star, 4 Star, 5 Star", "N/A",
     "Dry Clothes Wash Load Weight in kg (e.g. 8.0kg)", None, None),
    ("LED", "QLED,OLED,Qned , Mini LED", "Google TV, Android TV, WebOS, Tizen",
     TV_SIZES, "N/A", "N/A", "N/A", None, None),
]
FILTER_ROWS = [
    ("Filter Group", "Filter Values (Options)"),
    ("AC Capacity", "1 Ton, 1.2 Ton, 1.5 Ton, 2 Ton, 2.5 Ton, 3 Ton"),
    ("AC Star Rating", "2 Star, 3 Star, 4 Star, 5 Star"),
    ("AC Technology", "Inverter, Non-Inverter"),
    ("AC Type", "Split, Window"),
    ("AC Mode", "Hot & Cold"),
    ("TV Screen Size", TV_SIZES),
    ("TV Resolution", "HD Ready, Full HD, 4K UHD, 8K"),
    ("TV Display Tech", "LED, QLED, OLED, Mini LED, QNED"),
    ("TV Smart Features", "Google TV, Android TV, WebOS, Tizen"),
    ("TV Refresh Rate", "60Hz, 120Hz"),
    ("WM Washing Capacity", "6kg & below, 6.1-7kg, 7.1-8kg, 8.1kg & above"),
    ("WM Color", "Silver, Grey, White, Black"),
    ("Fridge Capacity", "Below 200L, 201L-300L, 300L+"),
]
AC = ("Split AC", "Air Conditioner")
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


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    _book(tmp_path / "master.xlsx", {"Sub-Category Attributes": SUBCAT_ROWS,
                                     "Sheet2": FILTER_ROWS})
    _book(tmp_path / "category_map.xlsx", {
        "Category Map": [("Parent", "Sub")],
        "Sub-Category Attributes": SUBCAT_ROWS, "Filters": FILTER_ROWS})
    client = SimpleNamespace(slug="scratch", name="Scratch", dir=tmp_path,
                             attributes_master=tmp_path / "master.xlsx",
                             category_map=tmp_path / "category_map.xlsx")
    monkeypatch.setattr(app, "FILTER_GROUPS", app.load_filter_groups(client))
    monkeypatch.setattr(app, "SUBCATEGORY_FILTER_AXES",
                        app.load_subcategory_filter_axes(client))
    monkeypatch.setattr(app, "SUBCATEGORY_ATTRIBUTES",
                        app.load_subcategory_attributes(client))
    return client


def _fa(where, title, scraped=()):
    sub, parent = where
    hits, notes = app.filter_attributes(sub, parent, title, list(scraped))
    return {h.group: h.value for h in hits}, notes, hits


def _note(group, value):
    return f"{group}: '{value}' is a new value, add it to the sheet"


# ------------------------------------------------- a. numeric, exact values
def test_owner_example_0_8_ton_is_written_as_scraped(scratch):
    got, notes, hits = _fa(AC, "LG Split AC", [("Capacity", "0.8 Ton")])
    assert got["AC Capacity"] == "0.8 Ton"
    assert notes == [_note("AC Capacity", "0.8 Ton")]
    assert [h.kind for h in hits if h.group == "AC Capacity"] == ["new"]


def test_owner_example_1_8_t_spelling_made_regular(scratch):
    got, notes, _ = _fa(AC, "LG Split AC", [("Capacity", "1.8 T")])
    assert got["AC Capacity"] == "1.8 Ton"
    assert notes == [_note("AC Capacity", "1.8 Ton")]


def test_new_number_from_the_title_when_no_label(scratch):
    got, notes, _ = _fa(AC, "Voltas 0.8 Ton 3 Star Window AC")
    assert got["AC Capacity"] == "0.8 Ton"
    assert notes == [_note("AC Capacity", "0.8 Ton")]


def test_scraped_label_wins_over_the_title(scratch):
    got, _, _ = _fa(AC, "LG 1.5 Ton Split AC", [("Capacity", "1.8 Ton")])
    assert got["AC Capacity"] == "1.8 Ton"


@pytest.mark.parametrize("raw,want", [
    ("60 cm (24 inch)", '24"'),          # list spells inches as '32"'
    ('24"', '24"'),
])
def test_tv_size_spelled_like_the_list(scratch, raw, want):
    got, notes, _ = _fa(TV, "Some TV", [("Screen Size", raw)])
    assert got["TV Screen Size"] == want
    assert notes == [_note("TV Screen Size", want)]


def test_refresh_rate_spelled_like_the_list(scratch):
    got, notes, _ = _fa(TV, "TV", [("Refresh Rate", "75 Hz")])
    assert got["TV Refresh Rate"] == "75Hz"
    assert notes == [_note("TV Refresh Rate", "75Hz")]
    got, _, _ = _fa(TV, "Sony Bravia 100 Hz Smart TV")   # title, axis unit
    assert got["TV Refresh Rate"] == "100Hz"


def test_never_rounded_and_listed_value_still_snaps(scratch):
    got, notes, _ = _fa(AC, "LG Split AC", [("Capacity", "1.2 Ton")])
    assert got["AC Capacity"] == "1.2 Ton" and notes == []    # in the list now
    got, _, _ = _fa(AC, "LG Split AC", [("Capacity", "1.25 Ton")])
    assert got["AC Capacity"] == "1.25 Ton"                   # not 1.2 / 1.5


# ------------------------------------------------- b. band groups
def test_owner_example_7_kg_band_label_and_exact_row_kept(scratch):
    scraped = [("Capacity", "7 kg")]
    title = "LG 7 kg Front Load"
    got, notes, hits = _fa(WM, title, scraped)
    assert got["WM Washing Capacity"] == "6.1-7kg" and notes == []
    rows = app.product_specs(title, "LG", "", scraped, hits)
    assert ("WM Washing Capacity", "6.1-7kg") in rows
    assert any(label == "Capacity" and "7" in value for label, value in rows)


def test_tv_75_plus_band_keeps_the_exact_size(scratch):
    scraped = [("Screen Size", "80 inch")]
    got, notes, hits = _fa(TV, "Sony 203 cm (80 inch) TV", scraped)
    assert got["TV Screen Size"] == '75"+' and notes == []
    rows = app.product_specs("Sony 203 cm (80 inch) TV", "Sony", "", scraped,
                             hits)
    assert ('TV Screen Size', '75"+') in rows
    assert any("80" in value for label, value in rows
               if label != "TV Screen Size")


def test_value_outside_every_band_is_written_exact(scratch):
    got, notes, _ = _fa(FRIDGE, "Haier Fridge", [("Capacity", "200.5 L")])
    assert got["Fridge Capacity"] == "200.5L"
    assert notes == [_note("Fridge Capacity", "200.5L")]


# ------------------------------------------------- c. text groups
def test_text_value_under_dedicated_label_written_as_scraped(scratch):
    got, notes, _ = _fa(TV, "Hisense TV", [("Smart Platform", "VIDAA  OS")])
    assert got["TV Smart Features"] == "VIDAA OS"            # spaces compacted
    assert notes == [_note("TV Smart Features", "VIDAA OS")]


def test_partly_matching_cell_writes_matched_plus_unmatched(scratch):
    got, notes, _ = _fa(TV, "TV", [("Display Technology", "qled, Quantum Dot")])
    assert got["TV Display Tech"] == "QLED, Quantum Dot"
    assert notes == [_note("TV Display Tech", "Quantum Dot")]


def test_text_never_new_from_the_title(scratch):
    got, notes, _ = _fa(TV, "Hisense 55 inch VIDAA Smart TV")
    assert "TV Smart Features" not in got and notes == []


def test_text_new_value_must_pass_firewall_and_length(scratch):
    long = "A very long platform description that goes on and on"
    got, notes, _ = _fa(TV, "TV", [("Smart Platform", long)])
    assert "TV Smart Features" not in got
    assert notes and "not in list" in notes[0]
    got, _, _ = _fa(TV, "TV", [("Smart Platform", "Buy now on Flipkart")])
    assert "TV Smart Features" not in got


def test_general_label_never_feeds_a_new_text_value(scratch):
    got, _, _ = _fa(FRIDGE, "LG Fridge", [("Configuration", "Refrigerator")])
    assert "Fridge Type" not in got


# ------------------------------------------------- d. never a value
@pytest.mark.parametrize("value", ["Smart TV", "smart", "Yes", "No", "NA",
                                   "N/A", "Not available"])
def test_owner_example_smart_tv_alone_writes_nothing(scratch, value):
    got, notes, _ = _fa(TV, "Samsung 55 inch Smart TV",
                        [("Smart Platform", value)])
    assert "TV Smart Features" not in got and notes == []


def test_named_platform_is_written(scratch):
    got, notes, _ = _fa(TV, "Samsung Smart TV", [("Smart Platform", "Tizen")])
    assert got["TV Smart Features"] == "Tizen" and notes == []
    got, _, _ = _fa(TV, "Sony 55 inch Google TV")
    assert got["TV Smart Features"] == "Google TV"


@pytest.mark.parametrize("value", ["Yes", "No", "na", "not available"])
def test_empty_words_never_a_colour(scratch, value):
    got, notes, _ = _fa(WM, "LG 8 kg", [("Colour", value)])
    assert "WM Color" not in got and notes == []


# ------------------------------------------------- e. nothing invented
def test_nothing_from_nowhere(scratch):
    got, notes, _ = _fa(AC, "LG Air Conditioner")
    assert "AC Capacity" not in got and notes == []
    got, notes, _ = _fa(TV, "TV", [("Display Technology", "No")])
    assert "TV Display Tech" not in got and notes == []


# ------------------------------------------------- f. order and global flag
def test_new_values_are_global_filter_attributes_first(scratch, monkeypatch):
    monkeypatch.setattr(app, "download_urls_for_sku",
                        lambda sku, urls, flags=None: [])
    df = pd.DataFrame([{
        "sku": "LG-TV24", "Brands": "LG", "title": "LG 60 cm (24 inch) LED TV",
        "categories": "LED > QLED,OLED,Qned , Mini LED", "regular price": "",
        "sale price": "12000", "url_link": "", "image_url": "",
        "specs": "Refresh Rate: 75 Hz | Colour: Black"}])
    out = app.process_inventory(df)
    row = out.iloc[0]
    attrs, i = [], 1
    while f"Attribute {i} name" in out.columns and \
            str(row[f"Attribute {i} name"]) not in ("", "nan"):
        attrs.append((row[f"Attribute {i} name"], row[f"Attribute {i} value(s)"],
                      row[f"Attribute {i} global"]))
        i += 1
    by_name = {n: (v, g) for n, v, g in attrs}
    assert by_name["TV Refresh Rate"] == ("75Hz", "1")
    assert by_name["TV Screen Size"] == ('24"', "1")
    filters = [n for n, _v, g in attrs if g == "1"]
    assert [n for n, _v, _g in attrs][:len(filters)] == filters  # first
    assert "is a new value" in row["_filter_unmatched"]


# ------------------------------------------------- g. validator
def test_validator_new_value_is_a_non_blocking_warning(scratch, monkeypatch):
    monkeypatch.setattr(V, "_FILTER_APP", [app])
    monkeypatch.setattr(app, "CLIENT", SimpleNamespace(slug=V.CLIENT.slug))
    row = {"categories": "Air Conditioner > Split AC",
           "Attribute 1 name": "AC Capacity", "Attribute 1 value(s)": "0.8 Ton",
           "Attribute 1 global": "1",
           "Attribute 2 name": "AC Type", "Attribute 2 value(s)": "Cassette",
           "Attribute 2 global": "0"}
    problems = V.filter_value_problems(
        row, ["Attribute 1 name", "Attribute 2 name"], row["categories"])
    assert problems == [f"{V.WARNING_PREFIX}filter_value_not_in_sheet "
                        "(AC Capacity: '0.8 Ton' is a new value, add it to "
                        "the filter sheet)"]
    assert not V.is_blocking_issue({"reasons": "; ".join(problems)})


def test_validator_does_not_repeat_new_value_notes(tmp_path):
    row = {"sku": "AC-LG-1", "title": "LG 0.8 Ton 3 Star Split AC Inverter",
           "Images": "https://x/AC-LG-1_1.webp,https://x/AC-LG-1_2.webp",
           "description": "<table></table>",
           "categories": "Air Conditioner > Split AC", "regular price": "40000",
           "sale price": "35000", "Brands": "LG", "_spec_gaps": "",
           "_filter_unmatched": (_note("AC Capacity", "0.8 Ton")
                                 + "; AC Type: 'Cassette' not in list"
                                 + "; AC Mode: 'Dry' not in list")}
    issues = V.validate_export(pd.DataFrame([row]), str(tmp_path / "x.csv"),
                               check_images=False, write_issues=False)
    text = " | ".join(str(i.get("reasons")) for i in issues)
    assert "is a new value, add it to the sheet" not in text
    assert ("warning_filter_value_unmatched (AC Type: 'Cassette' not in list"
            " | AC Mode: 'Dry' not in list)") in text
    # notes are joined with ' | ', so the '; ' reason split never turns the
    # second note into a blocking reason
    pieces = [r for i in issues for r in str(i.get("reasons")).split("; ")
              if "not in list" in r]
    assert pieces and all(r.startswith(V.WARNING_PREFIX) for r in pieces)


# ------------------------------------------------- publish: no new writes
def test_publish_sends_a_new_value_as_an_option_of_the_global(monkeypatch):
    class _Store:
        posts = []

        def filter_group_set(self):
            return {"ac capacity"}

        def global_attribute_id(self, name):
            return 7

    row = {"Attribute 1 name": "AC Capacity", "Attribute 1 value(s)": "0.8 Ton",
           "Attribute 1 visible": "1", "Attribute 1 global": "1"}
    attrs = W.attributes_for(row, _Store())
    assert attrs == [{"id": 7, "options": ["0.8 Ton"], "visible": True,
                      "variation": False}]


# ----------------- TV platform: named or nothing, in EVERY output (2026-11-19)
def _all_output_text(title, specs=""):
    cfg, short, long_, rows = app.build_descriptions(
        "TV-X", title, "Samsung", "LED", app.parse_scraped_specs(specs),
        sub="", parent="LED")
    return cfg + short + long_, rows


@pytest.mark.parametrize("title", ["80 cm (32) HD Smart TV UA32H4500FUXXL",
                                   "Samsung 32 inch Smart LED TV"])
def test_bare_smart_in_title_writes_no_platform(title):
    assert "platform" not in app.spec_tags(title)
    html, rows = _all_output_text(title)
    labels = {label.lower() for label, _ in rows}
    assert "smart platform" not in labels
    assert "Smart Platform" not in html


@pytest.mark.parametrize("label,value", [
    ("Operating System", "Smart TV"), ("Smart Platform", "Yes"),
    ("OS", "Smart"), ("Smart TV Platform", "smart tv"),
    ("TV Smart Features", "Smart TV"), ("Platform", "Yes"),
])
def test_bare_platform_spec_row_is_never_written(label, value):
    assert app.bare_platform_row(label, value)
    html, rows = _all_output_text("Samsung 32 inch Smart LED TV",
                                  f"{label}: {value}")
    assert all(not app.bare_platform_row(l, v) for l, v in rows)
    assert f">{value}<" not in html


@pytest.mark.parametrize("title,want", [
    ("Haier 32 inch Smart Google TV", "Google TV"),
    ("Hisense 43 inch VIDAA Smart TV", "VIDAA"),
    ("LG 43 inch WebOS Smart TV", "WebOS"),
])
def test_named_platform_in_title_is_still_written(title, want):
    assert app.spec_tags(title)["platform"] == want


def test_named_platform_in_spec_table_is_written():
    assert app.parse_scraped_specs("Operating System: Tizen") == [
        ("Smart Platform", "Tizen")]


def test_smart_tag_survives_without_a_platform():
    tags = app.build_tags("Samsung", "LED", "", "Samsung 32 inch Smart LED TV",
                          "", "LED")
    assert "Smart TV" in tags
