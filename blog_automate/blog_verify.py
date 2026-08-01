"""blog_verify.py - check finished prose back against the brief.

This is what makes the hybrid model safe to run. The prose layer is told not
to state facts; this checks whether it did anyway, mechanically, with no model
in the loop:

  * every number in the prose must appear in the brief
  * every capitalised product/brand-looking name must appear in the brief
  * a fixed list of unsupportable claims is rejected outright

It runs on the generated prose whether a model or a person wrote it, and a
failure holds the post. There is no "probably fine" verdict - a number that
cannot be traced to the brief is a defect, because the one thing a reader will
act on is the price.
"""

import re

from blog_brief import fact_strings

# Numbers that are ordinary English rather than claims about a product. "one
# of the", "two years" in a sentence the brief supports, small counts.
SAFE_SMALL_NUMBERS = {str(n) for n in range(0, 11)}

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
    "world-class",
)

NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# A model code: all caps/digits/hyphen, four or more characters, containing at
# least one letter AND one digit. 'UA43CU7700', 'LE43K7000', 'WW80T504DAB'.
#
# An earlier version also flagged any run of two capitalised words, on the
# theory that product names are title-case. That was unworkable: ordinary
# English prose is full of capitalised pairs ("The Haier", a sentence opening
# after a newline), and it flagged them all. Precision matters more than reach
# here - a verifier that cries wolf on correct prose gets switched off, and
# then it is protecting nothing.
MODEL_CODE_RE = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9][A-Z0-9-]{3,}\b")

# Technical vocabulary shaped like a model code.
NAME_ALLOW = {
    "HDR10", "HDR10+", "1080P", "720P", "2160P", "4K60", "H264", "H265",
    "USB3", "USB2", "HDMI2", "DTS", "MP3", "MP4", "AAC", "IPX4", "IPX5",
    "A2DP", "PM25", "R32", "R410A", "BLDC", "ISI", "BEE",
}

# The competitor-mention risk: a brand the catalogue does not stock, named as
# though it were on offer. A curated vocabulary is used rather than "any
# capitalised word" for the reason above.
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
)


def prose_text(prose):
    """Everything the model wrote, as one blob."""
    if not prose:
        return ""
    parts = [prose.get("title", ""), prose.get("intro", ""),
             prose.get("conclusion", "")]
    parts.extend((prose.get("blurbs") or {}).values())
    return "\n".join(p for p in parts if p)


def normalise_number(token):
    """'32,999' and '32999' and '32999.0' are the same number."""
    text = token.replace(",", "").rstrip(".")
    try:
        value = float(text)
    except ValueError:
        return token.lower()
    return str(int(value)) if value == int(value) else str(value)


def known_numbers(facts):
    out = set()
    for fact in facts:
        for token in NUMBER_RE.findall(fact):
            out.add(normalise_number(token))
    return out


def check_numbers(text, facts):
    """Every number in the prose must trace back to the brief."""
    allowed = known_numbers(facts) | SAFE_SMALL_NUMBERS
    problems = []
    for token in NUMBER_RE.findall(text):
        value = normalise_number(token)
        if value in allowed:
            continue
        problems.append(f"states the number '{token}', which is not in the brief")
    return problems


def check_names(text, facts):
    """A model code or brand the brief does not list has no business appearing.

    Two narrow checks rather than one broad one - see the note on MODEL_CODE_RE
    for why a general "capitalised name" rule had to go."""
    haystack = " ".join(facts)
    problems = []

    for token in sorted(set(MODEL_CODE_RE.findall(text))):
        if token.upper() in NAME_ALLOW:
            continue
        if token.lower() in haystack:
            continue
        problems.append(f"names the model code '{token}', which is not in the brief")

    lowered = text.lower()
    for brand in BRAND_VOCAB:
        if not re.search(r"\b" + re.escape(brand) + r"\b", lowered):
            continue
        if brand in haystack:
            continue                    # this brand IS in the brief - fine
        problems.append(f"names the brand '{brand}', which is not in the brief")
    return problems


def check_claims(text):
    lowered = text.lower()
    return [f"makes an unsupportable claim: '{phrase}'"
            for phrase in BANNED_CLAIMS if phrase in lowered]


def check_coverage(prose, brief):
    """Structural completeness - a missing blurb is a hole in the post, not a
    factual error, so it is reported separately from the hard failures."""
    warnings = []
    blurbs = prose.get("blurbs") or {}
    for product in brief["products"]:
        if not blurbs.get(product["sku"]):
            warnings.append(f"no blurb written for {product['sku']}")
    if not prose.get("intro"):
        warnings.append("no intro written")
    if not prose.get("conclusion"):
        warnings.append("no conclusion written")
    return warnings


def verify(prose, brief):
    """(ok, problems, warnings). `ok` False means do not publish."""
    text = prose_text(prose)
    if not text.strip():
        return False, ["the prose is empty"], []

    facts = fact_strings(brief)
    problems = []
    problems += check_claims(text)
    problems += check_numbers(text, facts)
    problems += check_names(text, facts)
    warnings = check_coverage(prose, brief)
    return (not problems), problems, warnings


def report(ok, problems, warnings):
    lines = []
    if ok:
        lines.append("VERIFIED: every number and name in the prose traces "
                     "back to the brief.")
    else:
        lines.append(f"HELD: {len(problems)} problem(s) - this post will not "
                     f"be published.")
        for problem in problems:
            lines.append(f"  - {problem}")
    for warning in warnings:
        lines.append(f"  [warn] {warning}")
    return "\n".join(lines)
