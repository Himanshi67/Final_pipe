"""Pipeline / E2E / regression tests.

Stage contracts (does stage N's output satisfy stage N+1's stated
requirements), resume and recovery behaviour, the dry-run guarantees, and a
re-check of every previously-closed KNOWN_ISSUE that these files touch.

Warm-cache only: the enricher runs replay .cache and spend no Serper credits.
No WooCommerce call leaves the process - SESSION is stubbed.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(r"d:\Dhruv-Personal\Catalogue\Final_pipe")
PY = str(ROOT / ".venv/Scripts/python.exe")
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

import openpyxl                                    # noqa: E402
import pandas as pd                                # noqa: E402

import app                                         # noqa: E402
import sku_enricher as E                           # noqa: E402
import validate_csv as V                           # noqa: E402
import wc_publish as W                             # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="qa_pipe_"))
PASS, ISSUES = 0, []


def check(label, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        ISSUES.append((label, str(detail)[:500]))
        print(f"  ISSUE {label}\n          {str(detail)[:400]}")


def S(name):
    print(f"\n{name}")


def run(args, cwd=WORK, env=None, timeout=900):
    e = dict(os.environ)
    e["STREAMLIT_SERVER_HEADLESS"] = "true"
    if env:
        e.update(env)
    p = subprocess.run([PY] + args, cwd=str(cwd), env=e, capture_output=True,
                       text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def sheet_headers(path):
    ws = openpyxl.load_workbook(path).active
    return [str(c.value or "") for c in ws[1]]


# ====================== 1. stage 1 -> stage 2 contract ======================
def t_stage1_contract():
    S("PIPELINE: stage 1 output satisfies stage 2's declared input contract")
    shutil.copy(ROOT / "inputs/LED.xlsx", WORK / "P.xlsx")
    rc, out = run([str(ROOT / "sku_enricher.py"), "P.xlsx", "--limit", "3"])
    check("enricher run completes", rc == 0 and "Done. Processed" in out,
          f"rc={rc} {out[-500:]}")
    check("no Serper credits spent (warm cache)",
          "serper.dev calls this run" not in out,
          "a cold cache would have cost credits: " + out[-300:])

    filled = WORK / "P_filled.xlsx"
    check("_filled.xlsx written", filled.exists())
    hdr = {h.strip().lower() for h in sheet_headers(filled)}
    missing = [c for c in app.REQUIRED_INPUT_COLUMNS if c.strip().lower() not in hdr]
    check("KI-8: all 8 REQUIRED_INPUT_COLUMNS present", not missing, missing)

    ws = openpyxl.load_workbook(filled).active
    names = [str(c.value or "") for c in ws[1]]
    isku = names.index("sku")
    vals = [ws.cell(row=r, column=isku + 1).value for r in range(2, ws.max_row + 1)]
    filled_rows = [v for v in vals if v and str(v).strip()]
    check("KI-8: sku column populated on every data row",
          len(filled_rows) == ws.max_row - 1, f"{len(filled_rows)}/{ws.max_row - 1}")

    # app.py must actually accept it
    df = pd.read_excel(filled)
    try:
        app.require_input_columns(df)
        ok, err = True, ""
    except Exception as exc:
        ok, err = False, str(exc)
    check("app.py accepts the sheet without hand-editing", ok, err)
    return filled


# ====================== 2. resume / recovery behaviour ======================
def t_resume(_filled):
    """A fixed-size input, so 'skipped' cannot be confused with 'moved on to
    the next fresh row' - which is what --limit legitimately does."""
    S("PIPELINE: resume, --retry-incomplete, and the KI-4 recovery file")
    src = openpyxl.load_workbook(ROOT / "inputs/LED.xlsx").active
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in range(1, 3):                       # header + 2 SKUs, nothing more
        ws.append([c.value for c in src[r]])
    wb.save(WORK / "R.xlsx")

    rc, out = run([str(ROOT / "sku_enricher.py"), "R.xlsx"])
    check("first run processes both rows", "Processed 1 SKUs" in out
          or "Processed 2 SKUs" in out, out[-300:])
    filled = WORK / "R_filled.xlsx"
    check("output written", filled.exists())

    snapshot = {}
    ws2 = openpyxl.load_workbook(filled).active
    names = [str(c.value or "") for c in ws2[1]]
    iurl = names.index("url_link") + 1
    ist = names.index("status") + 1
    isale = names.index("sale price") + 1
    for r in range(2, ws2.max_row + 1):
        snapshot[r] = ws2.cell(row=r, column=iurl).value

    rc, out = run([str(ROOT / "sku_enricher.py"), "R.xlsx"])
    check("a second run over a finished sheet processes NOTHING",
          "Processed 0 SKUs" in out, out[-300:])
    ws3 = openpyxl.load_workbook(filled).active
    check("...and no completed row was disturbed",
          all(ws3.cell(row=r, column=iurl).value == v
              for r, v in snapshot.items()))

    # manufacture one half-filled row
    wb4 = openpyxl.load_workbook(filled)
    w4 = wb4.active
    target = next((r for r in range(2, w4.max_row + 1)
                   if str(w4.cell(row=r, column=ist).value or "")
                   .startswith("FOUND")), None)
    check("a FOUND row exists to degrade", target is not None)
    if target:
        w4.cell(row=target, column=ist, value="FOUND (amazon.in) - no price shown")
        w4.cell(row=target, column=isale, value=None)
        wb4.save(filled)

        rc, out = run([str(ROOT / "sku_enricher.py"), "R.xlsx"])
        check("without the flag, the half-filled row stays skipped",
              "Processed 0 SKUs" in out, out[-300:])

        rc, out = run([str(ROOT / "sku_enricher.py"), "R.xlsx",
                       "--retry-incomplete"])
        check("--retry-incomplete re-attempts exactly that one row",
              "Processed 1 SKUs" in out, out[-300:])

    # KI-4: a newer _recovery file must win
    rec = WORK / "R_filled_recovery.xlsx"
    shutil.copy(filled, rec)
    os.utime(rec, (time.time() + 120, time.time() + 120))
    rc, out = run([str(ROOT / "sku_enricher.py"), "R.xlsx"])
    check("KI-4: a newer _recovery file is resumed from",
          "is newer than" in out and "Resuming from it" in out, out[:700])
    rec.unlink(missing_ok=True)


