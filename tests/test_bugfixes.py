"""Regression tests for the 15 bugs fixed in the 2026-08-10 sweep.

Every test here failed before that change and passes after it. Offline by
design - no network, no spreadsheet outside a temp directory, no WooCommerce
call. Run it with:

    .venv\\Scripts\\python.exe tests\\test_bugfixes.py

Numbering matches the bug list: #1 wc_publish image filenames, #2 404 as a
block, #3 BUSY sku mirroring, #4 invented specs, #5 NaN prices, #6 unmeasured
donor images, #7 screen size read as kg, #8 unreachable aspect check,
#9 duplicate media uploads, #10 HTML-escaped name diff, #11 missing sku column,
#12 KeyError as control flow, #13 branch name in the query, #14 gallery capped
before filtering, #15 dead second categoriser.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

import openpyxl                                   # noqa: E402
import pandas as pd                               # noqa: E402
from PIL import Image                             # noqa: E402

import app                                        # noqa: E402
import sku_enricher as E                          # noqa: E402
import validate_csv as V                          # noqa: E402
import wc_publish as W                            # noqa: E402

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
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")
        if UNDER_PYTEST:
            raise AssertionError(f"{label}: got {got!r}, want {want!r}")


def section(name):
    print(f"\n{name}")


# --------------------------------------------------------------------------
def test_02_dead_page_is_not_a_block():
    section("#2  a 404 is a dead page, not a block")
    check("404 is not a block", E.is_block_reason("http 404"), False)
    check("410 is not a block", E.is_block_reason("http 410"), False)
    check("403 is still a block", E.is_block_reason("http 403"), True)
    check("429 is still a block", E.is_block_reason("http 429"), True)
    check("503 is still a block", E.is_block_reason("http 503"), True)
    check("500 is still not a block", E.is_block_reason("http 500"), False)
    check("cloudflare is still a block",
          E.is_block_reason("cloudflare challenge"), True)
    # the consequence that actually cost us: a 404 must not close a domain
    E._COOLDOWN.clear()
    reasons = [("amazon.in", "http 404")]
    check("a lone 404 does not read as 'all sources blocked'",
          E.not_found_status(["u"], 1, reasons),
          "NOT FOUND - 1 page(s) checked, none matched")
    check("a lone 403 still reads as blocked",
          E.not_found_status(["u"], 1, [("amazon.in", "http 403")]),
          "NOT FOUND - all sources blocked (amazon.in http 403)")


# --------------------------------------------------------------------------
def test_07_screen_size_is_not_kilograms():
    section("#7  model digits are only a capacity for capacity-rated goods")
    check("43\" TV yields no kg capacity",
          E.expected_caps("SAMSUNG-UA43H4500", "UA43H4500",
                          "Samsung 43 inch Crystal 4K LED TV"), set())
    check("1.5 ton AC yields no kg capacity",
          E.expected_caps("VOLTAS-185V", "185V",
                          "Voltas 1.5 Ton 3 Star Split AC"), set())
    check("washer still yields its load rating",
          E.expected_caps("SAMSUNG-WW80T504DAB", "WW80T504DAB",
                          "Samsung 8 kg Front Load Washing Machine"), {8.0})
    check("an explicit kg in the SKU always counts, even on a TV row",
          E.expected_caps("HAIER-7.5KG-HWM75", "HWM75",
                          "Haier LED TV 32 inch"), {7.5})
    # and the rejection it used to cause
    rec = {"title": "Samsung 43 inch Crystal 4K Ultra HD Smart LED TV",
           "url_link": "https://www.samsung.com/in/tvs/ua43cue60ak/",
           "sale price": "32990", "regular price": ""}
    verdict, reason = E.assess(rec, "SAMSUNG-UA43CUE60AK", "UA43CUE60AK",
                               "SAMSUNG", 0, 0)
    check("a matching TV page is no longer rejected for 'lacking capacity'",
          verdict, "strong")


# --------------------------------------------------------------------------
def test_12_get_session_no_keyerror():
    section("#12 first use of a domain does not raise KeyError internally")
    E._SESSIONS.pop("example.test", None)
    s1 = E.get_session("example.test")
    s2 = E.get_session("example.test")
    check("same session returned on re-use", s1 is s2, True)
    s3 = E.get_session("example.test", fresh=True)
    check("fresh=True replaces it", s3 is not s1, True)
    E._SESSIONS.pop("example.test", None)


# --------------------------------------------------------------------------
def test_13_branch_name_leaves_the_query():
    section("#13 the stocking branch is not part of the model code")
    check("branch stripped",
          E.clean_busy_sku("VOLTAS 185V VECTRA PRISM (INNER) - GHAZIABAD"),
          "VOLTAS 185V VECTRA PRISM")
    check("two trailing branch notes stripped",
          E.clean_busy_sku("SAMSUNG WA70A4002GS (ODU) - NOIDA - MAIN"),
          "SAMSUNG WA70A4002GS")
    check("a segment carrying digits is kept (it may be the model)",
          E.clean_busy_sku("GODREJ WT EON 700 - 7 KG"),
          "GODREJ WT EON 700 - 7 KG")
    check("a hyphenated model code is untouched",
          E.clean_busy_sku("SAMSUNG WA70A4022FS-TL"),
          "SAMSUNG WA70A4022FS-TL")
    check("split halves still collapse to one model",
          E.clean_busy_sku("VOLTAS 185V (INNER) - X")
          == E.clean_busy_sku("VOLTAS 185V (OUTER) - X"), True)


# --------------------------------------------------------------------------
def test_14_gallery_capped_after_filtering():
    section("#14 the size filter gets spares to work with")
    urls = [f"https://cdn.example.com/product/photo{i}_1200.jpg"
            for i in range(1, 13)]
    check("dedupe still defaults to MAX_IMAGES",
          len(E.dedupe_images(urls, "https://example.com/p")), E.MAX_IMAGES)
    check("a pool can be requested",
          len(E.dedupe_images(urls, "https://example.com/p", limit=E.IMAGE_POOL)),
          min(len(urls), E.IMAGE_POOL))
    check("pool is at least the probe budget", E.IMAGE_POOL >= E.MAX_IMAGES, True)
    rec = {"image_url": ",".join(urls)}
    check("cap_images trims to the shipping gallery",
          len(E.split_image_list(E.cap_images(rec)["image_url"])), E.MAX_IMAGES)
    rec2 = {"image_url": ",".join(urls[:3])}
    check("cap_images leaves a short gallery alone",
          E.cap_images(rec2)["image_url"], ",".join(urls[:3]))


# --------------------------------------------------------------------------
def test_06_donated_images_are_measured():
    section("#6  images merged from another source are size-checked")
    big = [f"https://a.example.com/i/big{i}_1200.jpg" for i in (1, 2, 3)]
    donor_ok = "https://b.example.com/i/good_1200.jpg"
    donor_tiny = "https://b.example.com/i/tiny_1200.jpg"
    # pre-seed the measurement memo so nothing goes near the network
    E._IMG_DIMS.update({u: (1200, 1200) for u in big})
    E._IMG_DIMS[donor_ok] = (1400, 1400)
    E._IMG_DIMS[donor_tiny] = (120, 120)

    best = {"title": "t", "sale price": "1000", "regular price": "",
            "url_link": "https://a.example.com/p/1",
            "image_url": ",".join(big[:1]), "specs": ""}
    other = {"title": "t", "sale price": "1000", "regular price": "",
             "url_link": "https://amazon.in/dp/X",
             "image_url": f"{donor_ok},{donor_tiny}", "specs": ""}
    merged, donors = E.merge_sources(best, [best, other], min_images=3)
    got = E.split_image_list(merged["image_url"])
    check("the good donated image was taken", donor_ok in got, True)
    check("the 120px donated image was NOT taken", donor_tiny in got, False)


# --------------------------------------------------------------------------
def test_03_busy_sku_mirroring():
    section("#3  BUSY ERP: the cleaned model is what reaches app.py")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.cell(row=1, column=1, value="Item Details")
    raws = ["VOLTAS 185V VECTRA PRISM (INNER) - GHAZIABAD",
            "VOLTAS 185V VECTRA PRISM (OUTER) - GHAZIABAD"]
    for i, v in enumerate(raws, start=2):
        ws.cell(row=i, column=1, value=v)

    sku_col, cols = E.map_columns(ws, None)
    busy = E.norm_header(ws.cell(row=1, column=sku_col).value) in E.BUSY_SKU_HEADERS
    check("BUSY input detected", busy, True)
    sku_out = E.ensure_app_columns(ws, sku_col,
                                   E.clean_busy_sku if busy else None)

    mirrored = [ws.cell(row=r, column=sku_out).value for r in (2, 3)]
    check("row 2 mirrors the cleaned model", mirrored[0],
          "VOLTAS 185V VECTRA PRISM")
    check("both split halves mirror the SAME model",
          mirrored[0] == mirrored[1], True)
    check("what app.py exports is a clean SKU",
          app.prefixed_sku(mirrored[0], "Air Conditioner"),
          "AC-VOLTAS-185V-VECTRA-PRISM")
    check("app.py can now collapse the split-AC duplicate",
          app.prefixed_sku(mirrored[0], "Air Conditioner")
          == app.prefixed_sku(mirrored[1], "Air Conditioner"), True)

    # self-healing: a sheet left behind by the buggy version gets repaired
    ws.cell(row=2, column=sku_out, value=raws[0])
    E.ensure_app_columns(ws, sku_col, E.clean_busy_sku)
    check("a stale raw value from an earlier run is rewritten",
          ws.cell(row=2, column=sku_out).value, "VOLTAS 185V VECTRA PRISM")

    # a non-BUSY sheet must be untouched by any of this
    wb2 = openpyxl.Workbook()
    ws2 = wb2.active
    ws2.cell(row=1, column=1, value="Model No")
    ws2.cell(row=2, column=1, value="SAMSUNG-WW80T504DAB")
    c2, _ = E.map_columns(ws2, None)
    out2 = E.ensure_app_columns(ws2, c2, None)
    check("a normal sheet still mirrors verbatim",
          ws2.cell(row=2, column=out2).value, "SAMSUNG-WW80T504DAB")

    check("brackets never survive into a SKU",
          app.prefixed_sku("VOLTAS 185V (INNER)", "Air Conditioner"),
          "AC-VOLTAS-185V-INNER")


# --------------------------------------------------------------------------
def test_04_no_invented_specs():
    section("#4  a spec the listing does not state is omitted")
    check("iron with no soleplate stated",
          app.capacity_value("Bajaj DX-6 1000W Dry Iron", "Electric Irons"), "")
    check("iron that DOES state one",
          app.capacity_value("Philips GC1905 Non-Stick Soleplate Steam Iron",
                             "Electric Irons"), "Non-Stick Soleplate")
    check("sewing machine with nothing stated",
          app.capacity_value("Usha Wonder Stitch", "Sewing Machines"), "")
    check("sewing machine that states a stitch count",
          app.capacity_value("Usha Dream Stitch 14 Stitch Machine",
                             "Sewing Machines"), "14 Built-in Stitches")
    check("kettle with no material stated",
          app.configuration_text("Prestige PKOSS 1.5L Electric Kettle",
                                 "Electric Kettles"), "")
    check("kettle that states its body",
          app.configuration_text("Prestige 1.5L Stainless Steel Electric Kettle",
                                 "Electric Kettles"),
          "Stainless Steel Electric Kettle")
    check("water heater that says neither storage nor instant",
          app.configuration_text("Racold 15L Water Heater", "Water Heaters"), "")
    check("storage geyser still labelled correctly",
          app.configuration_text("Racold 15L Storage Water Heater",
                                 "Water Heaters"), "Storage Water Geyser")
    check("instant geyser still labelled correctly",
          app.configuration_text("Bajaj 3L Instant Water Heater",
                                 "Water Heaters"), "Instant Water Heater")
    # and no row is emitted for a value we could not read
    rows = app.product_specs("Bajaj DX-6 1000W Dry Iron", "Bajaj",
                             "Electric Irons")
    labels = [k for k, _ in rows]
    check("no empty 'Soleplate Variant' row is emitted",
          "Soleplate Variant" in labels, False)
    check("the wattage it DID state is still there",
          ("Power Rating", "1000W") in rows, True)
    check("the retired strings are on the dead-placeholder watchlist",
          {"Premium Non-Stick Glide", "Built-in Straight Stitching"}
          <= set(V.DEAD_PLACEHOLDERS), True)


# --------------------------------------------------------------------------
def test_05_nan_is_not_a_price():
    section("#5  'nan' is a missing price, not a number")
    check("nan -> None", V.to_number("nan"), None)
    check("NaN -> None", V.to_number("NaN"), None)
    check("inf -> None", V.to_number("inf"), None)
    check("None -> None", V.to_number("None"), None)
    check("'' -> None", V.to_number(""), None)
    check("a real price still parses", V.to_number("1,234.50"), 1234.5)
    check("a rupee price still parses", V.to_number("₹22,599"), 22599.0)
    # the failure it caused: the browser QA missed a priceless row
    df = pd.DataFrame([{"sku": "AP-X", "title": "A perfectly long title here",
                        "Images": "", "description": "d",
                        "categories": "LED > Mini LED",
                        "regular price": float("nan"),
                        "sale price": float("nan")}]).astype(str)
    buf = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(buf):
        issues = V.validate_export(df, "t", check_images=False,
                                   write_issues=False)
    reasons = issues[0]["reasons"] if issues else ""
    check("missing_price is now reported through astype(str)",
          "missing_price" in reasons, True)


# --------------------------------------------------------------------------
def test_08_aspect_check_reachable():
    section("#8  shape and resolution are independent faults")
    tmp = Path(tempfile.mkdtemp())
    try:
        # 900x250 = 3.6:1, past MAX_IMAGE_ASPECT. Exactly 3:1 is deliberately
        # allowed ('> MAX_IMAGE_ASPECT'), so the fixture has to clear it.
        Image.new("RGB", (900, 250), "white").save(tmp / "sliver.png")
        Image.new("RGB", (1600, 1600), "white").save(tmp / "good.png")
        Image.new("RGB", (1800, 400), "white").save(tmp / "bigbanner.png")
        got = " ".join(V.image_dimension_problems(["sliver.png"], root=str(tmp)))
        check("a 900x250 sliver is called low resolution",
              "low resolution" in got, True)
        check("...AND banner shape, which used to be unreachable",
              "banner shape" in got, True)
        check("a 1600x1600 photo is clean",
              V.image_dimension_problems(["good.png"], root=str(tmp)), [])
        got2 = " ".join(V.image_dimension_problems(["bigbanner.png"], root=str(tmp)))
        check("a large banner is still flagged", "banner shape" in got2, True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
class FakeResp:
    def __init__(self, payload=None, status=200, ctype="application/json"):
        self._payload = payload if payload is not None else {}
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self.text = "{}"
        self.content = b"{}"

    def json(self):
        return self._payload


class FakeSession:
    """Records calls so a test can assert what would have hit the store."""

    def __init__(self, media=()):
        self.media = list(media)
        self.gets, self.posts = [], []
        self.headers = {}
        self.cookies = type("J", (), {"set": lambda *a, **k: None})()

    def get(self, url, **kw):
        self.gets.append((url, kw))
        return FakeResp(self.media)

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return FakeResp({"id": 900 + len(self.posts)})


def test_09_10_11_wc_publish():
    section("#9  the Media Library is not duplicated on every push")
    saved = W.SESSION
    env_backup = {k: os.environ.get(k) for k in
                  ("WC_URL", "WP_APP_USER", "WP_APP_PASSWORD")}
    tmp = Path(tempfile.mkdtemp())
    try:
        os.environ["WC_URL"] = "https://store.invalid"
        os.environ["WP_APP_USER"] = "u"
        os.environ["WP_APP_PASSWORD"] = "p"
        local = tmp / "AP-X_2.webp"
        Image.new("RGB", (1200, 1200), "white").save(local, format="WEBP")
        size = local.stat().st_size

        W._MEDIA_BY_NAME.clear()
        W.SESSION = FakeSession(media=[{
            "id": 4242,
            "source_url": "https://store.invalid/wp-content/uploads/AP-X_2.webp",
            "media_details": {"file": "2026/08/AP-X_2.webp", "filesize": size}}])
        check("an identical file already in the library is re-used",
              W.upload_image(local, "alt"), 4242)
        check("...and nothing was uploaded", len(W.SESSION.posts), 0)

        W._MEDIA_BY_NAME.clear()
        W.SESSION = FakeSession(media=[{
            "id": 4242,
            "source_url": "https://store.invalid/wp-content/uploads/AP-X_2.webp",
            "media_details": {"file": "2026/08/AP-X_2.webp",
                              "filesize": size + 999}}])
        got = W.upload_image(local, "alt")
        check("a CHANGED file of the same name is uploaded afresh",
              got not in (4242, None), True)
        check("...which was one upload plus one alt-text write",
              len(W.SESSION.posts), 2)

        W._MEDIA_BY_NAME.clear()
        W.SESSION = FakeSession(media=[])
        W.upload_image(local, "alt")
        before = len(W.SESSION.posts)
        W.upload_image(local, "alt")
        check("the same file twice in one run uploads once",
              len(W.SESSION.posts), before)

        section("#10 the name diff ignores WordPress's HTML escaping")
        payload = {"name": "Haier 1.5 Ton AC & Cover", "regular_price": "100",
                   "sale_price": "", "tags": [], "attributes": [], "images": [],
                   "meta_data": []}
        existing = {"name": "Haier 1.5 Ton AC &amp; Cover",
                    "regular_price": "100"}
        check("an unchanged escaped name reports no change",
              "changes: none detected" in W.describe(payload, existing), True)
        check("a genuinely changed name is still reported",
              "name" in W.describe(payload, {"name": "Something else",
                                             "regular_price": "100"}), True)
    finally:
        W.SESSION = saved
        W._MEDIA_BY_NAME.clear()
        for k, v in env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    section("#11 a CSV with no sku column exits with a sentence")
    tmp2 = Path(tempfile.mkdtemp())
    try:
        bad = tmp2 / "nosku.csv"
        bad.write_text("title,price\nA widget,10\n", encoding="utf-8")
        env = dict(os.environ, WC_URL="https://store.invalid",
                   WC_KEY="k", WC_SECRET="s")
        proc = subprocess.run(
            [str(ROOT / ".venv/Scripts/python.exe"), str(ROOT / "wc_publish.py"),
             str(bad)],
            capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120)
        blob = proc.stdout + proc.stderr
        check("exits non-zero", proc.returncode != 0, True)
        check("says what is wrong", "has no 'sku' column" in blob, True)
        check("no raw traceback", "Traceback" in blob, False)
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)


# --------------------------------------------------------------------------
def test_15_one_categoriser():
    section("#15 only one categoriser survives")
    check("the dead flat categoriser is gone",
          hasattr(app, "infer_category"), False)
    check("the real one still works",
          app.detect_category("Samsung 8 kg Front Load Washing Machine"),
          ("Washing Machines", "Front Load"))


# --------------------------------------------------------------------------
def test_01_e2e_sparse_gallery():
    """#1 end to end: app.py writes a sparse gallery -> CSV -> validate_csv ->
    wc_publish resolves the SAME files off disk. This is the whole chain the
    positional-filename bug ran through."""
    section("#1  E2E: sparse image indexes survive app.py -> CSV -> wc_publish")

    sizes = {"a": (1400, 1400), "b": (300, 300), "c": (1600, 1600),
             "d": (200, 200), "e": (1500, 1500)}

    class R:
        def __init__(self, data):
            self.content = data

        def raise_for_status(self):
            pass

    def fake_get(url, **kw):
        key = url.rsplit("/", 1)[1].split(".")[0]
        buf = io.BytesIO()
        Image.new("RGB", sizes[key], "white").save(buf, format="PNG")
        return R(buf.getvalue())

    tmp = Path(tempfile.mkdtemp())
    cwd = os.getcwd()
    real_get = app.requests.get
    # The image folder is per-client and ABSOLUTE now (clients/<slug>/ config,
    # Phase 1 item 1), so chdir alone no longer redirects it. Point the three
    # consumers at the scratch dir instead - a test must never write into the
    # real product_images_for_upload\.
    import dataclasses
    saved_image_dir = (app.IMAGE_DIR, V.IMAGE_DIR, W.CLIENT)
    try:
        os.chdir(tmp)
        app.IMAGE_DIR = tmp / app.IMAGE_ROOT
        V.IMAGE_DIR = str(tmp / app.IMAGE_ROOT)
        W.CLIENT = dataclasses.replace(W.CLIENT, image_dir=tmp / app.IMAGE_ROOT)
        app.requests.get = fake_get
        base = "https://cdn.example.com/img"
        df = pd.DataFrame([{
            "sku": "SAMSUNG-UA32H4500",
            "title": "Samsung 32 inch HD Ready Smart LED TV UA32H4500",
            "categories": "", "Brands": "",
            "regular price": "24990", "sale price": "18990",
            "url_link": "https://www.amazon.in/dp/B01",
            # b and d are below MIN_IMAGE_EDGE -> indexes 2 and 4 are dropped
            "image_url": ",".join(f"{base}/{k}.png" for k in "abcde"),
        }])
        out = app.process_inventory(df, media_base="https://shop.invalid/u/2026/08")

        sku = out.iloc[0]["sku"]
        names = [u.rsplit("/", 1)[1] for u in
                 app.split_image_list(out.iloc[0]["Images"])]
        check("only the three usable images are referenced", len(names), 3)
        check("the numbering really is sparse", names,
              [f"{sku}_1.webp", f"{sku}_3.webp", f"{sku}_5.webp"])
        on_disk = sorted(p.name for p in (tmp / app.IMAGE_ROOT).glob("*.webp"))
        check("the files on disk match the Images column", on_disk, sorted(names))

        # a leftover from an earlier batch - exactly what used to get published
        (tmp / app.IMAGE_ROOT / f"{sku}_2.webp").write_bytes(
            (tmp / app.IMAGE_ROOT / names[0]).read_bytes())

        csv_path = tmp / "out.csv"
        out.to_csv(csv_path, index=False, quoting=1)
        reread = pd.read_csv(csv_path, dtype=str).fillna("")

        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            issues = V.validate_export(reread, str(csv_path), write_issues=False)
        blob = " ".join(i["reasons"] for i in issues)
        check("validate_csv finds no phantom image links",
              "not downloaded locally" in blob, False)
        check("validate_csv finds no dead placeholder text",
              "placeholder_spec_text" in blob, False)

        row = reread.iloc[0]
        payload, notes = W.images_for(row, sku, "t", "upload", allow_writes=False)
        note_blob = " ".join(notes)
        check("wc_publish reports no missing local file",
              "missing local file" in note_blob, False)
        check("wc_publish found all three", "3 local .webp file(s) ready"
              in note_blob, True)

        W._MEDIA_BY_NAME.clear()
        saved, env_backup = W.SESSION, {k: os.environ.get(k) for k in
                                        ("WC_URL", "WP_APP_USER",
                                         "WP_APP_PASSWORD")}
        try:
            os.environ.update({"WC_URL": "https://store.invalid",
                               "WP_APP_USER": "u", "WP_APP_PASSWORD": "p"})
            W.SESSION = FakeSession(media=[])
            W.images_for(row, sku, "t", "upload", allow_writes=True)
            uploaded = [kw["headers"]["Content-Disposition"]
                        for _u, kw in W.SESSION.posts if "headers" in kw]
            check("exactly the three real files were uploaded", len(uploaded), 3)
            check("the stale _2.webp was NOT uploaded",
                  any("_2.webp" in d for d in uploaded), False)
            check("the last image _5.webp WAS uploaded",
                  any("_5.webp" in d for d in uploaded), True)
        finally:
            W.SESSION = saved
            W._MEDIA_BY_NAME.clear()
            for k, v in env_backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        app.requests.get = real_get
        app.IMAGE_DIR, V.IMAGE_DIR, W.CLIENT = saved_image_dir
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
def test_16_validate_cli_flags():
    section("#16 validate_csv.py parses its own flags")
    exe = str(ROOT / ".venv/Scripts/python.exe")
    script = str(ROOT / "validate_csv.py")
    p = subprocess.run([exe, script, "--help"], capture_output=True, text=True,
                       cwd=str(ROOT), timeout=120)
    check("--help exits 0", p.returncode, 0)
    check("--help prints usage rather than 'File not found'",
          "--warn-only" in p.stdout and "File not found" not in p.stdout, True)
    p = subprocess.run([exe, script, "--nonsense"], capture_output=True,
                       text=True, cwd=str(ROOT), timeout=120)
    check("an unknown option is named, not treated as a path",
          "Unknown option" in p.stdout, True)


def test_17_console_encoding():
    section("#17 a non-cp1252 character does not abort a run")
    tmp = Path(tempfile.mkdtemp())
    try:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["sku", "title", "categories", "Brands", "regular price",
                   "sale price", "url_link", "image_url", "status"])
        ws.append(["HAIER-Ω750", "Haier 32 inch HD Ready LED TV Ω750",
                   "", "", "24990", "18990", "http://x", "",
                   "FOUND (amazon.in)"])
        wb.save(tmp / "ENC_filled.xlsx")
        env = dict(os.environ, PYTHONIOENCODING="cp1252",
                   STREAMLIT_SERVER_HEADLESS="true")
        p = subprocess.run(
            [str(ROOT / ".venv/Scripts/python.exe"), str(ROOT / "build_csv.py"),
             "ENC_filled.xlsx", "-o", "enc.csv", "--no-validate"],
            capture_output=True, text=True, cwd=str(tmp), env=env, timeout=300)
        blob = p.stdout + p.stderr
        check("build_csv survives a cp1252 console", p.returncode, 0)
        check("...with no UnicodeEncodeError", "UnicodeEncodeError" in blob, False)
        check("...and actually wrote the CSV", (tmp / "enc.csv").exists(), True)
        # the helper itself, on every entry point that prints scraped text
        for mod in ("build_csv", "validate_csv", "wc_publish", "wc_seo",
                    "wc_checkpoint"):
            m = __import__(mod)
            check(f"{mod} has the utf-8 stdout guard",
                  hasattr(m, "use_utf8_stdout"), True)
        check("app.safe_print cannot raise on an unmappable character",
              app.safe_print("Ω test") is None, True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_18_binary_upload_rejected():
    section("#18 a binary file uploaded as .csv is named as such")

    class Up:
        def __init__(self, name, data):
            self.name = name
            self._d = data

        def getvalue(self):
            return self._d

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
    try:
        app.read_uploaded_inventory(Up("x.csv", png))
        got = "accepted binary as a table"
    except ValueError as exc:
        got = "ValueError" if "not a text CSV" in str(exc) else str(exc)[:40]
    check("a PNG renamed .csv raises a clear ValueError", got, "ValueError")
    for label, data in (("utf-8", "sku,title\nA,W\n".encode()),
                        ("utf-16", "sku,title\nA,W\n".encode("utf-16")),
                        ("semicolon", "sku;title\nA;W\n".encode())):
        df = app.read_uploaded_inventory(Up("x.csv", data))
        check(f"a real {label} CSV still parses", list(df.columns),
              ["sku", "title"])


# --------------------------------------------------------------------------
def test_19_category_prefixes():
    section("#19  SKU prefixes encode the parent category (decision 2026-08-11)")
    for parent, code in {"Washing Machines": "WM", "Water Heaters": "WH",
                         "LED": "TV", "Air Conditioner": "AC",
                         "Cooling & Freezing": "CF",
                         "Kitchen Appliances": "KA", "Home Appliances": "HA",
                         "Small Appliances": "SA",
                         "Electronics & Computing": "EC"}.items():
        check(f"{parent} -> {code}-",
              app.prefixed_sku("SAMSUNG X100", parent), f"{code}-SAMSUNG-X100")
    check("unknown category falls back to AP-",
          app.prefixed_sku("SAMSUNG X100", "Something New"), "AP-SAMSUNG-X100")
    check("the right prefix is never doubled",
          app.prefixed_sku("TV-SAMSUNG-X100", "LED"), "TV-SAMSUNG-X100")
    check("a legacy AP- SKU is re-coded once its category is known",
          app.prefixed_sku("AP-SAMSUNG-X100", "LED"), "TV-SAMSUNG-X100")
    check("a legacy AP- SKU keeps AP- while the category is unknown",
          app.prefixed_sku("AP-SAMSUNG-X100", "Something New"),
          "AP-SAMSUNG-X100")
    check("an ERP code that contradicts the category is re-coded",
          app.prefixed_sku("TV-VOLTAS-185V", "Air Conditioner"),
          "AC-VOLTAS-185V")
    check("WM- is NEVER renamed, even on a category mismatch (live products)",
          app.prefixed_sku("WM-HTW120-178BK", "LED"), "WM-HTW120-178BK")
    check("model_code strips every prefix",
          app.model_code("TV-SAMSUNG-UA43T5310"), "UA43T5310")
    check("the map and the tuple agree",
          set(app.SKU_PREFIXES),
          set(app.SKU_PREFIX_BY_PARENT.values()) | {"AP"})
    # the FAMILY names (family_for output) must never be handed to
    # prefixed_sku - process_inventory passes the PARENT; pin that no family
    # string is silently accepted as a category
    for family in ("Microwave Ovens", "Electric Kettles", "Water Dispensers"):
        check(f"family name '{family}' is not a prefix key",
              family in app.SKU_PREFIX_BY_PARENT, False)
    # cross-file sync: validate_csv's stripping regex and the enricher's
    # input-code list must cover every prefix app.py can emit
    for p in app.SKU_PREFIXES:
        check(f"validate_csv strips {p}-",
              bool(V.SKU_PREFIX_RE.match(f"{p}-X")), True)
        check(f"enricher recognises {p} as a category code",
              p in E.CATEGORY_CODES, True)

    # PRODUCT_PLAN item 2: the four hand-synced copies became views of the
    # client's [sku] block. The expected values below are the LITERAL tables
    # app.py carried before that refactor - shreejee's output must not move by
    # a single byte.
    check("SKU_PREFIX_BY_PARENT still matches the pre-config literal table",
          app.SKU_PREFIX_BY_PARENT,
          {"Washing Machines": "WM", "Water Heaters": "WH", "LED": "TV",
           "Air Conditioner": "AC", "Cooling & Freezing": "CF",
           "Kitchen Appliances": "KA", "Home Appliances": "HA",
           "Small Appliances": "SA", "Electronics & Computing": "EC"})
    check("SKU_PREFIXES still matches the pre-config literal tuple",
          tuple(app.SKU_PREFIXES),
          ("WM", "WH", "TV", "AC", "CF", "KA", "HA", "SA", "EC", "AP"))
    check("SKU_PREFIX_CATEGORY still matches the pre-config literal table",
          app.SKU_PREFIX_CATEGORY,
          {"WM": ("Washing Machines", ""),
           "WH": ("Water Heaters", "Geysers"),
           "GWH": ("Water Heaters", "Geysers"),     # KI-35
           "SM": ("Home Appliances", "sewing machine"),
           "HWD": ("Home Appliances", "Water Dispensers"),
           "REF": ("Home Appliances", "Refrigerators"),
           "FRIDGE": ("Home Appliances", "Refrigerators"),
           "MWO": ("Kitchen Appliances", "Microwave Ovens"),
           "IC": ("Kitchen Appliances", "Induction Cooktops"),
           "ICT": ("Kitchen Appliances", "Induction Cooktops"),
           "DI": ("Small Appliances", "Electric Iron"),
           "DF": ("Cooling & Freezing", "Deep Freezers"),
           "AC": ("Air Conditioner", ""), "TV": ("LED", "")})
    check("the never-rename guard still means WM-/WH-",
          tuple(app.SKU_NEVER_RENAME), ("WM-", "WH-"))
    check("the fallback prefix is still AP", app.SKU_FALLBACK_PREFIX, "AP")
    check("validate_csv's regex is still the pre-config literal",
          V.SKU_PREFIX_RE.pattern, "^(WM|WH|TV|AC|CF|KA|HA|SA|EC|AP)-")

    # and every one of them is a VIEW of the client config, not a copy
    scheme = app.CLIENT.sku
    check("SKU_PREFIX_BY_PARENT is the client's [sku.prefix_by_parent]",
          app.SKU_PREFIX_BY_PARENT, dict(scheme.prefix_by_parent))
    check("SKU_PREFIXES is the client's derived prefix tuple",
          tuple(app.SKU_PREFIXES), tuple(scheme.prefixes))
    check("SKU_PREFIX_CATEGORY is the client's [sku.input_codes]",
          app.SKU_PREFIX_CATEGORY, dict(scheme.input_codes))
    check("SKU_NEVER_RENAME is the client's never_rename",
          tuple(app.SKU_NEVER_RENAME),
          tuple(f"{c}-" for c in scheme.never_rename))
    check("SKU_FALLBACK_PREFIX is the client's fallback_prefix",
          app.SKU_FALLBACK_PREFIX, scheme.fallback_prefix)
    check("validate_csv's regex covers EXACTLY the client's prefixes",
          V.SKU_PREFIX_RE.pattern,
          "^(" + "|".join(scheme.prefixes) + ")-")
    check("validate_csv strips nothing the client does not define",
          [p for p in ("ZZ", "QQ", "XY") if V.SKU_PREFIX_RE.match(f"{p}-X")],
          [])


def test_20_inflated_mrp_flagged_not_deleted():
    section("#20 an MRP many times the price is flagged for a human, not silently dropped")
    html = """<html><body>
    <script type="application/ld+json">
    {"@type": "Product", "name": "Testbrand Model123 Widget", "sku": "MODEL123",
     "offers": {"@type": "Offer", "price": "21999"},
     "image": "https://example.com/img.jpg"}
    </script>
    <p>MRP: Rs. 199999</p>
    </body></html>"""
    rec = E.extract("https://example.com/p/1", html)
    check("extract() keeps the scraped MRP instead of silently clearing it",
          (E.price_value(rec["regular price"]) or 0) > 100000, True)
    status = E.status_for(rec, "https://example.com/p/1")
    check("status_for() flags the mismatch for a human to check",
          "x the price" in status and "check" in status, True)


def test_21_gallery_collision_flagged():
    section("#21 a second SKU with an identical gallery is flagged against the first")
    seen = {}
    rec_a = {"url_link": "https://a.example.com/p", "status": "FOUND (a.example.com)",
              "image_url": "https://img.example.com/1.jpg,https://img.example.com/2.jpg"}
    E.flag_gallery_collision(rec_a, "SKU-A", 2, seen)
    check("the first row to claim a gallery is not flagged",
          "gallery matches" in rec_a["status"], False)

    rec_b = {"url_link": "https://b.example.com/p", "status": "FOUND (b.example.com)",
              "image_url": "https://img.example.com/2.jpg,https://img.example.com/1.jpg"}
    E.flag_gallery_collision(rec_b, "SKU-B", 3, seen)
    check("a DIFFERENT sku with the same gallery (any order) is flagged",
          "gallery matches row 2 (SKU-A), verify" in rec_b["status"], True)

    rec_c = {"url_link": "https://a.example.com/p", "status": "FOUND (a.example.com)",
              "image_url": "https://img.example.com/1.jpg,https://img.example.com/2.jpg"}
    E.flag_gallery_collision(rec_c, "SKU-A", 9, seen)
    check("the SAME sku reappearing (e.g. a retry) is never flagged against itself",
          "gallery matches" in rec_c["status"], False)


def test_22_image_floor_fallback():
    section("#22 an all-below-floor gallery keeps its largest photos instead of shipping zero")
    urls = ["https://example.com/tiny-a.jpg", "https://example.com/tiny-b.jpg",
            "https://example.com/tiny-c.jpg"]
    E._IMG_DIMS[urls[0]] = (400, 400)
    E._IMG_DIMS[urls[1]] = (800, 600)     # the largest of the three
    E._IMG_DIMS[urls[2]] = (300, 300)
    rec = {"image_url": ",".join(urls)}
    E.finalize_gallery(rec)
    check("keeps images instead of shipping zero",
          bool(rec["image_url"]), True)
    check("picks the LARGEST of the too-small candidates first",
          rec["image_url"].split(",")[0], urls[1])
    check("marks the row so status_for() can surface it",
          rec.get("_gallery_below_floor"), True)
    status = E.status_for(rec, "https://example.com/p/1", min_images=0)
    check("status_for() mentions the floor",
          "below" in status and "floor" in status, True)


def test_23_attribute_slugs_importable():
    section("#23 attribute names survive WooCommerce's slug rules")
    # the exact labels the 2026-08-15 TV import rejected
    for scraped_label, expect in [
            ("Display Backlight Configuration", "Backlight Type"),
            ("Picture Quality Enhancement Technology", "Picture Technology"),
            ("Motion Enhancement Technology", "Motion Technology"),
            ("Category", "Product Category")]:
        check(f"{scraped_label[:40]:42} -> {expect}",
              app.woo_attribute_name(scraped_label), expect)

    # labels with no curated rename still come out under the cap, mechanically
    for raw in ("Refrigerator Compressor Warranty Period Details",
                "Integrated Digital Television Reception Capability"):
        slug = app.woo_slug(app.woo_attribute_name(raw))
        check(f"generic shortening fits: {slug!r}",
              len(slug) <= app.WOO_ATTR_SLUG_MAX, True)
    check("short labels pass through untouched",
          app.woo_attribute_name("Energy Rating"), "Energy Rating")
    check("a reserved word is prefixed, not shipped",
          app.woo_attribute_name("Status"), "Product Status")

    # product_specs applies the rule to what retailers actually scraped
    rows = app.product_specs(
        "Samsung 138 cm (55 inches) Crystal 4K Ultra HD Smart LED TV",
        "Samsung", "LED TV",
        scraped=[("Motion Enhancement Technology", "Motion Xcelerator"),
                 ("Category", "QLED")])
    bad = [l for l, _v in rows
           if len(app.woo_slug(l)) > app.WOO_ATTR_SLUG_MAX
           or app.woo_slug(l) in app.RESERVED_ATTRIBUTE_SLUGS]
    check("every product_specs label is importable", bad, [])

    # both sides of the gate use the same rules - same pattern as the HD floors
    check("app and validate_csv agree on the cap",
          app.WOO_ATTR_SLUG_MAX, V.WOO_ATTR_SLUG_MAX)
    check("app and validate_csv agree on the reserved list",
          app.RESERVED_ATTRIBUTE_SLUGS, V.RESERVED_ATTRIBUTE_SLUGS)
    check("app and validate_csv derive the same slug",
          app.woo_slug("Display Backlight Configuration"),
          V.woo_attr_slug("Display Backlight Configuration"))

    # and validate_csv catches a bad name that slipped through anyway
    row = {"Attribute 1 name": "Brand",
           "Attribute 2 name": "Picture Quality Enhancement Technology",
           "Attribute 3 name": "Category"}
    cols = ["Attribute 1 name", "Attribute 2 name", "Attribute 3 name"]
    problems = V.attribute_name_problems(row, cols)
    check("validator flags the overlong slug",
          any("attribute_slug_too_long" in p for p in problems), True)
    check("validator flags the reserved term",
          any("attribute_name_reserved" in p for p in problems), True)
    check("validator does not flag the good name", len(problems), 2)


def test_24_promos_are_not_specs():
    section("#24 an offer is not a spec, and the short description stays short")
    # the exact row the 2026-08-15 report complained about
    check("enricher refuses the SuperDost cashback row",
          E.usable_spec("SuperDost Cashback",
                        "100 for new SuperDost Users* + 3% or 250 Cashback ."),
          False)
    for label, value in [("Bank Offer", "10% Instant Discount on HDFC cards"),
                         ("Exchange Offer", "Up to 11000 off"),
                         ("EMI", "No Cost EMI from 1,541/month")]:
        check(f"enricher refuses: {label}", E.usable_spec(label, value), False)
    check("a real spec still passes",
          E.usable_spec("Refresh Rate", "60 Hz"), True)
    check("a value that merely mentions an offer word is also out",
          E.usable_spec("Special Feature", "Extra 5% cashback with SuperCoins"),
          False)

    # app.py cleans sheets that were ALREADY enriched with the junk
    cell = ("SuperDost Cashback: 100 for new SuperDost Users* + 3% or 250 "
            "Cashback . | Refresh Rate: 60 Hz | Bank Offer: 10% off with ICICI")
    rows = app.parse_scraped_specs(cell)
    check("parse_scraped_specs keeps only the real spec",
          rows, [("Refresh Rate", "60 Hz")])

    # the merged firewall: seller, customer-care and origin rows are gone too
    cell = ("Country of Origin: India | Manufacturer: Haier Appliances | "
            "Customer Care: 1800 102 9999 | Sold by: Cloudtail | "
            "Warranty: 2 Years | Installation Type: Wall Mounted")
    check("parse_scraped_specs drops seller/origin/care rows",
          app.parse_scraped_specs(cell),
          [("Warranty", "2 Years"), ("Installation Type", "Wall Mounted")])
    check("enricher refuses a seller row", E.usable_spec("Sold by", "Cloudtail"),
          False)
    check("enricher keeps a plain warranty duration",
          E.usable_spec("Warranty", "1 Year Comprehensive"), True)

    # every description field carries at most KEY_POINTS_MAX points; the
    # attributes still carry the whole (capped at MAX_ATTRIBUTES) table
    scraped = [(f"Extra Spec {n}", f"Value {n}") for n in range(1, 13)]
    config, short_desc, long_desc, rows = app.build_descriptions(
        "TV-TEST-1", "Samsung 138 cm (55 inches) 4K Ultra HD Smart LED TV",
        "Samsung", "LED TV", scraped=scraped)
    check("short description: Brand + SKU + at most KEY_POINTS_MAX points",
          short_desc.count("<tr>") <= 2 + E.KEY_POINTS_MAX, True)
    check("long description: 4 template rows + at most KEY_POINTS_MAX points",
          long_desc.count("<tr>") <= 4 + E.KEY_POINTS_MAX, True)
    check("key features: Brand + at most KEY_POINTS_MAX points",
          config.count(":") <= 1 + E.KEY_POINTS_MAX, True)
    check("the attributes are not capped by KEY_POINTS_MAX",
          len(rows), app.MAX_ATTRIBUTES)
    check("no invented mains-supply row", "230 V" in long_desc, False)


@contextlib.contextmanager
def two_price_mode():
    """Run with [pricing] single_price = false (MRP + sale price), whatever
    the active client sets. #25 and #26 pin the two-price rules; the
    single-price rules are in tests/test_single_price.py."""
    import dataclasses
    import client_config
    saved = (app.CLIENT, V.CLIENT)
    try:
        for mod in (app, V):
            mod.CLIENT = dataclasses.replace(
                mod.CLIENT, pricing=client_config.PricingConfig(False))
        yield
    finally:
        app.CLIENT, V.CLIENT = saved


def test_25_live_site_audit_findings():
    with two_price_mode():
        _test_25_body()


def _test_25_body():
    section("#25 live-site audit: Mini LED catch-all, 3x TV MRP, raw-SKU titles")
    # the exact live product that was filed under Mini LED
    check("a plain Full HD TV goes to the bare LED parent",
          app.detect_category(
              "Foxsky 109 cm (43 Inch) Full HD Smart LED TV, 43FSFHS"),
          ("LED", ""))
    check("a real Mini LED still gets the sub-category",
          app.detect_category("TCL 55 inch 4K Mini LED Google TV 55C755"),
          ("LED", "Mini LED"))
    check("bare-parent products keep their required-axes check",
          app.SUBCATEGORY_ATTRIBUTES.get("led") is not None, True)

    # the live Foxsky price (41,499 -> 12,999 = 3.2x) now gets flagged
    check("TV context tightens the MRP limit to 3x",
          V.mrp_ratio_limit("LED", "Foxsky 43 inch Full HD Smart LED TV"),
          V.MRP_RATIO_LIMIT_TV)
    check("non-TV categories keep the 4x limit",
          V.mrp_ratio_limit("Kitchen Appliances > Kettles", "Electric Kettle"),
          V.MRP_RATIO_LIMIT)

    df = pd.DataFrame([
        {"sku": "TV-FOXSKY-43FSFHS", "title": "Foxsky 43 Full HD Smart LED TV",
         "categories": "LED", "description": "d", "Images": "",
         "regular price": 41499, "sale price": 12999},
        {"sku": "TV-ACER-1", "title": "Acer-AR32GT2841HDFL",
         "categories": "LED", "description": "d", "Images": "",
         "regular price": 15990, "sale price": 12670},
    ])
    issues = V.validate_export(df, "audit.csv", check_images=False,
                               write_issues=False)
    by_sku = {i["sku"]: i["reasons"] for i in issues}   # reasons: joined str
    check("3.2x TV MRP is now flagged",
          "mrp_implausible" in by_sku.get("TV-FOXSKY-43FSFHS", ""), True)
    check("a bare model code as the title is flagged",
          "title_looks_like_raw_sku" in by_sku.get("TV-ACER-1", ""), True)


def test_26_row_context_is_the_one_derivation():
    with two_price_mode():
        _test_26_body()


def _test_26_body():
    section("#26 row_context() derives a row the same way process_inventory does")
    row = {"sku": "43FSFHS", "title": "Foxsky 109 cm (43 Inch) Full HD Smart "
                                      "LED TV 43FSFHS",
           "categories": "", "Brands": "", "regular price": "22,599",
           "sale price": "18,999", "url_link": "", "image_url": "",
           "specs": ""}
    ctx = app.row_context(row)
    want_brand = app.infer_brand(row["title"], row["sku"])
    check("a blank Brands cell falls back to infer_brand", ctx["brand"],
          want_brand)
    check("infer_brand actually produced something", bool(want_brand), True)
    parent, sub = app.detect_category(row["title"], "", ctx["brand"],
                                      row["sku"])
    check("the hierarchy is the canonical taxonomy string",
          ctx["category_hierarchy"],
          app.canonicalize_category(parent, sub)[0])
    check("the hierarchy came from the taxonomy file, not a guess",
          ctx["category_mapped"], True)
    check("the sku is prefixed_sku of the raw sku and the PARENT",
          ctx["sku"], app.prefixed_sku(row["sku"], parent))
    # the prices go through the same normalisation the builder used to inline
    check("prices are parsed, not passed through", ctx["sale price"], 18999.0)
    check("an MRP is only invented when asked",
          app.row_context({**row, "regular price": ""})["regular price"], "")
    check("estimate_mrp fills it with 1.25x",
          app.row_context({**row, "regular price": ""},
                          estimate_mrp=True)["regular price"],
          round(18999.0 * 1.25, 2))


def test_27_sku_charset_is_a_whitelist():
    section("#27 a SKU keeps only characters that are safe in a filename AND "
            "a URL")
    check("comma and hash are hyphenated",
          app.prefixed_sku("ABC,12#3", "Home Appliances"), "HA-ABC-12-3")
    for raw, want in (("X100%", "AP-X100"),
                      ("A&B", "AP-A-B"),
                      ("A+B", "AP-A-B"),
                      ("SAMSUNG X100", "AP-SAMSUNG-X100")):
        check(f"{raw!r} -> {want}", app.prefixed_sku(raw, "Something New"),
              want)
    check("dots and underscores are legal in a filename, so they survive",
          app.prefixed_sku("WW80T504DAB_1.2", "Something New"),
          "AP-WW80T504DAB_1.2")

    # gui_helpers mirrors this sanitisation so the review back-map can find a
    # raw sku from the CSV sku; drift breaks Step 2 silently.
    sys.path.insert(0, str(ROOT))
    import gui_helpers                                 # noqa: E402
    for raw in ("ABC,12#3", "X100%", "A&B", "A+B", "SAMSUNG X100",
                "WA70A4022FS/TL"):
        check(f"gui_helpers agrees with app on {raw!r}",
              gui_helpers._sanitized_sku(raw),
              app.prefixed_sku(raw, None)[len("AP-"):])


def test_28_reviewer_category_pick_wins():
    section("#28 an explicit 'Parent > Sub' pick outranks the keyword rules")
    fridge = ("Haier 3 Star Inverter Frost Free Double Door Refrigerator "
              "with Water Dispenser")
    combo = "Home Appliances > Refrigerators"
    check("the combo exists in the taxonomy",
          app.CATEGORY_COMBOS.get(app._cat_norm(combo)), combo)
    check("the reviewer's pick is honoured",
          app.detect_category(fridge, combo), ("Home Appliances", "Refrigerators"))
    check("with no pick the dispenser rule still fires",
          app.detect_category(fridge, ""), ("Home Appliances", "Water Dispensers"))
    # a bare parent is not a pick from the taxonomy - it is what an ERP export
    # or a retailer breadcrumb carries, and it must not short-circuit
    check("a bare parent does not short-circuit the rules",
          app.detect_category("Samsung 8 kg Front Load Washing Machine "
                              "WW80T504DAB", "Home Appliances"),
          ("Washing Machines", "Front Load"))
    check("a retailer breadcrumb does not short-circuit the rules",
          app.detect_category("Prestige Iris 750 W Mixer Grinder",
                              "Home > Kitchen > Mixers"),
          ("Kitchen Appliances", "Juicers, Mixers & Grinders"))
    # the pick has to survive being typed by hand and being read back out of a
    # built CSV, where the importer escape '\,' is still on the category name
    for typed in ("Home Appliances >Refrigerators",
                  "home appliances>refrigerators",
                  "Home Appliances  >  Refrigerators"):
        check(f"a loosely spaced pick still wins: {typed!r}",
              app.detect_category(fridge, typed),
              ("Home Appliances", "Refrigerators"))
    check("a pick round-tripped through the CSV escape still wins",
          app.detect_category("Philips HL7756 750W Mixer Grinder",
                              app.escape_category_for_csv(
                                  "Kitchen Appliances > Juicers, "
                                  "Mixers & Grinders")),
          ("Kitchen Appliances", "Juicers, Mixers & Grinders"))
    check("...and that really was escaped",
          "\\," in app.escape_category_for_csv("Kitchen Appliances > Juicers, "
                                               "Mixers & Grinders"), True)
    check("a bare parent with no spacing to fix still does not short-circuit",
          app.detect_category("Samsung 8 kg Front Load Washing Machine "
                              "WW80T504DAB", "  Home Appliances  "),
          ("Washing Machines", "Front Load"))


def test_29_attribute_axes_lookup():
    section("#29 one lookup for the taxonomy's required axes, parent fallback "
            "only for a bare parent")
    check("a bare-parent product inherits the parent's axes",
          app.attribute_axes("", "LED"), app.SUBCATEGORY_ATTRIBUTES["led"])
    check("the parent really declares some", bool(app.attribute_axes("", "LED")),
          True)
    check("sub == parent is the same case",
          app.attribute_axes("LED", "LED"), app.SUBCATEGORY_ATTRIBUTES["led"])
    check("a merged taxonomy cell resolves as written",
          bool(app.attribute_axes("Juicers, Mixers & Grinders",
                                  "Kitchen Appliances")), True)
    # an unknown sub-category must NOT borrow its parent's axes - it would be
    # judged against axes nobody declared for it and hide the mis-detection
    check("an unknown sub-category inherits nothing",
          app.attribute_axes("No Such Sub", "LED"), {})
    check("missing_spec_axes sees the parent's axes for a bare parent",
          "Size" in app.missing_spec_axes("", "Foxsky LED TV", "LED",
                                          parent="LED"), True)
    check("...and reports nothing when the parent is unknown",
          app.missing_spec_axes("", "Foxsky LED TV", "LED"), [])
    # P1-17 residual: 'Juicers, Mixers & Grinders' is TWO taxonomy rows, and
    # returning only the first one dropped the 'Size' axis and every wattage
    # the mixer row lists. 'Philips HL7756 750W Mixer Grinder' was reported
    # missing 'Capacity' - a figure printed in its own title - and shipped
    # without the 750W tag.
    juicer = app.SUBCATEGORY_ATTRIBUTES["juicers"]
    mixer = app.SUBCATEGORY_ATTRIBUTES["mixers & grinders"]
    merged = app.attribute_axes("Juicers, Mixers & Grinders",
                                "Kitchen Appliances")
    check("the merged cell unions every part's axes",
          set(merged), set(juicer) | set(mixer))
    check("...including the axis only the mixer row declares",
          "Size" in merged and "Size" not in juicer, True)
    check("its Capacity keeps the juicer-only wattage",
          "400W" in merged["Capacity"], True)
    check("...and the mixer-only wattage",
          "750W" in merged["Capacity"], True)
    check("...and the Type vocabulary of the part that has one",
          "Centrifugal" in merged["Type"], True)
    check("no value is listed twice",
          len(merged["Capacity"]), len(set(merged["Capacity"])))
    check("the stored juicer row did not swallow the mixer wattages",
          "750W" in juicer["Capacity"] or "Size" in juicer, False)
    check("...nor the mixer row the juicer's",
          "400W" in mixer["Capacity"] or "Centrifugal" in mixer["Type"], False)
    check("...and the union is a new dict",
          merged is juicer or merged is mixer, False)
    check("a single-part sub is unchanged",
          app.attribute_axes("Juicers", "Kitchen Appliances"), juicer)
    mixer_title = "Philips HL7756 750W Mixer Grinder"
    check("the wattage in the title now answers Capacity",
          "Capacity" in app.missing_spec_axes(
              "Juicers, Mixers & Grinders", mixer_title, "Kitchen Appliances",
              parent="Kitchen Appliances"), False)
    check("and it is tagged",
          "750W" in app.build_tags(
              "Philips", "Kitchen Appliances", "Juicers, Mixers & Grinders",
              mixer_title, "4499",
              app.family_for("Kitchen Appliances",
                             "Juicers, Mixers & Grinders")).split(", "), True)


def test_30_many_row_parent_lends_no_axes():
    section("#30 a parent with many attribute rows lends its axes to nobody")
    # 'Home Appliances' has a dozen heterogeneous attribute rows and Fans is
    # the first of them. Inheriting "whichever row came first" put the fan
    # Type vocabulary on every bare-parent product: 'Bosch Table Top
    # Dishwasher' shipped the WooCommerce tag 'Table', 'Kent Wall Mounted
    # Water Softener' shipped 'Wall', and both got the fan axes reported as
    # _spec_gaps, which validate_csv turns into missing_required_attribute.
    # detect_category returns a bare ('Home Appliances', '') for dishwashers
    # and as its final catch-all, so this is the unclassified-row path.
    check("a many-row parent lends nothing",
          app.attribute_axes("", "Home Appliances"), {})
    for title, leak in (("Bosch Table Top Dishwasher 6 Place Settings", "Table"),
                        ("Kent Wall Mounted Water Softener 10 L", "Wall")):
        parent, sub = app.detect_category(title)
        check(f"{title[:20]}... is the bare-parent case", (parent, sub),
              ("Home Appliances", ""))
        family = app.family_for(parent, sub)
        tags = app.build_tags("Bosch", parent, sub, title, "", family)
        check(f"no fan Type '{leak}' tag leaks onto it", leak in tags, False)
        check("no fabricated gaps",
              app.missing_spec_axes(sub, title, family, {}, parent=parent), [])
    # the single-row parent (LED) still works - that is the case the fallback
    # exists for
    check("a single-row parent still lends its axes",
          bool(app.attribute_axes("", "LED")), True)


def test_31_stabilizer_free_family():
    section("#31 every way of saying 'needs no stabilizer' cancels the rule")
    # The guard only knew the literal 'stabilizer free'. These three phrasings
    # are just as common in Indian AC marketing names and all filed a 37,000
    # rupee air conditioner under Small Appliances > Voltage Stabilizers.
    for title in ("Samsung 1.5 Ton 5 Star Convertible Split AC with No "
                  "Stabilizer Required",
                  "Voltas 1.5 Ton Split AC Works without Stabilizer",
                  "Lloyd 1.5 Ton Split AC Stabilizer Not Required",
                  "Voltas Vertis Elite 1.5 Ton 3 Star Inverter Split AC "
                  "Stabilizer Free Operation"):
        parent, _ = app.detect_category(title)
        check(f"{title[:32]}... is an AC", parent, "Air Conditioner")
    # The guard was on the FIRST rule only, and an identical unguarded copy
    # sat 89 lines lower under '--- Small Appliances ---', shadowing every
    # rule below it. 'Stabilizer Free Operation' is standard LG/Samsung/
    # Whirlpool refrigerator marketing, not AC-only, so live fridges, TVs,
    # purifiers and geysers shipped as Voltage Stabilizers with _spec_gaps
    # 'Type, Size, Ton'. Each pair is (title, expected combo).
    for title, expect in (
            ("LG 260 L 3 Star Frost Free Double Door Refrigerator with "
             "Stabilizer Free Operation", ("Home Appliances", "Refrigerators")),
            ("Samsung 253 L Convertible Double Door Refrigerator Stabilizer "
             "Free Operation", ("Home Appliances", "Refrigerators")),
            ("Whirlpool 192 L Direct Cool Single Door Fridge with No "
             "Stabilizer Required", ("Home Appliances", "Refrigerators")),
            ("Sony Bravia 139 cm 55 inch 4K Ultra HD Smart LED TV Works "
             "without Stabilizer", ("LED", "")),
            ("Kent Grand Plus RO Water Purifier Stabilizer Free Operation",
             ("Home Appliances", "Water Purifiers")),
            ("Havells Instanio 3 L Instant Water Heater No Stabiliser Needed",
             ("Water Heaters", "Geysers"))):
        check(f"non-AC: {title[:30]}...", app.detect_category(title), expect)
    # the intended direction must survive: a real stabilizer is still one
    for title in ("V-Guard VG 400 Voltage Stabilizer for Air Conditioner",
                  "Microtek EM4090 Automatic Voltage Stabilizer"):
        check(f"{title[:28]}... is still a stabilizer",
              app.detect_category(title),
              ("Small Appliances", "Voltage Stabilizers & Regulators"))


def test_32_sku_dedupe_ignores_case():
    """P2-30: WooCommerce's SKU uniqueness check and NTFS both fold case, so
    'ha-x1' and 'HA-X1' are one product and one '<sku>_1.webp'. Two rows that
    differ only by case used to produce two CSV rows whose image files
    collided on disk.

    NB: the pair must not begin with a SKU_PREFIXES code. prefixed_sku matches
    those case-SENSITIVELY, so 'ha-x1'/'HA-X1' come out of it as 'AC-ha-x1'
    and 'AC-X1' - different products by then, and different filenames, so they
    never reach the dedupe as a case-only pair. That is a separate defect in
    prefixed_sku; this test pins the dedupe."""
    section("#32 SKUs that differ only by letter case are one product")
    df = pd.DataFrame([
        {"sku": "zx-x1", "title": "Haier 1.5 Ton Split AC ZX-X1",
         "categories": "", "Brands": "", "regular price": "30000",
         "sale price": "27000", "url_link": "", "image_url": ""},
        {"sku": "ZX-X1", "title": "Haier 1.5 Ton Split AC ZX-X1",
         "categories": "", "Brands": "", "regular price": "30000",
         "sale price": "27000", "url_link": "", "image_url": ""},
    ])
    tmp = Path(tempfile.mkdtemp())
    cwd = os.getcwd()
    try:
        os.chdir(tmp)                       # no image_url -> no network at all
        out = app.process_inventory(df, media_base="https://shop.invalid/u")
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)
    check("the case-only duplicate is dropped", len(out), 1)
    # the FIRST row wins and keeps its own case: re-casing a live WM-/WH- key
    # would duplicate the product on the next upsert instead of updating it
    check("the surviving SKU is not re-cased",
          str(out.iloc[0]["sku"]).endswith("zx-x1"), True)


def main():
    test_01_e2e_sparse_gallery()
    test_02_dead_page_is_not_a_block()
    test_03_busy_sku_mirroring()
    test_04_no_invented_specs()
    test_05_nan_is_not_a_price()
    test_06_donated_images_are_measured()
    test_07_screen_size_is_not_kilograms()
    test_08_aspect_check_reachable()
    test_09_10_11_wc_publish()
    test_12_get_session_no_keyerror()
    test_13_branch_name_leaves_the_query()
    test_14_gallery_capped_after_filtering()
    test_15_one_categoriser()
    test_16_validate_cli_flags()
    test_17_console_encoding()
    test_18_binary_upload_rejected()
    test_19_category_prefixes()
    test_20_inflated_mrp_flagged_not_deleted()
    test_21_gallery_collision_flagged()
    test_22_image_floor_fallback()
    test_23_attribute_slugs_importable()
    test_24_promos_are_not_specs()
    test_25_live_site_audit_findings()
    test_26_row_context_is_the_one_derivation()
    test_27_sku_charset_is_a_whitelist()
    test_28_reviewer_category_pick_wins()
    test_29_attribute_axes_lookup()
    test_30_many_row_parent_lends_no_axes()
    test_31_stabilizer_free_family()
    test_32_sku_dedupe_ignores_case()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
