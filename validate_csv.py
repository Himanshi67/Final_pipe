"""QA report for WooCommerce import files.

Usage:
    python validate_csv.py [csv_path]

If no path is given, tries wordpress_ready_clean_repaired.csv first, then
wordpress_ready.csv. Prints a summary and writes validation_issues.csv.

Checks per row:
  - missing sku / title / category / description (or placeholder text)
  - fewer than MIN_IMAGES images (the pipeline standard is 3+)
  - image URLs whose .webp file is NOT present locally in
    product_images_for_upload (phantom links = broken images after import)
  - image files that are too small or the wrong shape to be product photos
  - WARNING (never fails the check): flags app.py wrote to `_image_flags`,
    e.g. no white-background photo, so the featured image is a lifestyle shot
  - duplicate SKUs (would collide in WooCommerce)
  - price problems: both prices missing, regular price <= sale price, or a
    price outside the plausible band for the category. With [pricing]
    single_price the one price is 'regular price' and any 'sale price' is
    `sale_price_in_single_price_mode` (blocking)
  - attributes the product's own sub-category requires but nothing filled
  - attribute names whose WooCommerce slug is over 28 characters or is a
    reserved WordPress term - either one makes the importer reject the ROW
  - titles that say "front load" while the category/description says Top Load

Cross-row (the wrong-match signatures no per-row check can see):
  - two SKUs sharing a source URL, a first image, a whole gallery, any single
    image, or a title

Importable: build_csv.py calls validate_export() straight after writing the
CSV, so the QA gate is part of generating the file rather than a step someone
has to remember.
"""

import math
import os
import re
import sys
from collections import Counter

import pandas as pd

import client_config

# A comma-joined URL list cannot be split on commas alone: Amazon's rendition
# modifiers legitimately contain them (`._AC_UL165_SR165,165_.jpg`), and a plain
# split shredded those into fragments like '165_.jpg'. Seen live losing 7 of 12
# images on one product. Split only where a comma is followed by a URL start.
_URL_SPLIT_RE = re.compile(r',(?=\s*(?:https?://|//))')

# WooCommerce global attribute slugs: over this and the importer rejects the
# whole product row. app.py shortens names to fit (woo_attribute_name); this
# gate re-derives the slug the same way and catches anything that slipped by.
# tests/test_bugfixes.py pins that both sides use the same limit and list.
WOO_ATTR_SLUG_MAX = 28
RESERVED_ATTRIBUTE_SLUGS = frozenset((
    'attachment', 'attachment_id', 'author', 'author_name', 'calendar', 'cat',
    'category', 'category_name', 'cb', 'comments_per_page', 'comments_popup',
    'custom', 'customize_messenger_channel', 'customized', 'day', 'debug',
    'embed', 'error', 'exact', 'feed', 'fields', 'hour', 'link_category', 'm',
    'minute', 'monthnum', 'more', 'name', 'nav_menu', 'nonce', 'nopaging',
    'offset', 'order', 'orderby', 'p', 'page', 'page_id', 'paged', 'pagename',
    'pb', 'perm', 'post', 'post_format', 'post_mime_type', 'post_status',
    'post_tag', 'post_type', 'posts', 'posts_per_archive_page',
    'posts_per_page', 'preview', 'robots', 's', 'search', 'second', 'sentence',
    'showposts', 'static', 'status', 'subpost', 'subpost_id', 'tag', 'tag_id',
    'taxonomy', 'tb', 'term', 'terms', 'theme', 'title', 'type', 'types', 'w',
    'withcomments', 'withoutcomments', 'year',
))


# Discounts beyond these ratios are a wrongly-scraped MRP, not a deal. TVs
# get a tighter band: a real TV MRP is rarely 3x the street price (seen live:
# a Foxsky 43" shipped at 41,499 -> 12,999, a 69% "discount" that sailed
# under the old flat 4x limit), while small appliances legitimately hit 4x.
MRP_RATIO_LIMIT = 4.0
MRP_RATIO_LIMIT_TV = 3.0
_TV_CONTEXT_RE = re.compile(r'\bled\b|\btv\b|television|qled|oled|qned', re.I)


def mrp_ratio_limit(*context_texts):
    """The regular/sale ratio above which the MRP is implausible, picked from
    whatever category/title context the caller has."""
    text = ' '.join(str(t or '') for t in context_texts)
    return MRP_RATIO_LIMIT_TV if _TV_CONTEXT_RE.search(text) else MRP_RATIO_LIMIT


def woo_attr_slug(name):
    """The taxonomy slug WooCommerce will derive from an attribute name."""
    return re.sub(r'[^a-z0-9]+', '-', str(name).strip().lower()).strip('-')


def attribute_name_problems(row, attr_name_cols):
    """Import-blocking attribute names on one CSV row, as reason strings."""
    problems = []
    for col in attr_name_cols:
        name = str(row.get(col, '') or '').strip()
        if not name or name.lower() == 'nan':
            continue
        slug = woo_attr_slug(name)
        if len(slug) > WOO_ATTR_SLUG_MAX:
            problems.append(f'attribute_slug_too_long ("{slug}" is '
                            f'{len(slug)} chars, max {WOO_ATTR_SLUG_MAX})')
        elif slug in RESERVED_ATTRIBUTE_SLUGS:
            problems.append(f'attribute_name_reserved ("{name}")')
    return problems


def split_image_list(raw):
    """Split a stored image list without breaking URLs that contain commas."""
    text = str(raw or '').strip()
    if not text:
        return []
    parts = _URL_SPLIT_RE.split(text)
    if len(parts) == 1 and ',' in text and '://' not in text:
        parts = text.split(',')          # not a URL list - old behaviour
    return [p.strip() for p in parts if p.strip()]


MIN_IMAGES = 3
IMAGE_ROOT = 'product_images_for_upload'   # folder NAME (kept for compatibility)

# Which store this report is checking (PRODUCT_PLAN Phase 1 item 1). Resolved
# from --client / $PIPELINE_CLIENT / DEFAULT_CLIENT; build_csv.py and gui.py
# set PIPELINE_CLIENT before importing this module.
try:
    CLIENT = client_config.load_client()
except client_config.ClientConfigError as _exc:
    raise RuntimeError(
        f"Cannot start: {_exc} "
        f"(per-client config lives in clients/<slug>/client.toml - see "
        f"docs/CLIENT_CONFIG.md)") from _exc

