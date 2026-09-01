"""Sub-category prediction coverage across EVERY category in category_map.xlsx.

app.detect_category() is the only thing standing between a scraped title and
the storefront's category tree, and it was only ever exercised on LED TVs and
washing machines. This walks all 42 canonical 'Parent > Sub' strings with a
realistic Indian-retail title for each and reports what it actually predicts.

Titles are written the way Amazon.in / Croma / Reliance Digital write them,
because that is what the enricher puts in the `title` column.

    .venv\\Scripts\\python.exe tests\\test_category_coverage.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

import app                                          # noqa: E402

# (expected 'Parent > Sub', listing title as a retailer would write it)
CASES = [
    # ---- Air Conditioner -------------------------------------------------
    ("Air Conditioner > Split AC",
     "Voltas 1.5 Ton 3 Star Fixed Speed Split AC 183 Vectra Prism"),
    ("Air Conditioner > Window AC",
     "Voltas 1.5 Ton 3 Star Window AC 183 Venture Prism"),
    ("Air Conditioner > Inverter AC",
     "LG 1.5 Ton 5 Star AI DUAL Inverter Split AC PS-Q19YNZE"),
    ("Air Conditioner > Non-Inverter AC",
     "Blue Star 1.5 Ton 3 Star Fixed Speed Air Conditioner IA318DNU"),
    # ---- Washing Machines ------------------------------------------------
    ("Washing Machines > Front Load",
     "Samsung 8 kg Front Load Washing Machine WW80T504DAB"),
    ("Washing Machines > Top load",
     "Whirlpool 7 kg Fully Automatic Top Load Washing Machine WHITEMAGIC"),
    ("Washing Machines > semi auto",
     "Godrej 7.5 kg Semi Automatic Top Load Washing Machine WSEDGE"),
    # ---- Water Heaters ---------------------------------------------------
    ("Water Heaters > Geysers",
     "Racold Eterno Pro 15 Litre Storage Water Heater Geyser"),
    ("Water Heaters > Geyser & Accessories",
     "Racold Geyser Accessories Pressure Relief Valve Kit"),
    # ---- LED -------------------------------------------------------------
    ("LED > QLED", "Samsung 55 inch 4K Ultra HD QLED Smart TV QA55Q60C"),
    ("LED > OLED", "LG 55 inch 4K Ultra HD OLED evo Smart TV OLED55C3"),
    ("LED > Qned", "LG 65 inch 4K Ultra HD QNED Smart TV 65QNED80"),
    # a PLAIN LED TV goes to the bare parent - Mini LED is a premium panel
    # tech the listing has to actually claim (a live Full HD Foxsky was
    # shipped under Mini LED when this was the catch-all arm)
    ("LED", "Samsung 32 inch HD Ready Smart LED TV UA32H4500"),
    ("LED", "Foxsky 109 cm (43 Inch) Full HD Smart LED TV, 43FSFHS"),
    ("LED > Mini LED", "TCL 55 inch 4K Mini LED Google TV 55C755"),
    # ---- Kitchen Appliances ----------------------------------------------
    ("Kitchen Appliances > Chimneys",
     "Elica 60 cm 1200 m3/hr Auto Clean Kitchen Chimney WDFL 606 HAC"),
    ("Kitchen Appliances > Microwave Ovens",
     "IFB 20 L Convection Microwave Oven 20SC2"),
    ("Kitchen Appliances > Induction Cooktops",
     "Prestige PIC 16.0 Plus 1900 Watt Induction Cooktop"),
    ("Kitchen Appliances > Gas Stoves & Hobs",
     "Prestige Marvel Plus 3 Burner Glass Top Gas Stove"),
    ("Kitchen Appliances > Juicers, Mixers & Grinders",
     "Bajaj Rex 500 Watt Mixer Grinder with 3 Jars"),
    ("Kitchen Appliances > Toaster & Sandwich Makers",
     "Philips HD2583 830 Watt Pop Up Toaster"),
    ("Kitchen Appliances > Airfryer",
     "Philips HD9200 4.1 Litre Digital Air Fryer"),
    # NOTE: the taxonomy lists kettles TWICE - 'Kitchen Appliances > Kettles'
    # and 'Small Appliances > electric kettle'. detect_category deliberately
    # sends anything ELECTRIC to Small Appliances, which is every kettle sold,
    # so the Kitchen Appliances entry is unreachable in practice. Taxonomy
    # decision, not a code fault - see the duplicate report below.
    ("Small Appliances > electric kettle",
     "Pigeon Amaze Plus 1.5 Litre Electric Kettle Stainless Steel"),
    # ---- Small Appliances ------------------------------------------------
    ("Small Appliances > Electric Iron",
     "Bajaj DX-6 1000 Watt Dry Iron with Non-Stick Soleplate"),
    ("Small Appliances > Voltage Stabilizers & Regulators",
     "V-Guard VG 400 Voltage Stabilizer for Air Conditioner"),
    ("Small Appliances > electric kettle",
     "Prestige PKOSS 1.5 Litre Stainless Steel Electric Kettle"),
    # ---- Cooling & Freezing ----------------------------------------------
    ("Cooling & Freezing > Air Coolers",
     "Symphony Diet 3D 55 Litre Tower Air Cooler"),
    ("Cooling & Freezing > Water Coolers",
     "Blue Star 40 Litre Stainless Steel Water Cooler SDLX"),
    ("Cooling & Freezing > Deep Freezers",
     "Blue Star 300 Litre Double Door Deep Freezer CHFDD300"),
    ("Cooling & Freezing > Commercial Freezers",
     "Celfrost 400 Litre Commercial Freezer Visi Cooler"),
    # ---- Home Appliances -------------------------------------------------
    ("Home Appliances > Refrigerators",
     "LG 190 L 5 Star Direct Cool Single Door Refrigerator GL-D201ASPY"),
    ("Home Appliances > Water Purifiers",
     "Kent Grand Plus 9 Litre RO UV Water Purifier"),
    ("Home Appliances > Air Purifiers",
     "Philips AC1215 Air Purifier with HEPA Filter"),
    ("Home Appliances > Water Dispensers",
     "Voltas Mini Magic Pearl Water Dispenser with Cooling Cabinet"),
    ("Home Appliances > Room Heaters",
     "Bajaj Majesty RX11 2000 Watt Room Heater"),
    ("Home Appliances > Fans",
     "Crompton Hill Briz 1200 mm High Speed Ceiling Fan"),
    ("Home Appliances > sewing machine",
     "Usha Janome Dream Stitch 14 Stitch Sewing Machine"),
    ("Home Appliances > mattress", "Sleepwell Impressions 6 inch Queen Mattress"),
    ("Home Appliances > Trolly Bag",
     "Safari Pentagon 65 cm Polycarbonate Trolley Bag"),
    # ---- Electronics & Computing -----------------------------------------
    ("Electronics & Computing > Laptops",
     "HP 15s 12th Gen Intel Core i5 15.6 inch Laptop"),
    ("Electronics & Computing > Computers (CPUs)",
     "Dell OptiPlex 3000 Desktop PC Intel Core i5 Tower CPU"),
    ("Electronics & Computing > Mobiles",
     "Samsung Galaxy M14 5G Smartphone 6GB RAM 128GB"),
    ("Electronics & Computing > Tablets",
     "Samsung Galaxy Tab A9 8.7 inch Wi-Fi Tablet"),
    # NOTE: 'Electronics & Computing > Electronic Fans' duplicates
    # 'Home Appliances > Fans'. Every fan goes to Home Appliances.
    ("Home Appliances > Fans", "Bajaj Esteem 400 mm Table Fan Electronic"),
]

# Sub-categories the taxonomy declares that detect_category can never return,
# because another entry always claims the same words first. They will sit empty
# on the storefront forever unless the taxonomy or the rules change.
KNOWN_UNREACHABLE = {
    "Kitchen Appliances > Kettles",              # lost to Small Appliances
    "Electronics & Computing > Electronic Fans",  # lost to Home Appliances > Fans
}


def main():
    combos = {v for v in app.CATEGORY_COMBOS.values()}
    combos |= set(app.CATEGORY_PARENTS.values())
    print(f"taxonomy: {len(combos)} canonical strings; {len(CASES)} cases\n")

    right = wrong = unmapped = 0
    misses = []
    for expected, title in CASES:
        parent, sub = app.detect_category(title)
        got, mapped = app.canonicalize_category(parent, sub)
        if not mapped:
            unmapped += 1
            misses.append((title, expected, got, "NOT IN MAP"))
        elif got == expected:
            right += 1
        else:
            wrong += 1
            misses.append((title, expected, got, ""))

    for title, expected, got, note in misses:
        print(f"  MISS {title[:56]}")
        print(f"         expected: {expected}")
        print(f"         got     : {got}  {note}")

    reachable = set()
    for _expected, title in CASES:
        got, mapped = app.canonicalize_category(*app.detect_category(title))
        if mapped:
            reachable.add(got)
    declared = {c for c in combos if ">" in c}
    unreachable = sorted(declared - reachable)
    if unreachable:
        print(f"\n  {len(unreachable)} declared sub-categor(ies) nothing routed to:")
        for c in unreachable:
            note = "  (known duplicate)" if c in KNOWN_UNREACHABLE else ""
            print(f"    - {c}{note}")
    surprises = [c for c in unreachable if c not in KNOWN_UNREACHABLE]

    total = len(CASES)
    print(f"\ncorrect {right}/{total}   wrong {wrong}   unmapped {unmapped}"
          f"   unreachable {len(unreachable)} ({len(surprises)} unexpected)")
    return 1 if (wrong or unmapped or surprises) else 0


def test_taxonomy_coverage():
    """The script's own verdict, enforced under pytest too: every case routes
    to its declared combo and no NEW sub-category is unreachable."""
    assert main() == 0, ("detect_category coverage regressed - see the MISS / "
                         "unreachable lines printed above")


if __name__ == "__main__":
    sys.exit(main())
