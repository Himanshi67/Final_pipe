"""Card review shows the FINAL 1500 x 1500 images (owner request 2026-11-20).

app.preview_images_for_row runs the build's own image code - open_source_image
(fetch + 1000 px floor), square_image (white share + layout_on_canvas + WebP)
and order_square_results (_1 = whitest photo) - and writes nothing. These
tests pin that the preview and the build give the same order and the same
bytes, that a refused photo carries its reason, that no file is written and
that retire_stale_images never runs. No network: app.fetch_image_bytes is
stubbed with in-memory images.

    .venv\\Scripts\\python.exe -m pytest tests\\test_review_image_preview.py -q
"""
import io
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

from PIL import Image                              # noqa: E402

import app                                         # noqa: E402


def _photo(background, size=(1200, 1200), product=(200, 30, 30)):
    im = Image.new("RGB", size, background)
    w, h = size
    im.paste(Image.new("RGB", (w // 2, h // 3), product), (w // 4, h // 3))
    return im


def _off_white(size=(1200, 1200)):
    """White photo whose border is ~96% white (above WHITE_SHARE, below a
    pure white photo), so it loses the main slot to a pure white one."""
    im = _photo("white", size)
    im.paste(Image.new("RGB", (188, 24), (200, 200, 200)), (0, 0))
    return im


def _png(im):
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


GALLERY = {
    "a": _off_white(),                          # white, but not the whitest
    "b": _photo("white"),                       # whitest -> becomes _1
    "c": _photo("white", size=(500, 500)),      # below the 1000 px floor
    "d": _photo((90, 120, 160)),                # lifestyle (not white)
}
URLS = [f"https://cdn.example.com/i/{k}.png" for k in "abcd"]


@pytest.fixture
def stub_fetch(monkeypatch):
    calls = []

    def fake(url):
        calls.append(url)
        key = url.rsplit("/", 1)[1].split(".")[0]
        if key == "broken":
            raise app.requests.HTTPError("HTTP 404")
        return _png(GALLERY[key])

    monkeypatch.setattr(app, "fetch_image_bytes", fake)
    return calls


@pytest.fixture
def no_retire(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("retire_stale_images must not run in a preview")
    monkeypatch.setattr(app, "retire_stale_images", boom)


def test_preview_matches_the_build_order_and_bytes(stub_fetch, tmp_path,
                                                   monkeypatch):
    monkeypatch.setattr(app, "IMAGE_DIR", tmp_path / "build")
    flags = []
    saved = app.download_urls_for_sku("PV-1", URLS, flags)
    assert saved == [1, 2, 3]
    files = {n: (tmp_path / "build" / f"PV-1_{n}.webp").read_bytes()
             for n in saved}

    monkeypatch.setattr(app, "IMAGE_DIR", tmp_path / "preview")
    preview = app.preview_images_for_row(URLS)
    got = [(img["position"], img["url"]) for img in preview["images"]]
    # _1 = the whitest photo (b); the rest keep URL order; c is refused
    assert got == [(1, URLS[1]), (2, URLS[0]), (3, URLS[3])]
    for img in preview["images"]:
        assert img["data"] == files[img["position"]], img["position"]
    with Image.open(io.BytesIO(preview["images"][0]["data"])) as im:
        assert im.size == (app.CANVAS_EDGE, app.CANVAS_EDGE)
    assert preview["main_not_white"] is False and flags == []


def test_below_floor_photo_is_reported_with_its_reason(stub_fetch):
    preview = app.preview_images_for_row(URLS)
    assert [r["url"] for r in preview["rejected"]] == [URLS[2]]
    rej = preview["rejected"][0]
    assert rej["kind"] == app.PREVIEW_FLOOR
    assert f"{app.MIN_IMAGE_EDGE} px floor" in rej["reason"]
    assert "500 x 500" in rej["reason"]


def test_fetch_failure_is_reported_and_does_not_stop_the_rest(stub_fetch):
    urls = [URLS[1], "https://cdn.example.com/i/broken.png"]
    preview = app.preview_images_for_row(urls)
    assert [img["url"] for img in preview["images"]] == [URLS[1]]
    rej = preview["rejected"][0]
    assert rej["kind"] == app.PREVIEW_FAILED
    assert "download failed" in rej["reason"] and "404" in rej["reason"]


def test_preview_writes_no_file_and_never_retires(stub_fetch, no_retire,
                                                  tmp_path, monkeypatch):
    target = tmp_path / "upload"
    monkeypatch.setattr(app, "IMAGE_DIR", target)
    app.preview_images_for_row(URLS)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_dropped_urls_are_left_out_like_the_build(stub_fetch):
    preview = app.preview_images_for_row(URLS, dropped_cell=URLS[1])
    assert URLS[1] not in [img["url"] for img in preview["images"]]
    # without b, the off-white photo a is the whitest -> _1
    assert preview["images"][0]["url"] == URLS[0]
    assert URLS[1] not in stub_fetch            # a dropped photo is not fetched


def test_no_white_photo_sets_main_not_white(stub_fetch):
    preview = app.preview_images_for_row([URLS[3]])
    assert preview["main_not_white"] is True
    assert preview["images"][0]["position"] == 1
    assert app.preview_images_for_row([])["main_not_white"] is False


def test_injected_fetch_one_gives_the_same_order(stub_fetch):
    """The GUI's cached fetch_one (small WebP, same white/share) orders the
    photos exactly as the uncached path does."""
    def cached(url):
        white, share, data = app.square_image(url)
        return white, share, app.preview_thumbnail(data)

    plain = app.preview_images_for_row(URLS)
    small = app.preview_images_for_row(URLS, fetch_one=cached)
    assert [i["url"] for i in small["images"]] == \
        [i["url"] for i in plain["images"]]
    assert [r["url"] for r in small["rejected"]] == [URLS[2]]
    with Image.open(io.BytesIO(small["images"][0]["data"])) as im:
        assert im.format == "WEBP"
        assert max(im.size) <= app.PREVIEW_EDGE


def test_order_square_results_rules():
    rows = [(3, True, 0.97, b"c"), (1, False, 0.1, b"a"),
            (2, True, 0.99, b"b"), (4, True, 0.99, b"d")]
    ordered, main_white = app.order_square_results(rows)
    # whitest wins, tie -> earlier URL; the rest in URL order
    assert [r[0] for r in ordered] == [2, 1, 3, 4] and main_white
    ordered, main_white = app.order_square_results([(2, False, 0, b""),
                                                    (1, False, 0, b"")])
    assert [r[0] for r in ordered] == [1, 2] and not main_white
    assert app.order_square_results([]) == ([], True)


# ----------------------------------------------------------- gui (AppTest)
def image_captions(at):
    """Every st.image caption on the page (AppTest has no image accessor)."""
    out = []
    for el in at.get("image"):
        for img in getattr(el.proto, "imgs", []):
            out.append(img.caption)
    return " | ".join(out)


def test_card_review_shows_the_final_images(tmp, monkeypatch):
    import gui
    from test_gui_ui import boot, seeded_run, step2, assert_no_exception

    def fake(url):
        name = url.rsplit("/", 1)[1]
        if name == "a2.jpg":
            return _png(_photo("white", size=(400, 400)))     # below floor
        return _png(_photo("white"))

    monkeypatch.setattr(app, "fetch_image_bytes", fake)
    monkeypatch.setenv(gui.REVIEW_PREVIEW_ENV, "on")
    gui._final_image_preview.clear()
    runs = tmp / "runs"
    seeded_run(runs, "finalprev")
    at = boot(runs)
    step2(at)
    next(r for r in at.radio if "View" in str(r.label)).set_value(
        "Card review").run(timeout=60)
    assert_no_exception(at)
    # first card is SKU-B (one photo, b1.jpg): the canvas is _1, main image
    toggle = next(t for t in at.toggle if "final 1500" in str(t.label))
    assert toggle.value is True
    captions = " ".join(str(c.value) for c in at.caption)
    assert "Preview of the 1500 x 1500 output" in captions
    assert "_1 (photo 1) - main image" in image_captions(at)
    # move on to SKU-A (two photos, a2 below the floor)
    for _ in range(3):
        keep_keys = sorted(c.key for c in at.checkbox
                           if str(c.key).startswith("keep_"))
        if keep_keys and "SKU-A" in keep_keys[0]:
            break
        next(b for b in at.button if "Next" in str(b.label)).click().run(
            timeout=60)
        assert_no_exception(at)
    # the keep checkboxes keep their keys
    assert keep_keys == ["keep_finalprev_SKU-A_0", "keep_finalprev_SKU-A_1"]
    shown = image_captions(at)
    assert "_1 (photo 1) - main image" in shown
    assert f"below the {app.MIN_IMAGE_EDGE} px floor" in shown
    # switching the preview off still renders the card
    next(t for t in at.toggle if "final 1500" in str(t.label)).set_value(
        False).run(timeout=60)
    assert_no_exception(at)
    gui._final_image_preview.clear()


def test_gallery_falls_back_when_the_preview_breaks(monkeypatch):
    """final_image_gallery never lets a preview failure break the card."""
    import gui
    shown = []

    class Slot:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def boom(*a, **k):
        raise RuntimeError("no preview")

    monkeypatch.setattr(app, "preview_images_for_row", boom)
    monkeypatch.setattr(gui, "_configured_app", lambda: app)
    monkeypatch.setattr(gui.st, "image",
                        lambda src, **k: shown.append(src))
    monkeypatch.setattr(gui.st, "warning", lambda *a, **k: None)
    import contextlib
    monkeypatch.setattr(gui.st, "spinner",
                        lambda *a, **k: contextlib.nullcontext())
    gui.final_image_gallery(URLS[:2], {URLS[0]: True, URLS[1]: True},
                            [Slot(), Slot()], True)
    assert shown == URLS[:2]
