"""blog_verify.py - check finished prose back against the brief.

This is what makes the hybrid model safe to run. The prose layer is told not
to state facts; this checks whether it did anyway, mechanically, with no model
in the loop:

  * every number in the prose must appear in the brief - digits AND words,
    because 'forty-three inches' is exactly as wrong as '43 inches'
  * a number inside a product's blurb must belong to THAT product
  * every model code and brand must appear in the brief
  * a fixed list of unsupportable claims is rejected outright

It runs on the generated prose whether a model or a person wrote it, and a
failure holds the post. There is no "probably fine" verdict - a number that
cannot be traced to the brief is a defect, because the one thing a reader will
act on is the price.
"""

import os
import re
from pathlib import Path

from blog_brief import compact, fact_strings

# Words that state something the catalogue cannot back. Checked case-
# insensitively against the prose, whoever wrote it.
BANNED_CLAIMS = (
    "best price", "lowest price", "cheapest", "unbeatable", "guaranteed",
    "guarantee", "free delivery", "free shipping", "free installation",
    "limited time", "limited period", "hurry", "act now", "while stocks last",
    "number one", "#1", "no. 1", "award-winning", "award winning",
    "industry leading", "industry-leading", "market leader", "best-selling",
    "best selling", "top rated", "top-rated", "highest rated",
    "money back", "money-back", "lifetime warranty", "no questions asked",
    "cheaper than", "beats the competition", "unmatched", "world class",
    "world-class", "won't find it for less", "wont find it for less",
    "unrivalled", "unrivaled", "second to none", "risk free", "risk-free",
    "sale ends", "last chance", "clearance", "doorstep delivery",
    "easy returns", "no cost emi", "instant cashback",
)

NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# A unit turns an innocent small number into a claim. "one of the models" is
# ordinary English; "one ton" is a specification, and it has to be in the
# brief like any other. This is why 0-10 are no longer free.
UNIT_WORDS = (
    "star", "stars", "ton", "tons", "tonne", "tonnes", "kg", "kgs", "kilo",
    "kilos", "kilogram", "kilograms", "litre", "litres", "liter", "liters",
    "l", "ml", "inch", "inches", "cm", "mm", "metre", "metres", "meter",
    "meters", "ft", "feet", "watt", "watts", "w", "kw", "volt", "volts", "v",
    "amp", "amps", "year", "years", "yr", "yrs", "month", "months", "hour",
    "hours", "hr", "hrs", "minute", "minutes", "min", "day", "days", "hz",
    "rpm", "db", "decibel", "decibels", "degree", "degrees", "percent", "pc",
    "burner", "burners", "door", "doors", "seat", "seater", "blade", "blades",
    "speed", "speeds", "mode", "modes", "program", "programs", "programme",
    "programmes", "cycle", "cycles", "shelf", "shelves", "rack", "racks",
    "zone", "zones", "channel", "channels", "port", "ports", "hdmi", "usb",
    "bhp", "gb", "tb", "mp", "k", "p", "hp", "sqft", "square",
)
UNIT_AFTER_RE = re.compile(r"^[\s\-]{0,2}(?:%|" + "|".join(UNIT_WORDS) + r")\b",
                           re.IGNORECASE)
# 'Rs 4' / '₹4' - a currency marker makes any number a price claim.
MONEY_BEFORE_RE = re.compile(r"(?:rs\.?|inr|₹)\s*$", re.IGNORECASE)

SMALL_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
TENS_WORDS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40,
              "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
              "ninety": 90}
SCALE_WORDS = {"hundred": 100, "thousand": 1000, "lakh": 100000,
               "lakhs": 100000, "million": 1000000, "crore": 10000000,
               "crores": 10000000, "billion": 1000000000}
_STARTERS = sorted(set(SMALL_WORDS) | set(TENS_WORDS) | set(SCALE_WORDS),
                   key=len, reverse=True)
