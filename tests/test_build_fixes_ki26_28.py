"""KI-26 (filter-group names flagged as dead placeholders), KI-27 (a
semi-automatic washer also matched 'Fully Automatic Top Load') and KI-28
('2 Way Air Swing' read as 2 W). Pure functions and in-memory frames only."""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app                                         # noqa: E402
import validate_csv as V                           # noqa: E402

# the real allowed values of the washing-machine group (Sheet2 of
# clients\shreejee\SubCategory_Attributes_Master.xlsx)
WM = ["Fully Automatic Front Load", "Fully Automatic Top Load", "Semi-Automatic"]


# ------------------------------------------------------------ KI-28
def test_two_way_air_swing_is_no_wattage():
    assert app.wattage("LG 1.5 Ton AC, 2 Way Air Swing, ADC Sensor, 4.4 kW") == ""


@pytest.mark.parametrize("title, want", [
    ("Bajaj Mixer 1200 W Motor", "1200 W"),
    ("Bajaj Mixer 1200W Motor", "1200W"),
    ("Bajaj Heater 1200 Watts", "1200 Watts"),
    ("Bajaj Iron 1200W, Non-Stick", "1200W"),
])
def test_stated_wattage_is_still_read(title, want):
    assert app.wattage(title) == want


# ------------------------------------------------------------ KI-27
def test_semi_automatic_top_load_is_semi_automatic_only():
    assert app.snap_filter_values("Godrej 8 kg Semi-Automatic Top Load Washing "
                                  "Machine", WM) == ["Semi-Automatic"]


def test_fully_automatic_top_load_unchanged():
    assert app.snap_filter_values("Fully Automatic Top Load", WM) == \
        ["Fully Automatic Top Load"]


def test_bare_top_load_is_fully_automatic_top_load():
    assert app.snap_filter_values("Samsung 8 kg 5 Star Top Load Washing Machine",
                                  WM) == ["Fully Automatic Top Load"]


def test_front_load_unchanged():
    assert app.snap_filter_values("Front Load", WM) == ["Fully Automatic Front Load"]


def test_inverter_and_bldc_cases_unchanged():
    tech = ["Inverter", "Non-Inverter"]
    assert app.snap_filter_value("Inverter", tech) == "Inverter"
    assert app.snap_filter_value("non inverter", tech) == "Non-Inverter"
    assert app.snap_filter_value("Fixed Speed", tech) == "Non-Inverter"
    assert app.snap_filter_value("BLDC Motor", ["non BLDC", "BLDC"]) == "BLDC"
    assert app.snap_filter_value("Non-BLDC", ["non BLDC", "BLDC"]) == "non BLDC"


# ------------------------------------------------------------ KI-26
def _wm_row(**over):
    row = {"sku": "WM-GODREJ-1",
           "title": "Godrej 8 kg Semi-Automatic Top Load Washing Machine",
           "Images": "https://x/WM-GODREJ-1_1.webp,https://x/WM-GODREJ-1_2.webp",
           "description": ("<table><tr><td>WM Function Type: Semi-Automatic"
                           "</td></tr><tr><td>WM Washing Capacity: 8 kg"
                           "</td></tr></table>"),
           "categories": "Washing Machines > semi auto",
           "regular price": "15000", "sale price": "12000", "Brands": "Godrej",
           "_spec_gaps": "", "_filter_unmatched": "",
           "Attribute 1 name": "WM Function Type",
           "Attribute 1 value(s)": "Semi-Automatic", "Attribute 1 global": "1",
           "Attribute 2 name": "WM Washing Capacity",
           "Attribute 2 value(s)": "8 kg", "Attribute 2 global": "1"}
    row.update(over)
    return row


def _reasons(row, tmp_path):
    issues = V.validate_export(pd.DataFrame([row]), str(tmp_path / "x.csv"),
                               check_images=False, write_issues=False)
    return " | ".join(str(i.get("reasons")) for i in issues)


def test_washer_built_today_has_no_placeholder_issue(tmp_path):
    assert "placeholder_spec_text" not in _reasons(_wm_row(), tmp_path)


@pytest.mark.parametrize("dead", ["Standard Capacity", "Appliance Utility Core"])
def test_blob_placeholders_still_flagged_anywhere(tmp_path, dead):
    row = _wm_row(description=f"<table><tr><td>Capacity: {dead}</td></tr></table>")
    assert f"placeholder_spec_text ({dead})" in _reasons(row, tmp_path)


def test_filter_group_name_as_a_value_is_flagged(tmp_path):
    row = _wm_row(**{"Attribute 1 value(s)": "WM Function Type"})
    assert "placeholder_spec_text (WM Function Type)" in _reasons(row, tmp_path)


def test_filter_group_name_as_a_list_item_is_flagged(tmp_path):
    row = _wm_row(**{"Attribute 2 value(s)": "8 kg, WM Washing Capacity"})
    assert "placeholder_spec_text (WM Washing Capacity)" in _reasons(row, tmp_path)
