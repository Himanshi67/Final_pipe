"""Regression tests for the bugs a live multi-category crawl exposed.

These are the ones no synthetic fixture found - they only appear when real
retail copy and real ERP SKU codes meet the classifier. Every case below is
taken verbatim from the 41-SKU crawl in runs\\multicat-2026-08-11.

    .venv\\Scripts\\python.exe tests\\test_crawl_findings.py

#22 "Top Load" in a water-dispenser title  -> Washing Machines
#23 "Refrigerator" in a dispenser title    -> Refrigerators
#24 detect_category never saw the SKU      -> Home Appliances catch-all
#25 leading category code taken as brand   -> "brand not found in listing"
#26 wattage in a model code read as kg     -> "title lacks capacity"
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

import app                                        # noqa: E402
import sku_enricher as E                          # noqa: E402

# A script prints every failure and reports the tally at the end; under
# pytest a check that only prints is a test that always passes, so check()
# raises instead. Both modes see the same FAIL lines.
UNDER_PYTEST = "pytest" in sys.modules

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")
        if UNDER_PYTEST:
            raise AssertionError(f"{label}: got {got!r}, want {want!r}")


# Real titles, exactly as scraped.
DISPENSERS = [
    ("HWD-3WFMR", "Haier HWD-3WFMR Hot, Cold and Normal Top Load Water "
                  "Dispenser with Cooling Cabinet (White)"),
    ("HWD-3MPFMR", "Haier HWD-3MPFMR Bottled Water Dispenser with Mount "
                   "Refrigerator Floor Standing Digital Display Black"),
    ("HWD-3BPFMR", "Haier HWD-3BPFMR-Floor Mounted Refrigerator Water Dispenser"),
    ("HWD-3MPFS", "Haier HWD-3MPFS Bottled Water Dispenser with Hot, Cold & "
                  "Normal, | Floor Standing, | Digital Display, Black"),
]


def test_22_23_dispensers():
    print("\n#22/#23  a dispenser is a dispenser, whatever the copy says")
    seen = set()
    for sku, title in DISPENSERS:
        got = app.detect_category(title, "", "", sku)
        seen.add(got)
        check(f"{sku} -> Water Dispensers", got,
              ("Home Appliances", "Water Dispensers"))
    check("all four siblings agree (they used to split 3 ways)", len(seen), 1)
    # and the SKU prefix must survive canonicalisation into a real category
    text, mapped = app.canonicalize_category("Home Appliances",
                                             "Water Dispensers")
    check("the result is a real taxonomy string", (text, mapped),
          ("Home Appliances > Water Dispensers", True))
    # a dispenser must never take a washing-machine SKU prefix again - and
    # process_inventory hands prefixed_sku the PARENT (the family string sent
    # every FAMILY_BY_SUB product to the AP- fallback), so pin the parent form
    check("and it takes its parent's prefix, not WM- and not the AP- fallback",
          app.prefixed_sku("HWD-3WFMR", "Home Appliances"), "HA-HWD-3WFMR")


def test_24_sku_is_consulted():
    print("\n#24  the SKU is a fallback when the title says nothing")
    check("KENSTAR-WH MAXIMUS 3L -> Water Heaters",
          app.detect_category("KENSTAR-WH MAXIMUS 3L", "", "", "WH-MAXIMUS-3L"),
          ("Water Heaters", "Geysers"))
    check("an uninformative title + WM- SKU -> Washing Machines",
          app.detect_category("HTW120 178BK", "", "", "WM-HTW120-178BK")[0],
          "Washing Machines")
    check("an uninformative title + SM- SKU -> sewing machine",
          app.detect_category("ANAND DLX", "", "", "SM-ANAND-DLX"),
          ("Home Appliances", "sewing machine"))
    # the title still WINS when it actually says something
    check("a real title still beats the SKU prefix",
          app.detect_category("Samsung 8 kg Front Load Washing Machine",
                              "", "", "AP-WHATEVER"),
          ("Washing Machines", "Front Load"))
    check("an unknown prefix still lands in the catch-all",
          app.detect_category("mystery object", "", "", "ZZ-1234"),
          ("Home Appliances", ""))
    check("no SKU given behaves exactly as before",
          app.detect_category("mystery object"), ("Home Appliances", ""))


def test_25_category_code_is_not_a_brand():
    print("\n#25  a leading category code is not the manufacturer")
    for sku, brand, model in [
            ("WM-HTW120-178BK", "", "HTW120-178BK"),
            ("HWD-3WFMR", "", "3WFMR"),
            ("IC-CJ1360WPC", "", "CJ1360WPC"),
            ("63HNCCC21V9SN(WATER-DISPENSER)", "", "63HNCCC21V9SN"),
            ("SAMSUNG-WA70BG4441YYTL", "SAMSUNG", "WA70BG4441YYTL"),
            ("HAIER-H32K82FX", "HAIER", "H32K82FX"),
            ("WM HAIER HW80-B14959", "HAIER", "HW80-B14959"),
            ("LG-WM-1234ABC", "LG", "1234ABC")]:
        check(f"{sku[:30]:32} -> ({brand!r}, {model!r})",
              E.sku_parts(sku), (brand, model))

    print("\n         ...and the queries it builds stay well formed")
    for sku in ("WM-HTW120-178BK", "HWD-3WFMR", "SAMSUNG-WA70BG4441YYTL"):
        _b, _m, tiers = E.build_queries(sku)
        queries = [pair for tier in tiers for pair in tier]
        bad = [q for q, _d in queries
               if q.startswith(" ") or "  " in q or q.strip() != q]
        check(f"{sku}: no leading/double spaces", bad, [])
        leaked = [q for q, _d in queries if q.upper().startswith(("WM ", "HWD "))]
        check(f"{sku}: the category code is not searched for", leaked, [])
    # an unknown brand must not make assess() reject everything
    rec = {"title": "Haier 8 kg 5 Star Semi Automatic Washing Machine HTW80-178",
           "url_link": "https://www.croma.com/haier-8kg/p/123",
           "sale price": "15334", "regular price": ""}
    verdict, _why = E.assess(rec, "WM-HTW80-178", "HTW80-178", "", 0, 0)
    check("a brandless SKU can still verify on its model code",
          verdict, "strong")


def test_26_wattage_is_not_weight():
    print("\n#26  watts/volts/litres in a model code are not kilograms")
    for sku, model, title in [
            ("ICT-1200W-KISTO", "1200W-KISTO", "Kisto 1200W Induction Cooktop"),
            ("IC-CJ1360WPC", "CJ1360WPC", "Prestige CJ 1360W Induction Cooktop"),
            ("DI-ARDO-1000W", "ARDO-1000W", "Ardo 1000W Dry Iron"),
            ("HWD-3WFMR", "3WFMR", "Haier Water Dispenser")]:
        check(f"{sku:20} yields no kg capacity",
              E.expected_caps(sku, model, title), set())
    print("\n         ...while a real load rating is still read")
    for sku, model, title, want in [
            ("SAMSUNG-WA70BG4441YYTL", "WA70BG4441YYTL",
             "Samsung 7 kg Top Load Washing Machine", {7.0}),
            ("WM-HTW120-178BK", "HTW120-178BK",
             "Haier 12 Kg Top Load Washing Machine", {12.0}),
            ("HAIER-HW80-B14959", "HW80-B14959",
             "Haier 8 Kg Front Load Washing Machine", {8.0}),
            ("HAIER-7.5KG-HWM75", "HWM75",
             "Haier 7.5 kg Washing Machine", {7.5}),
            # letters SANDWICHED between digits are a model series, not a
            # unit - the first-letter bail used to blind the capacity guard
            # for Samsung H-series and LG V-series washers
            ("SAMSUNG-WA70H4200", "WA70H4200",
             "Samsung 7 kg Top Load Washing Machine", {7.0}),
            ("LG-T70VBMB1Z", "T70VBMB1Z",
             "LG 7 kg Top Load Washing Machine", {7.0}),
            # a following SEPARATE word is not a unit either
            ("MODEL-700", "700 WHITE",
             "Generic 7 kg Washing Machine", {7.0})]:
        check(f"{sku:24} -> {sorted(want)}",
              E.expected_caps(sku, model, title), want)


def main():
    test_22_23_dispensers()
    test_24_sku_is_consulted()
    test_25_category_code_is_not_a_brand()
    test_26_wattage_is_not_weight()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
