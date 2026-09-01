"""The two-pass flow: --discover finds links, --crawl-urls extracts from them.

The property that matters most is NEGATIVE: a discovery sheet is half-done, so
nothing downstream may treat it as finished. app.py imports on
`status.startswith("FOUND")` and gui.py buckets on the same words, so the
discovery vocabulary has to stay clear of both.

    .venv\\Scripts\\python.exe tests\\test_two_pass.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sku_enricher as E                                      # noqa: E402

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


def test_discovery_status():
    print("pass 1 rewrites the verdict into review vocabulary")
    found = {"url_link": "https://www.amazon.in/dp/B0CWVDXYX1",
             "status": "FOUND (amazon.in)"}
    check("a confident hit", E.discovery_status(found), "NEEDS CRAWL (amazon.in)")

    close = {"url_link": "https://www.croma.com/p/12345",
             "status": "CLOSE MATCH - CHECK (croma.com)"}
    check("a close match keeps its link but says to check it",
          E.discovery_status(close), "NEEDS CRAWL - CHECK LINK (croma.com)")

    weak = {"url_link": "https://www.flipkart.com/p/abc",
            "status": "CHECK MANUALLY (flipkart.com)"}
    check("so does a weak one", E.discovery_status(weak),
          "NEEDS CRAWL - CHECK LINK (flipkart.com)")

    missing = {"status": "NOT FOUND - 6 page(s) checked, none matched"}
    check("no link at all", E.discovery_status(missing),
          "NEEDS LINK - NOT FOUND - 6 page(s) checked, none matched")

    check("even with no status to explain it",
          E.discovery_status({}), "NEEDS LINK - NOT FOUND")


def test_discovery_is_not_importable():
    """The whole safety argument for pass 1, in one test."""
    print("a discovery sheet cannot be mistaken for a finished one")
    statuses = [
        E.discovery_status({"url_link": "https://x.test/p", "status": "FOUND (x.test)"}),
        E.discovery_status({"url_link": "https://x.test/p",
                            "status": "CLOSE MATCH - CHECK (x.test)"}),
        E.discovery_status({"status": "NOT FOUND"}),
    ]
    for status in statuses:
        # app.py:1729 and gui.py:541 both gate the import on exactly this
        check(f"{status[:34]!r} is not importable",
              status.upper().startswith("FOUND"), False)
        check(f"{status[:34]!r} is not blank", bool(status.strip()), True)


def test_gui_buckets():
    print("gui.py sorts the new statuses into the right tabs")
    sys.path.insert(0, ROOT)
    import gui                                                # noqa: E402

    check("a crawlable link is pending, not failed",
          gui.classify("NEEDS CRAWL (amazon.in)"), "pending")
    check("an uncertain link needs review",
          gui.classify("NEEDS CRAWL - CHECK LINK (croma.com)"), "review")
    check("no link is a failure the human must fix",
          gui.classify("NEEDS LINK - NOT FOUND"), "failed")
    # the pre-existing vocabulary must not have moved
    check("FOUND is still clean", gui.classify("FOUND (amazon.in)"), "found")
    check("FOUND with a caveat still needs review",
          gui.classify("FOUND (amazon.in) - no price shown"), "review")
    check("a pass-2 VERIFY lands in review",
          gui.classify("FOUND (amazon.in) - VERIFY: weak match only"), "review")
    check("NOT FOUND still fails", gui.classify("NOT FOUND"), "failed")
    check("blank still fails", gui.classify(""), "failed")
    check("every bucket has a badge",
          sorted(gui.BADGE) == sorted({"found", "review", "failed", "skipped",
                                       "pending"}), True)


def test_query_tiers():
    """Searches are tiered: model-code queries first, full-title queries only
    as a fallback. The title tier used to run on EVERY SKU - 3 searches per
    row that re-found the same listings the model tier already had."""
    print("query tiers")
    sku = "Voltas 183V Vectra Elegant 1.5 Ton 3 Star Inverter Split AC"

    E.DISCOVER_MODE = False
    try:
        brand, model, tiers = E.build_queries(sku)
        check("two tiers for a SKU with a model code", len(tiers), 2)
        check("tier 1 is the model code",
              all(f'"{model}"' in q or model in q for q, _ in tiers[0]), True)
        check("tier 2 is the full title",
              all("Vectra Elegant" in q for q, _ in tiers[1]), True)
        check("normal mode still asks croma",
              any(d == "croma.com" for _, d in tiers[0]), True)

        E.DISCOVER_MODE = True
        _, _, d_tiers = E.build_queries(sku)
        check("discover trims croma and the brand site",
              [d for _, d in d_tiers[0]], ["amazon.in", "flipkart.com", None])
        check("discover keeps the title tier as a fallback",
              len(d_tiers), 2)

        E.DISCOVER_MODE = False
        _, _, no_model = E.build_queries("Random Thing With No Model Code")
        check("no model code: the title IS tier 1", len(no_model), 1)
        check("and it still searches somewhere", len(no_model[0]) > 0, True)

        _, _, bare = E.build_queries("X1")
        check("even a bare code gets a query", len(bare) >= 1, True)
    finally:
        E.DISCOVER_MODE = False


def test_flags_are_exclusive():
    print("the two passes cannot be run at once")
    import subprocess
    py = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    if not os.path.exists(py):
        py = sys.executable
    out = subprocess.run(
        [py, os.path.join(ROOT, "sku_enricher.py"), "nonexistent.xlsx",
         "--discover", "--crawl-urls"],
        capture_output=True, text=True, timeout=120)
    check("it exits non-zero", out.returncode != 0, True)
    check("and explains the flow",
          "two halves of one flow" in (out.stdout + out.stderr), True)


def main():
    for test in (test_discovery_status, test_discovery_is_not_importable,
                 test_gui_buckets, test_query_tiers, test_flags_are_exclusive):
        test()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