# <repo>/product_images_for_upload/<slug> - absolute, so the local-file
# existence check no longer depends on which directory this was run from.
IMAGE_DIR = str(CLIENT.image_dir)
# The "we could not read a description" filler app.py writes for this client.
PLACEHOLDER = CLIENT.placeholder_html

# A product photo below this on its long edge is a thumbnail: it will look soft
# on a product page and terrible in a lightbox.
MIN_IMAGE_PX = 400
# Below this it is usable but not good. WooCommerce re-encodes its own sizes
# from whatever master it is given, so a soft master compounds. This is the
# SOURCE floor (= app.MIN_IMAGE_EDGE): app.py refuses any source under it and
# finishes every survivor on the fixed CANVAS_EDGE square below, so a final
# file under it means a stale or hand-made image, not a pipeline output.
GOOD_IMAGE_PX = 1000
# Every final image app.py writes is exactly CANVAS_EDGE x CANVAS_EDGE (owner
# rule: one fixed layout for every photo). Must equal app.CANVAS_EDGE -
# tests/test_hd_images.py pins it; it is copied, not imported, because this
# script stays import-free of app.py (app.py drags in streamlit).
CANVAS_EDGE = 1500          # owner decision 2026-11-19 (was 1200)
# Beyond this the file is a banner, a size chart or a sliver of one - not a
# photo of the appliance. 1:3 either way is already generous for a product shot.
MAX_IMAGE_ASPECT = 3.0

# A selling price below this for the category means an accessory (a remote, a
# stand, a cover) was matched instead of the appliance. Keyed on a substring of
# the category string, checked longest-first.
CATEGORY_PRICE_FLOOR = {
    'air conditioner': 12000,
    'refrigerator': 6000,
    'washing machine': 6000,
    'microwave': 3000,
    'water heater': 1500,
    'led': 5000,
    'television': 5000,
}

# The other end of the same test. A number far above the category's ceiling is
# almost never a real price - it is a bundle, a dealer pack, or an MRP read out
# of the wrong element. Deliberately loose: these are "look at this", not "this
# is wrong", and the premium-panel exemption in implausible_tv_price() applies
# to TVs on top of this.
CATEGORY_PRICE_CEILING = {
    'air conditioner': 250000,
    'refrigerator': 400000,
    'washing machine': 250000,
    'microwave': 100000,
    'water heater': 60000,
    'electric iron': 25000,
    'electric kettle': 25000,
    'induction cooktop': 40000,
    'led': 1000000,
    'television': 1000000,
}

# Wording that app.py used to emit when it could not read a spec. Nothing should
# produce these any more (KI-14); if one appears, a stale CSV is being validated
# or the placeholder behaviour has come back.
# NOT client-derived on purpose: these are app.py's own invented spec VALUES
# ('Standard Capacity'), identical for every store. The one string that IS
# store-specific is PLACEHOLDER above, and it comes from client.toml.
DEAD_PLACEHOLDERS = (
    'Standard Capacity', 'Standard Power Rating', 'Standard Washing Capacity',
    'Standard Tank Volume', 'Standard Cavity Volume', 'Standard Liquid Volume',
    'Appliance Utility Core', 'WM Function Type', 'WM Washing Capacity',
    # the four the KI-14 sweep missed: asserted for every iron / sewing machine
    # regardless of the listing. The readers that produced them now return ''
    # unless the title states the fact, so nothing can emit these again.
    'Premium Non-Stick Glide', 'Built-in Straight Stitching',
)


def mostly_non_latin(text):
    """A title from a site's Arabic/Chinese locale. Seen live: haier.com served
    the Arabic page for H43S80GFX."""
    letters = [c for c in str(text) if c.isalpha()]
    if len(letters) < 6:
        return False
    return sum(1 for c in letters if 'a' <= c.lower() <= 'z') / len(letters) < 0.6


def implausible_tv_price(title, sale):
    """A price ceiling driven by the panel spec rather than the screen size.

    Size alone is a bad ceiling - a 55" OLED legitimately costs six figures. But
    panel class is not negotiable: HD Ready (720p) is entry-level hardware, so a
    HD Ready set priced like a 4K one means the wrong listing, a bundle, or an
    MRP read as the price. Caught live: a 32" HD Ready TV at Rs 34,590, roughly
    double its real price, which every other check passed.
    """
    if sale is None or sale <= 0:
        return ''
    t = str(title).lower()
    if re.search(r'\b(8k|4k|uhd|ultra\s*hd|qled|oled|qned|nano\s*cell)\b', t):
        return ''                       # premium panels have no sane ceiling
    if re.search(r'hd\s*ready|\b720p?\b', t):
        return ('hd_ready_priced_like_premium' if sale > 25000 else '')
    if re.search(r'full\s*hd|\b1080p?\b|\bfhd\b', t):
        return ('full_hd_priced_like_premium' if sale > 45000 else '')
    return ''


def issues_report_path(source_path):
    """Where to write validation_issues.csv: NEXT TO the file being validated,
    not in whatever directory the command happened to be run from. Keeps a
    `validate_csv.py runs\\led-2026-07\\out.csv` from littering the repo root."""
    folder = os.path.dirname(os.path.abspath(str(source_path)))
    return os.path.join(folder, 'validation_issues.csv')


def remove_stale_report(source_path):
    """A clean run must not leave the PREVIOUS run's validation_issues.csv on
    disk. gui.validator_flags reads that file whenever it exists, so a stale
    copy keeps showing 'Validator flagged this SKU last run' after the operator
    fixed the rows and rebuilt, and archive_run files it next to a README
    saying 'Validation issues: 0' (P3-52). A locked file (open in Excel) must
    not abort the build - say so and carry on. Returns True if one was removed.
    """
    report = issues_report_path(source_path)
    if not os.path.exists(report):
        return False
    try:
        os.remove(report)
    except OSError as exc:
        print(f'\nCould not remove the stale {report}: {exc}')
        return False
    print(f'\nRemoved the stale {report} - nothing flagged this run.')
    return True


# Reasons that start with this are WARNINGS: they land in validation_issues.csv
# for a human to review, but a row that carries only warnings does not fail the
# gate (the CLI still exits 0). Everything else stays an error.
WARNING_PREFIX = 'warning_'

