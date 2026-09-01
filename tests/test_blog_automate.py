"""blog_automate: the brief must be honest and the verifier must actually bite.

The hybrid design only holds up if blog_verify.py rejects what it claims to
reject. Everything here is an edge case that used to pass silently, so a
regression in any of them puts a wrong number in front of a customer.

    .venv\\Scripts\\python.exe tests\\test_blog_automate.py
"""
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "blog_automate"))

import blog_brief as B                                        # noqa: E402
import blog_render as R                                       # noqa: E402
import blog_verify as V                                       # noqa: E402

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
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        if UNDER_PYTEST:
            raise AssertionError(f"{label}: got {got!r}, want {want!r}")


def check_true(label, got):
    check(label, bool(got), True)


def product(sku, name, brand, category, price, mrp=None, specs=None,
            tags=(), slug="", image=""):
    return {
        "sku": sku, "name": name, "brand": brand, "category": category,
        "tags": list(tags), "price": price, "price_text": B.rupees(price),
        "mrp": mrp, "mrp_text": B.rupees(mrp) if mrp else "",
        "specs": dict(specs or {}), "image": image, "images": [image] if image else [],
        "slug": slug,
    }


VOLTAS = product("AC-V", "Voltas 123V 1 Ton 3 Star Split AC", "Voltas",
                 "Air Conditioner > Split AC", 30500, 35000,
                 {"Capacity": "1 Ton", "Energy Rating": "3 Star"},
                 slug="voltas-123v", image="https://x.test/v.webp")
LLOYD = product("AC-L", "Lloyd GLS12I3FWAEV 1 Ton 3 Star Split AC", "Lloyd",
                "Air Conditioner > Split AC", 31490, 36990,
                {"Capacity": "1 Ton", "Energy Rating": "3 Star"},
                slug="lloyd-gls12", image="https://x.test/l.webp")
BRIEF = B.build("roundup", [VOLTAS, LLOYD], built_at="2026-08-11")


def prose(intro="", conclusion="", title="A post", blurbs=None):
    return {"title": title, "intro": intro or "Two air conditioners.",
            "conclusion": conclusion or "Prices change.",
            "blurbs": dict(blurbs or {})}


def problems_for(intro="", blurbs=None, brief=BRIEF):
    ok, problems, _ = V.verify(prose(intro=intro, blurbs=blurbs), brief)
    return ok, problems


# --------------------------------------------------------------- money/words

def test_money():
    print("to_int refuses to guess")
    check("plain", B.to_int("30500"), 30500)
    check("grouped", B.to_int("30,500"), 30500)
    check("currency prefix", B.to_int("Rs. 30,500"), 30500)
    check("decimal", B.to_int("30500.00"), 30500)
    check("a range is not a price", B.to_int("12999-15999"), None)
    check("'to' range is not a price", B.to_int("12999 to 15999"), None)
    check("zero is not a price", B.to_int("0"), None)
    check("negative is not a price", B.to_int("-500"), None)
    check("blank", B.to_int(""), None)
    check("no digits", B.to_int("On request"), None)

    print("an unreadable sale price is not replaced by the MRP")
    row = {"sku": "X", "title": "X", "sale price": "28000-32000",
           "regular price": "35000"}
    bad = B.product_from_row(row)
    check("no price at all", bad["price"], None)
    check_true("and it says why", "28000-32000" in bad["price_problem"])

    empty = B.product_from_row({"sku": "X", "title": "X", "sale price": "",
                                "regular price": "35000"})
    check("an EMPTY sale price still falls back to the MRP",
          empty["price"], 35000)


def test_word_forms():
    print("singular/plural")
    check("plural leaf", B.singular("Washing Machines"), "Washing Machine")
    check("-ss word survives", B.singular("Glass"), "Glass")
    check("already singular", B.singular("Split AC"), "Split AC")
    check("-ies", B.singular("Categories"), "Category")
    check("pluralise AC", B.plural("Split AC", 5), "Split ACs")
    check("pluralise machine", B.plural("Washing Machine", 3), "Washing Machines")
    check("count of one", B.plural("Split AC", 1), "Split AC")
    check("-ss pluralises", B.plural("Glass", 2), "Glasses")


# ----------------------------------------------------------------- selection

def test_category_match():
    print("category matching is whole words")
    led = product("TV-1", "Bladeless Sealed Unit", "Acme", "Fans > Tower", 5000)
    check("'LED' does not match 'Bladeless'", B.matches_category(led, "LED"), False)
    check("real match", B.matches_category(VOLTAS, "Split AC"), True)
    check("multi-word must all appear",
          B.matches_category(VOLTAS, "Front Load"), False)


