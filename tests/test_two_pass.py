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
        # app.main()'s 'Import only verified rows' filter
        # (status_upper.str.startswith('FOUND')) and
        # gui_helpers.found_status() both gate the import on exactly this.
        # Anchor by symbol, not line number - the old app.py:1729 /
        # gui.py:541 anchors had drifted onto unrelated code.
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


def test_gui_gate_refuses_discovery():
    """The other half of the gate: even a human ✔ cannot import a discovery
    row. gui.py confirms and builds through gui_helpers, so both the tick
    and the build scope have to say no."""
    print("a tick on a discovery row cannot reach the CSV")
    import pandas as pd                                       # noqa: E402
    from gui_helpers import found_status, select_build_rows   # noqa: E402

    statuses = [
        E.discovery_status({"url_link": "https://x.test/p",
                            "status": "FOUND (x.test)"}),
        E.discovery_status({"url_link": "https://x.test/p",
                            "status": "CLOSE MATCH - CHECK (x.test)"}),
        E.discovery_status({"url_link": "https://x.test/p",
                            "status": "CHECK MANUALLY (x.test)"}),
        E.discovery_status({"status": "NOT FOUND"}),
        E.discovery_status({}),
    ]
    for status in statuses:
        check(f"{status[:34]!r} is not tickable", found_status(status), False)
        df = pd.DataFrame([{"sku": "S1", "status": status}])
        kept, skipped = select_build_rows(df, "confirmed", {"S1"})
        check(f"{status[:34]!r} is dropped from a confirmed build",
              len(kept), 0)
        check(f"{status[:34]!r} is named in the skipped list",
              skipped, ["S1"])


def test_discover_hint_names_the_original_list():
    """P2-32: the 'Next:' banner printed at the end of --discover must name the
    ORIGINAL list, not '<stem>_filled.xlsx' - main()'s KI-5 guard exits on any
    input path containing '_filled', so the old banner handed the operator a
    command that could not run. The banner is inline in main(), not a callable,
    so this is a SOURCE-LEVEL assertion."""
    print("the --discover 'Next' banner is a runnable command")
    src = open(os.path.join(ROOT, "sku_enricher.py"), encoding="utf-8").read()
    check("the banner names the input list, not the output sheet",
          'sku_enricher.py "{src.name}" --crawl-urls' in src, True)
    check("no banner tells the user to feed _filled.xlsx back in",
          'sku_enricher.py "{out.name}" --crawl-urls' in src, False)


def test_crawl_urls_selects_a_pasted_needs_link_row(tmp_path=None):
    """Pass 2 must crawl a NEEDS LINK row once a human has pasted its link -
    that is the whole remediation the GUI's failed tab asks for. It crawls
    every row with a url_link that is not FOUND, and nothing else. No
    network: crawl_url is stubbed, the ledger goes to a scratch client."""
    print("--crawl-urls crawls a NEEDS LINK row that now has a url_link")
    import contextlib
    import io
    import tempfile
    from pathlib import Path
    from openpyxl import Workbook, load_workbook
    import client_config                                      # noqa: E402
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_credits import _write_client                    # noqa: E402

    tmp = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    rows = [("WM-AAA111", "https://x.test/a", "NEEDS LINK - NOT FOUND"),
            ("WM-BBB222", "", "NEEDS LINK - NOT FOUND"),
            ("WM-CCC333", "https://x.test/c", "FOUND (x.test)"),
            ("WM-DDD444", "https://x.test/d", "NEEDS CRAWL (x.test)")]
    src = tmp / "SCRATCH.xlsx"
    wb = Workbook()
    wb.active.append(["SKU"])
    for sku, _, _ in rows:
        wb.active.append([sku])
    wb.save(src)
    wb = Workbook()
    wb.active.append(["SKU", "url_link", "status"])
    for row in rows:
        wb.active.append(list(row))
    wb.save(tmp / "SCRATCH_filled.xlsx")
    _write_client(tmp / "clients", "acme", api_key_env="ACME_SERPER",
                  monthly_budget=0, default_run_budget=0)

    crawled = []

    def fake_crawl(url, sku="", min_images=0):
        crawled.append(url)
        return {"status": "NOT FOUND - stub"}

    real = (client_config.CLIENTS_DIR, E.crawl_url, E.polite_sleep, sys.argv)
    real_globals = {name: getattr(E, name) for name in
                    ("CATEGORY_CODES", "SERPER_KEY_ENV", "CACHE_DIR",
                     "CACHE_ENABLED", "CACHE_REFRESH", "CACHE_TTL_DAYS",
                     "SPECS_ENABLED", "IMAGE_CHECK", "IMAGE_MIN_PX",
                     "MERGE_BUDGET", "DISCOVER_MODE", "PW_ENABLED")}
    client_config.CLIENTS_DIR = tmp / "clients"
    E.crawl_url = fake_crawl
    E.polite_sleep = lambda *a, **k: None
    sys.argv = ["sku_enricher.py", str(src), "--client", "acme",
                "--crawl-urls", "--no-cache",
                "--cache-dir", str(tmp / "cache"), "--delay", "0", "0"]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            E.main()
    finally:
        (client_config.CLIENTS_DIR, E.crawl_url, E.polite_sleep,
         sys.argv) = real
        for name, value in real_globals.items():
            setattr(E, name, value)
        E.RUN_STATS["stopped_by"] = "done"
        E.RUN_STATS["client"] = client_config.DEFAULT_CLIENT

    check("the pasted NEEDS LINK row and the NEEDS CRAWL row are crawled, "
          "the FOUND one is not", crawled,
          ["https://x.test/a", "https://x.test/d"])
    ws = load_workbook(tmp / "SCRATCH_filled.xlsx").active
    head = [c.value for c in ws[1]]
    status = {r[0]: r[head.index("status")]
              for r in ws.iter_rows(min_row=2, values_only=True)}
    check("a row with no link stays NEEDS LINK",
          str(status["WM-BBB222"]).startswith("NEEDS LINK"), True)
    check("the FOUND row is untouched", status["WM-CCC333"], "FOUND (x.test)")


def main():
    for test in (test_discovery_status, test_discovery_is_not_importable,
                 test_gui_buckets, test_gui_gate_refuses_discovery,
                 test_discover_hint_names_the_original_list,
                 test_query_tiers, test_flags_are_exclusive,
                 test_crawl_urls_selects_a_pasted_needs_link_row):
        test()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
