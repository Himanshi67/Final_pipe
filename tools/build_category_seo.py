"""Generate category_seo.xlsx - curated tags and SEO keywords per category.

The seed data below is HAND-WRITTEN, not derived from product titles, because
tags exist to group products the way a customer searches, and a customer
searching "5 star inverter ac" never types the model code. Regenerating
overwrites the sheet, so edit the sheet for one-off tweaks and edit this file
when a change should survive a rebuild.

    .venv\\Scripts\\python.exe tools\\build_category_seo.py
"""
import os
import sys

import openpyxl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import client_config                      # noqa: E402  (after sys.path)

# The sheet belongs to a client, not to the repo root (Phase 1 item 1).
# `--client <slug>` / $PIPELINE_CLIENT pick which one gets rebuilt.
OUT = str(client_config.load_client().category_seo)

# parent, sub, tags, focus keyword, supporting keywords
SEED = [
    ("Air Conditioner", "Inverter AC", "inverter ac, split inverter ac, 5 star inverter ac, energy saving ac, low power consumption ac", "inverter ac", "buy inverter ac online, best inverter ac india, 1.5 ton inverter ac price"),
    ("Air Conditioner", "Non-Inverter AC", "non inverter ac, fixed speed ac, budget ac, 3 star ac", "non inverter ac", "fixed speed air conditioner, budget ac price india"),
    ("Air Conditioner", "Split AC", "split ac, wall mounted ac, 1.5 ton split ac, copper condenser ac, 5 star split ac", "split ac", "split ac price, 1.5 ton split ac, best split ac india"),
    ("Air Conditioner", "Window AC", "window ac, compact ac, 1 ton window ac, budget air conditioner", "window ac", "window ac price, 1 ton window ac, window air conditioner india"),

    ("Cooling & Freezing", "Air Coolers", "air cooler, desert cooler, personal cooler, room cooler, honeycomb pad cooler", "air cooler", "desert air cooler price, best room cooler india"),
    ("Cooling & Freezing", "Commercial Freezers", "commercial freezer, display freezer, glass top freezer, shop freezer", "commercial freezer", "commercial freezer price, display freezer for shop"),
    ("Cooling & Freezing", "Deep Freezers", "deep freezer, chest freezer, double door deep freezer, ice cream freezer", "deep freezer", "deep freezer price, chest freezer india"),
    ("Cooling & Freezing", "Water Coolers", "water cooler, commercial water cooler, storage water cooler, water cooler cum purifier", "water cooler", "commercial water cooler price, water cooler for office"),

    ("Electronics & Computing", "Computers (CPUs)", "desktop computer, cpu tower, assembled desktop, home office pc", "desktop computer", "desktop cpu price, assembled pc india"),
    ("Electronics & Computing", "Electronic Fans", "rechargeable fan, usb fan, portable fan, table fan electric", "rechargeable fan", "rechargeable fan price, portable usb fan"),
    ("Electronics & Computing", "Laptops", "laptop, thin and light laptop, student laptop, office laptop", "laptop", "laptop price india, best laptop under 50000"),
    ("Electronics & Computing", "Mobiles", "smartphone, mobile phone, android phone, 5g phone", "smartphone", "mobile phone price, best 5g phone india"),
    ("Electronics & Computing", "Tablets", "tablet, android tablet, tablet with keyboard, kids tablet", "tablet", "tablet price india, best android tablet"),

    ("Home Appliances", "Air Purifiers", "air purifier, hepa air purifier, room air purifier, pm2.5 air purifier", "air purifier", "air purifier price india, hepa purifier for home"),
    ("Home Appliances", "Fans", "ceiling fan, table fan, pedestal fan, wall fan, bldc fan", "ceiling fan", "ceiling fan price, bldc fan india, pedestal fan"),
    ("Home Appliances", "Refrigerators", "refrigerator, fridge, double door refrigerator, single door fridge, frost free refrigerator, inverter refrigerator", "refrigerator", "fridge price india, double door refrigerator, best refrigerator"),
    ("Home Appliances", "Room Heaters", "room heater, oil filled radiator, fan heater, halogen heater", "room heater", "room heater price, oil filled heater india"),
    ("Home Appliances", "Trolly Bag", "trolley bag, luggage trolley, cabin bag, travel suitcase", "trolley bag", "trolley bag price, cabin luggage india"),
    ("Home Appliances", "Water Dispensers", "water dispenser, hot and cold water dispenser, bottled water dispenser, floor standing dispenser", "water dispenser", "water dispenser price, hot and cold dispenser"),
    ("Home Appliances", "Water Purifiers", "water purifier, ro water purifier, uv water purifier, ro uv uf purifier, alkaline water purifier", "water purifier", "ro water purifier price, best water purifier india"),
    ("Home Appliances", "mattress", "mattress, orthopedic mattress, memory foam mattress, spring mattress, queen size mattress", "mattress", "mattress price india, memory foam mattress"),
    ("Home Appliances", "sewing machine", "sewing machine, electric sewing machine, portable sewing machine, tailoring machine", "sewing machine", "sewing machine price india, electric sewing machine"),

    ("Kitchen Appliances", "Airfryer", "air fryer, oil free fryer, digital air fryer, healthy fryer", "air fryer", "air fryer price india, best air fryer"),
    ("Kitchen Appliances", "Chimneys", "kitchen chimney, auto clean chimney, filterless chimney, wall mounted chimney, 90cm chimney", "kitchen chimney", "chimney price india, auto clean chimney 90cm"),
    ("Kitchen Appliances", "Gas Stoves & Hobs", "gas stove, built in hob, 3 burner gas stove, 4 burner hob, toughened glass stove", "gas stove", "gas stove price, 3 burner gas stove india"),
    ("Kitchen Appliances", "Induction Cooktops", "induction cooktop, induction stove, portable induction, 2000w induction", "induction cooktop", "induction cooktop price india, best induction stove"),
    ("Kitchen Appliances", "Juicers, Mixers & Grinders", "mixer grinder, juicer mixer grinder, 750w mixer grinder, wet grinder", "mixer grinder", "mixer grinder price, juicer mixer grinder india"),
    ("Kitchen Appliances", "Kettles", "electric kettle, stainless steel kettle, 1.5 litre kettle, tea kettle", "electric kettle", "electric kettle price india, stainless steel kettle"),
    ("Kitchen Appliances", "Microwave Ovens", "microwave oven, convection microwave, solo microwave, grill microwave", "microwave oven", "microwave oven price india, convection microwave"),
    ("Kitchen Appliances", "Toaster & Sandwich Makers", "sandwich maker, pop up toaster, grill toaster, breakfast maker", "sandwich maker", "sandwich maker price, pop up toaster india"),

    ("LED", "Mini LED", "mini led tv, 4k mini led tv, premium led tv, mini led backlight", "mini led tv", "mini led tv price india, 4k mini led television"),
    ("LED", "OLED", "oled tv, 4k oled tv, self lit oled, premium oled television", "oled tv", "oled tv price india, best oled television"),
    ("LED", "QLED", "qled tv, 4k qled tv, quantum dot tv, smart qled tv", "qled tv", "qled tv price india, 4k qled television"),
    ("LED", "Qned", "qned tv, 4k qned tv, quantum nanocell tv, lg qned", "qned tv", "qned tv price india, quantum nanocell television"),

    ("Small Appliances", "Electric Iron", "steam iron, dry iron, electric iron, cordless iron, garment steamer", "steam iron", "steam iron price india, best dry iron"),
    ("Small Appliances", "Voltage Stabilizers & Regulators", "voltage stabilizer, ac stabilizer, fridge stabilizer, automatic voltage regulator", "voltage stabilizer", "stabilizer price india, ac voltage stabilizer"),
    ("Small Appliances", "electric kettle", "electric kettle, cordless kettle, stainless steel electric kettle, 1.8 litre kettle", "electric kettle", "electric kettle price india, cordless kettle"),

    ("Washing Machines", "Front Load", "front load washing machine, fully automatic front load, inverter washing machine, 6kg front load", "front load washing machine", "front load washing machine price, best front load washer india"),
    ("Washing Machines", "Top load", "top load washing machine, fully automatic top load, 7kg top load washer", "top load washing machine", "top load washing machine price, fully automatic top load"),
    ("Washing Machines", "semi auto", "semi automatic washing machine, twin tub washing machine, 7kg semi automatic", "semi automatic washing machine", "semi automatic washing machine price india"),

    ("Water Heaters", "Geyser & Accessories", "geyser accessories, water heater pipe, geyser installation kit, pressure relief valve", "geyser accessories", "geyser installation kit, water heater accessories india"),
    ("Water Heaters", "Geysers", "geyser, water heater, storage geyser, instant water heater, 15 litre geyser", "geyser", "geyser price india, storage water heater, instant geyser"),
]

