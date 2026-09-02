"""Manual / CLI surface tests: every entry point, every error path.

Nothing here touches the live store or a live batch file. Credentials are
deliberately UNSET for the WooCommerce scripts so the no-creds path is what
gets exercised.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv/Scripts/python.exe")
WORK = Path(tempfile.mkdtemp(prefix="qa_cli_"))
PASS, ISSUES = 0, []


def run(args, cwd=WORK, env=None, timeout=300):
    e = dict(os.environ)
    for k in ("WC_URL", "WC_KEY", "WC_SECRET", "WP_APP_USER", "WP_APP_PASSWORD"):
        e.pop(k, None)
    e["STREAMLIT_SERVER_HEADLESS"] = "true"
    if env:
        e.update(env)
    p = subprocess.run([PY] + args, cwd=str(cwd), env=e, capture_output=True,
                       text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def check(label, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        ISSUES.append((label, detail))
        print(f"  ISSUE {label}\n          {detail[:400]}")


def S(name):
    print(f"\n{name}")


# ---------------------------------------------------------------- fixtures
def make_fixtures():
    import openpyxl
    import pandas as pd

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["sku"])
    ws.append(["SAMSUNG-UA32H4500FUXXL"])
    wb.save(WORK / "TINY.xlsx")

    # a hand-made "enricher output" so stage 2/3 need no network
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["sku", "title", "categories", "Brands", "regular price",
               "sale price", "url_link", "image_url", "specs", "status"])
    ws.append(["SAMSUNG-UA32H4500FUXXL",
               "Samsung 32 inch HD Ready Smart LED TV UA32H4500FUXXL",
               "", "", "24990", "18990", "https://www.amazon.in/dp/B01",
               "", "Energy Rating: 4 Star | Warranty: 1 Year", "FOUND (amazon.in)"])
    ws.append(["HAIER-LE32A9G", "Haier 32 inch HD Ready Google TV LE32A9G",
               "", "", "", "", "https://www.haier.com/in/x", "",
               "", "FOUND (haier.com) - no price shown"])
    ws.append(["LG-DEADROW", "", "", "", "", "", "", "", "", "NOT FOUND"])
    wb.save(WORK / "STAGE1_filled.xlsx")

    pd.DataFrame([{"sku": "SAMSUNG-UA32H4500FUXXL",
                   "title": "Samsung 32 inch HD Ready Smart LED TV",
                   "categories": "", "Brands": "", "regular price": "24990",
                   "sale price": "18990", "url_link": "", "image_url": ""}]
                 ).to_csv(WORK / "INPUT.csv", index=False)

    # a CSV that is import-ready enough to exit 0, and one that is not
    pd.DataFrame([{"sku": "AP-X", "title": "A sufficiently long product title",
                   "Images": "", "description": "d",
                   "categories": "LED > Mini LED", "regular price": "100",
                   "sale price": "90"}]).to_csv(WORK / "DIRTY.csv", index=False)


# ---------------------------------------------------------------- tests
def t_help():
    S("--help on every entry point")
    for script in ("sku_enricher.py", "build_csv.py", "validate_csv.py",
                   "wc_publish.py", "wc_seo.py", "wc_checkpoint.py"):
        rc, out = run([str(ROOT / script), "--help"])
        check(f"{script} --help exits 0", rc == 0, f"rc={rc} {out[:200]}")


def t_enricher_cli():
    S("sku_enricher.py error and utility paths")
    rc, out = run([str(ROOT / "sku_enricher.py")])
    check("no args -> asks for an input", rc != 0 and "input file" in out.lower(),
          f"rc={rc} {out[:200]}")

    rc, out = run([str(ROOT / "sku_enricher.py"), "nope.xlsx"])
    check("missing file -> 'File not found'", rc != 0 and "not found" in out.lower(),
          f"rc={rc} {out[:200]}")

    rc, out = run([str(ROOT / "sku_enricher.py"), "STAGE1_filled.xlsx"])
    check("KI-5: refuses its own _filled output",
          rc != 0 and "enricher output" in out, f"rc={rc} {out[:300]}")
    check("KI-5: no _filled_filled.xlsx was created",
          not (WORK / "STAGE1_filled_filled.xlsx").exists())

    rc, out = run([str(ROOT / "sku_enricher.py"), "--cache-info"])
    check("--cache-info exits 0", rc == 0 and "Cache:" in out, f"rc={rc} {out[:200]}")

    rc, out = run([str(ROOT / "sku_enricher.py"), "--probe",
                   "https://this-host-does-not-exist.invalid/p/1"])
    check("--probe on a dead host exits cleanly",
          rc == 0 and "RESULT: no page" in out, f"rc={rc} {out[-300:]}")
    check("--probe writes no spreadsheet",
          not list(WORK.glob("*_filled.xlsx")) or
          not (WORK / "TINY_filled.xlsx").exists())

    rc, out = run([str(ROOT / "sku_enricher.py"), "TINY.xlsx", "--sku-col", "nope"])
    check("bad --sku-col names the headers it did find",
          rc != 0 and "Headers found" in out, f"rc={rc} {out[:300]}")


def t_validate_cli():
    S("validate_csv.py exit codes and inputs")
    rc, out = run([str(ROOT / "validate_csv.py"), "DIRTY.csv"])
    check("a flagged file exits 1", rc == 1, f"rc={rc}")
    check("...and says why it exited", "Exiting 1" in out, out[-200:])

    rc, out = run([str(ROOT / "validate_csv.py"), "DIRTY.csv", "--warn-only"])
    check("--warn-only exits 0", rc == 0, f"rc={rc} {out[-200:]}")

    rc, out = run([str(ROOT / "validate_csv.py"), "nope.csv"])
    check("missing file -> message, not a traceback",
          rc == 1 and "File not found" in out and "Traceback" not in out,
          f"rc={rc} {out[:300]}")

    rc, out = run([str(ROOT / "validate_csv.py"), "STAGE1_filled.xlsx"])
    check("a _filled.xlsx routes to the stage-1 checks",
          "stage 1 checks" in out, out[:300])
    check("stage-1 checks read the status column",
          "Status breakdown" in out, out[:300])


def t_build_csv_cli():
    S("build_csv.py inputs and flags")
    rc, out = run([str(ROOT / "build_csv.py"), "nope.xlsx"])
    check("missing file -> message", rc != 0 and "not found" in out.lower(),
          f"rc={rc} {out[:200]}")

    rc, out = run([str(ROOT / "build_csv.py"), "STAGE1_filled.xlsx",
                   "-o", "b1.csv", "--no-validate"])
    check("builds from a _filled.xlsx", rc == 0 and (WORK / "b1.csv").exists(),
          f"rc={rc} {out[-400:]}")
    check("unverified rows excluded by default", "Excluded 1 unverified" in out,
          out[:400])

    rc, out = run([str(ROOT / "build_csv.py"), "STAGE1_filled.xlsx",
                   "-o", "b2.csv", "--all-rows", "--no-validate"])
    check("--all-rows keeps them", rc == 0 and "Excluded" not in out, out[:300])

    rc, out = run([str(ROOT / "build_csv.py"), "INPUT.csv", "-o", "b3.csv",
                   "--no-validate"])
    check("accepts a CSV input too", rc == 0 and (WORK / "b3.csv").exists(),
          f"rc={rc} {out[-300:]}")

    rc, out = run([str(ROOT / "build_csv.py"), "STAGE1_filled.xlsx",
                   "-o", "b4.csv", "--limit", "1", "--no-validate"])
    check("--limit honoured", rc == 0 and "building 1 row" in out, out[:300])

    rc, out = run([str(ROOT / "build_csv.py"), "STAGE1_filled.xlsx",
                   "-o", "b5.csv", "--seo", "--seo-plugin", "both",
                   "--no-validate"])
    check("--seo --seo-plugin both", rc == 0 and "rank_math" in out,
          f"rc={rc} {out[-300:]}")

    rc, out = run([str(ROOT / "build_csv.py"), "STAGE1_filled.xlsx", "-o", "b6.csv"])
    check("the QA gate runs automatically", "Issue summary" in out, out[-400:])
    check("a flagged QA pass does not fail the build", rc == 0, f"rc={rc}")


def t_woo_cli_no_creds():
    S("WooCommerce scripts with NO credentials set")
    rc, out = run([str(ROOT / "wc_publish.py"), "--check"])
    check("wc_publish --check -> asks for the env vars",
          rc != 0 and "WC_URL" in out and "Traceback" not in out,
          f"rc={rc} {out[:300]}")

    rc, out = run([str(ROOT / "wc_publish.py")])
    check("wc_publish with no CSV -> message",
          rc != 0 and "Traceback" not in out, f"rc={rc} {out[:300]}")

    rc, out = run([str(ROOT / "wc_seo.py"), "--limit", "1"])
    check("wc_seo -> asks for the env vars",
          rc != 0 and "WC_URL" in out and "Traceback" not in out,
          f"rc={rc} {out[:300]}")

    rc, out = run([str(ROOT / "wc_checkpoint.py"), "list"])
    check("wc_checkpoint list works without credentials",
          rc == 0 and "Traceback" not in out, f"rc={rc} {out[:300]}")

    rc, out = run([str(ROOT / "wc_checkpoint.py"), "create"])
    check("wc_checkpoint create -> asks for the env vars",
          rc != 0 and "WC_URL" in out and "Traceback" not in out,
          f"rc={rc} {out[:300]}")

    # and the #11 fix, with fake creds so it reaches the CSV read
    fake = {"WC_URL": "https://store.invalid", "WC_KEY": "k", "WC_SECRET": "s"}
    (WORK / "nosku.csv").write_text("title,price\nA,1\n", encoding="utf-8")
    rc, out = run([str(ROOT / "wc_publish.py"), "nosku.csv"], env=fake)
    check("wc_publish on a CSV with no sku column -> sentence",
          rc != 0 and "has no 'sku' column" in out and "Traceback" not in out,
          f"rc={rc} {out[:300]}")

    rc, out = run([str(ROOT / "wc_publish.py"), "nope.csv"], env=fake)
    check("wc_publish on a missing CSV -> message",
          rc != 0 and "File not found" in out, f"rc={rc} {out[:300]}")


def main():
    make_fixtures()
    t_help()
    t_enricher_cli()
    t_validate_cli()
    t_build_csv_cli()
    t_woo_cli_no_creds()
    print(f"\n{PASS} passed, {len(ISSUES)} issue(s)")
    for label, detail in ISSUES:
        print(f"  - {label}")
    print(f"\nworkspace: {WORK}")
    return 1 if ISSUES else 0


if __name__ == "__main__":
    code = main()
    sys.exit(code)
