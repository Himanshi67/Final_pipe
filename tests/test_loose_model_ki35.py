"""KI-35: a SKU with no model code ('KENSTAR-WH AQUALIA 25L') used to accept
any same-brand page as 'weak' and then pick the best SOURCE, so a complete
amazon sibling ('Aqualia Vogue 10L', 'Star 6L') beat the right flipkart page.
Now its series words and litre size reject siblings and rank the weak hits.
Model-coded SKUs must not change at all. Offline - no network.

    .venv\\Scripts\\python.exe -m pytest tests\\test_loose_model_ki35.py -q
"""

import contextlib
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sku_enricher as E                          # noqa: E402

AQUALIA = "KENSTAR-WH AQUALIA 25L"
JETT = "KENSTAR-GWH JETT 6L"

FK_AQUALIA = ("https://www.flipkart.com/kenstar-aqualia-25l-25-l-storage-"
              "water-geyser/p/itmbc5869542668f")
FK_JETT = ("https://www.flipkart.com/kenstar-jett-max-6l-6-l-gas-water-"
           "geyser/p/itm2070070e3ba47")


@pytest.fixture(autouse=True)
def _quiet_log(monkeypatch):
    monkeypatch.setattr(E, "log", lambda *a, **k: None)


def rec(title, url="https://www.amazon.in/dp/B0TEST0001", price="₹9,990",
        n_img=0, mrp=""):
    return {"title": title, "url_link": url, "sale price": price,
            "regular price": mrp, "specs": "",
            "image_url": ",".join(f"https://img.example/{i}.jpg"
                                  for i in range(n_img))}


def verdict(r, sku):
    brand, model = E.sku_parts(sku)
    return E.assess(r, sku, model, brand, 0, 0)


# ------------------------------------------------------------- parsing
def test_gwh_is_a_category_code():
    assert "GWH" in E.CATEGORY_CODES
    assert "WH" in E.CATEGORY_CODES


@pytest.mark.parametrize("sku,words,litres", [
    (AQUALIA, ["aqualia"], {25.0}),
    (JETT, ["jett"], {6.0}),
])
def test_no_model_code_but_series_and_size(sku, words, litres):
    brand, model = E.sku_parts(sku)
    assert (brand, model) == ("KENSTAR", "")
    assert E.loose_model_terms(sku, brand) == (words, litres)


def test_litre_spellings():
    # regex unit test only; since KI-38 a size token in a SKU ('25LTR',
    # 'AQUAGENIE-25L') is never the model code (tests/test_sku_parts_ki38.py)
    for s in ("25L", "25 L", "25-litre", "25 Ltr", "25 ltrs", "25 Liters"):
        assert [float(x) for x in E.LITRE_RE.findall(s)] == [25.0], s
    assert E.LITRE_RE.findall("5 Star rating, 2000 W") == []


@pytest.mark.parametrize("s", ["12 L/min", "12 L / min", "12 LPM", "12 lpm",
                               "12L/min"])
def test_flow_rate_is_not_a_litre_size(s):
    assert E.LITRE_RE.findall(s) == []


def test_flow_rate_title_is_not_a_size_reject():
    # electric (WH) SKU: L/min is not a tank size (KI-39 changes GWH only)
    v, reason = verdict(rec("Kenstar Aqualia 10 L/min Storage Water Geyser"),
                        AQUALIA)
    assert "size mismatch" not in reason
    assert v == "weak"


# ------------------------------------------------------- KI-39 gas flow
@pytest.mark.parametrize("sku,gas", [
    (JETT, True), ("BAJAJ GWH SHAKTI 15L", True), (AQUALIA, False),
    ("KENSTAR-GWHX 6L", False),
    ("KENSTAR GWH(6L) JETT", True), ("KENSTAR_GWH JETT 6L", True),
])
def test_sku_is_gas(sku, gas):
    assert E.sku_is_gas(sku) is gas


@pytest.mark.parametrize("s", ["6 L/min", "6 L / min", "6 LPM", "6 lpm",
                               "6lpm", "6L/min", "6 Ltr/minute", "6-LPM",
                               "6 Litre/Mins", "6 L/mins"])
def test_pure_flow_spellings_count_on_gas_only(s):
    assert E.size_values(s, True) == [6.0], s
    assert E.size_values(s, False) == [], s


def test_litres_per_minute_reads_on_both():
    # 'per minute' has no '/', so LITRE_RE reads it on an electric SKU too
    assert E.size_values("6 litres per minute", True) == [6.0]
    assert E.size_values("6 litres per minute", False) == [6.0]


def test_electric_flow_rate_is_not_a_size():
    assert E.size_values("6 L/min", False) == []