_CONTINUERS = sorted(set(_STARTERS) | {"and"}, key=len, reverse=True)
WORD_NUMBER_RE = re.compile(
    r"\b(?:" + "|".join(_STARTERS) + r")(?:[\s-]+(?:" + "|".join(_CONTINUERS)
    + r"))*\b", re.IGNORECASE)

# A model code: four or more characters containing at least one letter AND one
# digit. 'UA43CU7700', 'LE43K7000', 'Ftl35u'.
#
# An earlier version also flagged any run of two capitalised words, on the
# theory that product names are title-case. That was unworkable: ordinary
# English prose is full of capitalised pairs ("The Haier", a sentence opening
# after a newline), and it flagged them all. Precision matters more than reach
# here - a verifier that cries wolf on correct prose gets switched off, and
# then it is protecting nothing. The pattern is now case-insensitive, so a
# model code written 'Gls12i3fwaev' no longer slips past; hyphenated
# measurements like '5-star' are excluded by the unit check below rather than
# by the pattern.
MODEL_CODE_RE = re.compile(r"\b(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])"
                           r"[A-Za-z0-9][A-Za-z0-9-]{3,}\b")

# Technical vocabulary shaped like a model code.
NAME_ALLOW = {
    "HDR10", "HDR10+", "1080P", "720P", "2160P", "4K60", "H264", "H265",
    "USB3", "USB2", "HDMI2", "DTS", "MP3", "MP4", "AAC", "IPX4", "IPX5",
    "A2DP", "PM25", "R32", "R410A", "BLDC", "ISI", "BEE", "WIFI", "IOT",
    "5GHZ", "24GHZ", "MP4", "AC3", "EAC3", "COVID19",
}

# The competitor-mention risk: a brand the catalogue does not stock, named as
# though it were on offer. A curated vocabulary is used rather than "any
# capitalised word" for the reason above. Extend it with a known_brands.txt
# next to this file (one per line) or $BLOG_EXTRA_BRANDS, so a brand the store
# starts competing with does not need a code change.
BRAND_VOCAB = (
    "samsung", "lg", "haier", "whirlpool", "godrej", "ifb", "voltas", "bosch",
    "panasonic", "onida", "toshiba", "sansui", "crompton", "symphony",
    "bajaj", "philips", "prestige", "pigeon", "usha", "kenstar", "sunflame",
    "celfrost", "sony", "tcl", "hisense", "xiaomi", "redmi", "oneplus",
    "realme", "thomson", "kodak", "blaupunkt", "motorola", "infinix",
    "daikin", "hitachi", "carrier", "lloyd", "blue star", "midea",
    "electrolux", "siemens", "liebherr", "faber", "elica", "hindware",
    "kaff", "glen", "morphy richards", "havells", "orient", "v-guard",
    "racold", "ao smith", "venus", "iffalcon", "vu", "acer", "nokia",
    "amazon basics", "croma", "bpl", "sharp", "sanyo", "micromax", "mi",
    "westinghouse", "candy", "eurolex", "impex", "lifelong", "solimo",
)


def brand_vocab():
    """The curated list plus any local additions."""
    brands = set(BRAND_VOCAB)
    extra_file = Path(__file__).resolve().parent / "known_brands.txt"
    if extra_file.exists():
        for line in extra_file.read_text(encoding="utf-8").splitlines():
            line = compact(line).lower()
            if line and not line.startswith("#"):
                brands.add(line)
    for name in os.environ.get("BLOG_EXTRA_BRANDS", "").split(","):
        name = compact(name).lower()
        if name:
            brands.add(name)
    return sorted(brands)


# ------------------------------------------------------------------ segments

