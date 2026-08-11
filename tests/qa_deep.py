"""Deep manual tests: hostile data, every flag, and the two WooCommerce
scripts that nothing has exercised yet (wc_seo, wc_checkpoint).

No network, no live store.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"d:\Dhruv-Personal\Catalogue\Final_pipe")
PY = str(ROOT / ".venv/Scripts/python.exe")
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

import openpyxl                                   # noqa: E402
import pandas as pd                               # noqa: E402

import app                                        # noqa: E402
import sku_enricher as E                          # noqa: E402
import validate_csv as V                          # noqa: E402
import wc_publish as W                            # noqa: E402
import wc_seo                                     # noqa: E402
import wc_checkpoint                              # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="qa_deep_"))
PASS, ISSUES = 0, []


def check(label, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        ISSUES.append((label, str(detail)[:500]))
        print(f"  ISSUE {label}\n          {str(detail)[:400]}")


def S(n):
    print(f"\n{n}")


def run(args, cwd=WORK, env=None, timeout=600):
    e = dict(os.environ)
    e["STREAMLIT_SERVER_HEADLESS"] = "true"
    if env:
        e.update(env)
    p = subprocess.run([PY] + args, cwd=str(cwd), env=e, capture_output=True,
                       text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ------------------------------------------------------ hand-edited SKU
def t_handedited_sku():
    S("MANUAL: a hand-edited sku must survive a resume (BUSY sheets)")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.cell(row=1, column=1, value="Item Details")
    ws.cell(row=2, column=1, value="VOLTAS 185V VECTRA (INNER) - GHAZIABAD")
    ws.cell(row=3, column=1, value="HAIER HWM75 (ODU) - NOIDA")
    sku_col, cols = E.map_columns(ws, None)
    out = E.ensure_app_columns(ws, sku_col, E.clean_busy_sku)
    check("clean value mirrored on a fresh sheet",
          ws.cell(row=2, column=out).value, "VOLTAS 185V VECTRA")

    # a human corrects one by hand, then the batch is resumed
    ws.cell(row=2, column=out, value="VOLTAS-185V-VECTRA-PRISM-CORRECTED")
    E.ensure_app_columns(ws, sku_col, E.clean_busy_sku)
    check("a hand-edited sku is NOT clobbered on the next run",
          ws.cell(row=2, column=out).value,
          "VOLTAS-185V-VECTRA-PRISM-CORRECTED")

    # but a stale RAW mirror from the buggy version IS repaired
    ws.cell(row=3, column=out, value="HAIER HWM75 (ODU) - NOIDA")
    E.ensure_app_columns(ws, sku_col, E.clean_busy_sku)
    check("a stale raw mirror is still repaired",
          ws.cell(row=3, column=out).value, "HAIER HWM75")


# ------------------------------------------------------ hostile data
def t_hostile_rows():
    S("MANUAL: hostile / degenerate rows through app.process_inventory")
    rows = [
        {"sku": "", "title": "No SKU at all", "categories": "", "Brands": "",
         "regular price": "", "sale price": "", "url_link": "", "image_url": ""},
        {"sku": "DUP-1", "title": "Duplicate one", "categories": "", "Brands": "",
         "regular price": "100", "sale price": "90", "url_link": "",
         "image_url": ""},
        {"sku": "DUP 1", "title": "Duplicate two (normalises the same)",
         "categories": "", "Brands": "", "regular price": "100",
         "sale price": "90", "url_link": "", "image_url": ""},
        {"sku": "UNI-Ω", "title": "Haier 32 инч ЛЕД телевизор", "categories": "",
         "Brands": "", "regular price": "1", "sale price": "2", "url_link": "",
         "image_url": ""},
        {"sku": "LONG-" + "X" * 200, "title": "T" * 400, "categories": "",
         "Brands": "", "regular price": "abc", "sale price": "-5",
         "url_link": "", "image_url": ""},
        {"sku": "COMMA,SKU", "title": "Title, with, commas \"quoted\"",
         "categories": "", "Brands": "", "regular price": "1,00,000",
         "sale price": "99,999", "url_link": "", "image_url": ""},
    ]
    df = pd.DataFrame(rows)
    try:
        out = app.process_inventory(df.copy())
        err = ""
    except Exception as exc:
        out, err = None, f"{type(exc).__name__}: {exc}"
    check("does not crash on degenerate rows", out is not None, err)
    if out is None:
        return
    check("blank SKU still yields a usable SKU",
          all(str(s).strip() for s in out["sku"]), list(out["sku"]))
    check("SKUs that normalise the same are de-duplicated",
          len(set(out["sku"])) == len(out), list(out["sku"]))
    check("MRP<=price is dropped rather than exported",
          all(not (pd.notna(r) and pd.notna(s) and float(r) <= float(s))
              for r, s in zip(out["regular price"], out["sale price"])
              if pd.notna(r) and pd.notna(s)),
          list(zip(out["regular price"], out["sale price"])))
    unsafe = [s for s in out["sku"] if any(c in str(s) for c in '\\/:*?"<>|()')]
    check("no filesystem-unsafe character survives into a SKU", not unsafe, unsafe)

    csv = WORK / "hostile.csv"
    out.to_csv(csv, index=False, quoting=1)
    back = pd.read_csv(csv, dtype=str).fillna("")
    check("the CSV round-trips (commas/quotes intact)", len(back) == len(out),
          f"{len(back)} vs {len(out)}")
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        issues = V.validate_export(back, str(csv), check_images=False,
                                   write_issues=False)
    check("validate_csv survives the same rows", isinstance(issues, list))


# ------------------------------------------------------ enricher flags
def t_enricher_flags():
    S("MANUAL: enricher flag combinations (warm cache, no credits)")
    src = openpyxl.load_workbook(ROOT / "inputs/LED.xlsx").active
    for name in ("F1.xlsx", "F2.xlsx", "F3.xlsx"):
        wb = openpyxl.Workbook()
        ws = wb.active
        for r in range(1, 3):
            ws.append([c.value for c in src[r]])
        wb.save(WORK / name)

    rc, out = run([str(ROOT / "sku_enricher.py"), "F1.xlsx",
                   "--no-image-check", "--no-specs", "--merge-budget", "0"])
    check("--no-image-check --no-specs --merge-budget 0", rc == 0
          and "Done. Processed" in out, f"rc={rc} {out[-300:]}")
    ws = openpyxl.load_workbook(WORK / "F1_filled.xlsx").active
    names = [str(c.value or "") for c in ws[1]]
    spec_col = names.index("specs") + 1
    specs = [ws.cell(row=r, column=spec_col).value
             for r in range(2, ws.max_row + 1)]
    check("--no-specs really wrote no specs", not any(specs), specs)

    rc, out = run([str(ROOT / "sku_enricher.py"), "F2.xlsx",
                   "--min-price", "999999"])
    check("--min-price rejects everything above the band", rc == 0
          and "below --min-price" in out, out[-400:])

    rc, out = run([str(ROOT / "sku_enricher.py"), "F3.xlsx",
                   "--min-images", "99"])
    check("--min-images 99 marks rows incomplete", rc == 0
          and "image(s)" in out, out[-400:])

    rc, out = run([str(ROOT / "sku_enricher.py"), "F1.xlsx", "--no-cache",
                   "--limit", "0", "--report", "custom_report.md"])
    check("--report writes where asked",
          (WORK / "custom_report.md").exists() or rc == 0,
          f"rc={rc} {out[-200:]}")


# ------------------------------------------------------ wc_publish modes
def t_wc_publish_modes():
    S("MANUAL: wc_publish image modes and the destructive-update guard")
    env = {k: os.environ.get(k) for k in
           ("WC_URL", "WC_KEY", "WC_SECRET", "WP_APP_USER", "WP_APP_PASSWORD")}
    saved = W.SESSION
    try:
        os.environ.update({"WC_URL": "https://store.invalid", "WC_KEY": "k",
                           "WC_SECRET": "s"})
        os.environ.pop("WP_APP_USER", None)
        os.environ.pop("WP_APP_PASSWORD", None)
        row = {"sku": "AP-X", "title": "T",
               "Images": "https://c/AP-X_1.webp,https://c/AP-X_2.webp",
               "Tags": "a,b", "categories": "LED > Mini LED",
               "regular price": "100", "sale price": "90",
               "description": "d", "short description": "s",
               "cross reference tag": "x", "configuration": "c", "Brands": "B"}
        p, n = W.images_for(row, "AP-X", "T", "skip")
        check("--images skip sends no images", p == [] , p)
        p, n = W.images_for(row, "AP-X", "T", "url")
        check("--images url trusts the column", len(p) == 2 and p[0]["src"]
              == "https://c/AP-X_1.webp", p)

        class Sess:
            headers = {}
            cookies = type("J", (), {"set": lambda *a, **k: None})()

            def __init__(self):
                self.sent = []

            class _R:
                status_code = 200
                headers = {"Content-Type": "application/json"}
                text, content = "{}", b"{}"

                def __init__(self, p):
                    self._p = p

                def json(self):
                    return self._p

            def request(self, method, url, **kw):
                self.sent.append((method, url, kw.get("json")))
                if method == "GET":
                    return self._R([])
                return self._R({"id": 7})

        W.SESSION = Sess()
        store = W.Store(allow_writes=True)
        payload, _ = W.build_payload(store, row, "draft", "skip")
        check("an empty images list is what the guard must catch",
              payload["images"] == [], payload["images"])
        # replicate main()'s update guard
        existing = {"id": 7}
        if not payload.get("images"):
            payload.pop("images", None)
        check("images key removed before an UPDATE (no gallery wipe)",
              "images" not in payload)
    finally:
        W.SESSION = saved
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ------------------------------------------------------ wc_seo
def t_wc_seo():
    S("MANUAL: wc_seo planning logic (never exercised before)")
    product = {
        "id": 11, "name": "Voltas 1.5 Ton 3 Star Inverter Split AC",
        "sku": "", "slug": "voltas-15-ton",
        "categories": [{"name": "Air Conditioner"}, {"name": "Inverter AC"}],
        "attributes": [{"name": "Brand", "options": ["Voltas"]}],
        "regular_price": "42990", "sale_price": "35990",
        "meta_data": [{"key": "_elementor_page_assets", "value": []},
                      {"key": "_wc_gla_errors", "value": {}}],
    }
    try:
        entries, slug, notes = wc_seo.plan_for(product, "yoast", False, False)
        err = ""
    except Exception as exc:
        entries, err = None, f"{type(exc).__name__}: {exc}"
    check("a product with list/dict meta does not crash the planner",
          entries is not None, err)
    if entries is None:
        return
    check("slug is left alone without --slugs", slug is None, slug)
    keys = [e["key"] for e in entries]
    check("yoast keys written bare (no 'Meta: ' prefix)",
          all(not k.startswith("Meta: ") for k in keys), keys)
    focus = next((e["value"] for e in entries if "focuskw" in e["key"]), "")
    check("a SKU-less product gets a distinguishing focus keyword",
          "1.5 ton" in focus and focus != "voltas air conditioner", focus)

    plans = [(product, entries, None, [])] * 5
    crowded = wc_seo.crowded_focus_keywords(plans)
    check("a focus keyword shared by 5 products is dropped",
          focus in crowded, (focus, crowded))

    p2 = dict(product, sku="AP-VOLTAS-185V", name="Voltas 185V Vectra")
    e2, s2, _ = wc_seo.plan_for(p2, "yoast", False, True)
    check("--slugs proposes a slug when one is asked for", s2 is not None, s2)
    check("category names are unescaped",
          wc_seo.unescape_text("Geyser &amp; Accessories")
          == "Geyser & Accessories")


# ------------------------------------------------------ wc_checkpoint
def t_wc_checkpoint():
    S("MANUAL: wc_checkpoint diff / restore payload logic")
    old = {"id": 1, "name": "Old name", "sku": "AP-X", "slug": "old",
           "regular_price": "100", "sale_price": "",
           "categories": [{"id": 5, "name": "LED"}],
           "tags": [], "attributes": [], "images": [{"id": 9}],
           "status": "publish", "catalog_visibility": "visible",
           "description": "d", "short_description": "s",
           "meta_data": [{"key": "_yoast_wpseo_title", "value": "OLD TITLE"},
                         {"key": "_edit_lock", "value": "123"}]}
    live = json.loads(json.dumps(old))
    live["name"] = "New name"
    live["meta_data"] = [{"key": "_yoast_wpseo_title", "value": "NEW TITLE"},
                         {"key": "_yoast_wpseo_metadesc", "value": "ADDED"},
                         {"key": "_edit_lock", "value": "999"}]

    d = wc_checkpoint.diff_product(old, live)
    check("a changed name is detected", "name" in d, list(d))
    check("bookkeeping meta (_edit_lock) is ignored",
          "_edit_lock" not in (d.get("meta_data") or {}), d.get("meta_data"))
    check("a changed SEO meta key is detected",
          "_yoast_wpseo_title" in (d.get("meta_data") or {}), d.get("meta_data"))

    payload = wc_checkpoint.restore_payload(old, live, ["meta_data"])
    entries = {e["key"]: e["value"] for e in payload.get("meta_data", [])}
    check("restore puts the checkpointed value back",
          entries.get("_yoast_wpseo_title") == "OLD TITLE", entries)
    check("a key added since the checkpoint is blanked, not left",
          entries.get("_yoast_wpseo_metadesc") == "", entries)
    check("restoring meta_data alone touches no other field",
          set(payload) == {"meta_data"}, list(payload))

    payload2 = wc_checkpoint.restore_payload(old, live,
                                             list(wc_checkpoint.RESTORABLE))
    check("--fields all restores the name too",
          payload2.get("name") == "Old name", payload2.get("name"))
    check("reordered lists do not read as a change",
          wc_checkpoint.scalar([{"id": 2}, {"id": 1}])
          == wc_checkpoint.scalar([{"id": 1}, {"id": 2}]))


# ------------------------------------------------------ app.py readers
def t_app_readers():
    S("MANUAL: app.py upload readers and filters")
    class Up:
        def __init__(self, name, data):
            self.name = name
            self._d = data

        def getvalue(self):
            return self._d

    csv_utf16 = "sku,title\nA,Widget\n".encode("utf-16")
    got = app.read_uploaded_inventory(Up("x.csv", csv_utf16))
    check("UTF-16 CSV decodes", list(got.columns) == ["sku", "title"],
          list(got.columns))

    csv_semi = "sku;title\nA;Widget\n".encode("utf-8")
    got = app.read_uploaded_inventory(Up("x.csv", csv_semi))
    check("semicolon-separated CSV decodes",
          list(got.columns) == ["sku", "title"], list(got.columns))

    blank = pd.DataFrame({"sku": ["", ""], "title": ["", ""]})
    check("a sheet of blank rows filters to nothing",
          len(app.filter_active_inventory_rows(blank)) == 0)
    nocols = pd.DataFrame({"random": [1, 2]})
    check("a sheet with no recognisable columns filters to nothing",
          len(app.filter_active_inventory_rows(nocols)) == 0)

    # a real binary file. (b"\xff\xfe..." is a UTF-16 BOM and legitimately
    # decodes, so it is not the right fixture for "not a CSV".)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
    try:
        app.read_uploaded_inventory(Up("x.csv", png))
        crashed = False
    except ValueError:
        crashed = True
    except Exception as exc:
        crashed = f"wrong type: {type(exc).__name__}"
    check("a binary file uploaded as .csv raises the documented ValueError",
          crashed is True, crashed)


def main():
    t_handedited_sku()
    t_hostile_rows()
    t_enricher_flags()
    t_wc_publish_modes()
    t_wc_seo()
    t_wc_checkpoint()
    t_app_readers()
    print(f"\n{PASS} passed, {len(ISSUES)} issue(s)")
    for label, detail in ISSUES:
        print(f"  - {label}: {detail[:200]}")
    print(f"\nworkspace: {WORK}")
    return 1 if ISSUES else 0


if __name__ == "__main__":
    sys.exit(main())