def test_bracketed_gas_sku_rejects_other_flow():
    v, reason = verdict(rec("Kenstar Jett 10 L/min Gas Geyser"),
                        "KENSTAR GWH(6L) JETT")
    assert v == ""
    assert reason == "size mismatch: listing says 10 L, SKU says 6 L"


def test_gas_sku_rejects_hyphenated_lpm():
    v, reason = verdict(rec("Kenstar Jett Gas Geyser 10-LPM"), JETT)
    assert v == ""
    assert reason == "size mismatch: listing says 10 L, SKU says 6 L"


def test_electric_l_mins_is_not_a_size_reject():
    v, reason = verdict(rec("Kenstar Aqualia 10 L/mins Storage Water Geyser"),
                        AQUALIA)
    assert "size mismatch" not in reason
    assert v == "weak"


def test_flow_sku_parses_size():
    sku = "KENSTAR GWH JETT 6 LPM"
    brand, model = E.sku_parts(sku)
    assert (brand, model) == ("KENSTAR", "")
    assert E.loose_model_terms(sku, brand) == (["jett"], {6.0})


@pytest.mark.parametrize("title", [
    "Kenstar Jett 6 L/min Gas Geyser",
    "Kenstar Jett 6 LPM Gas Water Heater",
    "Kenstar JETT MAX 6L Gas Water Geyser",
    "Kenstar Jett 6 Ltr Gas Geyser",
    "Kenstar Jett 6 Liter Gas Geyser",
    "Kenstar Jett Gas Water Heater",
])
def test_gas_flow_rate_is_the_size(title):
    assert verdict(rec(title), JETT) == ("weak", "")


@pytest.mark.parametrize("title,says", [
    ("Kenstar Jett 10 L/min Gas Geyser", "10"),
    ("Kenstar Jett 10L Gas Geyser", "10"),
])
def test_gas_flow_mismatch_is_rejected(title, says):
    v, reason = verdict(rec(title), JETT)
    assert v == ""
    assert reason == f"size mismatch: listing says {says} L, SKU says 6 L"


def test_gas_sized_title_ranks_above_unsized():
    brand, model = E.sku_parts(JETT)
    sized = E.loose_overlap("Kenstar Jett 6 L/min Gas Geyser", JETT, brand, model)
    bare = E.loose_overlap("Kenstar Jett Gas Water Heater", JETT, brand, model)
    assert (sized, bare) == (2, 1)


@pytest.mark.parametrize("sku", ["KENSTAR-WH AQUALIA 25 L",
                                 "KENSTAR-WH AQUALIA(25L)",
                                 "KENSTAR-WH AQUALIA 25 Litre"])
def test_spaced_or_bracketed_size(sku):
    brand, model = E.sku_parts(sku)
    assert model == ""
    assert E.loose_model_terms(sku, brand) == (["aqualia"], {25.0})


@pytest.mark.parametrize("sku,words,litres", [
    ("BAJAJ-GWH NEW SHAKTI 15L", ["shakti"], {15.0}),
    ("KENSTAR-WH AQUALIA WITH WHITE 25L", ["aqualia"], {25.0}),
    ("FOXSKY-32 FSELS PRO", ["fsels", "pro"], set()),
])
def test_stop_words_are_not_series_words(sku, words, litres):
    brand, _ = E.sku_parts(sku)
    assert E.loose_model_terms(sku, brand) == (words, litres)


# ------------------------------------------------------------- assess
@pytest.mark.parametrize("title,sku,why", [
    ("Kenstar Aqualia Vogue 10L Storage Water Geyser 2000 W", AQUALIA,
     "size mismatch"),
    ("KENSTAR Star 6L Instant Gas Water Geyser", JETT,
     "series word not in title"),
])
def test_siblings_are_rejected(title, sku, why):
    v, reason = verdict(rec(title), sku)
    assert v == ""
    assert reason.startswith(why), reason


def test_star_does_not_match_inside_kenstar():
    # 6 L matches, so only the whole-word check can reject this
    v, reason = verdict(rec("KENSTAR 6L Gas Water Geyser"), "KENSTAR-GWH STAR 6L")
    assert v == "" and "series word" in reason
    assert E.loose_overlap("KENSTAR 6L Gas Water Geyser",
                           "KENSTAR-GWH STAR 6L", "KENSTAR", "") == 1


@pytest.mark.parametrize("title,url,sku", [
    ("Kenstar Aqualia 25L 25 L Storage Water Geyser (Aqualia 25L, White)",
     FK_AQUALIA, AQUALIA),
    ("Kenstar Jett Max 6L 6 L Gas Water Geyser (Jett Max 6L, White)",
     FK_JETT, JETT),
])
def test_right_pages_stay_weak(title, url, sku):
    assert verdict(rec(title, url), sku) == ("weak", "")