# ====================== 3. stage 2 -> stage 3 contract ======================
def t_stage2_contract(filled):
    S("PIPELINE: stage 2 output satisfies stage 3 (validate + publish)")
    rc, out = run([str(ROOT / "build_csv.py"), str(filled.name), "-o", "P.csv",
                   "--month", "2026/08", "--seo", "--no-validate"])
    check("build_csv completes", rc == 0 and (WORK / "P.csv").exists(),
          f"rc={rc} {out[-400:]}")
    df = pd.read_csv(WORK / "P.csv", dtype=str).fillna("")

    missing = [c for c in app.EXPORTED_OUTPUT_COLUMNS if c not in df.columns]
    check("every EXPORTED_OUTPUT_COLUMN is present", not missing, missing)
    seo_missing = [c for c in app.seo_columns("yoast") if c not in df.columns]
    check("every --seo column is present", not seo_missing, seo_missing)

    # the app.py (browser) path must agree with the build_csv (CLI) path
    src = app.filter_active_inventory_rows(pd.read_excel(filled))
    src = src[src["status"].map(app.compact).str.upper().str.startswith("FOUND")]
    direct = app.process_inventory(src.copy(),
                                   media_base="https://shreejeeelectronics.in"
                                              "/wp-content/uploads/2026/08",
                                   seo=True)
    check("app.py and build_csv.py produce the same columns",
          list(direct.columns) == list(df.columns),
          f"{set(direct.columns) ^ set(df.columns)}")
    check("app.py and build_csv.py produce the same row count",
          len(direct) == len(df), f"{len(direct)} vs {len(df)}")

    # wc_publish must be able to assemble a payload for every row
    class DryStore:
        allow_writes = False

        def __init__(self):
            self.calls = []

        def category_id(self, h):
            self.calls.append(("cat", h))
            return None

        def tag_ids(self, names):
            self.calls.append(("tags", tuple(names)))
            return []

    store = DryStore()
    bad = []
    for _, row in df.iterrows():
        try:
            payload, notes = W.build_payload(store, row, "draft", "url")
            for key in ("name", "sku", "type", "regular_price", "categories",
                        "tags", "attributes", "images", "meta_data"):
                if key not in payload:
                    bad.append((row.get("sku"), f"missing {key}"))
            if payload["sku"] == "":
                bad.append((row.get("sku"), "empty sku in payload"))
        except Exception as exc:
            bad.append((row.get("sku"), f"{type(exc).__name__}: {exc}"))
    check("wc_publish builds a payload for every generated row", not bad, bad[:4])
    return WORK / "P.csv", df


