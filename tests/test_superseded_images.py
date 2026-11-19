"""Old image files leave the upload folder (owner decision 2026-11-19, fifth
round). After a build writes a SKU's images, every other `<sku>_<n>.webp` in
product_images_for_upload\\<slug>\\ is MOVED (never deleted) to
`_superseded\\<YYYY-MM-DD>\\` - also when the build saved 0 images. The SKU is
matched exactly. Every test works in tmp_path.

    .venv\\Scripts\\python.exe -m pytest tests\\test_superseded_images.py -q
"""
import io
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

import app                                         # noqa: E402

DAY = "2026-11-19"


def _touch(folder, name, text="old"):
    (folder / name).write_text(text, encoding="utf-8")


def _names(folder):
    return sorted(p.name for p in folder.iterdir() if p.is_file())


def test_unwritten_files_of_the_sku_are_moved(tmp_path):
    for name in ("TV-X_1.webp", "TV-X_2.webp", "TV-X_3.webp",
                 "TV-X2_1.webp", "TV-X-B_1.webp", "TV-X_1.webp.bak",
                 "TV-X_cover.webp", "AP-Y_1.webp"):
        _touch(tmp_path, name)
    moved = app.retire_stale_images("TV-X", [1], root=tmp_path, today=DAY)
    assert moved == ["TV-X_2.webp", "TV-X_3.webp"]
    # exact SKU: TV-X2 / TV-X-B / other names stay where they are
    assert _names(tmp_path) == ["AP-Y_1.webp", "TV-X-B_1.webp",
                                "TV-X2_1.webp", "TV-X_1.webp",
                                "TV-X_1.webp.bak", "TV-X_cover.webp"]
    assert _names(tmp_path / "_superseded" / DAY) == moved


def test_zero_saved_moves_every_file_of_the_sku(tmp_path):
    _touch(tmp_path, "TV-X_1.webp")
    _touch(tmp_path, "TV-X_2.webp")
    assert app.retire_stale_images("TV-X", [], root=tmp_path, today=DAY) == [
        "TV-X_1.webp", "TV-X_2.webp"]
    assert _names(tmp_path) == []


def test_name_collision_gets_a_numeric_suffix_and_nothing_is_deleted(tmp_path):
    target = tmp_path / "_superseded" / DAY
    target.mkdir(parents=True)
    _touch(target, "TV-X_2.webp", "first")
    _touch(target, "TV-X_2-2.webp", "second")
    _touch(tmp_path, "TV-X_2.webp", "third")
    app.retire_stale_images("TV-X", [1], root=tmp_path, today=DAY)
    assert _names(target) == ["TV-X_2-2.webp", "TV-X_2-3.webp", "TV-X_2.webp"]
    assert (target / "TV-X_2-3.webp").read_text(encoding="utf-8") == "third"
    assert (target / "TV-X_2.webp").read_text(encoding="utf-8") == "first"


def test_case_only_difference_is_the_same_file(tmp_path):
    _touch(tmp_path, "tv-x_1.webp")      # NTFS: the same file as TV-X_1.webp
    _touch(tmp_path, "tv-x_4.webp")
    assert app.retire_stale_images("TV-X", [1], root=tmp_path,
                                   today=DAY) == ["tv-x_4.webp"]


def test_superseded_folder_is_never_rescanned(tmp_path):
    _touch(tmp_path, "TV-X_5.webp")
    app.retire_stale_images("TV-X", [1], root=tmp_path, today=DAY)
    # a second build the same day: nothing left to move, the archive is intact
    assert app.retire_stale_images("TV-X", [1], root=tmp_path, today=DAY) == []
    assert _names(tmp_path / "_superseded" / DAY) == ["TV-X_5.webp"]


def test_download_retires_old_files_and_prints_one_line(tmp_path, monkeypatch,
                                                         capsys):
    class R:
        status_code = 200

        def __init__(self, data):
            self.content = data

        def raise_for_status(self):
            pass

    def fake_get(url, **kw):
        buf = io.BytesIO()
        Image.new("RGB", (1200, 1200), "white").save(buf, format="PNG")
        return R(buf.getvalue())

    monkeypatch.setattr(app, "IMAGE_DIR", tmp_path)
    monkeypatch.setattr(app.requests, "get", fake_get)
    for n in (1, 2, 3):                  # a 3-photo gallery from an older build
        _touch(tmp_path, f"TV-X_{n}.webp")
    saved = app.download_urls_for_sku("TV-X", ["https://cdn.test/a.png"])
    assert saved == [1]
    assert _names(tmp_path) == ["TV-X_1.webp"]
    with Image.open(tmp_path / "TV-X_1.webp") as im:
        assert im.size == (app.CANVAS_EDGE, app.CANVAS_EDGE) == (1500, 1500)
    day_dirs = list((tmp_path / "_superseded").iterdir())
    assert len(day_dirs) == 1
    assert _names(day_dirs[0]) == ["TV-X_2.webp", "TV-X_3.webp"]
    out = capsys.readouterr().out
    assert out.count("TV-X: moved 2 old image(s)") == 1


@pytest.mark.parametrize("consumer", ["validate_csv", "wc_publish",
                                      "gui_helpers"])
def test_no_consumer_lists_the_upload_folder(consumer):
    """Every consumer opens images BY NAME from the CSV; none lists the
    folder, so _superseded\\ can never be picked up."""
    src = (ROOT / f"{consumer}.py").read_text(encoding="utf-8")
    for call in ("IMAGE_DIR).iterdir", "image_dir.iterdir", "image_dir).iterdir",
                 "listdir(IMAGE_DIR", "listdir(root", "image_dir.rglob",
                 "image_dir.glob"):
        assert call not in src, (consumer, call)