def test_one_series_word_is_enough():
    # a colour word in the SKU must not reject the right page
    sku = "KENSTAR-WH AQUALIA WHITE 25L"
    assert verdict(rec("Kenstar Aqualia 25 L Storage Geyser"), sku)[0] == "weak"


def test_no_size_sku_skips_the_series_word_reject():
    # without a litre size a missing word is weak evidence, not a reject
    r = rec("USHA Umang With A Manual Sewing Machine")
    assert verdict(r, "UMANG-COMPOSITE") == ("weak", "")


def test_no_litre_in_title_is_not_a_size_reject():
    assert verdict(rec("Kenstar Aqualia Storage Geyser"), AQUALIA)[0] == "weak"


# ------------------------------------------------------- model-coded SKUs
MODEL_SKU = "SAMSUNG-WA70BG4441YYTL"


# expected values taken from HEAD before KI-35 (verified equal), hardcoded
@pytest.mark.parametrize("title,expected", [
    ("Samsung 7 kg Fully Automatic Top Load WA70BG4441YYTL", ("strong", "")),
    ("Samsung 7 kg Fully Automatic Top Load Washing Machine",
     ("weak", "model code not verified")),
    ("Samsung 8 kg Fully Automatic WA80BG4441YYTL",
     ("", "capacity mismatch: listing says 8 kg, SKU implies [7.0]")),
    ("Samsung 10L Something",
     ("", "title lacks capacity/model info - cannot verify")),
    ("Samsung WA70BG4441 7 kg Top Load", ("weak", "model code not verified")),
])
def test_model_coded_sku_unchanged(title, expected):
    brand, model = E.sku_parts(MODEL_SKU)
    assert model == "WA70BG4441YYTL"
    assert E.loose_overlap(title, MODEL_SKU, brand, model) == 0
    r = rec(title, "https://www.amazon.in/dp/B0TEST0002")
    assert E.assess(r, MODEL_SKU, model, brand, 0, 0) == expected


# ------------------------------------------------------- the weak pick
@pytest.fixture
def lookup(monkeypatch):
    """lookup_sku with fetch/extract/search stubbed. assess() is the real one."""
    monkeypatch.setattr(E, "IMAGE_CHECK", False)
    monkeypatch.setattr(E, "PW_ENABLED", False)
    monkeypatch.setattr(E, "MERGE_BUDGET", 0)
    monkeypatch.setattr(E, "polite_sleep", lambda *a, **k: None)
    monkeypatch.setattr(E, "URL_INDEX_ON", False)
    monkeypatch.setattr(E, "SITE_SEARCH_ON", False)
    monkeypatch.setattr(E, "SITE_SEARCH_RULES", [])
    monkeypatch.setattr(E, "_COOLDOWN", {})
    pages = {}
    monkeypatch.setattr(E, "fetch", lambda u: (
        (f"<html>{u}</html>", "") if u in pages else (None, "http 404")))
    monkeypatch.setattr(E, "extract_page", lambda u, html: dict(pages[u]))
    monkeypatch.setattr(E, "web_search", lambda q: [
        {"href": u, "title": p["title"]} for u, p in pages.items()])

    def run(sku):
        with contextlib.redirect_stdout(io.StringIO()):
            return E.lookup_sku(sku)
    run.pages = pages
    return run


@pytest.mark.parametrize("sku,better_source,right", [
    (AQUALIA, "Kenstar Aqualia Storage Water Geyser",
     ("Kenstar Aqualia 25L 25 L Storage Water Geyser", FK_AQUALIA)),
    (JETT, "KENSTAR Jett Gas Water Geyser",
     ("Kenstar Jett Max 6L 6 L Gas Water Geyser", FK_JETT)),
])
def test_weak_pick_prefers_title_fit(lookup, sku, better_source, right):
    amz = "https://www.amazon.in/dp/B0TEST0003"
    # amazon: complete, but its title names only the series word
    lookup.pages[amz] = rec(better_source, amz, n_img=8, mrp="₹12,990")
    # flipkart: thin, but its title names series word AND size
    lookup.pages[right[1]] = rec(right[0], right[1], price="", n_img=1)
    assert E.rec_score(lookup.pages[amz]) > E.rec_score(lookup.pages[right[1]])
    out = lookup(sku)
    assert out["url_link"] == right[1]
    assert out["status"].startswith("CHECK MANUALLY (flipkart.com)")