def segments(prose, brief):
    """(label, text, sku) for every piece of prose, with the SKU it belongs to.

    The scope is the point: a blurb is checked against its own product's facts
    only. Verifying everything against one pooled set of numbers was
    checkable but not attributable - every number was valid everywhere, which
    is exactly how a price ends up on the wrong product and still passes."""
    out = [("title", prose.get("title", ""), None),
           ("intro", prose.get("intro", ""), None),
           ("conclusion", prose.get("conclusion", ""), None)]
    known = {p["sku"] for p in brief["products"]}
    for sku, text in (prose.get("blurbs") or {}).items():
        out.append((f"blurb {sku}", text, sku if sku in known else None))
    return [(label, text, sku) for label, text, sku in out if compact(text)]


def prose_text(prose):
    """Everything the model wrote, as one blob."""
    if not prose:
        return ""
    parts = [prose.get("title", ""), prose.get("intro", ""),
             prose.get("conclusion", "")]
    parts.extend((prose.get("blurbs") or {}).values())
    return "\n".join(p for p in parts if p)


# -------------------------------------------------------------------- checks

def normalise_number(token):
    """'32,999' and '32999' and '32999.0' are the same number."""
    text = str(token).replace(",", "").rstrip(".")
    try:
        value = float(text)
    except ValueError:
        return str(token).lower()
    return str(int(value)) if value == int(value) else str(value)


def known_numbers(facts):
    out = set()
    for fact in facts:
        for token in NUMBER_RE.findall(fact):
            out.add(normalise_number(token))
    return out


def word_number(phrase):
    """'twenty five thousand' -> 25000. None if it is not a number phrase."""
    total, current, seen = 0, 0, False
    for word in re.split(r"[\s-]+", phrase.lower()):
        if not word or word == "and":
            continue
        if word in SMALL_WORDS:
            current += SMALL_WORDS[word]
        elif word in TENS_WORDS:
            current += TENS_WORDS[word]
        elif word in SCALE_WORDS:
            scale = SCALE_WORDS[word]
            if scale == 100:
                current = (current or 1) * 100
            else:
                total += (current or 1) * scale
                current = 0
        else:
            return None
        seen = True
    return (total + current) if seen else None


def is_measurement(text, start, end):
    """Is this number measuring something, or is it ordinary English?

    A unit or a currency marker next to it turns a number into a
    specification: 'one of the models' is prose, 'one ton' and 'Rs 5' are
    claims about a product."""
    return bool(UNIT_AFTER_RE.match(text[end:])
                or MONEY_BEFORE_RE.search(text[:start]))


def check_numbers(text, facts, measured_facts=None):
    """Every number in the prose must trace back to the brief.

    A measurement is checked against `measured_facts` - the brief without its
    post-level aggregates. Without that split, a roundup of 2 products made
    '2 Ton' verifiable, because 2 was in the brief as the product count."""
    allowed = known_numbers(facts)
    measured = known_numbers(measured_facts if measured_facts is not None else facts)
    problems = []

    def judge(match, value, label):
        measuring = is_measurement(text, match.start(), match.end())
        if value in (measured if measuring else allowed):
            return None
        if not measuring:
            numeric = float(value) if re.fullmatch(r"\d+(?:\.\d+)?", value) else None
            if numeric is not None and numeric <= 10:
                return None                 # 'one of the', 'both of the two'
        return f"{label}, which is not in the brief"

    for match in NUMBER_RE.finditer(text):
        problem = judge(match, normalise_number(match.group()),
                        f"states the number '{match.group()}'")
        if problem:
            problems.append(problem)

    for match in WORD_NUMBER_RE.finditer(text):
        number = word_number(match.group())
        if number is None:
            continue
        problem = judge(match, str(number),
                        f"spells out the number '{match.group()}' ({number})")
        if problem:
            problems.append(problem)
    return problems


def _variants(token):
    low = token.lower()
    return {low, low.replace("-", " "), low.replace("-", "")}