# app.py writes these exact strings into the `_image_flags` column (see
# app.FLAG_MAIN_NOT_WHITE); each becomes the warning reason on the right.
IMAGE_FLAG_REASONS = {
    'main image background not white':
        'warning_main_image_not_white (no photo has a white background, so '
        'the featured image is a lifestyle shot - review, does not block '
        'the import)',
}


def image_flag_reasons(cell):
    """Warning reasons for one `_image_flags` cell ('; '-separated flags).
    An unknown flag is still reported, so a new app.py flag is never lost."""
    text = str(cell or '').strip()
    if not text or text.lower() == 'nan':
        return []
    out = []
    for flag in (f.strip() for f in text.split(';')):
        if flag:
            out.append(IMAGE_FLAG_REASONS.get(
                flag, f'{WARNING_PREFIX}image_flag ({flag})'))
    return out


def is_blocking_issue(issue):
    """True when an issue row has at least one reason that is not a warning.
    `reasons` is a '; '-joined string (validate_export) or a list
    (validate_filled)."""
    reasons = issue.get('reasons', [])
    if isinstance(reasons, str):
        reasons = reasons.split('; ')
    return any(str(r).strip() and not str(r).startswith(WARNING_PREFIX)
               for r in reasons)


def summary_key(reason):
    """The reason with its per-row detail stripped, so the summary counts
    kinds of problem rather than instances. Everything after '(' is detail,
    and so is the SKU list a cross-row reason names - without this,
    'identical_gallery_files_as AP-X' and '...as AP-Y' count as two problems
    when they are one."""
    head = str(reason).split(' (')[0]
    match = re.match(r'^(\w+(?:_as|_with))\b', head)
    return match.group(1) if match else head


def price_floor_for(categories):
    cats = str(categories).lower()
    for key in sorted(CATEGORY_PRICE_FLOOR, key=len, reverse=True):
        if key in cats:
            return key, CATEGORY_PRICE_FLOOR[key]
    return None, None


def price_ceiling_for(categories):
    cats = str(categories).lower()
    for key in sorted(CATEGORY_PRICE_CEILING, key=len, reverse=True):
        if key in cats:
            return key, CATEGORY_PRICE_CEILING[key]
    return None, None


def image_dimension_problems(filenames, root=None):
    """Sizes and shapes that disqualify a file as a product photo.

    Reads the header only (Pillow is lazy about pixel data), so this stays
    cheap across a few hundred images. Silent when Pillow is absent - the QA
    gate must keep running on a machine that only has pandas."""
    try:
        from PIL import Image
    except ImportError:
        return []
    root = IMAGE_DIR if root is None else root
    problems = []
    for name in filenames:
        path = os.path.join(root, name)
        if not os.path.exists(path):
            continue                     # the existence check already reports it
        try:
            with Image.open(path) as img:
                width, height = img.size
        except Exception:
            problems.append(f'{name} is not a readable image')
            continue
        # 1.91:1 is the OpenGraph share ratio: a social banner, not a product
        # photo. Only reported for the FIRST image, because that is the
        # storefront thumbnail and the only position where it does harm.
        if (name == filenames[0] and height
                and 1.85 <= width / height <= 2.05):
            problems.append(f'{name} is {width}x{height} - a share banner is '
                            f'the thumbnail')
        # Resolution and SHAPE are independent faults, so they are reported
        # independently. As an elif chain the aspect test was unreachable for
        # anything under GOOD_IMAGE_PX: a 900x300 sliver reported only "low
        # resolution" and its 3:1 shape was never mentioned.
        if max(width, height) < MIN_IMAGE_PX:
            problems.append(f'{name} is {width}x{height} (thumbnail)')
        elif max(width, height) < GOOD_IMAGE_PX:
            # not unusable, but it will look soft in the zoom/lightbox and
            # worse again after WooCommerce re-encodes its own sizes from it
            problems.append(f'{name} is only {width}x{height} (low resolution)')
        if min(width, height) and max(width, height) / min(width, height) > MAX_IMAGE_ASPECT:
            problems.append(f'{name} is {width}x{height} (banner shape)')
    return problems


def image_canvas_problems(filenames, root=None):
    """Final image files that are not on the house canvas: every file app.py
    writes is exactly CANVAS_EDGE x CANVAS_EDGE, so a product whose gallery
    mixes sizes (a stale file, a hand-dropped image, an old tiered build) is
    reported. Missing or unreadable files are left to the other checks.
    Silent when Pillow is absent, like image_dimension_problems."""
    try:
        from PIL import Image
    except ImportError:
        return []
    root = IMAGE_DIR if root is None else root
    problems = []
    for name in filenames:
        path = os.path.join(root, name)
        if not os.path.exists(path):
            continue
        try:
            with Image.open(path) as img:
                size = img.size
        except Exception:
            continue
        if size != (CANVAS_EDGE, CANVAS_EDGE):
            problems.append(f'{name} is {size[0]}x{size[1]}')
    return problems


_CONTENT_HITS = []


def _content_hits_fn():
    """sku_enricher.content_hits, imported on first use. The content firewall
    has ONE definition (sku_enricher.py); importing it lazily keeps this
    module import-free at load time, the same way Pillow is lazy above.
    Returns None (and the check is skipped with a note) when the scraper's
    dependencies are not installed."""
    if not _CONTENT_HITS:
        try:
            from sku_enricher import content_hits
        except (ImportError, SystemExit) as exc:
            print(f'Note: content firewall check skipped - cannot import '
                  f'sku_enricher ({exc}).')
            content_hits = None
        _CONTENT_HITS.append(content_hits)
    return _CONTENT_HITS[0]


_TAG_RE = re.compile(r'<[^>]+>')

_FILTER_APP = []


def _filter_app():
    """app.py, imported on first use, for the filter-group lists
    (app.filter_allowed_for). Lazy for the same reason as _content_hits_fn:
    this module stays import-free at load time. None (the check is skipped
    with a note) when app cannot be imported or is configured for another
    client than this module."""
    if not _FILTER_APP:
        try:
            import app as _app
        except Exception as exc:              # streamlit missing, bad config
            print(f'Note: filter-value check skipped - cannot import app '
                  f'({exc}).')
            _app = None
        _FILTER_APP.append(_app)
    found = _FILTER_APP[0]
    if found is not None and getattr(found.CLIENT, 'slug', None) != CLIENT.slug:
        print(f'Note: filter-value check skipped - app.py is configured for '
              f'{found.CLIENT.slug}, this validator for {CLIENT.slug}.')
        return None
    return found