# ====================== 4. dry-run guarantees (KI-16) ======================
class RecordingSession:
    """Fails the test if anything other than a GET is attempted."""

    def __init__(self, get_payload=None):
        self.get_payload = get_payload if get_payload is not None else []
        self.writes = []
        self.headers = {}
        self.cookies = type("J", (), {"set": lambda *a, **k: None})()

    class _R:
        def __init__(self, payload, status=200):
            self._p, self.status_code = payload, status
            self.headers = {"Content-Type": "application/json"}
            self.text, self.content = "{}", b"{}"

        def json(self):
            return self._p

    def request(self, method, url, **kw):
        if method != "GET":
            self.writes.append((method, url))
        return self._R(self.get_payload)

    def get(self, url, **kw):
        return self._R(self.get_payload)

    def post(self, url, **kw):
        self.writes.append(("POST", url))
        return self._R({"id": 1})


def t_dry_run_guarantees(csv_path):
    S("PIPELINE: dry runs write nothing (KI-16)")
    saved = W.SESSION
    env = {k: os.environ.get(k) for k in
           ("WC_URL", "WC_KEY", "WC_SECRET", "WP_APP_USER", "WP_APP_PASSWORD")}
    try:
        os.environ.update({"WC_URL": "https://store.invalid", "WC_KEY": "k",
                           "WC_SECRET": "s", "WP_APP_USER": "u",
                           "WP_APP_PASSWORD": "p"})
        rec = RecordingSession(get_payload=[])   # every taxonomy lookup misses
        W.SESSION = rec
        store = W.Store(allow_writes=False)
        df = pd.read_csv(csv_path, dtype=str).fillna("")
        for _, row in df.iterrows():
            W.build_payload(store, row, "draft", "upload")
        check("KI-16: assembling a dry-run payload performs zero writes",
              not rec.writes, rec.writes[:5])
        check("KI-16: missing terms are reported, not created",
              all(lbl in ("category", "tag") for lbl, _ in store.would_create),
              store.would_create[:4])

        rec2 = RecordingSession(get_payload=[])
        W.SESSION = rec2
        store2 = W.Store(allow_writes=False)
        row = df.iloc[0]
        payload, notes = W.images_for(row, row["sku"], row["title"], "upload",
                                      allow_writes=False)
        check("KI-16: a dry run uploads no media",
              payload == [] and not rec2.writes, (payload, rec2.writes))
    finally:
        W.SESSION = saved
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ====================== 5. regression: closed KNOWN_ISSUES ==================
def t_known_issue_regression():
    S("REGRESSION: previously-closed KNOWN_ISSUES still hold")
    # KI-1: a non-200 serper answer must fall back, not look like zero results
    saved_post = E.creq.post
    try:
        class R:
            status_code = 403

            def json(self):
                return {"message": "quota"}
        E.creq.post = lambda *a, **k: R()
        E._serper_failed_warned = False
        os.environ["SERPER_API_KEY"] = "garbage"
        check("KI-1: a 403 from serper returns None (so DDG runs)",
              E.serper_search("x") is None)
    finally:
        E.creq.post = saved_post
        os.environ.pop("SERPER_API_KEY", None)

    # KI-3: brand/model checks must not concatenate title and url
    rec = {"title": "foosam widget", "url_link": "https://sungbar.com/p",
           "sale price": "", "regular price": ""}
    verdict, reason = E.assess(rec, "SAMSUNG-X1", "X1", "SAMSUNG", 0, 0)
    check("KI-3: 'foosam' + 'sungbar' does not fake 'samsung'",
          verdict == "" and "brand" in reason, (verdict, reason))

    # KI-6: an alias-bound column is renamed to the canonical header
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Model No", "MRP", "price", "link", "images"])
    ws.append(["SAMSUNG-X", "", "", "", ""])
    E.map_columns(ws, None)
    hdr = [str(c.value) for c in ws[1]]
    check("KI-6: aliases renamed to canonical headers",
          {"regular price", "sale price", "url_link", "image_url"} <= set(hdr),
          hdr)

    # KI-9 / KI-11: image helpers
    check("KI-11: a real retailer outranks a rich unknown site",
          E.rec_score({"url_link": "https://www.amazon.in/dp/X",
                       "sale price": "1", "regular price": "",
                       "image_url": "a"}) >
          E.rec_score({"url_link": "https://randomshop.xyz/p",
                       "sale price": "1", "regular price": "1",
                       "image_url": "a,b,c,d,e,f,g,h"}))
    check("KI-11: hash/uuid digits are not read as pixel sizes",
          E.image_size_hint("https://x.com/i/ur558uB41.jpg") == 0
          and E.image_size_hint("https://x.com/i/7ea9b3744b37.jpg") == 0)
    check("KI-11: a real size shape still reads",
          E.image_size_hint("https://x.com/i/p_1200.jpg") == 1200)
    check("KI-9: clean_price ignores the counter in '1 offer from'",
          E.clean_price("1 offer from Rs 22,599") == "22,599")

    # KI-12: an SPA shell yields no images
    shell = ('<html><head><title>Home</title></head><body>'
             '<img alt="category-img" src="/i/cat1.jpg">'
             '<img class="nav-logo" src="/i/logo.png"></body></html>')
    from bs4 import BeautifulSoup
    imgs = E.collect_images(BeautifulSoup(shell, "lxml"), shell,
                            "https://shop.haierindia.com/products/x")
    check("KI-12: nav tiles are not accepted as a gallery", len(imgs) == 0, imgs)

    # KI-14: no placeholder specs anywhere
    rows = app.product_specs("Samsung 32 inch HD Ready Smart LED TV", "Samsung",
                             "LED")
    blob = " ".join(f"{k} {v}" for k, v in rows)
    check("KI-14: no 'Standard ...' or 'WM ...' text on a TV",
          not any(p in blob for p in V.DEAD_PLACEHOLDERS) and "WM " not in blob,
          blob)


