"""Reviewer edits of 'description' and 'short description' (owner request
2026-11-19).

Step 2 of gui.py shows the two fields AS THE BUILD WRITES THEM (one
"Label: value" line per table row) and lets the reviewer edit them. The edit
is a per-SKU override in runs\\<name>\\description_overrides.json
(gui_helpers); app.process_inventory - used by gui.py and build_csv.py -
writes the override instead of the generated rows. Every override line
passes the content firewall again at build time. _filled.xlsx is never
touched.

    .venv\\Scripts\\python.exe -m pytest tests\\test_description_overrides.py -q
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

import openpyxl                                    # noqa: E402
import pandas as pd                                # noqa: E402

import app                                         # noqa: E402
import gui_helpers as GH                           # noqa: E402

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"


def row(**over):
    base = {"sku": "43FSFHS",
            "title": "Foxsky 109 cm (43 Inch) Full HD Smart LED TV 43FSFHS",
            "categories": "", "Brands": "Foxsky", "regular price": "",
            "sale price": "28,990", "url_link": "", "image_url": "",
            "specs": "Screen Size: 43 Inch | Resolution: 1920 x 1080"}
    base.update(over)
    return base


def no_downloads(monkeypatch):
    monkeypatch.setattr(app, "download_urls_for_sku",
                        lambda sku, urls, flags=None: [])


# ------------------------------------------------------------- app.py logic
def test_generated_fields_match_what_the_build_writes(monkeypatch):
    no_downloads(monkeypatch)
    fields = app.description_fields_for_row(pd.Series(row()))
    out = app.process_inventory(pd.DataFrame([row()]),
                                media_base="https://x.test/m").iloc[0]
    assert app.html_block(fields["short description"]) == \
        out["short description"]
    assert app.html_block(fields["description"]) == out["description"]
    # and the editor's lines round-trip to the SAME html, byte for byte
    for field in app.DESCRIPTION_FIELDS:
        lines = app.description_lines(fields[field])
        rows, refused = app.parse_description_lines("\n".join(lines),
                                                    "Foxsky")
        assert refused == []
        assert app.render_description_rows(rows) == out[field]


def test_parse_lines_form_bullets_and_blank_lines():
    rows, refused = app.parse_description_lines(
        "- Brand: Foxsky\n\n• Screen Size: 43 Inch\n1. Note: a: b\n"
        "just words\nLabel:\n", "Foxsky")
    assert rows == [("Brand", "Foxsky"), ("Screen Size", "43 Inch"),
                    ("Note", "a: b")]
    assert [line for line, _ in refused] == ["just words", "Label:"]
    assert all(why == app.LINE_FORM_REASON for _, why in refused)


@pytest.mark.parametrize("line", [
    "Seller: Appario Retail",
    "Offer: No Cost EMI available",
    "Website: https://www.amazon.in/dp/B0X",
    "Call: +91 98765 43210",
])
def test_parse_lines_refuses_what_the_firewall_refuses(line):
    rows, refused = app.parse_description_lines(
        ["Brand: Foxsky", line], "Foxsky")
    assert rows == [("Brand", "Foxsky")]
    assert refused == [(line, app.LINE_FIREWALL_REASON)]


def test_render_escapes_tags_and_empty_is_empty():
    assert app.render_description_rows([]) == ""
    html = app.render_description_rows([("Note", "<b>x</b>")])
    assert "<b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html


def test_override_key_normalises_numeric_skus():
    assert app.override_key(12345) == "12345"
    assert app.override_key(12345.0) == "12345"
    assert app.override_key("12345.0") == "12345"
    assert app.override_key(" AB-1 ") == "AB-1"
    assert app.override_key(float("nan")) == ""


def test_build_uses_the_override_and_refilters_it(monkeypatch):
    no_downloads(monkeypatch)
    df = pd.DataFrame([row(), row(sku="OTHER1", title="Foxsky 32 inch HD "
                                  "Ready LED TV OTHER1")])
    overrides = {"43FSFHS": {
        "short description": ["Brand: Foxsky", "Screen Size: 43 Inch",
                              "Seller: Appario Retail"],
        "description": [],                    # cleared on purpose
    }}
    out = app.process_inventory(df, media_base="https://x.test/m",
                                description_overrides=overrides)
    edited, other = out.iloc[0], out.iloc[1]
    assert edited["short description"] == app.html_block(
        [("Brand", "Foxsky"), ("Screen Size", "43 Inch")])
    assert "Appario" not in edited["short description"]
    assert pd.isna(edited["description"]) or edited["description"] == ""
    # the other SKU keeps its generated text
    plain = app.process_inventory(df, media_base="https://x.test/m").iloc[1]
    assert other["description"] == plain["description"]
    assert other["short description"] == plain["short description"]


def test_no_overrides_is_byte_identical(monkeypatch):
    no_downloads(monkeypatch)
    df = pd.DataFrame([row()])
    a = app.process_inventory(df, media_base="https://x.test/m")
    b = app.process_inventory(df, media_base="https://x.test/m",
                              description_overrides={})
    c = app.process_inventory(df, media_base="https://x.test/m",
                              description_overrides=None)
    assert a.equals(b) and a.equals(c)


# ------------------------------------------------------ gui_helpers (state)
def test_state_set_reset_and_build_form(tmp_path):
    path = GH.overrides_path(tmp_path)
    assert path.name == "description_overrides.json"
    state = GH.load_overrides_state(path)            # missing file
    assert state == {"version": 1, "skus": {}}
    gen = ["Brand: Foxsky", "Product SKU: TV-1"]
    assert GH.set_override(state, "SKU-A", "short description",
                           ["Brand: Foxsky"], gen, at="t") == "saved"
    assert GH.set_override(state, "SKU-A", "description", [], gen,
                           at="t") == "saved"
    GH.save_overrides_state(path, state)
    back = GH.load_overrides_state(path)
    assert GH.overrides_for_build(back) == {
        "SKU-A": {"short description": ["Brand: Foxsky"], "description": []}}
    assert GH.get_override(back, "SKU-A", "short description")["generated"] \
        == gen
    # an edit equal to the generated text is no override
    assert GH.set_override(back, "SKU-A", "short description", gen,
                           gen) == "removed"
    assert GH.get_override(back, "SKU-A", "short description") is None
    assert GH.reset_override(back, "SKU-A") is True
    assert GH.overrides_for_build(back) == {}
    assert GH.reset_override(back, "SKU-A") is False
    with pytest.raises(ValueError):
        GH.set_override(back, "SKU-A", "title", [], [])


def test_state_tolerates_a_broken_file(tmp_path):
    path = GH.overrides_path(tmp_path)
    path.write_text("{not json", encoding="utf-8")
    assert GH.overrides_for_build(GH.load_overrides_state(path)) == {}


def test_stale_override_is_detected():
    entry = {"lines": ["A: 1"], "generated": ["A: 1", "B: 2"]}
    assert GH.override_is_stale(entry, ["A: 1", "B: 2"]) is False
    assert GH.override_is_stale(entry, ["A: 1", "B: 3"]) is True
    assert GH.override_is_stale(None, ["A: 1"]) is False


# -------------------------------------------------- build_csv.py (headless)
def _filled_xlsx(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    r = row(status="FOUND (amazon)")
    ws.append(list(r))
    ws.append(list(r.values()))
    wb.save(path)


@pytest.mark.skipif(not VENV_PY.exists(), reason="needs the repo .venv")
def test_build_csv_flag_applies_the_overrides(tmp_path):
    src = tmp_path / "x_filled.xlsx"
    _filled_xlsx(src)
    state = {"version": 1, "skus": {}}
    GH.set_override(state, "43FSFHS", "short description",
                    ["Brand: Foxsky", "Reviewer Note: kept point"], [])
    ov = tmp_path / "description_overrides.json"
    GH.save_overrides_state(ov, state)
    out = tmp_path / "out.csv"
    proc = subprocess.run(
        [str(VENV_PY), "build_csv.py", str(src), "-o", str(out),
         "--no-validate", "--description-overrides", str(ov)],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
        env={**os.environ, "PIPELINE_CLIENT": "shreejee"})
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    csv = pd.read_csv(out, dtype=str, keep_default_na=False)
    assert "Reviewer Note" in csv.loc[0, "short description"]
    assert "Description overrides: 1 SKU(s)" in proc.stdout
    # without the flag: generated text
    proc = subprocess.run(
        [str(VENV_PY), "build_csv.py", str(src), "-o", str(out),
         "--no-validate"], cwd=ROOT, capture_output=True, text=True,
        timeout=300, env={**os.environ, "PIPELINE_CLIENT": "shreejee"})
    assert proc.returncode == 0
    csv = pd.read_csv(out, dtype=str, keep_default_na=False)
    assert "Reviewer Note" not in csv.loc[0, "short description"]


# ----------------------------------------------------------- gui (AppTest)
def test_card_review_shows_and_saves_a_description_edit(tmp):
    import gui
    from test_gui_ui import boot, seeded_run, step2, assert_no_exception
    runs = tmp / "runs"
    run = seeded_run(runs, "descedit")
    P = gui.run_paths(run)
    at = boot(runs)
    step2(at)
    next(r for r in at.radio if "View" in str(r.label)).set_value(
        "Card review").run(timeout=60)
    assert_no_exception(at)
    # first card is SKU-B (review bucket sorts first)
    areas = {str(t.label): t for t in at.text_area}
    short = next(t for k, t in areas.items()
                 if k.startswith("Short description"))
    assert "Brand: LG" in short.value
    short.set_value("Brand: LG\nSeller: Appario Retail").run(timeout=60)
    next(b for b in at.button if "Save description edits" in str(b.label)) \
        .click().run(timeout=60)
    assert_no_exception(at)
    state = GH.load_overrides_state(GH.overrides_path(run))
    assert GH.overrides_for_build(state) == {
        "SKU-B": {"short description": ["Brand: LG"]}}
    # the refused line is named to the reviewer
    errors = " ".join(str(e.value) for e in at.error)
    assert "Seller: Appario Retail" in errors
    # the label now says EDITED, and reset puts the generated text back
    assert any("EDITED" in str(t.label) for t in at.text_area)
    next(b for b in at.button if "Reset short description" in str(b.label)) \
        .click().run(timeout=60)
    assert_no_exception(at)
    state = GH.load_overrides_state(GH.overrides_path(run))
    assert GH.overrides_for_build(state) == {}
    # the scraped sheet was never written by the description editor
    sheet = pd.read_excel(P["filled"], dtype=str).fillna("")
    assert "Appario" not in " ".join(sheet.astype(str).values.ravel())
    assert not (run / "review_state.json").exists() or "Appario" not in \
        (run / "review_state.json").read_text(encoding="utf-8")
    assert json.loads(GH.overrides_path(run).read_text(
        encoding="utf-8"))["skus"] == {}