def filter_value_problems(row, attr_name_cols, categories, unmatched=''):
    """WARNINGS (non-blocking) for filter-group attributes whose value is not
    in the allowed lists (the filter sheet's group or the product's
    sub-category row). Owner decision 2026-11-19: app.py WRITES such a value
    as scraped, so this is a reminder to add it to the sheet
    (`warning_filter_value_not_in_sheet`), never a block. `unmatched` is
    kept for callers; every written value is reported."""
    app_mod = _filter_app()
    if app_mod is None or not categories:
        return []
    parent, sub = app_mod.split_hierarchy(categories)
    allowed = {name.lower(): values for name, values
               in app_mod.filter_allowed_for(sub, parent).items()}
    problems = []
    for col in attr_name_cols:
        name = str(row.get(col, '') or '').strip()
        values = allowed.get(name.lower())
        if not name or values is None:
            continue
        if str(row.get(col.replace(' name', ' global'), '1')).strip() in (
                '0', '0.0'):
            continue                      # a spec that only shares the name
        cell = str(row.get(col.replace(' name', ' value(s)'), '') or '')
        bad = [v.strip() for v in cell.split(',')
               if v.strip() and v.strip().lower() != 'nan'
               and v.strip().lower() not in values]
        for value in bad:
            problems.append(f'{WARNING_PREFIX}filter_value_not_in_sheet '
                            f'({name}: {value!r} is a new value, add it to '
                            f'the filter sheet)')
    return problems


def content_firewall_problems(row, columns, brand=''):
    """Retailer-owned text (retailer names, offers, EMI, delivery/returns,
    seller lines, badges, ratings, customer care, URLs, phones, e-mails) in
    any field the pipeline writes: name, descriptions, key features, tags,
    SEO meta and attribute names/values. One reason per offending field,
    naming the field and the first matched term. HTML tags are stripped
    first so markup is never judged."""
    hits_of = _content_hits_fn()
    if hits_of is None:
        return []
    problems = []
    for col in columns:
        raw = row.get(col, '')
        text = '' if raw is None else str(raw)
        if not text.strip() or text.strip().lower() == 'nan':
            continue
        hits = hits_of(_TAG_RE.sub(' ', text), brand=brand)
        if hits:
            problems.append(f'retailer_content_in_field ({col}: "{hits[0]}")')
    return problems


def content_firewall_columns(df):
    """The written-content columns of an export, in sheet order."""
    named = {'title', 'name', 'description', 'short description',
             'configuration', 'tags'}
    return [c for c in df.columns
            if str(c).strip().lower() in named
            or str(c).startswith('Meta:')
            or re.fullmatch(r'Attribute \d+ (?:name|value\(s\))', str(c))]


def duplicate_image_content(records, root=None):
    """Two SKUs whose DOWNLOADED FILES are byte-identical.

    On this file the URL-based cross-row checks are structurally unable to
    fire: app.py rewrites every image to '<media base>/<sku>_<n>.webp', so the
    URLs are derived from the SKU and can never collide however wrong the match
    was. Comparing the bytes on disk is the only way to see it here - and it is
    the check that matters, because two SKUs sharing a gallery means one of
    them resolved to the other's listing.

    Caught live on the LED batch: UA32H4500FUXXL and UA32H4510FUXXL shipped
    the same eight photos.

    Keyed on (size, hash of the first 64 KB) rather than a full digest: same
    verdict, a fraction of the I/O on a batch of several hundred images.
    """
    import hashlib

    root = IMAGE_DIR if root is None else root
    digests = {}

    def key_for(name):
        if name in digests:
            return digests[name]
        path = os.path.join(root, name)
        try:
            size = os.path.getsize(path)
            with open(path, 'rb') as fh:
                head = hashlib.md5(fh.read(65536)).hexdigest()
            digests[name] = (size, head)
        except Exception:
            digests[name] = None
        return digests[name]

    for rec in records:
        rec['_content'] = frozenset(
            k for k in (key_for(os.path.basename(u)) for u in rec['images'])
            if k is not None)

    by_set, by_single = {}, {}
    for rec in records:
        if not rec['_content']:
            continue
        by_set.setdefault(rec['_content'], []).append(rec)
        for k in rec['_content']:
            by_single.setdefault(k, []).append(rec)

    flagged = set()
    for group in by_set.values():
        if len(group) < 2:
            continue
        for rec in group:
            others = sorted(g['sku'] for g in group if g is not rec)[:3]
            rec['reasons'].append('identical_gallery_files_as ' + ', '.join(others))
            flagged.add(id(rec))

    partners = {}
    for group in by_single.values():
        if len(group) < 2:
            continue
        for rec in group:
            if id(rec) in flagged:
                continue
            for other in group:
                if other is not rec:
                    partners.setdefault(id(rec), set()).add(other['sku'])
    for rec in records:
        names = partners.get(id(rec))
        if names:
            rec['reasons'].append('shares_image_files_with '
                                  + ', '.join(sorted(names)[:3]))
    for rec in records:
        rec.pop('_content', None)


def shared_image_pairs(records):
    """SKUs sharing ANY single image, beyond the first-image and whole-gallery
    checks that already run. This is the case where two SKUs resolved to
    different listings of the same wrong product, so their galleries overlap
    without matching.

    Only reported when the blunter checks did not already flag the row, so one
    wrong match produces one line rather than four."""
    by_image = {}
    for rec in records:
        for url in rec['images']:
            by_image.setdefault(url, []).append(rec)
    partners = {}
    for group in by_image.values():
        if len(group) < 2:
            continue
        for rec in group:
            for other in group:
                if other is not rec:
                    partners.setdefault(id(rec), set()).add(other['sku'])
    for rec in records:
        names = partners.get(id(rec))
        if not names:
            continue
        if any(r.startswith(('same_first_image_as', 'identical_gallery_as'))
               for r in rec['reasons']):
            continue
        rec['reasons'].append('shares_an_image_with '
                              + ', '.join(sorted(names)[:3]))