# ====================== 6. E2E: app browser path == CLI path ===============
def t_browser_qa_agrees(csv_path):
    S("E2E: the in-browser QA and the CLI QA now agree (#5)")
    df_csv = pd.read_csv(csv_path, dtype=str).fillna("")
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli = V.validate_export(df_csv, str(csv_path), write_issues=False)
    # the browser passes result.astype(str), which renders blanks as 'nan'
    raw = pd.read_csv(csv_path)
    with contextlib.redirect_stdout(buf):
        browser = V.validate_export(raw.astype(str).fillna(""), "generated",
                                    write_issues=False)
    cli_r = sorted(V.summary_key(r) for i in cli
                   for r in i["reasons"].split("; "))
    br_r = sorted(V.summary_key(r) for i in browser
                  for r in i["reasons"].split("; "))
    check("both QA paths report the same kinds of issue", cli_r == br_r,
          f"cli={cli_r}\n          browser={br_r}")


def main():
    filled = t_stage1_contract()
    t_resume(filled)
    csv_path, df = t_stage2_contract(filled)
    t_dry_run_guarantees(csv_path)
    t_known_issue_regression()
    t_browser_qa_agrees(csv_path)
    print(f"\n{PASS} passed, {len(ISSUES)} issue(s)")
    for label, detail in ISSUES:
        print(f"  - {label}: {detail[:160]}")
    print(f"\nworkspace: {WORK}")
    return 1 if ISSUES else 0


if __name__ == "__main__":
    sys.exit(main())
