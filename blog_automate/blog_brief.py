"""blog_brief.py - turn catalogue data into a BRIEF: every fact a post may state.

Deterministic, no LLM, no network. The brief is the source of truth for
everything downstream: the template renderer reads it, the prose writer is
given it and nothing else, and the verifier checks the finished post back
against it. A fact that is not in the brief has no business being in the post.
"""

import csv
import json
import re
from datetime import datetime
from pathlib import Path

MONEY_RE = re.compile(r"[^\d.]")


def compact(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def to_int(value):
    text = MONEY_RE.sub("", str(value or ""))
    if not text:
        return None
    try:
        return int(round(float(text)))
    except ValueError:
        return None


def rupees(value):
    """75000 -> 'Rs 75,000' with Indian digit grouping."""
    if value is None:
        return ""
    s = str(int(value))
    if len(s) <= 3:
        return f"Rs {s}"
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return "Rs " + ",".join(parts + [tail])


def read_products(path):
    """Load the WooCommerce CSV the pipeline generates into product records.

    Only the columns the pipeline actually writes are read, so a CSV produced
    with or without --seo works either way."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No such catalogue file: {path}")

    if path.suffix.lower() in (".xlsx", ".xls"):
        raise ValueError(
            f"{path.name} is a spreadsheet. Build the WooCommerce CSV first "
            f"(build_csv.py) and point --source at that - the CSV is where "
            f"categories, tags and attributes actually exist.")

    products = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            product = product_from_row(row)
            if product:
                products.append(product)
    return products


def product_from_row(row):
    sku = compact(row.get("sku"))
    name = compact(row.get("title"))
    if not sku or not name:
        return None

    specs = {}
    index = 1
    while f"Attribute {index} name" in row:
        label = compact(row.get(f"Attribute {index} name"))
        value = compact(row.get(f"Attribute {index} value(s)"))
        if label and value:
            specs[label] = value
        index += 1

    sale = to_int(row.get("sale price"))
    regular = to_int(row.get("regular price"))
    # WooCommerce keeps the struck-through price in `regular price`; with no
    # discount the regular price IS what a customer pays
    if sale is None:
        sale, regular = regular, None
    if regular is not None and sale is not None and regular <= sale:
        regular = None

    images = [u.strip() for u in str(row.get("Images") or "").split(",") if u.strip()]

    return {
        "sku": sku,
        "name": name,
        "brand": compact(row.get("Brands")),
        "category": compact(row.get("categories")),
        "tags": [t.strip() for t in str(row.get("Tags") or "").split(",") if t.strip()],
        "price": sale,
        "price_text": rupees(sale),
        "mrp": regular,
        "mrp_text": rupees(regular) if regular else "",
        "specs": specs,
        "image": images[0] if images else "",
        "slug": compact(row.get("slug")),
    }


def matches_category(product, needle):
    if not needle:
        return True
    haystack = " ".join([product["category"], " ".join(product["tags"]),
                         product["name"]]).lower()
    return needle.lower() in haystack


def pick(products, category="", max_price=None, min_price=None, brand="",
         limit=0, skus=()):
    """The products a post is about. Sorted by price so a roundup reads as a
    ladder rather than in whatever order the CSV happened to be in."""
    if skus:
        wanted = {s.strip().upper() for s in skus}
        chosen = [p for p in products if p["sku"].upper() in wanted]
        # keep the order the caller asked for - a comparison of A vs B should
        # not silently become B vs A
        order = {s.strip().upper(): i for i, s in enumerate(skus)}
        chosen.sort(key=lambda p: order.get(p["sku"].upper(), 99))
        return chosen

    chosen = [p for p in products if matches_category(p, category)]
    if brand:
        chosen = [p for p in chosen if brand.lower() in p["brand"].lower()]
    if max_price is not None:
        chosen = [p for p in chosen if p["price"] is not None and p["price"] <= max_price]
    if min_price is not None:
        chosen = [p for p in chosen if p["price"] is not None and p["price"] >= min_price]
    # a product with no price cannot appear in a post that quotes prices
    chosen = [p for p in chosen if p["price"] is not None]
    chosen.sort(key=lambda p: p["price"])
    return chosen[:limit] if limit else chosen


def common_spec_axes(products):
    """Spec labels that EVERY product in the set states - the only ones a
    comparison table can honestly use as rows."""
    if not products:
        return []
    shared = set(products[0]["specs"])
    for product in products[1:]:
        shared &= set(product["specs"])
    # stable, readable order: the first product's order, filtered
    return [label for label in products[0]["specs"] if label in shared]


def price_span(products):
    prices = [p["price"] for p in products if p["price"] is not None]
    if not prices:
        return None, None
    return min(prices), max(prices)


def build(kind, products, category="", max_price=None, brand="", site_name="Shree Jee Electronics"):
    """The brief. Everything a post is allowed to say, and nothing else."""
    low, high = price_span(products)
    brands = []
    for product in products:
        if product["brand"] and product["brand"] not in brands:
            brands.append(product["brand"])

    leaf = ""
    if products:
        leaf = compact(str(products[0]["category"]).split(">")[-1])

    return {
        "kind": kind,
        "built_at": datetime.now().strftime("%Y-%m-%d"),
        "site_name": site_name,
        "category_filter": category,
        "category_leaf": leaf or category,
        "brand_filter": brand,
        "max_price": max_price,
        "product_count": len(products),
        "price_low": low,
        "price_low_text": rupees(low),
        "price_high": high,
        "price_high_text": rupees(high),
        "brands": brands,
        "common_specs": common_spec_axes(products),
        "products": products,
    }


def headline(brief):
    """The post title, derived from the brief so it can never claim something
    the brief does not contain."""
    leaf = brief["category_leaf"] or "Appliances"
    count = brief["product_count"]
    if brief["kind"] == "comparison":
        names = [p["name"].split("(")[0].strip() for p in brief["products"]]
        short = [" ".join(n.split()[:4]) for n in names]
        return " vs ".join(short[:3])
    if brief["kind"] == "guide":
        return f"How to Choose a {leaf.rstrip('s')}: A Buying Guide"
    cap = brief["max_price"]
    if cap:
        return f"{count} {leaf} Under {rupees(cap)}"
    if brief["price_high"]:
        return f"{count} {leaf} From {brief['price_low_text']} to {brief['price_high_text']}"
    return f"{count} {leaf} Worth Knowing About"


def slugify(text):
    text = re.sub(r"[^a-z0-9]+", "-", str(text).lower())
    return re.sub(r"-{2,}", "-", text).strip("-")[:70]


def fact_strings(brief):
    """Every literal a post is allowed to contain, for the verifier.

    Numbers appear in several renderings (32999, 32,999, Rs 32,999), so each
    is registered in all of them - otherwise the verifier would reject the
    post's own correct prices."""
    facts = set()

    def add(value):
        text = compact(value)
        if text:
            facts.add(text.lower())

    def add_number(number):
        if number is None:
            return
        add(str(int(number)))
        add(f"{int(number):,}")
        add(rupees(number))
        add(rupees(number).replace("Rs ", ""))

    add(brief["site_name"])
    add(brief["category_leaf"])
    add(brief["built_at"])
    add_number(brief["max_price"])
    add_number(brief["price_low"])
    add_number(brief["price_high"])
    add_number(brief["product_count"])
    for b in brief["brands"]:
        add(b)
    for product in brief["products"]:
        add(product["sku"])
        add(product["name"])
        add(product["brand"])
        add(product["category"])
        add_number(product["price"])
        add_number(product["mrp"])
        for label, value in product["specs"].items():
            add(label)
            add(value)
        for tag in product["tags"]:
            add(tag)
    return facts


def save(brief, path):
    Path(path).write_text(json.dumps(brief, indent=2), encoding="utf-8")
    return path