def test_pick_applies_filters_to_sku_path():
    print("--sku goes through the same gauntlet")
    priceless = product("AC-N", "No Price AC", "Acme", "Air Conditioner > Split AC", None)
    catalogue = [VOLTAS, LLOYD, priceless]

    chosen, notes = B.pick(catalogue, skus=["AC-V", "AC-N"])
    check("priceless product dropped", [p["sku"] for p in chosen], ["AC-V"])
    check_true("and said so", any("AC-N" in n and "price" in n for n in notes))

    chosen, notes = B.pick(catalogue, skus=["AC-V", "AC-L"], max_price=31000)
    check("--max-price applies to --sku", [p["sku"] for p in chosen], ["AC-V"])

    chosen, notes = B.pick(catalogue, skus=["AC-V", "AC-L"], limit=1)
    check("--limit applies to --sku", len(chosen), 1)

    chosen, notes = B.pick(catalogue, skus=["AC-V", "NOPE"])
    check_true("a missing SKU is reported",
               any(n.startswith("--sku NOPE") for n in notes))

    chosen, _ = B.pick(catalogue, skus=["AC-L", "AC-V"])
    check("caller order is kept", [p["sku"] for p in chosen], ["AC-L", "AC-V"])


def test_leaf_and_headline():
    print("a mixed set is not named after its cheapest item")
    semi = product("WM-S", "Godrej Semi", "Godrej",
                   "Washing Machine > Semi Automatic", 19990)
    front = product("WM-F", "IFB Front", "IFB",
                    "Washing Machine > Front Load", 26490)
    mixed = B.build("guide", [semi, front], built_at="2026-08-11")
    check("falls back to the shared parent", mixed["category_leaf"],
          "Washing Machine")
    check("guide headline", B.headline(mixed),
          "How to Choose a Washing Machine: A Buying Guide")
    check("roundup pluralises", B.headline(BRIEF).startswith("2 Split ACs"), True)

    capped = B.build("roundup", [VOLTAS, LLOYD], max_price=35000,
                     built_at="2026-08-11")
    check("cap in the headline", B.headline(capped), "2 Split ACs Under Rs 35,000")


def test_slug():
    print("slugs")
    long_title = "5 Split ACs From Rs 30,500 to Rs 44,990 and Some More Words Here"
    slug = B.slugify(long_title)
    check("no mid-word cut", slug.endswith("-"), False)
    check_true("stays inside the limit", len(slug) <= 70)
    check_true("whole words only", all(part for part in slug.split("-")))
    check("fingerprint is stable",
          B.fingerprint("roundup", [VOLTAS, LLOYD]),
          B.fingerprint("roundup", [LLOYD, VOLTAS]))
    check("a different set is a different fingerprint",
          B.fingerprint("roundup", [VOLTAS]) == B.fingerprint("roundup", [VOLTAS, LLOYD]),
          False)


# ------------------------------------------------------------------ verifier

def test_scoped_numbers():
    print("a blurb may only use its own product's numbers")
    ok, problems = problems_for(blurbs={"AC-V": "It is listed at Rs 31,490."})
    check("Lloyd's price in the Voltas blurb is caught", ok, False)
    check_true("named as a number problem",
               any("31,490" in p for p in problems))

    ok, _ = problems_for(blurbs={"AC-V": "It is listed at Rs 30,500."})
    check("its own price is fine", ok, True)

    ok, _ = problems_for(intro="Prices run from Rs 30,500 to Rs 31,490.")
    check("the intro may use every price", ok, True)


def test_word_numbers():
    print("spelled-out numbers are numbers")
    ok, problems = problems_for(intro="These are forty-three inch screens.")
    check("'forty-three inch' is caught", ok, False)
    check_true("reported with its value", any("43" in p for p in problems))

    ok, _ = problems_for(intro="Twenty five thousand rupees buys a lot.")
    check("'twenty five thousand' is caught", ok, False)

    ok, _ = problems_for(intro="One of these will suit a small room.")
    check("'one of these' is ordinary English", ok, True)

    ok, _ = problems_for(intro="Both carry a two year warranty.")
    check("'two year warranty' is a claim", ok, False)


def test_small_numbers_with_units():
    print("small numbers stop being free next to a unit")
    ok, _ = problems_for(blurbs={"AC-V": "A 3 Star rating keeps the bill down."})
    check("'3 Star' is in the brief", ok, True)

    ok, _ = problems_for(blurbs={"AC-V": "A 5 Star rating keeps the bill down."})
    check("'5 Star' is not", ok, False)

    ok, _ = problems_for(blurbs={"AC-V": "It has a 2 Ton compressor."})
    check("'2 Ton' is not in the brief", ok, False)

    ok, _ = problems_for(intro="There are 2 models here.")
    check("a bare count is fine", ok, True)

    ok, _ = problems_for(blurbs={"AC-V": "Rated 3-star for efficiency."})
    check("'3-star' is not a false positive", ok, True)


