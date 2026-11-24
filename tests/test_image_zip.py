"""Step 3's "Download images (.zip)": gui_helpers.build_image_zip.

The zip must unpack INTO wp-content/uploads/ so every image URL in the CSV
resolves, must never read a file outside the image folder, and must list
every URL in image_map.csv with ok / missing / refused.

    .venv\\Scripts\\python.exe -m pytest tests\\test_image_zip.py -q
"""

import csv
import io
import sys
import zipfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gui_helpers as gh  # noqa: E402

BASE = "https://shop.example/wp-content/uploads/2026/11/"


def write_csv(path: Path, rows):
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def image_dir(tmp_path: Path, *names) -> Path:
    d = tmp_path / "images"
    d.mkdir()
    for n in names:
        (d / n).write_bytes(b"RIFF" + n.encode())
    return d


def open_zip(data: bytes):
    zf = zipfile.ZipFile(io.BytesIO(data))
    rows = list(csv.DictReader(io.StringIO(
        zf.read("image_map.csv").decode("utf-8"))))
    return zf, rows


def test_entry_paths_mirror_the_url_paths(tmp_path):
    imgs = image_dir(tmp_path, "WM-A_1.webp", "WM-A_2.webp", "AC-B_1.webp")
    c = write_csv(tmp_path / "out.csv", [
        {"sku": "WM-A", "Images": f"{BASE}WM-A_1.webp, {BASE}WM-A_2.webp"},
        {"sku": "AC-B", "Images": f"{BASE}AC-B_1.webp"}])
    data, s = gh.build_image_zip(c, imgs)
    zf, rows = open_zip(data)
    assert sorted(zf.namelist()) == sorted([
        "2026/11/WM-A_1.webp", "2026/11/WM-A_2.webp", "2026/11/AC-B_1.webp",
        "image_map.csv", "README.txt"])
    assert zf.read("2026/11/AC-B_1.webp") == b"RIFFAC-B_1.webp"
    assert "wp-content/uploads/" in zf.read("README.txt").decode()
    assert s["images"] == 3 and s["products"] == 2 and s["urls"] == 3
    assert s["missing"] == [] and s["refused"] == []
    assert [(r["sku"], r["position"], r["zip_path"], r["status"])
            for r in rows] == [
        ("WM-A", "1", "2026/11/WM-A_1.webp", "ok"),
        ("WM-A", "2", "2026/11/WM-A_2.webp", "ok"),
        ("AC-B", "1", "2026/11/AC-B_1.webp", "ok")]
    assert rows[1]["image_url"] == f"{BASE}WM-A_2.webp"


def test_missing_image_is_listed_not_fatal(tmp_path):
    imgs = image_dir(tmp_path, "WM-A_1.webp")
    c = write_csv(tmp_path / "out.csv", [
        {"sku": "WM-A", "Images": f"{BASE}WM-A_1.webp,{BASE}WM-A_2.webp"}])
    data, s = gh.build_image_zip(c, imgs)
    zf, rows = open_zip(data)
    assert "2026/11/WM-A_2.webp" not in zf.namelist()
    assert s["images"] == 1 and s["missing"] == ["2026/11/WM-A_2.webp"]
    assert [r["status"] for r in rows] == ["ok", "missing"]
    assert "1 missing" in gh.image_zip_caption(s)


