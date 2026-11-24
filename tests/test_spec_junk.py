"""Two step-2 review bugs (2026-11-25).

A. A reviewer attribute row 'Star rating: 3' did not clear the 'Star Rating'
   gap: missing_spec_axes compared the RAW label with the canonical labels.
B. Retailer banner / trust-badge rows from a Vijay Sales page passed the
   content firewall as spec rows ('Trusted Tech: Delivered Fast', ...).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
import sku_enricher as se  # noqa: E402

# Split AC: axes Type, Star Rating, Ton. The title states Type and Ton only.
SUB, TITLE = 'Split AC', 'Voltas 1.5 Ton Inverter Split AC'


def _gaps(rows, removed=()):
    ctx = {'sub': SUB, 'title': TITLE, 'category_family': 'ac', 'parent': '',
           'scraped': [], 'description': '', 'filters': (),
           'attribute_edit': {'rows': {k.lower(): (k, v) for k, v in rows},
                              'removed_axes': set(removed)}}
    return app.row_spec_gaps(ctx)


def test_star_rating_gap_present_without_a_row():
    assert 'Star Rating' in _gaps([])


def test_reviewer_star_rating_row_clears_the_gap():
    assert 'Star Rating' not in _gaps([('Star rating', '3')])


def test_canonical_energy_rating_row_still_clears_the_gap():
    assert 'Star Rating' not in _gaps([('Energy Rating', '3 Star')])
    assert 'Star Rating' not in app.missing_spec_axes(
        SUB, TITLE, 'ac', [('Energy Rating', '3 Star')])


def test_removed_axis_still_reported_missing():
    assert 'Star Rating' in _gaps([('Star rating', '3')],
                                  removed={'Star Rating'})


def test_empty_value_row_does_not_clear_the_gap():
    assert 'Star Rating' in _gaps([('Star rating', '')])
    assert 'Star Rating' in _gaps([('Star rating', '   ')])


JUNK = [
    ('Notify Me', 'Enter your email to receive product availability notifications'),
    ('Extended Warranty', 'Got a question? Look no further calls us.'),
    ('Trusted Tech', 'Delivered Fast'),
    ('Easy Installment', 'Pay for your purchase in easy EMIs.'),
]
REAL = [('Warranty', '1 Year on Product'), ('Energy Rating', '3 Star'),
        ('Capacity', '25 L'), ('Air Delivery', '3500 m3/h'),
        ('Manufacturer Warranty', '1 Year')]


def test_banner_rows_refused_by_the_firewall():
    for label, value in JUNK:
        assert not se.spec_row_ok(label, value), (label, value)
        assert not se.usable_spec(label, value), (label, value)


def test_real_spec_rows_survive_the_firewall():
    for label, value in REAL:
        assert se.spec_row_ok(label, value), (label, value)
        assert se.usable_spec(label, value), (label, value)


def test_build_drops_banner_rows_from_the_specs_cell():
    cell = ' | '.join(f'{k}: {v}' for k, v in JUNK + REAL)
    labels = [label for label, _v in app.parse_scraped_specs(cell)]
    assert labels == ['Warranty', 'Energy Rating', 'Capacity', 'Air Delivery',
                      'Manufacturer Warranty']


def test_reviewer_cannot_save_a_banner_row():
    for label, value in JUNK:
        assert app.check_attribute_edit(label, value), (label, value)
