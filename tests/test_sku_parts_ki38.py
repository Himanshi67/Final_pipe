"""KI-38: a SKU token that is only a size ('25LTR', '1.5L', '15-L', '7KG',
'2000W') was taken as the model code, so the SKU skipped the KI-35 loose
checks. And 'AO SMITH' gave brand 'AO', which made 'smith' a series word.
Offline - no network.

    .venv\\Scripts\\python.exe -m pytest tests\\test_sku_parts_ki38.py -q
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sku_enricher as E                          # noqa: E402


@pytest.fixture(autouse=True)
def _quiet_log(monkeypatch):
    monkeypatch.setattr(E, "log", lambda *a, **k: None)


def rec(title, url="https://www.amazon.in/dp/B0TEST0001", price="₹9,990"):
    return {"title": title, "url_link": url, "sale price": price,
            "regular price": "", "specs": "", "image_url": ""}


def verdict(r, sku):
    brand, model = E.sku_parts(sku)
    return E.assess(r, sku, model, brand, 0, 0)


# ------------------------------------------------------- size is not a model
@pytest.mark.parametrize("sku,brand", [
    ("KENSTAR WH AQUALIA 25LTR", "KENSTAR"),
    ("KENSTAR WH AQUALIA 1.5L", "KENSTAR"),
    ("CROMPTON ARNO NEO 15-L", "CROMPTON"),
    ("KENSTAR WH X 6 Lt", "KENSTAR"),
    ("IFB WM 7KG", "IFB"),
    ("IFB WM 7.5KG", "IFB"),
    ("BAJAJ X 2000W", "BAJAJ"),
    ("BAJAJ X 2000WATTS", "BAJAJ"),
    ("KENSTAR-WH AQUALIA 25LTRS", "KENSTAR"),
])
def test_size_token_is_never_the_model(sku, brand):
    assert E.sku_parts(sku) == (brand, "")


@pytest.mark.parametrize("sku,words,litres", [
    ("KENSTAR WH AQUALIA 25LTR", ["aqualia"], {25.0}),
    ("KENSTAR WH AQUALIA 1.5L", ["aqualia"], {1.5}),
    ("CROMPTON ARNO NEO 15-L", ["arno", "neo"], {15.0}),
    ("KENSTAR WH X 6 Lt", [], {6.0}),
])
def test_size_sku_takes_the_loose_path(sku, words, litres):
    brand, _ = E.sku_parts(sku)
    assert E.loose_model_terms(sku, brand) == (words, litres)


@pytest.mark.parametrize("sku,brand,model", [
    ("SAMSUNG-WA70BG4441YYTL", "SAMSUNG", "WA70BG4441YYTL"),
    ("WM-HTW120-178BK", "", "HTW120-178BK"),
    ("KENSTAR-GWH JETT 6L", "KENSTAR", ""),
    ("HAIER-HW80-B14959 25L", "HAIER", "HW80-B14959"),
    ("HAIER HW80B14 25LTR", "HAIER", "HW80B14"),
    # a digit outside the size piece: the original token stays the model
    ("HAIER-7.5KG-HWM75", "HAIER", "7.5KG-HWM75"),
    ("A.O.SMITH-SDS15X 25L", "AO SMITH", "SDS15X"),
])
def test_real_model_code_is_kept(sku, brand, model):
    assert E.sku_parts(sku) == (brand, model)


# a size joined by '-' to a series word is not a model code either
@pytest.mark.parametrize("sku,brand,words,litres", [
    # brands are what HEAD already gave (a brandless ERP code): not KI-38
    ("WH-AQUAGENIE-25L", "AQUAGENIE", [], {25.0}),
    ("EK-STYLEE-1.5L", "EK", ["stylee"], {1.5}),
    ("ICT-1200W-KISTO", "", ["kisto"], set()),
    ("DI-ARDO-1000W", "ARDO", [], set()),
    ("WH-MAXIMUS-3L", "MAXIMUS", [], {3.0}),
])
def test_hyphen_joined_size_is_not_the_model(sku, brand, words, litres):
    b, model = E.sku_parts(sku)
    assert (b, model) == (brand, "")
    assert E.loose_model_terms(sku, b) == (words, litres)


# ------------------------------------------------------- AO SMITH
@pytest.mark.parametrize("sku", [
    "AO SMITH SDS 15L", "AO-SMITH SDS 15L", "A O SMITH SDS 15L",
    "A.O. SMITH SDS 15L", "A.O.SMITH SDS 15L", "WH AO SMITH SDS 15L",
    "ao smith sds 15l",
])
def test_ao_smith_is_one_brand(sku):
    brand, model = E.sku_parts(sku)
    assert (brand, model) == ("AO SMITH", "")
    words, litres = E.loose_model_terms(sku, brand)
    assert (words, litres) == (["sds"], {15.0})
    assert "smith" not in words and "ao" not in words


def test_ao_smith_glued_model_is_freed():
    assert E.sku_parts("A.O.SMITH-SDS15X 25L") == ("AO SMITH", "SDS15X")


def test_ao_prefix_of_another_word_is_not_ao_smith():
    assert E.sku_parts("AOSMITHX-AB1234")[0] != "AO SMITH"


@pytest.mark.parametrize("sku", ["AO SMITH SDS 15L", "WH AO SMITH SDS 15L",
                                 "A.O. SMITH SDS 15L", "AO-SMITH SDS 15L"])
def test_ao_smith_queries_strip_the_brand(sku):
    brand, model, tiers = E.build_queries(sku)
    queries = [q for tier in tiers for q, _d in tier]
    assert queries
    for q in queries:
        assert q.startswith("AO SMITH SDS 15L"), q
        assert q.upper().count("SMITH") == 1, q


@pytest.mark.parametrize("title", ["A.O. Smith SDS 15 L Storage Water Heater",
                                   "AO Smith SDS 15 Litre Geyser"])
def test_ao_smith_title_spellings_pass_the_brand_check(title):
    assert verdict(rec(title), "AO SMITH SDS 15L") == ("weak", "")


def test_ao_smith_other_series_is_rejected():
    v, reason = verdict(rec("A. O. Smith HSE 15L Water Heater"),
                        "AO SMITH SDS 15L")
    assert (v, reason) == ("", "series word not in title (sds)")


# ------------------------------------------------------- 'Lt' / 'Lts'
@pytest.mark.parametrize("s", ["6 Lt", "6Lt", "6 Lts", "6Lts", "6-Lt"])
def test_lt_spellings_read_as_litres(s):
    assert E.size_values(s, False) == [6.0], s
    assert [float(x) for x in E.LITRE_RE.findall(s)] == [6.0], s


def test_lt_flow_rate_counts_on_gas_only():
    assert E.size_values("12 Lt/min", True) == [12.0]
    assert E.size_values("12 Lt/min", False) == []
    assert E.size_values("12 L/min", False) == []
    assert E.size_values("12 L/min", True) == [12.0]


def test_lt_does_not_match_inside_a_word():
    assert E.LITRE_RE.findall("6 Ltd edition") == []


# ------------------------------------------------------- assess
SIZE_SKU = "KENSTAR WH AQUALIA 25LTR"


def test_size_sku_right_page_is_weak():
    assert verdict(rec("Kenstar Aqualia 25 Litre Water Heater"),
                   SIZE_SKU) == ("weak", "")


def test_size_sku_other_series_is_rejected():
    assert verdict(rec("Kenstar Ultra 25 Ltr Water Heater"), SIZE_SKU) == (
        "", "series word not in title (aqualia)")


def test_size_sku_other_size_is_rejected():
    assert verdict(rec("Kenstar Aqualia 15 L Water Heater"), SIZE_SKU) == (
        "", "size mismatch: listing says 15 L, SKU says 25 L")


# ------------------------------------------------------- wattage (loose path)
@pytest.mark.parametrize("sku,title,why", [
    ("BAJAJ X 2000W", "Bajaj Majesty 1000 W Dry Iron",
     "wattage mismatch: listing says 1000 W, SKU says 2000 W"),
    ("BAJAJ DX-1000W", "Bajaj Rex 500 W Mixer Grinder",
     "wattage mismatch: listing says 500 W, SKU says 1000 W"),
    ("BAJAJ PIC-2000W", "Bajaj Majesty 1600 Watts Induction Cooktop",
     "wattage mismatch: listing says 1600 W, SKU says 2000 W"),
])
def test_other_wattage_is_rejected(sku, title, why):
    assert E.sku_parts(sku)[1] == ""
    assert verdict(rec(title), sku) == ("", why)


@pytest.mark.parametrize("sku,title", [
    ("BAJAJ X 2000W", "Bajaj X 2000 W Room Heater"),
    ("BAJAJ X 2000W", "Bajaj X 2 kW Room Heater"),
    ("BAJAJ DX-1000W", "Bajaj DX 1000 Watts Dry Iron"),
    ("BAJAJ DX-1000W", "Bajaj DX Dry Iron"),          # no wattage: unchanged
])
def test_matching_or_absent_wattage_stays_weak(sku, title):
    assert verdict(rec(title), sku) == ("weak", "")


def test_geyser_wattage_in_title_does_not_reject():
    # the SKU names no wattage, so the title's '2000 W' is not compared
    title = "Kenstar Aqualia 25 L 2000 W Storage Water Heater"
    assert verdict(rec(title), SIZE_SKU) == ("weak", "")


def test_watt_values():
    assert E.watt_values("1000 W, 2000W, 1.2 kW, 750 Watts") == [
        1000.0, 2000.0, 1200.0, 750.0]
    assert E.watt_values("25 L Water Heater, 5 Way") == []
    assert E.size_values("25 L 2000 W", False) == [25.0]


def test_matching_wattage_counts_in_overlap():
    sku = "BAJAJ DX-1000W"
    # 'DX' is too short to be a series word: only the wattage counts
    assert E.loose_overlap("Bajaj DX 1000 W Dry Iron", sku, "BAJAJ", "") == 1
    assert E.loose_overlap("Bajaj DX Dry Iron", sku, "BAJAJ", "") == 0


# ------------------------------------------------------- lpm / ton / star
@pytest.mark.parametrize("sku,brand,words,litres", [
    ("KENSTAR GWH 6LPM", "KENSTAR", [], {6.0}),
    ("LG AC 1.5TON 5STAR", "LG", [], set()),
    ("CROMPTON 1.5-TON", "CROMPTON", [], set()),
    ("LG AC 1.5 TON 5 STAR", "LG", [], set()),
])
def test_lpm_ton_star_tokens(sku, brand, words, litres):
    assert E.sku_parts(sku) == (brand, "")
    assert E.loose_model_terms(sku, brand) == (words, litres)


def test_bare_star_is_still_a_series_word():
    assert E.loose_model_terms("KENSTAR-GWH STAR 6L", "KENSTAR") == (
        ["star"], {6.0})
