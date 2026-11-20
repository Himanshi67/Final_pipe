"""Attribute sources, the review summary, and the reviewer's price and
attribute edits (owner requests 2026-11-19, late).

A. Every required attribute and filter group reads, in a fixed order, the
   scraped spec rows, then the page's description text (`page_text`), then
   the title; the first source that STATES the value wins, and each filter
   hit records its source.
C. Step 2 of gui.py shows price and attributes (with sources) and lets the
   reviewer edit them as per-SKU overrides (description_overrides.json,
   keys 'price' / 'attributes'); app.process_inventory applies them.

    .venv\\Scripts\\python.exe -m pytest tests\\test_price_attribute_review.py -q
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

import pandas as pd                                # noqa: E402

import app                                         # noqa: E402
import gui_helpers as GH                           # noqa: E402

TV_GROUPS = {
    'TV Screen Size': ['32"', '43"', '50"', '55"', '65"', '75"+'],
    'TV Resolution': ['HD Ready', 'Full HD', '4K UHD', '8K'],
    'TV Display Tech': ['LED', 'QLED', 'OLED', 'Mini LED', 'QNED'],
    'TV Smart Features': ['Google TV', 'Android TV', 'WebOS', 'Tizen'],
    'TV Refresh Rate': ['60Hz', '120Hz'],
}
TV_TABLE = {app._cat_norm('LED'): ('LED', {
    'Type': app.AxisSpec(('Google TV', 'Android TV', 'WebOS', 'Tizen'), '',
                         None, False),
    'Size': app.AxisSpec(('32"', '43"', '50"', '55"', '65"', '75"+'), '',
                         'inch', False)})}


def hits(title, scraped=(), description=''):
    found, notes = app.filter_attributes('LED', 'LED', title, scraped,
                                         table=TV_TABLE, groups=TV_GROUPS,
                                         description=description)
    return {h.group: (h.value, h.source) for h in found}, notes


# ------------------------------------------------ A. screen size forms
@pytest.mark.parametrize("text", [
    "80 cm (32)", "80cm(32)", "80 cm (32 inch)", "32 inch", '32"',
    "81.28 cm (32 inches)",
])
def test_screen_size_forms_read_as_32_inch(text):
    assert hits(f"Brand {text} HD TV")[0]['TV Screen Size'] == ('32"', 'title')


def test_43_inches_in_brackets():
    assert hits("108 cm (43 inches) Full HD TV")[0]['TV Screen Size'] \
        == ('43"', 'title')


def test_cm_alone_is_never_converted():
    found, _notes = hits("Brand 80 cm HD TV")
    assert 'TV Screen Size' not in found


def test_spec_tags_reads_the_cm_inch_form():
    assert app.spec_tags("Haier 80cm(32) Smart Google TV")['screen'] == '32 Inch'


# ------------------------------------------------ A. the source order
def test_spec_row_wins_over_description_and_title():
    found, _ = hits("Brand 108 cm (43) TV", [("Screen Size", "32 Inch")],
                    "80 cm (32) TV")
    assert found['TV Screen Size'] == ('32"', 'specification')


def test_description_wins_over_title_when_the_spec_does_not_state_it():
    # Samsung: the spec row says only '80 cm'; the description states 32
    found, notes = hits("UA32H4500FUXXL", [("Screen Size", "80 cm")],
                        "80 cm (32) HD Smart TV UA32H4500FUXXL")
    assert found['TV Screen Size'] == ('32"', 'description')
    assert notes == []                    # nothing "not in list" once written


def test_title_is_the_last_source():
    found, _ = hits("Haier 80cm(32) Smart Google TV", [("Bluetooth", "Yes")],
                    "")
    assert found['TV Screen Size'] == ('32"', 'title')
    assert found['TV Smart Features'] == ('Google TV', 'title')


def test_resolution_with_a_thousands_comma_snaps_from_the_spec_row():
    found, notes = hits("Samsung TV", [("Resolution", "HD (1,366 x 768)")])
    assert found['TV Resolution'] == ('HD Ready', 'specification')
    assert not any('new value' in n for n in notes)


def test_platform_from_the_description():
    found, _ = hits("Samsung 80 cm (32) TV", (), "Smart TV with Tizen OS")
    assert found['TV Smart Features'] == ('Tizen', 'description')


def test_description_text_is_never_a_not_in_list_note():
    _found, notes = hits("Brand TV", (),
                         "a long marketing text with 100 cm and nothing else")
    assert notes == []


def test_spec_gaps_read_the_description_and_the_filters():
    ctx_title = "Samsung UA32H4500FUXXL"
    # without the description the Size axis has nothing; with it, filled
    assert 'Size' in app.missing_spec_axes('LED', ctx_title, 'tv', (), 'LED')
    assert 'Size' not in app.missing_spec_axes(
        'LED', ctx_title, 'tv', (), 'LED', description="80 cm (32) HD TV")
    hit = app.FilterHit('TV Screen Size', '32"', 'Size', frozenset(), 'exact')
    assert 'Size' not in app.missing_spec_axes('LED', ctx_title, 'tv', (),
                                               'LED', filters=[hit])
    assert 'Size' in app.missing_spec_axes(
        'LED', "Samsung 32 inch TV", 'tv', (), 'LED', removed={'Size'})


# ------------------------------------------------ C. price rule + overrides
def test_check_publish_price():
    assert app.check_publish_price('', 37790) == (None, '')
    assert app.check_publish_price('20,590', 37790) == (20590.0, '')
    assert app.check_publish_price('40000', 37790) == (None, app.PRICE_ABOVE_MRP)
    assert app.check_publish_price('abc', 37790) == (None, app.PRICE_NOT_NUMBER)
    assert app.check_publish_price('0', '') == (None, app.PRICE_NOT_NUMBER)
    assert app.check_publish_price('-5', '') == (None, app.PRICE_NOT_NUMBER)
    assert app.check_publish_price('99999', '') == (99999.0, '')   # no MRP


def test_apply_price_override_edits_the_inputs():
    regular, sale, published, refused = app.apply_price_override(
        37790.0, 18590.0, {'sale price': '19,000'})
    assert (regular, sale, published, refused) == (37790.0, 19000, None, [])
    regular, sale, published, refused = app.apply_price_override(
        37790.0, 18590.0, {'regular price': '', 'publish price': '50000'})
    assert regular == '' and published == 50000      # no MRP -> no cap
    _r, _s, published, refused = app.apply_price_override(
        37790.0, 18590.0, {'publish price': '40000', 'sale price': 'x'})
    assert published is None
    assert refused == ['sale price: ' + app.PRICE_NOT_NUMBER,
                       'publish price: ' + app.PRICE_ABOVE_MRP]


HAIER = {"sku": "HAIER-H32K82FX",
         "title": "Haier H32K82FX-HD Ready K82 Series 80cm(32) Smart Google TV",
         "categories": "", "Brands": "", "regular price": "37,790",
         "sale price": "18,590", "url_link": "", "image_url": "",
         "specs": "Operating System: Google TV | Bluetooth: Yes",
         "price_kind": "MOP",
         "page_text": "Haier HD Ready K82 Series 80cm(32) Smart Google TV"}


def test_review_summary_price_words_and_sources():
    info = app.review_summary(pd.Series(HAIER))
    assert info['kind'] == 'MOP'
    assert info['published'] == 20590
    assert info['rule'] == '18,590 + 2,000 = 20,590, cap 37,790'
    assert info['hand_price'] is False
    srcs = {n: (s, f) for n, _v, s, f in info['attributes']}
    assert srcs['TV Smart Features'] == ('specification', True)
    assert srcs['TV Screen Size'] == ('description', True)
    assert info['missing'] == []


def test_review_summary_with_reviewer_edits():
    ov = {'price': {'publish price': '25000'},
          'attributes': {'set': {'TV Screen Size': '43 inch', 'Colour': 'Black',
                                 'TV Refresh Rate': '100 Hz'},
                         'remove': ['TV Smart Features']}}
    info = app.review_summary(pd.Series(HAIER), ov)
    assert info['published'] == 25000 and info['hand_price']
    assert 'entered by the reviewer' in info['rule']
    attrs = {n: (v, s, f) for n, v, s, f in info['attributes']}
    assert attrs['TV Screen Size'] == ('43"', 'reviewer', True)   # snapped
    assert attrs['TV Refresh Rate'] == ('100Hz', 'reviewer', True)  # new value
    assert attrs['Colour'] == ('Black', 'reviewer', False)
    assert 'TV Smart Features' not in attrs
    assert 'Type' in info['missing']          # removed required attribute
    assert any("'100Hz' is a new value" in n for n in info['unmatched'])


def test_reviewer_value_refused_by_the_firewall():
    ov = {'attributes': {'set': {'Seller': 'Sold by Appario Retail'}}}
    info = app.review_summary(pd.Series(HAIER), ov)
    assert 'Seller' not in {n for n, *_ in info['attributes']}
    assert any('content firewall' in r for r in info['refused'])
    assert app.check_attribute_edit('Colour', 'Black') == ''
    assert app.check_attribute_edit('Colour', '') == app.ATTR_FORM_REASON
    assert app.check_attribute_edit('Wi-Fi', 'Yes') == app.ATTR_EMPTY_WORD_REASON


def test_reviewer_fills_a_required_attribute():
    row = dict(HAIER, title="Haier H32K82FX Smart TV", page_text="",
               specs="Bluetooth: Yes")
    assert 'Size' in app.review_summary(pd.Series(row))['missing']
    ov = {'attributes': {'set': {'TV Screen Size': '32'}}}
    info = app.review_summary(pd.Series(row), ov)
    assert 'Size' not in info['missing']
    assert ('TV Screen Size', '32"', 'reviewer', True) in info['attributes']


def no_downloads(monkeypatch):
    monkeypatch.setattr(app, "download_urls_for_sku",
                        lambda sku, urls, flags=None: [])


def test_build_without_overrides_is_byte_identical(monkeypatch):
    no_downloads(monkeypatch)
    df = pd.DataFrame([HAIER])
    a = app.process_inventory(df, media_base="https://x.test/m")
    b = app.process_inventory(df, media_base="https://x.test/m",
                              description_overrides={})
    assert a.to_csv(index=False) == b.to_csv(index=False)
    assert 'page_text' not in a.columns and 'price_kind' not in a.columns
    assert a.iloc[0]['_spec_gaps'] == ''
    assert str(a.iloc[0]['regular price']) == '20590'


def test_build_applies_price_and_attribute_overrides(monkeypatch):
    no_downloads(monkeypatch)
    state = {"version": 1, "skus": {}}
    assert GH.set_price_override(
        state, "HAIER-H32K82FX", {"regular price": "37,790",
                                  "sale price": "18590",
                                  "publish price": "21,000"},
        {"regular price": "37,790", "sale price": "18,590"}) == "saved"
    assert state["skus"]["HAIER-H32K82FX"]["price"]["values"] == {
        "publish price": "21000"}
    GH.set_attribute_override(state, "HAIER-H32K82FX",
                              {"Colour": "Black"}, ["Bluetooth"])
    out = app.process_inventory(pd.DataFrame([HAIER]),
                                media_base="https://x.test/m",
                                description_overrides=GH.overrides_for_build(
                                    state)).iloc[0]
    assert str(out['regular price']) == '21000'
    assert out['sale price'] in ('', None) or pd.isna(out['sale price'])
    names = {str(out[c]): c for c in out.index if str(c).startswith(
        'Attribute') and str(c).endswith('name')}
    assert 'Colour' in names and 'Bluetooth' not in names


def test_gui_helpers_price_state_round_trip():
    state = {"version": 1, "skus": {}}
    gen = {"regular price": "37,790", "sale price": "18,590"}
    assert GH.set_price_override(state, "X", {"regular price": "37790",
                                              "sale price": "18,590",
                                              "publish price": ""}, gen) \
        == "removed"
    assert GH.get_price_override(state, "X") is None
    GH.set_price_override(state, "X", {"sale price": ""}, gen)
    assert GH.overrides_for_build(state) == {"X": {"price": {"sale price": ""}}}
    assert GH.reset_override(state, "X", GH.PRICE_FIELD)
    assert GH.overrides_for_build(state) == {}


def test_gui_helpers_attribute_edits_and_merge():
    current = [("TV Screen Size", '32"'), ("Brand", "Haier"),
               ("Bluetooth", "Yes")]
    edited = [("TV Screen Size", '43"'), ("Brand", "Haier"),
              ("Bluetooth", ""), ("Colour", "Black"), (None, None)]
    sets, removes = GH.attribute_edits(current, edited)
    assert sets == {"TV Screen Size": '43"', "Colour": "Black"}
    assert removes == ["Bluetooth"]
    sets2, removes2 = GH.merge_attribute_edits(
        {"set": {"Colour": "Black"}, "remove": ["Bluetooth"]},
        {"Bluetooth": "Yes"}, ["Colour"])
    assert sets2 == {"Bluetooth": "Yes"} and removes2 == ["Colour"]


# ----------------------------------------------------------- gui (AppTest)
def test_card_review_saves_refuses_and_resets_price_and_attributes(tmp):
    import gui
    from test_gui_ui import boot, seeded_run, step2, assert_no_exception
    runs = tmp / "runs"
    run = seeded_run(runs, "paedit")
    P = gui.run_paths(run)
    before = P["filled"].read_bytes()
    at = boot(runs)
    step2(at)
    next(r for r in at.radio if "View" in str(r.label)).set_value(
        "Card review").run(timeout=60)
    assert_no_exception(at)
    inputs = {str(t.label): t for t in at.text_input}
    # a refused publish price is named in red and NOT saved
    inputs["Publish this exact price (empty = rule)"].set_value("abc") \
        .run(timeout=60)
    next(b for b in at.button if "Save price & attributes" in str(b.label)) \
        .click().run(timeout=60)
    assert_no_exception(at)
    assert "not a positive number" in " ".join(str(e.value) for e in at.error)
    assert GH.overrides_for_build(GH.load_overrides_state(
        GH.overrides_path(run))) == {}
    # a valid publish price is saved and marked hand-entered
    inputs = {str(t.label): t for t in at.text_input}
    inputs["Publish this exact price (empty = rule)"].set_value("1000") \
        .run(timeout=60)
    next(b for b in at.button if "Save price & attributes" in str(b.label)) \
        .click().run(timeout=60)
    assert_no_exception(at)
    built = GH.overrides_for_build(GH.load_overrides_state(
        GH.overrides_path(run)))
    (sku, entry), = built.items()
    assert entry == {"price": {"publish price": "1000"}}
    assert "HAND-ENTERED" in " ".join(str(m.value) for m in at.markdown)
    # reset puts the page price back; the scraped sheet was never written
    next(b for b in at.button if "Reset price to the page" in str(b.label)) \
        .click().run(timeout=60)
    assert_no_exception(at)
    assert json.loads(GH.overrides_path(run).read_text(
        encoding="utf-8"))["skus"] == {}
    assert P["filled"].read_bytes() == before
