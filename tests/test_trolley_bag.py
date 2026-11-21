"""'Trolley Bag' spelling (fixed 2026-11-20) and a guard against the next one.

Every taxonomy lookup goes through app._cat_norm, which only strips, collapses
spaces and lowercases. The category list said 'Trolly Bag' while the
'Sub-Category Attributes' sheet said 'Trolley Bag', so a trolley bag got no
required attributes, no filter groups and a blank _spec_gaps. The owner
decided the right spelling is 'Trolley Bag'. 'Top load' / 'Top Load' differ
only in case, which _cat_norm already ignores, and the live store uses
'Top load', so that pair stays as it is.
"""
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app                                          # noqa: E402
import client_config                                # noqa: E402

TITLE = "Safari Pentagon 65 cm Polycarbonate Trolly Bag Hard Sided Luggage"


def _check_tool():
    spec = importlib.util.spec_from_file_location(
        "check_filter_sheet", ROOT / "tools" / "check_filter_sheet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_trolley_bag_title_gets_category_axes_and_filters():
    app.configure("shreejee")
    for title in (TITLE, TITLE.replace("Trolly", "Trolley")):
        parent, sub = app.detect_category(title)
        assert (parent, sub) == ("Home Appliances", "Trolley Bag"), title
        got, mapped = app.canonicalize_category(parent, sub)
        assert mapped and got == "Home Appliances > Trolley Bag"
    axes = app.attribute_axes("Trolley Bag", "Home Appliances")
    assert {"Capacity", "Size", "Type"} <= set(axes), axes
    _parent, filters = app.filter_axes("Trolley Bag", "Home Appliances")
    assert filters, "no filter groups for Trolley Bag"


def _letters(text):
    return re.sub(r"[^a-z]", "", str(text).lower())


def _edit1(a, b):
    """True when a and b differ by exactly one insert, delete or substitute."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = diffs = 0
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            diffs += 1
            if diffs > 1:
                return False
            if len(a) == len(b):
                i += 1
            j += 1
        else:
            i += 1
            j += 1
    return diffs + (len(b) - j) == 1


def test_category_list_names_match_attribute_rows_exactly():
    """Every sub-category in the shipped shreejee category lists that has a
    near-identical 'Sub-Category Attributes' row (same letters, or one edit
    apart) must match that row exactly after app._cat_norm - otherwise the
    lookup silently returns no axes for it."""
    client = client_config.load_client("shreejee")
    tool = _check_tool()
    records, _source, _problems = app._subcategory_sheet(client)
    rows = {app._cat_norm(s)
            for subs in tool._subcat_rows_as_list(records).values() for s in subs}
    lists = [tool.read_category_list(app.attributes_master_path(client)),
             tool.read_category_list(client.category_map,
                                     first_sheet_fallback=True)]
    bad = []
    for listing in lists:
        assert listing, "category list sheet not found"
        for subs in listing.values():
            for sub in subs:
                key = app._cat_norm(sub)
                if key in rows:
                    continue
                for row in rows:
                    if (_letters(row) == _letters(key)
                            or _edit1(row, key)):
                        bad.append((sub, row))
    assert not bad, f"near-identical names that do not match: {bad}"


def test_check_tool_no_longer_reports_the_trolley_pair():
    tool = _check_tool()
    report = tool.check(client_config.load_client("shreejee"))
    text = "\n".join(report["category_list"])
    assert "Trolly" not in text