def test_names():
    print("model codes and brands")
    ok, problems = problems_for(intro="The Samsung unit is similar.")
    check("an off-catalogue brand is caught", ok, False)

    ok, _ = problems_for(intro="The Voltas unit is the cheaper one.")
    check("a brand in the brief is fine", ok, True)

    ok, problems = problems_for(blurbs={"AC-V": "Also sold as the Ua43cu7700."})
    check("a mixed-case model code is caught", ok, False)
    check_true("named", any("Ua43cu7700" in p for p in problems))

    ok, _ = problems_for(blurbs={"AC-L": "The GLS12I3FWAEV is a 1 Ton unit."})
    check("its own model code is fine", ok, True)


def test_claims():
    print("unsupportable claims")
    ok, problems = problems_for(intro="This is the cheapest AC anywhere.")
    check("'cheapest' is held", ok, False)

    ok, _ = problems_for(intro="You won't find it for less.")
    check("a paraphrase is held too", ok, False)

    catalogue_claim = product(
        "AC-B", "Acme Best Selling 1 Ton AC", "Acme",
        "Air Conditioner > Split AC", 20000, specs={"Capacity": "1 Ton"})
    brief = B.build("roundup", [catalogue_claim], built_at="2026-08-11")
    ok, problems, warnings = V.verify(
        prose(blurbs={"AC-B": "The Acme Best Selling model is a 1 Ton unit."}),
        brief)
    check("the catalogue's own wording is not a hard failure", ok, True)
    check_true("but it is warned about",
               any("best selling" in w.lower() for w in warnings))


def test_coverage():
    print("coverage")
    _, _, warnings = V.verify(prose(blurbs={"AC-V": "Fine."}), BRIEF)
    check_true("a missing blurb is warned about",
               any("AC-L" in w for w in warnings))
    ok, problems, _ = V.verify({"title": "", "intro": "", "conclusion": "",
                                "blurbs": {}}, BRIEF)
    check("empty prose fails", ok, False)


# ------------------------------------------------------------------ renderer

def test_render():
    print("renderer")
    text = "One sentence here. A second one that runs past the limit entirely."
    trimmed = R.sentence_trim(text, 40)
    check("cuts on a sentence boundary", trimmed, "One sentence here.")

    check("blank lines make paragraphs",
          len(R.paragraphs("First para.\n\nSecond para.")), 2)

    html = R.render(BRIEF, None, "https://shop.test")
    check_true("links products when a slug and site url exist",
               "https://shop.test/product/voltas-123v/" in html)
    check_true("falls back to a template blurb",
               "as stated on the product listing" in html)
    check_true("emits schema.org", "application/ld+json" in html)
    check_true("prices appear once, escaped", "Rs 30,500" in html)

    unlinked = R.render(BRIEF, None, "")
    check("no site url means no link", "<a href" in unlinked, False)
    check("missing links are reportable",
          R.missing_links(BRIEF, ""), ["AC-V", "AC-L"])

    payload = R.post_payload(BRIEF, None, "https://shop.test")
    check("excerpt does not end mid-list",
          payload["excerpt"].endswith(","), False)
    check_true("tags carry the brands", "Voltas" in payload["tags"])
    check_true("and the category", "Split AC" in payload["tags"])

    escaped = R.render(BRIEF, {"intro": "<script>alert(1)</script>",
                               "blurbs": {}, "conclusion": "", "title": ""},
                       "https://shop.test")
    check("model output cannot inject html",
          "<script>alert(1)</script>" in escaped, False)


def test_slug_collision_guard():
    print("two different posts cannot claim one slug")
    import shutil
    import tempfile
    import blog_run                                            # noqa: E402

    out = Path(tempfile.mkdtemp(prefix="blogslug-"))
    try:
        title = "How to Choose a Washing Machine: A Buying Guide"
        first = B.build("guide", [VOLTAS], built_at="2026-08-11")
        slug = blog_run.resolve_slug(first, title, out)
        first["slug"] = slug
        B.save(first, out / "guide-one.brief.json")
        check("the first post takes the plain slug", slug,
              "how-to-choose-a-washing-machine-a-buying-guide")

        again = blog_run.resolve_slug(first, title, out)
        check("re-running the SAME post keeps its slug", again, slug)

        other = B.build("guide", [LLOYD], built_at="2026-08-11")
        moved = blog_run.resolve_slug(other, title, out)
        check("a DIFFERENT post gets its own slug", moved == slug, False)
        check_true("suffixed with its fingerprint",
                   moved.endswith(other["fingerprint"][:6]))
    finally:
        shutil.rmtree(out, ignore_errors=True)


def main():
    for test in (test_money, test_word_forms, test_category_match,
                 test_pick_applies_filters_to_sku_path, test_leaf_and_headline,
                 test_slug, test_scoped_numbers, test_word_numbers,
                 test_small_numbers_with_units, test_names, test_claims,
                 test_coverage, test_render, test_slug_collision_guard):
        test()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