def check_names(text, facts):
    """A model code or brand the brief does not list has no business appearing.

    Two narrow checks rather than one broad one - see the note on MODEL_CODE_RE
    for why a general "capitalised name" rule had to go."""
    haystack = " ".join(facts)
    problems = []

    for token in sorted(set(MODEL_CODE_RE.findall(text))):
        if token.upper() in NAME_ALLOW:
            continue
        # '5-star', '1-ton', '11-5kg': a measurement, not a model code. The
        # number itself is already checked by check_numbers.
        letters = re.sub(r"[^A-Za-z]", "", token).lower()
        if letters in UNIT_WORDS:
            continue
        if any(form in haystack for form in _variants(token)):
            continue
        problems.append(f"names the model code '{token}', which is not in the brief")

    lowered = text.lower()
    for brand in brand_vocab():
        if not re.search(r"\b" + re.escape(brand) + r"\b", lowered):
            continue
        if brand in haystack:
            continue                    # this brand IS in the brief - fine
        problems.append(f"names the brand '{brand}', which is not in the brief")
    return problems


def check_claims(text, facts):
    """(problems, warnings).

    A banned phrase the CATALOGUE itself uses - a product literally titled
    '... Best Selling ...', a tag the taxonomy generated - is the store's own
    words, not an invented claim. Holding the post over that is a false
    positive with no fix available to the writer, so it is reported as a
    warning instead."""
    lowered = text.lower()
    haystack = " ".join(facts)
    problems, warnings = [], []
    for phrase in BANNED_CLAIMS:
        if phrase not in lowered:
            continue
        if phrase in haystack:
            warnings.append(f"repeats '{phrase}' from the catalogue's own text")
        else:
            problems.append(f"makes an unsupportable claim: '{phrase}'")
    return problems, warnings


def check_coverage(prose, brief):
    """Structural completeness - a missing blurb is a hole in the post, not a
    factual error, so it is reported separately from the hard failures. The
    renderer fills the hole with the deterministic sentence for that product,
    so the post is still complete."""
    warnings = []
    blurbs = prose.get("blurbs") or {}
    known = {p["sku"] for p in brief["products"]}
    for product in brief["products"]:
        if not compact(blurbs.get(product["sku"], "")):
            warnings.append(f"no blurb written for {product['sku']} "
                            f"(the template sentence will be used)")
    for sku in blurbs:
        if sku not in known:
            warnings.append(f"blurb for '{sku}', which is not in this post - dropped")
    if not compact(prose.get("intro")):
        warnings.append("no intro written (the template intro will be used)")
    if not compact(prose.get("conclusion")):
        warnings.append("no conclusion written (the template close will be used)")
    return warnings


def verify(prose, brief):
    """(ok, problems, warnings). `ok` False means do not publish."""
    if not compact(prose_text(prose)):
        return False, ["the prose is empty"], []

    problems, warnings = [], []

    for label, text, sku in segments(prose, brief):
        if sku is None:
            # the intro and the close are about the post, so the post-level
            # numbers - how many products, what the range is - belong there
            facts = fact_strings(brief)
            measured = fact_strings(brief, counts=False)
        else:
            # a blurb is about ONE product: its own values, and no aggregate
            # that happens to coincide with another product's price
            facts = fact_strings(brief, sku=sku, spans=False)
            measured = fact_strings(brief, sku=sku, spans=False, counts=False)
        found = check_numbers(text, facts, measured) + check_names(text, facts)
        claim_problems, claim_warnings = check_claims(text, facts)
        problems += [f"{label}: {item}" for item in found + claim_problems]
        warnings += [f"{label}: {item}" for item in claim_warnings]

    warnings += check_coverage(prose, brief)
    return (not problems), problems, warnings


def report(ok, problems, warnings):
    lines = []
    if ok:
        lines.append("VERIFIED: every number and name in the prose traces "
                     "back to the brief, and to the right product.")
    else:
        lines.append(f"HELD: {len(problems)} problem(s) - this post will not "
                     f"be published.")
        for problem in problems:
            lines.append(f"  - {problem}")
    for warning in warnings:
        lines.append(f"  [warn] {warning}")
    return "\n".join(lines)
