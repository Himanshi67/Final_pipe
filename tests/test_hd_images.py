"""The HD image contract, end to end.

What "HD" means here, and where each rule lives:

  stage 1  sku_enricher.IMAGE_MIN_PX   reject a source image under this
  stage 2  app.MIN_IMAGE_EDGE          refuse to download one under this
           app.MAX_IMAGE_EDGE          downscale past this, never upscale
           app.SQUARE_IMAGES           pad (never crop) onto a 1:1 canvas
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

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")


def section(name):
    print(f"\n{name}")


def test_thresholds_agree():
    section("the two HD floors are the same number")
    check("enricher floor == app download floor",
          E.IMAGE_MIN_PX, app.MIN_IMAGE_EDGE)
    check("the HD floor is what validate_csv calls good",
          app.MIN_IMAGE_EDGE, V.GOOD_IMAGE_PX)
    check("masters are capped, not unbounded", app.MAX_IMAGE_EDGE, 2400)
    check("cap is above the floor", app.MAX_IMAGE_EDGE > app.MIN_IMAGE_EDGE, True)
    check("catalogue tiles are square", app.SQUARE_IMAGES, True)
    check("webp quality is the high setting", app.WEBP_QUALITY, 95)


def test_download_rules():
    """Drive app.download_urls_for_sku with generated images of known size."""
    section("what actually lands in product_images_for_upload")
    sizes = {
        "under":  (999, 999),      # one pixel below the floor -> refused
        "atfloor": (1000, 1000),   # exactly the floor -> kept
        "wide":   (3000, 1200),    # oversized + wide -> downscaled, padded
        "huge":   (4000, 4000),    # oversized square -> downscaled
        "small":  (300, 300),      # plainly a thumbnail -> refused
    }
    order = ["under", "atfloor", "wide", "huge", "small"]

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
    try:
        os.chdir(tmp)
        app.requests.get = fake_get
        urls = [f"https://cdn.example.com/i/{k}.png" for k in order]
        saved = app.download_urls_for_sku("AP-HD", urls)
        check("sub-HD sources are refused", saved, [2, 3, 4])

        root = tmp / app.IMAGE_ROOT
        got = {}
        for i in saved:
            with Image.open(root / f"AP-HD_{i}.webp") as im:
                got[order[i - 1]] = (im.size, im.format)
        check("the 1000px source is kept at 1000x1000",
              got["atfloor"][0], (1000, 1000))
        check("a 3000x1200 master is downscaled to the 2400 cap and squared",
              got["wide"][0], (2400, 2400))
        check("a 4000x4000 master is downscaled to the cap",
              got["huge"][0], (2400, 2400))
        check("everything is WEBP", {v[1] for v in got.values()}, {"WEBP"})
        check("everything is 1:1", {v[0][0] == v[0][1] for v in got.values()},
              {True})

        names = [f"AP-HD_{i}.webp" for i in saved]
        problems = V.image_dimension_problems(names, root=str(root))
        check("validate_csv finds nothing wrong with them", problems, [])

        # a padded wide photo must keep its whole subject - no cropping
        with Image.open(root / "AP-HD_3.webp") as im:
            px = im.convert("RGB").load()
            check("padding is white, not a crop", px[5, 5], (255, 255, 255))
    finally:
        app.requests.get = real
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