def test_traversal_url_never_reads_outside_image_dir(tmp_path):
    imgs = image_dir(tmp_path, "ok_1.webp")
    secret = tmp_path / "x.webp"            # sits NEXT TO the image folder
    secret.write_bytes(b"SECRET")
    bad = [
        "https://shop.example/wp-content/uploads/../../etc/x.webp",
        "https://shop.example/wp-content/uploads/2026/11/..",
        "https://shop.example/wp-content/uploads/2026/%2e%2e/x.webp",
        "https://shop.example/wp-content/uploads/2026/11/..%5Cx.webp",
    ]
    c = write_csv(tmp_path / "out.csv", [
        {"sku": "S", "Images": ",".join(bad + [f"{BASE}ok_1.webp"])}])
    data, s = gh.build_image_zip(c, imgs)
    zf, rows = open_zip(data)
    for name in zf.namelist():
        assert ".." not in name and not name.startswith("/"), name
        assert zf.read(name) != b"SECRET", name
    assert sorted(zf.namelist()) == ["2026/11/ok_1.webp", "README.txt",
                                     "image_map.csv"]
    assert [r["status"] for r in rows] == ["refused"] * 4 + ["ok"]
    assert len(s["refused"]) == 4 and s["images"] == 1
    # the pack uses the same guard: a '..' name copies nothing
    assert gh.safe_image_source(imgs, "..") is None
    assert gh.safe_image_source(imgs, "..\\x.webp") is None
    assert gh.safe_image_source(imgs, "ok_1.webp") == (imgs / "ok_1.webp"
                                                       ).resolve()


def test_non_wp_url_falls_back_to_the_url_path(tmp_path):
    imgs = image_dir(tmp_path, "P_1.webp")
    c = write_csv(tmp_path / "out.csv", [
        {"sku": "P", "Images": "https://cdn.example/media/p/P_1.webp"}])
    data, s = gh.build_image_zip(c, imgs)
    zf, rows = open_zip(data)
    assert "media/p/P_1.webp" in zf.namelist()
    assert rows[0]["zip_path"] == "media/p/P_1.webp"
    assert rows[0]["status"] == "ok"


def test_zero_image_csv_gives_an_empty_result(tmp_path):
    imgs = image_dir(tmp_path)
    c = write_csv(tmp_path / "out.csv", [{"sku": "A", "Images": ""},
                                         {"sku": "B", "Images": ""}])
    data, s = gh.build_image_zip(c, imgs)
    assert data == b"" and s["urls"] == 0 and s["images"] == 0
    c2 = write_csv(tmp_path / "noimg.csv", [{"sku": "A", "title": "x"}])
    data2, s2 = gh.build_image_zip(c2, imgs)
    assert data2 == b"" and s2["urls"] == 0


def test_shared_image_is_one_entry_two_map_rows(tmp_path):
    imgs = image_dir(tmp_path, "X_1.webp")
    c = write_csv(tmp_path / "out.csv", [
        {"sku": "A", "Images": f"{BASE}X_1.webp"},
        {"sku": "B", "Images": f"{BASE}X_1.webp"}])
    data, s = gh.build_image_zip(c, imgs)
    zf, rows = open_zip(data)
    assert zf.namelist().count("2026/11/X_1.webp") == 1
    assert [r["status"] for r in rows] == ["ok", "ok"]


def test_zip_and_pack_take_the_same_files(tmp_path):
    """The button and the client pack can never disagree on WHICH files."""
    imgs = image_dir(tmp_path, "WM-A_1.webp", "AC-B_1.webp")
    c = write_csv(tmp_path / "out.csv", [
        {"sku": "WM-A", "Images": f"{BASE}WM-A_1.webp,{BASE}WM-A_9.webp"},
        {"sku": "AC-B", "Images": f"{BASE}AC-B_1.webp"}])
    data, s = gh.build_image_zip(c, imgs)
    pack = gh.assemble_pack(tmp_path / "pack", {}, imgs, c, internal=True)
    zf, _ = open_zip(data)
    zipped = sorted(Path(n).name for n in zf.namelist()
                    if n.endswith(".webp"))
    packed = sorted(p.name for p in (tmp_path / "pack" / "images").iterdir())
    assert zipped == packed
    assert pack["missing"] == len(s["missing"]) == 1


def test_unreadable_csv_is_reported(tmp_path):
    c = tmp_path / "empty.csv"
    c.write_bytes(b"")
    data, s = gh.build_image_zip(c, tmp_path)
    assert data == b"" and s["csv_error"]