def load_valid_categories():
    """Exact category strings allowed by the CLIENT's category map (if present).

    Only the client's own directory is searched. It used to try the cwd first,
    which meant a stray category_map.xlsx in whatever folder the report was run
    from silently decided which categories counted as valid.
    """
    candidates = ('category_map.xlsx', 'category_map.csv',
                  'reorganise the catgory and sub category.xlsx')
    for base in (str(CLIENT.dir),):
        for name in (CLIENT.category_map.name,) + candidates:
            path = os.path.join(base, name)
            if not os.path.exists(path):
                continue
            try:
                df = (pd.read_csv(path, dtype=str) if path.lower().endswith('.csv')
                      else pd.read_excel(path, dtype=str)).fillna('')
            except Exception:
                return set()
            valid = set()
            for _, row in df.iterrows():
                vals = [str(v) for v in row.tolist()]
                parent = vals[0].strip()
                if not parent or parent.lower() == 'parent category':
                    continue
                valid.add(parent)
                parts = [s.strip() for s in (vals[1] if len(vals) > 1 else '').split(',') if s.strip()]
                i = 0
                while i < len(parts):
                    if (parts[i] == 'Juicers' and i + 1 < len(parts)
                            and parts[i + 1] == 'Mixers & Grinders'):
                        valid.add(f'{parent} > Juicers, Mixers & Grinders')
                        i += 2
                        continue
                    valid.add(f'{parent} > {parts[i]}')
                    i += 1
            return valid
    return set()


def cross_row_duplicates(records):
    """Two SKUs that resolved to the SAME listing - the classic wrong-match
    signature, invisible to any per-row check. Appends reasons in place."""
    for label, key_of in (
        ('same_source_url_as', lambda r: r.get('url', '')),
        ('same_first_image_as', lambda r: r['images'][0] if r['images'] else ''),
        ('identical_gallery_as',
         lambda r: ','.join(sorted(r['images'])) if len(r['images']) > 1 else ''),
        ('same_title_as', lambda r: r['title'].strip().lower()),
    ):
        groups = {}
        for rec in records:
            key = key_of(rec)
            if key:
                groups.setdefault(key, []).append(rec)
        for group in groups.values():
            if len(group) < 2:
                continue
            for rec in group:
                others = [g['sku'] for g in group if g is not rec][:3]
                rec['reasons'].append(f'{label} {", ".join(others)}')
    shared_image_pairs(records)


def validate_filled(df, path):
    """QA for the enricher's own output, BEFORE the human review step.

    Nothing checked this file before, which is where wrong matches are cheapest
    to catch - one spreadsheet cell, versus a wrong product on the storefront.
    """
    print(f'Input: {path}   (enricher output - stage 1 checks)')
    print(f'Rows: {len(df)}')

    def col(*names):
        for n in names:
            for c in df.columns:
                if str(c).strip().lower() == n:
                    return c
        return None

    c_sku = col('sku', 'item details')
    c_title, c_status = col('title'), col('status')
    c_url, c_img = col('url_link'), col('image_url')
    c_reg, c_sale = col('regular price'), col('sale price')

    records, statuses = [], Counter()
    for idx, row in df.iterrows():
        reasons = []
        sku = str(row.get(c_sku, '')).strip()
        title = str(row.get(c_title, '')).strip()
        status = str(row.get(c_status, '')).strip()
        url = str(row.get(c_url, '')).strip()
        images = split_image_list(row.get(c_img, ''))
        statuses[status.split(' - ')[0] or '(blank)'] += 1

        verified = status.upper().startswith('FOUND')
        if verified:
            sale = to_number(row.get(c_sale, ''))
            reg = to_number(row.get(c_reg, ''))
            # [pricing] single_price: the build writes ONE price
            # (client_config.one_price - basis/markup), so an MRP alone is a
            # price, and the TV price check reads the number that will ship.
            # The MRP-ratio checks stay: a wrong MRP can become (or cap) the
            # price in this mode.
            shipped = sale
            if single_price_mode():
                shipped = client_config.one_price(
                    reg, sale, getattr(CLIENT, 'pricing', None))
            if shipped is None:
                reasons.append('verified_but_no_price')
            if len(images) < MIN_IMAGES:
                reasons.append(f'verified_but_images<{MIN_IMAGES} (has {len(images)})')
            if reg is not None and sale is not None and sale > 0:
                limit = mrp_ratio_limit(title)
                if reg <= sale:
                    reasons.append(f'mrp<=price ({reg:g} <= {sale:g})')
                elif reg > sale * limit:
                    reasons.append(f'mrp_implausible ({reg:g} is '
                                   f'{reg / sale:.1f}x, limit {limit:g}x)')
            over = implausible_tv_price(title, shipped)
            if over:
                reasons.append(f'{over} ({shipped:g})')
            if title and mostly_non_latin(title):
                reasons.append('title_not_in_english (wrong locale page)')
            brand = re.split(r'[-\s]', sku)[0].lower()
            if len(brand) > 3 and title and brand not in title.lower():
                reasons.append(f'brand_missing_from_title (sku says {brand})')
            if 'unlisted store' in status:
                reasons.append('source_is_an_unlisted_store')
        records.append({'idx': idx, 'sku': sku, 'title': title, 'url': url,
                        'images': images, 'reasons': reasons})

    cross_row_duplicates(records)

    print('\nStatus breakdown:')
    for status, n in statuses.most_common():
        print(f'  {n:4}  {status}')

    issues = [r for r in records if r['reasons']]
    counts = Counter()
    for rec in issues:
        for reason in rec['reasons']:
            counts[summary_key(reason)] += 1

    print('\nIssue summary:')
    if not counts:
        print('  none - every verified row is complete and unique')
    for reason, n in counts.most_common():
        print(f'  - {reason}: {n}')

    print(f'\nRows with issues: {len(issues)} / {len(df)}')
    for rec in issues[:30]:
        print(f"  row {rec['idx'] + 2}: sku={rec['sku']} | {'; '.join(rec['reasons'])}")
    if len(issues) > 30:
        print(f'  ... and {len(issues) - 30} more (see validation_issues.csv)')

    if issues:
        report = issues_report_path(path)
        pd.DataFrame([{'row': r['idx'] + 2, 'sku': r['sku'],
                       'title': r['title'][:120],
                       'reasons': '; '.join(r['reasons']),
                       'url': r['url']} for r in issues]
                     ).to_csv(report, index=False)
        print(f'\nSaved details to {report}')
    else:
        remove_stale_report(path)
    print('\nFix what matters, then run app.py / build_csv.py on this file.')
    return issues