HEADERS = ["Parent Category", "Sub-Category", "Tags",
           "Focus Keyword", "SEO Keywords"]


def main():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "category_seo"
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.font = openpyxl.styles.Font(bold=True)
    for row in SEED:
        ws.append(list(row))
    widths = (22, 30, 68, 28, 56)
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(index)].width = width
    ws.freeze_panes = "A2"
    wb.save(OUT)
    print(f"wrote {OUT}")
    print(f"{len(SEED)} categories")

    # every seeded category must exist in the taxonomy, or its tags never apply
    sys.path.insert(0, ROOT)
    import app
    missing = [f"{p} > {s}" for p, s, *_ in SEED
               if app._cat_norm(f"{p} > {s}") not in app.CATEGORY_COMBOS]
    if missing:
        print(f"\nWARNING - {len(missing)} not found in category_map.xlsx:")
        for m in missing:
            print(f"  {m}")
    else:
        print("all seeded categories exist in category_map.xlsx")

    covered = {app._cat_norm(f"{p} > {s}") for p, s, *_ in SEED}
    gaps = sorted({v for k, v in app.CATEGORY_COMBOS.items()
                   if ">" in v and app._cat_norm(v) not in covered})
    if gaps:
        print(f"\n{len(gaps)} taxonomy categories have NO curated tags yet:")
        for g in gaps:
            print(f"  {g}")
    else:
        print("every taxonomy category has curated tags")


if __name__ == "__main__":
    main()
