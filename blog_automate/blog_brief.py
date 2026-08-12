"""blog_brief.py - turn catalogue data into a BRIEF: every fact a post may state.

Deterministic, no LLM, no network. The brief is the source of truth for
everything downstream: the template renderer reads it, the prose writer is
given it and nothing else, and the verifier checks the finished post back
against it. A fact that is not in the brief has no business being in the post.
"""

import csv
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

# One numeric token: 30500 / 30,500 / 30500.00. Used to pull a price out of a
# cell that may also contain currency symbols or stray words.
NUM_TOKEN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
WORD_RE = re.compile(r"[a-z0-9]+")

# "12999-15999", "12999 to 15999", "-500". Any of these means the cell does not
# state one price, so there is no price to quote.
AMBIGUOUS_MONEY_RE = re.compile(r"[-–—]\s*\d|\bto\b", re.IGNORECASE)


def compact(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def to_int(value):
    """A price, or None when the cell does not unambiguously state one.

    Refuses rather than guesses. An earlier version stripped every non-digit,
    which silently turned the range cell '12999-15999' into 1299915999 and
    'Rs. 30,500' into 0 - a wrong price is worse than no price, because the
    price is the one thing a reader acts on."""
    text = compact(value)
    if not text:
        return None
    if AMBIGUOUS_MONEY_RE.search(text):
        return None
    tokens = NUM_TOKEN_RE.findall(text)
    if len(tokens) != 1:                    # no number, or two of them
        return None
    try:
        number = float(tokens[0].replace(",", ""))
    except ValueError:
        return None
    if number <= 0:                         # 'Rs 0' is not a price either
        return None
    return int(round(number))


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


# ---------------------------------------------------------------- word forms

IRREGULAR_SINGULAR = {
    "tvs": "TV", "acs": "AC", "leds": "LED", "ovens": "Oven",
    "boxes": "Box", "watches": "Watch", "brushes": "Brush",
}


def singular(phrase):
    """'Washing Machines' -> 'Washing Machine'. Only the last word changes.

    A bare rstrip('s') turned 'Glass' into 'Gla'; -ss, -us and -is words are
    already singular and must be left alone."""
    phrase = compact(phrase)
    if not phrase:
        return phrase
    head, _, word = phrase.rpartition(" ")
    low = word.lower()
    if low in IRREGULAR_SINGULAR:
        word = IRREGULAR_SINGULAR[low]
    elif low.endswith(("ss", "us", "is")) or not low.endswith("s"):
        pass                                        # already singular
    elif low.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif low.endswith(("ches", "shes", "xes", "zes", "sses")):
        word = word[:-2]
    else:
        word = word[:-1]
    return (head + " " + word).strip() if head else word


def plural(phrase, count):
    """'Split AC' + 5 -> 'Split ACs'. Leaves the phrase alone when count is 1."""
    phrase = compact(phrase)
    if not phrase or count == 1:
        return phrase
    head, _, word = phrase.rpartition(" ")
    low = word.lower()
    if low.endswith("s") and not low.endswith("ss"):
        pass                                        # already plural
    elif low.endswith(("s", "x", "z", "ch", "sh")):
        word += "es"
    elif low.endswith("y") and len(low) > 1 and low[-2] not in "aeiou":
        word = word[:-1] + "ies"
    else:
        word += "s"
    return (head + " " + word).strip() if head else word


# ------------------------------------------------------------------- loading

def read_products(path):
    """(products, malformed_skus) from the WooCommerce CSV the pipeline writes.

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

    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        missing = [c for c in ("sku", "title") if c not in headers]
        if missing:
            raise ValueError(
                f"{path.name} does not look like a WooCommerce CSV from "
                f"build_csv.py - it has no {' or '.join(missing)} column. "
                f"Columns found: {', '.join(headers[:8])}"
                + (" ..." if len(headers) > 8 else ""))
        products, malformed = [], []
        for row in reader:
            # DictReader parks surplus fields under None. A row with more
            # fields than headers is shifted, which means its price column is
            # not its price - silently wrong rather than obviously broken.
            if row.get(None):
                malformed.append(compact(row.get("sku")) or "(no sku)")
                continue
            product = product_from_row(row)
            if product:
                products.append(product)
    return products, malformed


def dedupe(products):
    """(unique, dropped_skus). The same SKU twice in the CSV would otherwise
    appear twice in the post, with the second copy silently winning nothing."""
    seen = {}
    dropped = []
    for product in products:
        key = product["sku"].upper()
        if key in seen:
            dropped.append(product["sku"])
            continue
        seen[key] = product
    return list(seen.values()), dropped


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

    sale_raw = compact(row.get("sale price"))
    regular_raw = compact(row.get("regular price"))
    sale, regular = to_int(sale_raw), to_int(regular_raw)

    def unreadable(raw, parsed):
        """The cell has a number in it that is not a price we can use, as
        opposed to being empty."""
        return bool(raw) and parsed is None and bool(NUM_TOKEN_RE.search(raw))

    # WooCommerce keeps the struck-through price in `regular price`; with no
    # discount the regular price IS what a customer pays. That fallback is
    # only safe when the sale cell is EMPTY - falling back because the sale
    # cell was unreadable quoted the MRP as the selling price, which is a
    # wrong price rather than a missing one.
    price_problem = ""
    if unreadable(sale_raw, sale):
        price_problem = (f"the sale price cell says {sale_raw!r}, which is not "
                         f"a usable price")
        sale = regular = None
    elif sale is None:
        if unreadable(regular_raw, regular):
            price_problem = (f"the regular price cell says {regular_raw!r}, "
                             f"which is not a usable price")
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
        "images": images,
        "slug": compact(row.get("slug")),
        "price_problem": price_problem,
    }


# ----------------------------------------------------------------- selection

def words_of(text):
    return set(WORD_RE.findall(str(text).lower()))


def matches_category(product, needle):
    """Whole-word match, not substring.

    '--category LED' used to match 'Bladeless', 'Sealed' and 'Assembled',
    because the test was `needle in haystack` on raw text."""
    if not compact(needle):
        return True
    wanted = WORD_RE.findall(needle.lower())
    if not wanted:
        return True
    haystack = words_of(" ".join([product["category"], " ".join(product["tags"]),
                                  product["name"]]))
    return all(word in haystack for word in wanted)


def matches_brand(product, needle):
    if not compact(needle):
        return True
    wanted = WORD_RE.findall(needle.lower())
    haystack = words_of(product["brand"])
    return all(word in haystack for word in wanted)


def pick(products, category="", max_price=None, min_price=None, brand="",
         limit=0, skus=()):
    """(chosen, notes) - the products a post is about, plus a line for every
    product that was asked for and not used.

    The explicit-SKU path used to return early, which quietly skipped the
    category/brand/price filters, the limit AND the 'a product with no price
    cannot appear in a post that quotes prices' rule. Both paths now run the
    same gauntlet; only the ordering differs."""
    notes = []

    if skus:
        wanted = [s.strip().upper() for s in skus if s.strip()]
        by_sku = {p["sku"].upper(): p for p in products}
        chosen = []
        for key in wanted:
            if key in by_sku:
                if by_sku[key] not in chosen:
                    chosen.append(by_sku[key])
            else:
                notes.append(f"--sku {key}: not in the catalogue file")
        ordered_by_caller = True
    else:
        chosen = list(products)
        ordered_by_caller = False

    def drop(predicate, reason):
        kept = []
        for product in chosen:
            if predicate(product):
                kept.append(product)
            else:
                notes.append(f"{product['sku']}: {reason}")
        return kept

    if compact(category):
        chosen = drop(lambda p: matches_category(p, category),
                      f"does not match --category {category!r}")
    if compact(brand):
        chosen = drop(lambda p: matches_brand(p, brand),
                      f"does not match --brand {brand!r}")
    # a product with no price cannot appear in a post that quotes prices
    priceless = [p for p in chosen if p["price"] is None]
    for product in priceless:
        notes.append(f"{product['sku']}: "
                     + (product.get("price_problem")
                        or "has no price in the CSV"))
    chosen = [p for p in chosen if p["price"] is not None]
    if max_price is not None:
        chosen = drop(lambda p: p["price"] <= max_price,
                      f"costs more than --max-price {max_price}")
    if min_price is not None:
        chosen = drop(lambda p: p["price"] >= min_price,
                      f"costs less than --min-price {min_price}")

    if not ordered_by_caller:
        # sorted by price so a roundup reads as a ladder rather than in
        # whatever order the CSV happened to be in
        chosen.sort(key=lambda p: p["price"])

    if limit and len(chosen) > limit:
        for product in chosen[limit:]:
            notes.append(f"{product['sku']}: beyond --limit {limit}")
        chosen = chosen[:limit]

    return chosen, notes


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


def spec_values(products, label):
    """The distinct values products state for one axis, in first-seen order."""
    values = []
    for product in products:
        value = compact(product["specs"].get(label))
        if value and value not in values:
            values.append(value)
    return values


def price_span(products):
    prices = [p["price"] for p in products if p["price"] is not None]
    if not prices:
        return None, None
    return min(prices), max(prices)


def category_leaf_for(products, fallback=""):
    """What to call this set of products.

    Taking `products[0]` was wrong: the list is sorted by price, so a mixed
    roundup got named after its cheapest item ('How to Choose a Semi
    Automatic' for a set spanning semi-automatic and front-load machines).
    A mixed set falls back to the shared parent category."""
    leaves, parents = [], set()
    for product in products:
        parts = [compact(part) for part in str(product["category"]).split(">")]
        parts = [part for part in parts if part]
        if not parts:
            continue
        if parts[-1] not in leaves:
            leaves.append(parts[-1])
        parents.add(parts[0])
    if len(leaves) == 1:
        return leaves[0]
    if len(parents) == 1:
        return parents.pop()
    return compact(fallback) or (leaves[0] if leaves else "")


def fingerprint(kind, products):
    """A stable id for 'this post, about exactly these products'. Two runs that
    select different products must not be able to overwrite each other just
    because their titles happen to slugify the same way."""
    key = kind + "|" + "|".join(sorted(p["sku"].upper() for p in products))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def build(kind, products, category="", max_price=None, brand="",
          site_name="Shree Jee Electronics", built_at=None):
    """The brief. Everything a post is allowed to say, and nothing else."""
    low, high = price_span(products)
    brands = []
    for product in products:
        if product["brand"] and product["brand"] not in brands:
            brands.append(product["brand"])

    leaf = category_leaf_for(products, category)
    return {
        "kind": kind,
        "built_at": built_at or datetime.now().strftime("%Y-%m-%d"),
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
        "fingerprint": fingerprint(kind, products),
        "products": products,
    }


# ------------------------------------------------------------------- titling

MODELISH_RE = re.compile(r"^(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])"
                         r"[A-Za-z0-9][A-Za-z0-9-]{3,}$")


def short_name(product):
    """'Lloyd GLS12I3FWAEV' out of 'Lloyd GLS12I3FWAEV 1 Ton 3 Star Inverter
    Split AC White'. Brand plus model code reads as a name; the first four
    words gave things like 'VOLTAS SAC 123V VECTRA'."""
    words = product["name"].split("(")[0].split()
    brand = compact(product["brand"])
    brand_words = {w.lower() for w in brand.split()}
    model = next((w for w in words
                  if MODELISH_RE.match(w) and w.lower() not in brand_words), "")
    if brand and model:
        return f"{brand} {model}"
    if brand:
        rest = [w for w in words if w.lower() not in brand_words][:3]
        return compact(brand + " " + " ".join(rest))
    return " ".join(words[:4])


def headline(brief):
    """The post title, derived from the brief so it can never claim something
    the brief does not contain."""
    leaf = brief["category_leaf"] or "Appliances"
    count = brief["product_count"]
    if brief["kind"] == "comparison":
        names = [short_name(p) for p in brief["products"]]
        if len(names) > 3:
            return " vs ".join(names[:2]) + f" and {len(names) - 2} more"
        return " vs ".join(names)
    if brief["kind"] == "guide":
        return f"How to Choose a {singular(leaf)}: A Buying Guide"
    countable = plural(leaf, count)
    cap = brief["max_price"]
    if cap:
        return f"{count} {countable} Under {rupees(cap)}"
    if brief["price_high"] and brief["price_low"] != brief["price_high"]:
        return (f"{count} {countable} From {brief['price_low_text']} to "
                f"{brief['price_high_text']}")
    if brief["price_high"]:
        return f"{count} {countable} at {brief['price_high_text']}"
    return f"{count} {countable} Worth Knowing About"


def slugify(text, limit=70):
    """Word-boundary truncation - a slug cut mid-word reads as a typo and makes
    two unrelated posts collide more often, not less."""
    text = re.sub(r"[^a-z0-9]+", "-", str(text).lower())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if len(text) <= limit:
        return text
    clipped = text[:limit]
    if "-" in clipped:
        clipped = clipped.rsplit("-", 1)[0]
    return clipped.strip("-")


# ------------------------------------------------------------------- verifier
# `fact_strings` is the allowlist blog_verify.py checks the prose against.

def _add(facts, value):
    text = compact(value)
    if text:
        facts.add(text.lower())


def _add_number(facts, number):
    """Numbers appear in several renderings (32999, 32,999, Rs 32,999), so each
    is registered in all of them - otherwise the verifier would reject the
    post's own correct prices."""
    if number is None:
        return
    _add(facts, str(int(number)))
    _add(facts, f"{int(number):,}")
    _add(facts, rupees(number))
    _add(facts, rupees(number).replace("Rs ", ""))


def naming_facts(product):
    """What a product may be CALLED anywhere in the post. Naming another
    product in the intro is legitimate; quoting its price in someone else's
    blurb is not, so values live in `value_facts` instead."""
    facts = set()
    _add(facts, product["sku"])
    _add(facts, product["name"])
    _add(facts, product["brand"])
    _add(facts, product["category"])
    for tag in product["tags"]:
        _add(facts, tag)
    for label in product["specs"]:
        _add(facts, label)
    return facts


def value_facts(product):
    """The numbers and specification values that belong to ONE product."""
    facts = set()
    _add_number(facts, product["price"])
    _add_number(facts, product["mrp"])
    for value in product["specs"].values():
        _add(facts, value)
    return facts


def fact_strings(brief, sku=None, counts=True, spans=True):
    """Every literal a post is allowed to contain.

    With `sku`, only the shared facts plus that one product's values - so a
    blurb cannot borrow another product's price or capacity. The pooled
    version was checkable but not attributable: every number in the set was
    valid everywhere, which is exactly how a price ends up on the wrong
    product and still passes.

    `counts` and `spans` exist because two brief-level numbers are not
    measurements of anything:

      * product_count - a post about 2 products made '2 Ton' verifiable
      * price_low/price_high - the top of the range IS some product's price,
        so a blurb quoting it was indistinguishable from a blurb quoting the
        wrong product's price

    The caller drops whichever of them cannot apply in the position being
    checked. See blog_verify.check_numbers."""
    facts = set()
    _add(facts, brief["site_name"])
    _add(facts, brief["category_leaf"])
    _add(facts, brief["built_at"])
    if spans:
        _add_number(facts, brief["max_price"])
        _add_number(facts, brief["price_low"])
        _add_number(facts, brief["price_high"])
    if counts:
        _add_number(facts, brief["product_count"])
    for name in brief["brands"]:
        _add(facts, name)
    for product in brief["products"]:
        facts |= naming_facts(product)
        if sku is None or product["sku"] == sku:
            facts |= value_facts(product)
    return facts


def save(brief, path):
    Path(path).write_text(json.dumps(brief, indent=2), encoding="utf-8")
    return path
