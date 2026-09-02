"""The HD image contract, end to end.

What "HD" means here, and where each rule lives:

  stage 1  sku_enricher.IMAGE_MIN_PX   reject a source image under this
  stage 2  app.MIN_IMAGE_EDGE          refuse to download one under this
           app.SQUARE_IMAGES           pad (never crop) onto a 1:1 canvas
           app.CATALOG_EDGE            finish the square at exactly 1200x1200,
                                       or 1000x1000 (= MIN_IMAGE_EDGE) when the
                                       source square is smaller - never upscale
           app.WEBP_QUALITY            re-encode quality
  stage 3  validate_csv.GOOD_IMAGE_PX  flag anything softer than this

The two floors have to be the SAME number. They were 500 and 1000, so stage 1
counted images that stage 2 then threw away - the sheet promised eight photos
and the product shipped with three.

    .venv\\Scripts\\python.exe tests\\test_hd_images.py
"""
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

import pandas as pd                                # noqa: E402
from PIL import Image                              # noqa: E402

import app                                         # noqa: E402
import sku_enricher as E                           # noqa: E402
import validate_csv as V                           # noqa: E402

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


def test_thresholds_agree():
    section("the two HD floors are the same number")
    check("enricher floor == app download floor",
          E.IMAGE_MIN_PX, app.MIN_IMAGE_EDGE)
    check("the HD floor is what validate_csv calls good",
          app.MIN_IMAGE_EDGE, V.GOOD_IMAGE_PX)
    check("catalogue masters land on the 1200 grid size", app.CATALOG_EDGE, 1200)
    check("the fallback rendition is the HD floor itself",
          app.MIN_IMAGE_EDGE, 1000)
    check("catalogue tiles are square", app.SQUARE_IMAGES, True)
    check("webp quality is the high setting", app.WEBP_QUALITY, 95)


def test_download_rules():
    """Drive app.download_urls_for_sku with generated images of known size."""
    section("what actually lands in product_images_for_upload")
    sizes = {
        "under":  (999, 999),      # one pixel below the floor -> refused
        "atfloor": (1000, 1000),   # exactly the floor -> kept at 1000x1000
        "wide":   (3000, 1200),    # oversized + wide -> padded, 1200x1200
        "huge":   (4000, 4000),    # oversized square -> 1200x1200
        "small":  (300, 300),      # plainly a thumbnail -> refused
        "between": (1100, 1050),   # between the renditions -> 1000x1000
    }
    order = ["under", "atfloor", "wide", "huge", "small", "between"]

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
    real = app.requests.get
    # app.IMAGE_DIR is per-client and absolute now (Phase 1 item 1), so chdir
    # no longer redirects the downloads - repoint it at the scratch dir.
    saved_image_dir = app.IMAGE_DIR
    try:
        os.chdir(tmp)
        app.IMAGE_DIR = tmp / app.IMAGE_ROOT
        app.requests.get = fake_get
        urls = [f"https://cdn.example.com/i/{k}.png" for k in order]
        saved = app.download_urls_for_sku("AP-HD", urls)
        check("sub-HD sources are refused", saved, [2, 3, 4, 6])

        root = tmp / app.IMAGE_ROOT
        got = {}
        for i in saved:
            with Image.open(root / f"AP-HD_{i}.webp") as im:
                got[order[i - 1]] = (im.size, im.format)
        check("the 1000px source is kept at 1000x1000",
              got["atfloor"][0], (1000, 1000))
        check("a 3000x1200 master is squared and finished at 1200x1200",
              got["wide"][0], (1200, 1200))
        check("a 4000x4000 master is downscaled to 1200x1200",
              got["huge"][0], (1200, 1200))
        check("an 1100px source drops to 1000x1000, never up to 1200",
              got["between"][0], (1000, 1000))
        check("everything is WEBP", {v[1] for v in got.values()}, {"WEBP"})
        check("every master is one of the two catalogue renditions",
              {v[0] for v in got.values()}, {(1000, 1000), (1200, 1200)})

        names = [f"AP-HD_{i}.webp" for i in saved]
        problems = V.image_dimension_problems(names, root=str(root))
        check("validate_csv finds nothing wrong with them", problems, [])

        # a padded wide photo must keep its whole subject - no cropping
        with Image.open(root / "AP-HD_3.webp") as im:
            px = im.convert("RGB").load()
            check("padding is white, not a crop", px[5, 5], (255, 255, 255))
    finally:
        app.requests.get = real
        app.IMAGE_DIR = saved_image_dir
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def test_upsize_rules():
    section("the enricher asks each CDN for its largest rendition")
    check("amazon rendition modifier dropped",
          E.upsize_image_url(
              "https://m.media-amazon.com/images/I/71abc._SL1500_.jpg"),
          "https://m.media-amazon.com/images/I/71abc.jpg")
    check("magento resize params dropped",
          E.upsize_image_url("https://x.in/media/catalog/product/a.jpg"
                             "?width=300&height=300"),
          "https://x.in/media/catalog/product/a.jpg")
    check("tatacliq is deliberately left alone (it 404s on upsize)",
          E.upsize_image_url("https://img.tatacliq.com/i/437Wx649H/x.jpg"),
          "https://img.tatacliq.com/i/437Wx649H/x.jpg")
    check("app.py applies the same rule at download time",
          app.largest_rendition(
              "https://m.media-amazon.com/images/I/71abc._SL1500_.jpg"),
          "https://m.media-amazon.com/images/I/71abc.jpg")
    check("a haier share-banner rendition is dropped",
          app.largest_rendition("https://image.haier.com/i/W0202_1200_p1911.jpg"),
          "https://image.haier.com/i/W0202.jpg")
    check("a thumbnail URL is recognised as one",
          E.is_thumbnail("https://x.com/i/p_120.jpg"), True)
    check("an HD URL is not", E.is_thumbnail("https://x.com/i/p_1500.jpg"), False)


def main():
    test_thresholds_agree()
    test_download_rules()
    test_upsize_rules()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
