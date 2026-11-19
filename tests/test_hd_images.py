"""The HD image contract, end to end.

What "HD" means here, and where each rule lives:

  stage 1  sku_enricher.IMAGE_MIN_PX   reject a source image under this
  stage 2  app.MIN_IMAGE_EDGE          refuse to download one under this
           app.SQUARE_IMAGES           pad (never crop) onto a 1:1 canvas
           app.CANVAS_EDGE             EVERY final image is exactly 1500x1500,
                                       product centred, upscale capped at
                                       app.MAX_UPSCALE
           app.white_share             the whitest white-background photo is _1
           app.WEBP_QUALITY            re-encode quality
  stage 3  validate_csv.GOOD_IMAGE_PX  flag anything softer than this
           validate_csv.CANVAS_EDGE    warn on a final file off the 1500 canvas

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
    check("every catalogue master is on the one 1500 canvas (2026-11-19)",
          app.CANVAS_EDGE, 1500)
    check("validate_csv checks the same canvas size",
          V.CANVAS_EDGE, app.CANVAS_EDGE)
    check("the per-product canvas tiers are gone",
          hasattr(app, "CANVAS_EDGES") or hasattr(app, "choose_canvas"), False)
    check("the HD floor is 1000px", app.MIN_IMAGE_EDGE, 1000)
    # on the 1500 canvas a floor-size source can NOT fill the margin box:
    # the upscale cap holds and the product sits smaller, centred (no new
    # upscaling rule - owner decision 2026-11-19 changed only the edge)
    inner = app.CANVAS_EDGE * (1 - 2 * app.MARGIN_PCT / 100)
    check("a floor-size source would need more than MAX_UPSCALE",
          inner / app.MIN_IMAGE_EDGE > app.MAX_UPSCALE, True)
    check("so it is grown by MAX_UPSCALE only",
          app.canvas_scale(1000, 1000, True), app.MAX_UPSCALE)
    check("the upscale cap is unchanged", app.MAX_UPSCALE, 1.15)
    check("catalogue tiles are square", app.SQUARE_IMAGES, True)
    check("webp quality is the high setting", app.WEBP_QUALITY, 95)


def test_download_rules():
    """Drive app.download_urls_for_sku with generated images of known size."""
    section("what actually lands in product_images_for_upload")
    sizes = {
        "under":  (999, 999),      # one pixel below the floor -> refused
        "atfloor": (1000, 1000),   # exactly the floor -> 1500x1500 canvas
        "wide":   (3000, 1200),    # oversized + wide -> padded, 1500x1500
        "huge":   (4000, 4000),    # oversized square -> 1500x1500
        "small":  (300, 300),      # plainly a thumbnail -> refused
        "between": (1100, 1050),   # just above the floor -> 1500x1500
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
        # files are numbered contiguously (_1.._n); with every source equally
        # white the main-image tie goes to URL order, so _n maps to `kept`
        kept = ["atfloor", "wide", "huge", "between"]
        check("sub-HD sources are refused", saved, [1, 2, 3, 4])

        root = tmp / app.IMAGE_ROOT
        got = {}
        for i in saved:
            with Image.open(root / f"AP-HD_{i}.webp") as im:
                got[kept[i - 1]] = (im.size, im.format)
        check("the 1000px source lands on the 1500x1500 canvas",
              got["atfloor"][0], (1500, 1500))
        check("a 3000x1200 master is squared and finished at 1500x1500",
              got["wide"][0], (1500, 1500))
        check("a 4000x4000 master is downscaled to 1500x1500",
              got["huge"][0], (1500, 1500))
        check("an 1100px source lands on the same 1500x1500 canvas",
              got["between"][0], (1500, 1500))
        check("everything is WEBP", {v[1] for v in got.values()}, {"WEBP"})
        check("every master is on the one fixed canvas",
              {v[0] for v in got.values()}, {(1500, 1500)})

        names = [f"AP-HD_{i}.webp" for i in saved]
        problems = V.image_dimension_problems(names, root=str(root))
        check("validate_csv finds nothing wrong with them", problems, [])
        check("validate_csv canvas check agrees",
              V.image_canvas_problems(names, root=str(root)), [])

        # a padded wide photo must keep its whole subject - no cropping
        with Image.open(root / "AP-HD_2.webp") as im:      # the 3000x1200
            px = im.convert("RGB").load()
            check("padding is white, not a crop", px[5, 5], (255, 255, 255))
    finally:
        app.requests.get = real
        app.IMAGE_DIR = saved_image_dir
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def _photo(background, product, size=(1200, 1200)):
    """A source photo: a flat background with a centred product block."""
    im = Image.new("RGB", size, background)
    w, h = size
    im.paste(Image.new("RGB", (w // 2, h // 3), product),
             (w // 4, h // 3))
    return im


def _off_white_photo(product):
    """White photo whose border strip is 96% white: the first 188 px of the
    top strip are grey (188 x 24 of the 112,896 strip pixels on 1200x1200)."""
    im = _photo("white", product)
    im.paste(Image.new("RGB", (188, 24), (200, 200, 200)), (0, 0))
    return im


def _run_gallery(images):
    """Drive download_urls_for_sku over in-memory images (URL order = list
    order). Returns (saved indexes, flags, {n: RGB image of SKU_n.webp})."""
    class R:
        def __init__(self, data):
            self.content = data

        def raise_for_status(self):
            pass

    def fake_get(url, **kw):
        key = int(url.rsplit("/", 1)[1].split(".")[0])
        buf = io.BytesIO()
        images[key].save(buf, format="PNG")
        return R(buf.getvalue())

    tmp = Path(tempfile.mkdtemp())
    real = app.requests.get
    saved_image_dir = app.IMAGE_DIR
    try:
        app.IMAGE_DIR = tmp
        app.requests.get = fake_get
        flags = []
        urls = [f"https://cdn.example.com/i/{k}.png" for k in range(len(images))]
        saved = app.download_urls_for_sku("AP-WF", urls, flags)
        out = {}
        for n in saved:
            with Image.open(tmp / f"AP-WF_{n}.webp") as im:
                out[n] = im.convert("RGB")
        return saved, flags, out
    finally:
        app.requests.get = real
        app.IMAGE_DIR = saved_image_dir
        shutil.rmtree(tmp, ignore_errors=True)


def _dominant(rgb_pixel):
    """'r' / 'g' / 'b' - which channel a (lossy WebP) product pixel leans to."""
    return "rgb"[max(range(3), key=lambda i: rgb_pixel[i])]


def _product_colour(im):
    """'r' / 'g' / 'b' - the colour of the product block in a finished image:
    the channel with the most strongly saturated pixels."""
    counts = [0, 0, 0]
    small = im.resize((60, 60))
    for px in (small.getpixel((x, y)) for y in range(60) for x in range(60)):
        for i in range(3):
            if px[i] > 150 and all(px[j] < 100 for j in range(3) if j != i):
                counts[i] += 1
    return "rgb"[max(range(3), key=lambda i: counts[i])]


def test_fixed_canvas_every_output():
    section("every output is exactly CANVAS_EDGE square, whatever the source")
    gallery = [_photo("white", (220, 0, 0), (3000, 1200)),
               _photo((150, 150, 150), (0, 200, 0), (1000, 1600)),
               _photo("white", (0, 0, 220), (1050, 1000)),
               _photo("white", (0, 0, 0), (5000, 5000))]
    saved, _, out = _run_gallery(gallery)
    check("all four kept", saved, [1, 2, 3, 4])
    check("all outputs are exactly 1500x1500",
          {im.size for im in out.values()}, {(1500, 1500)})
    # the lifestyle (grey) photo is fitted, not cropped: its grey reaches
    # the vertical edges' middle but white bands fill the left/right sides
    grey = out[2]
    check("a tall lifestyle shot is padded left/right, not cropped",
          (grey.getpixel((5, 600)), _dominant(grey.getpixel((600, 600)))),
          ((255, 255, 255), "g"))


def test_small_source_is_padded_not_stretched():
    section("a source smaller than the canvas grows by MAX_UPSCALE, then pads")
    # a 1000x1000 lifestyle shot (grey, the HD floor): 1000 x 1.15 = 1150 px,
    # centred on the 1500 canvas with (1500 - 1150) / 2 = 175 px of white
    saved, _, out = _run_gallery([Image.new("RGB", (1000, 1000), (90, 90, 90))])
    im = out[saved[0]]
    check("canvas is 1500x1500", im.size, (1500, 1500))
    check("white band outside the product", im.getpixel((170, 750)),
          (255, 255, 255))
    grey = im.getpixel((180, 750))
    check("the product starts at 175 px (1150 px wide, not stretched)",
          max(abs(c - 90) for c in grey) < 12, True)
    check("and ends at 1325 px", im.getpixel((1330, 750)), (255, 255, 255))


def test_main_image_is_whitest():
    section("_1 is the whitest white-background photo; the rest keep URL order")
    check("pure white scores 1.0",
          app.white_share(_photo("white", (220, 0, 0))), 1.0)
    share = app.white_share(_off_white_photo((0, 200, 0)))
    check("the off-white photo scores ~0.96 (still white)",
          (round(share, 2), share >= app.WHITE_SHARE), (0.96, True))
    check("a grey backdrop scores 0",
          app.white_share(_photo((180, 180, 180), (0, 0, 220))), 0.0)
    gallery = [_photo((180, 180, 180), (220, 0, 0)),   # URL 1: grey  -> red
               _off_white_photo((0, 200, 0)),           # URL 2: 96%   -> green
               _photo("white", (0, 0, 220))]            # URL 3: 100%  -> blue
    saved, flags, out = _run_gallery(gallery)
    order = [_product_colour(out[n]) for n in saved]
    check("pure white (URL 3) is _1, then URL order 1, 2",
          order, ["b", "r", "g"])
    check("no main-not-white flag", flags, [])


def test_no_white_keeps_first_url():
    section("no white photo: _1 is the first URL and the flag is set")
    gallery = [_photo((180, 180, 180), (220, 0, 0)),
               _photo((120, 120, 160), (0, 200, 0))]
    saved, flags, out = _run_gallery(gallery)
    order = [_product_colour(out[n]) for n in saved]
    check("URL order kept", order, ["r", "g"])
    check("flag set", flags, [app.FLAG_MAIN_NOT_WHITE])


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
    test_fixed_canvas_every_output()
    test_small_source_is_padded_not_stretched()
    test_main_image_is_whitest()
    test_no_white_keeps_first_url()
    test_upsize_rules()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