USAGE = """QA report for a WooCommerce import file or an enricher _filled.xlsx.

Usage:
    python validate_csv.py [path] [--warn-only] [--client SLUG]

    path         the CSV to check, or a *_filled.xlsx (which routes to the
                 stage-1 checks instead). Defaults to
                 wordpress_ready_clean_repaired.csv, then wordpress_ready.csv.
    --warn-only  report without failing: exit 0 even when rows are flagged.
                 Without it the exit code is 1 when anything is flagged, so
                 this can gate a script.
    --client SLUG  which client's config to use (clients/<slug>/client.toml):
                 the placeholder text, the category map and the image folder
                 all come from it. Falls back to $PIPELINE_CLIENT, then
                 'shreejee'.

Writes validation_issues.csv next to the file being checked."""


def single_price_mode(client=None):
    """True when the client's client.toml sets [pricing] single_price."""
    cfg = client or CLIENT
    return bool(getattr(getattr(cfg, 'pricing', None), 'single_price', False))


def configure(slug):
    """Switch this module to another client and return its ClientConfig.

    Same contract as app.configure(): the GUI calls both so one selector
    controls the whole pipeline. Raises ClientConfigError and changes nothing
    on an unknown slug.
    """
    global CLIENT, IMAGE_DIR, PLACEHOLDER, SKU_PREFIX_RE
    incoming = client_config.load_client(slug)
    CLIENT = incoming
    IMAGE_DIR = str(incoming.image_dir)
    PLACEHOLDER = incoming.placeholder_html
    SKU_PREFIX_RE = _sku_prefix_re(incoming)
    return CLIENT


def argv_without_client(argv=None):
    """sys.argv[1:] with '--client X' / '--client=X' removed.

    This script hand-rolls its argv scanning (there is no argparse), so the
    slug and its value have to be taken out before the positional path is
    picked - otherwise `validate_csv.py --client shreejee` would try to open a
    file called 'shreejee'.
    """
    items = list(sys.argv[1:] if argv is None else argv)
    out, skip = [], False
    for i, arg in enumerate(items):
        if skip:
            skip = False
            continue
        if arg == '--client':
            skip = i + 1 < len(items) and not items[i + 1].startswith('-')
            continue
        if arg.startswith('--client='):
            continue
        out.append(arg)
    return out


def pick_input_path():
    # Flags are not paths. sys.argv[1] was taken verbatim, so `--help` was read
    # as a filename ("File not found: --help") and `--warn-only` on its own
    # became the path instead of selecting the default file.
    positional = [a for a in argv_without_client() if not a.startswith('-')]
    if positional:
        return positional[0]
    for candidate in ('wordpress_ready_clean_repaired.csv', 'wordpress_ready.csv'):
        if os.path.exists(candidate):
            return candidate
    return 'wordpress_ready_clean_repaired.csv'


def find_col(df, *candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None


def to_number(value):
    """A price as a float, or None when there isn't one.

    The isfinite guard is load-bearing. float('nan') SUCCEEDS, and app.py runs
    this validator over `result.astype(str)`, which renders a missing price as
    the string 'nan' - so `reg is None and sale is None` was never true and
    missing_price was never reported in the browser, while the same file
    checked through build_csv.py (which round-trips via CSV, giving '')
    reported it correctly. Every downstream comparison against NaN is False
    too, so mrp<=price and the category bands silently passed as well."""
    try:
        text = str(value).replace(',', '').replace('₹', '').strip()
        if not text:
            return None
        number = float(text)
    except Exception:
        return None
    return number if math.isfinite(number) else None


# Every prefix app.py can emit, straight from the client's [sku] block
# (PRODUCT_PLAN item 2). This script stays import-free of app.py on purpose
# (app.py drags in streamlit), so it used to carry a hand-synced copy of the
# prefix list that could drift; now both read client_config. configure()
# rebinds it, and tests\test_bugfixes.py test_19 still pins the two in sync.
def _sku_prefix_re(client):
    return re.compile(r'^(' + '|'.join(re.escape(p) for p in client.sku.prefixes)
                      + r')-')


SKU_PREFIX_RE = _sku_prefix_re(CLIENT)


def validate_export(df, csv_path, check_images=True, write_issues=True):
    """QA for the generated WooCommerce CSV. Returns the list of issue dicts.

    Importable so build_csv.py can run it the moment the file is written -
    "run it automatically after generation rather than as a separate step".
    """
    print(f'Input: {csv_path}')
    print(f'Rows: {len(df)}')

    sku_col = find_col(df, 'sku', 'SKU', 'Sku')
    title_col = find_col(df, 'title', 'Title', 'name')
    images_col = find_col(df, 'Images', 'images', 'image_url')
    desc_col = find_col(df, 'description', 'Description')
    categories_col = find_col(df, 'categories', 'Categories', 'category')
    reg_col = find_col(df, 'regular price', 'regular_price')
    sale_col = find_col(df, 'sale price', 'sale_price')
    gaps_col = find_col(df, '_spec_gaps')
    unmatched_col = find_col(df, '_filter_unmatched')
    image_flags_col = find_col(df, '_image_flags')

    local_images_available = os.path.isdir(IMAGE_DIR)
    if not local_images_available:
        print(f"Note: '{IMAGE_DIR}' folder not found - skipping the "
              f'local image-file existence check.')

    valid_categories = load_valid_categories()
    if valid_categories:
        print(f'Category map loaded: {len(valid_categories)} allowed category strings.')
    else:
        print(f'Note: {CLIENT.category_map} not found - skipping the category check.')

    attr_name_cols = [c for c in df.columns
                      if re.fullmatch(r'Attribute \d+ name', str(c))]
    content_cols = content_firewall_columns(df)
    brand_col = find_col(df, 'Brands', 'brands', 'Brand')

    issues = []
    seen_skus = {}
    records = []                    # kept for the cross-row pass after the loop

    for idx, row in df.iterrows():
        reasons = []
        sku = str(row.get(sku_col, '')).strip() if sku_col else ''
        title = str(row.get(title_col, '')).strip() if title_col else ''

        if not sku:
            reasons.append('missing_sku')
        if not title:
            reasons.append('missing_title')
        elif ' ' not in title and re.search(r'\d', title):
            # 'Acer-AR32GT2841HDFL' shipped as a product NAME (seen live) -
            # a bare model code with no brand words, size or series
            reasons.append('title_looks_like_raw_sku (a model code is not a '
                           'product name)')

        if sku:
            if sku in seen_skus:
                reasons.append(f'duplicate_sku (first at row {seen_skus[sku] + 2})')
            else:
                seen_skus[sku] = idx
            if re.search(r'[\\/:*?"<>|]', sku):
                reasons.append('sku_has_unsafe_characters (breaks image files/URLs)')

        image_urls = []
        if images_col:
            raw_images = str(row.get(images_col, '')).strip()
            image_urls = split_image_list(raw_images)
        if len(image_urls) == 0:
            reasons.append('images=0')
        elif len(image_urls) < MIN_IMAGES:
            reasons.append(f'images<{MIN_IMAGES} (has {len(image_urls)})')

        if local_images_available and image_urls:
            missing_local = [os.path.basename(u) for u in image_urls
                             if not os.path.exists(os.path.join(IMAGE_DIR, os.path.basename(u)))]
            if missing_local:
                reasons.append(f'{len(missing_local)} image file(s) not downloaded locally')
            # a 60px file and a 1:4 sliver both pass every URL-level check and
            # both look broken on the storefront
            if check_images:
                bad_size = image_dimension_problems(
                    [os.path.basename(u) for u in image_urls])
                if bad_size:
                    reasons.append(f'image_not_a_product_photo ({bad_size[0]}'
                                   + (f' +{len(bad_size) - 1} more' if len(bad_size) > 1 else '')
                                   + ')')
                off_canvas = image_canvas_problems(
                    [os.path.basename(u) for u in image_urls])
                if off_canvas:
                    reasons.append(
                        f'{WARNING_PREFIX}image_not_house_canvas ('
                        f'expected {CANVAS_EDGE}x{CANVAS_EDGE}: {off_canvas[0]}'
                        + (f' +{len(off_canvas) - 1} more'
                           if len(off_canvas) > 1 else '') + ')')

        # flags app.py recorded while it laid out the images (a WARNING each)
        if image_flags_col:
            reasons.extend(image_flag_reasons(row.get(image_flags_col, '')))

        desc =str(row.get(desc_col, '')).strip() if desc_col else ''
        if not desc or desc == PLACEHOLDER:
            reasons.append('missing_description')

        cats = str(row.get(categories_col, '')).strip() if categories_col else ''
        # app.escape_category_for_csv writes '\,' for a comma inside a category
        # NAME ('Juicers\, Mixers & Grinders'), because the WooCommerce importer
        # otherwise splits the cell into two categories. Compare unescaped so a
        # correctly-escaped row is not reported as category_not_in_map.
        cats_plain = cats.replace('\\,', ',')
        if not cats:
            reasons.append('missing_category')
        elif valid_categories and cats_plain not in valid_categories:
            reasons.append(f'category_not_in_map ({cats})')
        elif ',' in cats_plain and cats_plain == cats:
            reasons.append(f'unescaped_comma_in_category ({cats})')

        reasons.extend(attribute_name_problems(row, attr_name_cols))
        # the content firewall: nothing of the source retailer's may ship
        # (BLOCKING - app.py should have removed it; a hit means a stale CSV
        # or a hole in sku_enricher's firewall lists)
        brand = str(row.get(brand_col, '') or '').strip() if brand_col else ''
        reasons.extend(content_firewall_problems(
            row, content_cols, brand='' if brand.lower() == 'nan' else brand))

        reg = to_number(row.get(reg_col, '')) if reg_col else None
        sale = to_number(row.get(sale_col, '')) if sale_col else None
        if single_price_mode():
            # [pricing] single_price: the ONE price is in 'regular price' and
            # 'sale price' must be empty. A sale price here is a CSV built
            # before the switch (or edited by hand): it would show a
            # struck-through MRP and a saving on the store. BLOCKING.
            if sale is not None:
                reasons.append(f'sale_price_in_single_price_mode ({sale:g})')
            # the floor / ceiling / TV checks below read 'sale' as "the price
            # the shopper pays"; in this mode that is the one price, and the
            # MRP-ratio checks have no pair to compare
            sale, reg = (sale if sale is not None else reg), None
            if sale is None:
                reasons.append('missing_price')
        elif reg is None and sale is None:
            reasons.append('missing_price')
        elif reg is not None and sale is not None and reg <= sale:
            reasons.append(f'regular_price<=sale_price ({reg:g} <= {sale:g})')
        # an MRP several times the price is a number scraped off the wrong
        # element, not a discount
        if reg is not None and sale is not None and sale > 0:
            limit = mrp_ratio_limit(cats, title)
            if reg > sale * limit:
                reasons.append(f'mrp_implausible ({reg:g} is {reg / sale:.1f}x '
                               f'{sale:g}, limit {limit:g}x)')
        # a price far below the category's floor means an accessory was matched
        key, floor = price_floor_for(cats)
        if sale is not None and floor and sale < floor:
            reasons.append(f'price_too_low_for_category ({sale:g} < {floor} '
                           f'for {key} - accessory?)')
        # and the other end: a bundle or a mis-read MRP sitting in the price
        key_hi, ceiling = price_ceiling_for(cats)
        if sale is not None and ceiling and sale > ceiling:
            reasons.append(f'price_too_high_for_category ({sale:g} > {ceiling} '
                           f'for {key_hi} - bundle or wrong number?)')
        over = implausible_tv_price(title, sale)
        if over:
            reasons.append(f'{over} ({sale:g})')

        # the taxonomy declares which axes this sub-category needs; app.py
        # writes the ones it could not fill into _spec_gaps
        gaps = str(row.get(gaps_col, '')).strip() if gaps_col else ''
        if gaps:
            reasons.append(f'missing_required_attribute ({gaps})')
        # a filter value the page/title states but the filter sheet does not
        # list. Since the owner decision of 2026-11-19 app.py WRITES it (note
        # "... is a new value, add it to the sheet"); filter_value_problems
        # reports those as warning_filter_value_not_in_sheet. The other notes
        # (a value that could not be written) stay filter_value_unmatched.
        # Both are WARNINGS - the fix is adding the value to the master.
        unmatched = str(row.get(unmatched_col, '')).strip() if unmatched_col else ''
        if unmatched.lower() == 'nan':
            unmatched = ''
        other_notes = [n.strip() for n in unmatched.split(';')
                       if n.strip() and not n.strip().endswith(
                           'is a new value, add it to the sheet')]
        if other_notes:
            reasons.append(f'{WARNING_PREFIX}filter_value_unmatched '
                           f'({" | ".join(other_notes)})')   # '; ' splits reasons
        reasons.extend(filter_value_problems(row, attr_name_cols, cats_plain,
                                             unmatched))

        if title and mostly_non_latin(title):
            reasons.append('title_not_in_english (wrong locale page)')
        if title and len(title) < 15:
            reasons.append(f'title_suspiciously_short ({len(title)} chars)')
        # the SKU normally leads with the brand; if that word is nowhere in the
        # title, the row probably belongs to a different product
        sku_brand = re.split(r'[-\s]',
                             SKU_PREFIX_RE.sub('', sku))[0].lower()
        if len(sku_brand) > 3 and title and sku_brand not in title.lower():
            reasons.append(f'brand_missing_from_title (sku says {sku_brand})')

        blob = ' '.join([str(row.get(c, '')) for c in df.columns])
        found_dead = [p for p in DEAD_PLACEHOLDERS if p in blob]
        if found_dead:
            reasons.append(f'placeholder_spec_text ({found_dead[0]}) - rebuild '
                           f'this CSV, it predates the KI-14 fix')

        # check cats+desc directly rather than removing the title's text from a
        # joined blob: str.replace could splice 'top'+'load' together out of
        # unrelated fragments (or miss, if the description re-words the title)
        rest = ' '.join([cats.lower(), desc.lower()])
        if 'front load' in title.lower() and 'top load' in rest:
            reasons.append('front_load_titled_but_top_load_labelled')

        records.append({'idx': idx, 'sku': sku, 'title': title,
                        'images': image_urls, 'reasons': reasons})

    # ---- cross-row checks: two SKUs that resolved to the SAME listing -------
    # This is the classic wrong-match signature and no per-row check can see it.
    for label, key_of in (
        ('same_first_image_as', lambda r: r['images'][0] if r['images'] else ''),
        ('identical_gallery_as', lambda r: ','.join(sorted(r['images'])) if len(r['images']) > 1 else ''),
        ('same_title_as', lambda r: r['title'].strip().lower()),
    ):
        groups = {}
        for rec in records:
            key = key_of(rec)
            if key:
                groups.setdefault(key, []).append(rec)
        for group in groups.values():
            if len(group) < 2:
                continue
            for rec in group:
                others = [g['sku'] for g in group if g is not rec][:3]
                rec['reasons'].append(f'{label} {", ".join(others)}')
    # partial gallery overlap: different listings of the same wrong product
    shared_image_pairs(records)
    # ...and the same check against the FILES, which is the only one that can
    # see a duplicate gallery once app.py has renamed every image per SKU
    if local_images_available and check_images:
        duplicate_image_content(records)

    for rec in records:
        if rec['reasons']:
            issues.append({'row': rec['idx'] + 2, 'sku': rec['sku'],
                           'title': rec['title'][:120],
                           'reasons': '; '.join(rec['reasons']),
                           'first_image': ', '.join(rec['images'][:1])})

    counts = Counter()
    for item in issues:
        for reason in item['reasons'].split('; '):
            counts[summary_key(reason)] += 1

    print('\nIssue summary:')
    if not counts:
        print('  none - file looks import-ready')
    for reason, count in counts.most_common():
        print(f'  - {reason}: {count}')

    print(f'\nRows with issues: {len(issues)} / {len(df)}')
    for item in issues[:30]:
        print(f"  row {item['row']}: sku={item['sku']} | {item['reasons']}")
    if len(issues) > 30:
        print(f'  ... and {len(issues) - 30} more (see validation_issues.csv)')

    if issues and write_issues:
        report = issues_report_path(csv_path)
        pd.DataFrame(issues).to_csv(report, index=False)
        print(f'\nSaved details to {report}')
    elif write_issues:
        remove_stale_report(csv_path)

    return issues



def use_utf8_stdout():
    """A Windows console is cp1252, and scraped titles/SKUs are not. Without
    this a single unmappable character aborts the run with a UnicodeEncodeError
    traceback - mid-batch, after work has already been done. sku_enricher.py
    has always done this; the rest of the entry points had not."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

def main():
    use_utf8_stdout()
    if {'-h', '--help'} & set(sys.argv[1:]):
        print(USAGE)
        sys.exit(0)
    unknown = [a for a in argv_without_client()
               if a.startswith('-') and a not in ('--warn-only',)]
    if unknown:
        print(f"Unknown option: {unknown[0]}\n")
        print(USAGE)
        sys.exit(2)
    print(f'Client: {CLIENT.name} ({CLIENT.slug})')
    csv_path = pick_input_path()
    try:
        if csv_path.lower().endswith(('.xlsx', '.xls')):
            df = pd.read_excel(csv_path, dtype=str).fillna('')
        else:
            df = pd.read_csv(csv_path, dtype=str).fillna('')
    except FileNotFoundError:
        print(f'File not found: {csv_path}')
        print('Usage: python validate_csv.py [csv_path|filled.xlsx]')
        sys.exit(1)

    # an enricher sheet and a WooCommerce CSV need different checks; the
    # 'status' + 'url_link' pair only exists on the former
    lowered = {str(c).strip().lower() for c in df.columns}
    if {'status', 'url_link'} <= lowered:
        issues = validate_filled(df, csv_path)
    else:
        issues = validate_export(df, csv_path)

    # A-4: exit non-zero when anything was flagged, so this can gate a script.
    # It used to always exit 0, which made "clean" and "16 rows are wrong"
    # indistinguishable to anything calling it. --warn-only keeps the old
    # behaviour for a chain that wants the report without the gate.
    # Rows that carry only warning_* reasons are reported but never fail it.
    blocking = [i for i in issues if is_blocking_issue(i)]
    if blocking and '--warn-only' not in sys.argv:
        print(f'\nExiting 1: {len(blocking)} row(s) flagged. '
              f'Pass --warn-only to report without failing.')
        sys.exit(1)
    if len(issues) > len(blocking):
        print(f'\n{len(issues) - len(blocking)} row(s) carry warnings only - '
              f'review them; they do not fail this check.')
    sys.exit(0)


if __name__ == '__main__':
    main()
