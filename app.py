import streamlit as st
import pandas as pd
import re
import io
import os
import shutil
import sys
from collections import namedtuple
from datetime import datetime
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageChops

import client_config
# The content firewall (owner rule 2026-11-19: nothing that belongs to the
# source retailer reaches a field we write) and the key-point cap live in
# sku_enricher.py - ONE source of truth for the scraper and this builder.
# Applied here too so already-enriched *_filled.xlsx sheets are cleaned at
# build time without a re-scrape.
from sku_enricher import (KEY_POINTS_MAX, content_clean, content_ok,
                          spec_row_ok)

# Which store this process is building for (PRODUCT_PLAN Phase 1 item 1).
# Resolved from --client / $PIPELINE_CLIENT / DEFAULT_CLIENT, so a caller that
# needs a different client must set PIPELINE_CLIENT BEFORE importing app, or
# call app.configure(slug) afterwards.
try:
    CLIENT = client_config.load_client()
except client_config.ClientConfigError as _exc:
    raise RuntimeError(
        f"Cannot start: {_exc} "
        f"(per-client config lives in clients/<slug>/client.toml - see "
        f"docs/CLIENT_CONFIG.md)") from _exc

# A comma-joined URL list cannot be split on commas alone: Amazon's rendition
# modifiers legitimately contain them (`._AC_UL165_SR165,165_.jpg`), and a plain
# split shredded those into fragments like '165_.jpg'. Seen live losing 7 of 12
# images on one product. Split only where a comma is followed by a URL start.
_URL_SPLIT_RE = re.compile(r',(?=\s*(?:https?://|//))')


def split_image_list(raw):
    """Split a stored image list without breaking URLs that contain commas."""
    text = str(raw or '').strip()
    if not text:
        return []
    parts = _URL_SPLIT_RE.split(text)
    if len(parts) == 1 and ',' in text and '://' not in text:
        parts = text.split(',')          # not a URL list - old behaviour
    return [p.strip() for p in parts if p.strip()]



def safe_print(message):
    """Print without letting the console's encoding abort a batch.

    A Windows console is cp1252 by default, and a scraped SKU or category can
    carry a character it has no mapping for. One Greek letter in a SKU used to
    kill build_csv.py mid-run with a UnicodeEncodeError traceback, after images
    had already been downloaded. The entry points reconfigure stdout to UTF-8,
    but this module is also imported by scripts that may not have, so the
    fallback belongs here too."""
    text = str(message)
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, 'encoding', None) or 'ascii'
        print(text.encode(encoding, 'replace').decode(encoding, 'replace'))


class _ConsoleUI:
    """Stand-in for `st` when process_inventory() is driven from a CLI instead
    of the browser (build_csv.py). Same call shape, prints instead of drawing."""

    def progress(self, _value=0):
        return self

    def empty(self):
        return self

    def text(self, message):
        safe_print(message)

    def warning(self, message):
        safe_print('[warn] ' + str(message))

    def info(self, message):
        safe_print('[info] ' + str(message))


def _ui():
    """`st` when a Streamlit script is running, a console shim otherwise.
    Checked at call time, so importing this module from a script is safe."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        # suppress_warning: outside Streamlit the absence of a context is the
        # expected case, not something to warn a CLI user about
        if get_script_run_ctx(suppress_warning=True) is not None:
            return st
    except Exception:
        pass
    return _ConsoleUI()


REQUIRED_INPUT_COLUMNS = ['sku', 'title', 'categories', 'Brands', 'regular price', 'sale price', 'url_link', 'image_url']
EXPORTED_OUTPUT_COLUMNS = ['sku', 'cross reference tag', 'configuration', 'short description', 'description', 'title', 'categories', 'Tags', 'regular price', 'sale price', 'Brands', '_consolidated_sku', 'Images', '_spec_gaps', '_filter_unmatched', '_image_flags']
IMAGE_ROOT = 'product_images_for_upload'   # folder NAME (kept for compatibility)
# Where images are actually written: <repo>/product_images_for_upload/<slug>.
# Absolute and per-client, so two clients cannot overwrite each other's
# SKU_n.webp and a run no longer depends on the process's cwd (P3-57).
IMAGE_DIR = CLIENT.image_dir
MAX_ATTRIBUTES = 15   # each one costs 4 CSV columns, so the sheet stays usable
# The key-features paragraph, the short description and the long description
# each show at most sku_enricher.KEY_POINTS_MAX relevant spec points, chosen
# by key_points() (owner rule 2026-11-19). The attributes are NOT capped by it.

# ---- image quality ---------------------------------------------------------
# 90 was visibly soft on a 1500px product shot once WooCommerce re-encoded its
# own thumbnails from it. 95 costs roughly 40% more bytes and stops the loss
# compounding through that second encode.
WEBP_QUALITY = 95
# Encoder EFFORT, not quality - it only changes how hard the encoder searches
# for a smaller file at the WEBP_QUALITY above. Measured over 60 real catalogue
# images at q=95: method=6 took 586 ms each and beat method=4 by 0.27% of
# bytes. That is ~20 minutes of extra CPU across a 6,400-image catalogue to
# save a quarter of a percent, so this sits at 4 (also PIL's own default).
# Dropping to 2 is 4.9x faster again but costs +2.7% bytes - a real tradeoff,
# not a free one.
WEBP_METHOD = 4
# Long edge to keep. WooCommerce generates its own thumbnails, so the master
# only has to be big enough for the zoom/lightbox; past this it is bandwidth.
# Only governs the non-square path: squared catalogue masters are finished on
# the fixed CANVAS_EDGE below, which supersedes this cap.
MAX_IMAGE_EDGE = 2400
# Below this a photo is a thumbnail, not a product image. Skipped outright -
# one image fewer beats a blurry one on the storefront. Raised from 500 to an
# HD floor: 500px filled a product tile but went soft the moment a customer
# opened the zoom/lightbox. Measured against the LED batch this rejects the
# bottom ~11% of images; drop it back toward 500 if a category is coming out
# with too few photos (the count is reported per run).
MIN_IMAGE_EDGE = 1000

# Catalogue tiles only line up if every master shares one aspect ratio, so each
# photo is padded - never cropped - onto a square canvas. Padding keeps wide
# products (TVs, front-load washers) whole; a centre-crop cut their edges off.
SQUARE_IMAGES = True
SQUARE_BACKGROUND = (255, 255, 255)

# ---- house layout (applies only while SQUARE_IMAGES is True) ---------------
# Owner rule (2026-11-19): ONE fixed layout for every photo of every product -
# a CANVAS_EDGE x CANVAS_EDGE WebP canvas, pure white, the product centred with
# MARGIN_PCT of white on its LIMITING side (the axis that fills first); the
# other axis takes what is left, so a 16:9 TV gets wide white bands top and
# bottom. Never cropped. (This replaced the per-product 1500/1200/1000 tiers,
# which gave one product's gallery three different canvas sizes.)
#
# Owner decision (2026-11-19, fifth round): the canvas is 1500 x 1500 (it was
# 1200). The layout rule did not change. A source needs inner/long-edge to
# fill the canvas, where inner = 1500 x (1 - 2 x MARGIN_PCT%) = 1380 for a
# white-background photo and 1500 for a lifestyle shot. MAX_UPSCALE caps the
# growth at 15%, so a source with a long edge under 1200 (white) / 1305
# (lifestyle) does NOT fill the canvas: it grows by 1.15x and sits centred,
# smaller, with more white around it. A 1000px source (the MIN_IMAGE_EDGE
# floor) becomes a 1150px product on the 1500 canvas. It is never dropped and
# never upscaled past 15%.
CANVAS_EDGE = 1500
MAX_UPSCALE = 1.15
# White space on each side of a white-background product, as a percentage of
# the canvas edge, measured on the limiting axis.
MARGIN_PCT = 4
# A background is "white" when at least WHITE_SHARE of the pixels in the outer
# BORDER_PCT strip on all four sides have EVERY channel >= WHITE_MIN.
# Transparent pixels count as white (they are composited onto white first).
BORDER_PCT = 2
WHITE_MIN = 245
WHITE_SHARE = 0.95
# Product pixels for the trim: any channel more than BBOX_TOL below 255. JPEG
# noise in a white backdrop stays under it.
BBOX_TOL = 12
# A trim box narrower or shorter than this percentage of the source is a speck
# of dust or noise, not a product: the layout then uses the untrimmed image.
BBOX_MIN_PCT = 2
# The reason written to _image_flags when no gallery image has a white
# background, so the featured image is a lifestyle shot. validate_csv.py turns
# it into a WARNING - it never blocks the import.
FLAG_MAIN_NOT_WHITE = 'main image background not white'
BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36')

# Amazon puts the rendition in the filename (._SL1500_. = longest side 1500);
# dropping the modifier returns the original upload. Measured: 1500x927/113 KB
# -> 2392x1479/220 KB. Magento stores do the same once resize params go.
_AMAZON_HOSTS = ('media-amazon.com', 'images-amazon.com', 'ssl-images-amazon.com')
_AMAZON_RENDITION_RE = re.compile(r'\._[A-Z0-9_,%-]+_\.(?=[A-Za-z]{3,4}$)')

# Haier serves TWO files per photo. '..._1200_p1911.jpg' is a 1200x628 SHARE
# BANNER - the product sits in the middle of a letterbox and fills under half
# the frame. The bare name is the same shot at 1200x1200, properly framed.
# Measured on W020250724478599787988: 1200x628 -> 1200x1200. This is also what
# validate_csv flags as image_not_a_product_photo on Haier rows.
_HAIER_HOSTS = ('image.haier.com',)
_HAIER_RENDITION_RE = re.compile(r'_\d{3,4}(?:_p\d+)?(?=\.[A-Za-z]{3,4}$)')

# Shopify renders any width on demand; the stored original is usually far
# larger than the page asked for. Measured on foxskyindia: ?width=1946 gave
# 1946x1946, dropping it gave 3600x3600. Where the theme already asked for the
# original this is a no-op, so it is safe to apply blindly.
_SHOPIFY_WIDTH_PARAMS = {'width', 'height'}
_SHOPIFY_SUFFIX_RE = re.compile(r'_(?:\d{2,4}x\d{0,4}|\d{2,4}x)(?=\.[A-Za-z]{3,4}$)')

# Thumbor/imgproxy style filter segments. 'filters:quality(70)' is the CDN
# being told to throw detail away; measured +161% bytes at identical pixel
# dimensions once removed.
_THUMBOR_FILTER_RE = re.compile(r'/filters:[a-z_]+\([^)]*\)', re.IGNORECASE)


def largest_rendition(url):
    """Rewrite a CDN URL to the biggest version it will serve.

    Applied at DOWNLOAD time as well as in the enricher, so batches whose
    image_url column was captured before this existed still get full
    resolution without re-scraping a single page."""
    text = compact(url)
    if not text:
        return url
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(text)
    except Exception:
        return text
    host = parts.netloc.lower()
    if any(h in host for h in _AMAZON_HOSTS):
        return urlunsplit((parts.scheme, parts.netloc,
                           _AMAZON_RENDITION_RE.sub('.', parts.path),
                           parts.query, parts.fragment))
    if any(h in host for h in _HAIER_HOSTS):
        return urlunsplit((parts.scheme, parts.netloc,
                           _HAIER_RENDITION_RE.sub('', parts.path),
                           parts.query, parts.fragment))
    if _THUMBOR_FILTER_RE.search(parts.path):
        return urlunsplit((parts.scheme, parts.netloc,
                           _THUMBOR_FILTER_RE.sub('', parts.path),
                           parts.query, parts.fragment))
    if '/cdn/shop/' in parts.path.lower():        # Shopify storefront
        keep = [kv for kv in parts.query.split('&')
                if kv and kv.split('=')[0].lower() not in _SHOPIFY_WIDTH_PARAMS]
        return urlunsplit((parts.scheme, parts.netloc,
                           _SHOPIFY_SUFFIX_RE.sub('', parts.path),
                           '&'.join(keep), parts.fragment))
    if '/media/catalog/product/' in parts.path.lower() and parts.query:
        drop = {'width', 'height', 'w', 'h', 'quality', 'fit', 'resize', 'size'}
        keep = [kv for kv in parts.query.split('&')
                if kv and kv.split('=')[0].lower() not in drop]
        return urlunsplit((parts.scheme, parts.netloc, parts.path,
                           '&'.join(keep), parts.fragment))
    return text


def build_media_base(month=None):
    """The WordPress uploads URL this client's images will live at.

    Same signature as before (callers pass nothing); the store URL and the
    /wp-content/uploads/{yyyy}/{mm} shape now come from the client's
    client.toml instead of being frozen into this file.
    """
    return CLIENT.media_base(month)


MEDIA_BASE = build_media_base()
IRON_WORD_RE = re.compile(r'\biron\b', re.IGNORECASE)

# Materials a listing has to actually STATE before we put them on the product
# page. KI-14: the spec readers below used to assert a soleplate finish and a
# kettle body material for every product in those categories, read off nothing.
SOLEPLATE_RE = re.compile(
    r'\b(non-?stick|ceramic|stainless steel|steel|teflon|granite|titanium)\b'
    r'(?=[^.]{0,30}\b(?:soleplate|sole plate|plate|coating|coated)\b)|'
    r'\b(?:soleplate|sole plate)\s*[:\-]?\s*'
    r'(non-?stick|ceramic|stainless steel|steel|teflon|granite|titanium)\b',
    re.IGNORECASE)
KETTLE_BODY_RE = re.compile(
    r'\b(stainless steel|borosilicate glass|glass|plastic|copper)\b',
    re.IGNORECASE)


def compact(value):
    if value is None or pd.isna(value):
        return ''
    return re.sub(r'\s+', ' ', str(value)).strip()


def number_or_blank(value):
    text = compact(value)
    if not text:
        return ''
    cleaned = text.replace(',', '').replace('₹', '').replace('$', '')
    match = re.search(r'-?\d+(?:\.\d+)?', cleaned)
    if not match:
        return ''
    try:
        return float(match.group(0))
    except Exception:
        return ''


def brand_case(name):
    """Short acronym brands stay uppercase (IFB, LG, TCL), the rest title-case."""
    return name.title() if len(name) > 3 else name.upper()


def infer_brand(title, sku=''):
    text = ' ' + compact(title).upper() + ' '
    brands = ['SAMSUNG', 'GODREJ', 'HAIER', 'WHIRLPOOL', 'IFB', 'VOLTAS',
              'BOSCH', 'PANASONIC', 'ONIDA', 'TOSHIBA', 'SANSUI', 'CROMPTON',
              'SYMPHONY', 'BAJAJ', 'PHILIPS', 'PRESTIGE', 'PIGEON', 'USHA',
              'KENSTAR', 'SUNFLAME', 'CELFROST', 'LG']
    for brand in brands:
        if re.search(r'\b' + re.escape(brand) + r'\b', text):
            return brand_case(brand)

    # Fall back to the SKU's leading token. This catalogue's own convention is
    # BRAND-MODEL (SAMSUNG-UA32H4560FUXXL, TCL-32S4K), so the brand is right
    # there - and unlike the list above it needs no maintenance as brands are
    # added. Caught live: TCL, iFFALCON and Foxsky were all exporting as
    # "Authorized Brand" because they were simply missing from the list.
    token = re.split(r'[-\s_]+', compact(sku))[0].strip()
    # a leading never-rename code or the fallback prefix is a SKU prefix, not a
    # brand (PRODUCT_PLAN item 2: both come from the client's [sku] block)
    not_a_brand = set(CLIENT.sku.never_rename) | {CLIENT.sku.fallback_prefix}
    if re.fullmatch(r'[A-Za-z]{2,}', token) and token.upper() not in not_a_brand:
        return brand_case(token)
    return 'Authorized Brand'


# NOTE: a second categoriser, `infer_category`, used to live here. It had zero
# call sites and returned a FLAT taxonomy ('Washing Machines', 'Home
# Appliances') incompatible with the detect_category/canonicalize_category pair
# below, which is the one that is actually used and which snaps to the exact
# strings in category_map.xlsx. Two disagreeing categorisers in one file is a
# trap, so the dead one is gone; use detect_category().


# ------------- category taxonomy (loaded from category_map.xlsx) -------------
# The output 'categories' strings are snapped to the EXACT text in the map
# file (case- and space-sensitive), e.g. 'Washing Machines > Top load'.

CATEGORY_MAP_CANDIDATES = ('category_map.xlsx', 'category_map.csv',
                           'reorganise the catgory and sub category.xlsx')


def category_map_path(client=None):
    """The taxonomy workbook for `client` (default: the loaded CLIENT), or ''.

    The file named in client.toml wins; the historical alternate spellings are
    still accepted, but only INSIDE the client's own directory - a second
    client must never fall back to the first one's taxonomy.
    """
    client = client or CLIENT
    if client.category_map.exists():
        return str(client.category_map)
    for name in CATEGORY_MAP_CANDIDATES:
        candidate = client.dir / name
        if candidate.exists():
            return str(candidate)
    return ''

# taxonomy entries whose own names contain commas (the list separator)
CATEGORY_COMMA_MERGES = (
    (('Juicers', 'Mixers & Grinders'), 'Juicers, Mixers & Grinders'),
)


def escape_category_for_csv(text):
    """WooCommerce's CSV importer treats the Categories cell as a comma
    separated LIST (parse_categories_field -> explode_values), so a category
    whose own name contains a comma must escape it as '\\,'. Without this
    'Kitchen Appliances > Juicers, Mixers & Grinders' imports as a bogus
    'Kitchen Appliances > Juicers' plus a new top-level 'Mixers & Grinders'.

    Only the CSV cell is escaped - the taxonomy file, the GUI dropdown, tags
    and the SEO fields all keep the human-readable name.
    """
    return re.sub(r'(?<!\\),', r'\\,', str(text or ''))


def unescape_category_from_csv(text):
    """Inverse of escape_category_for_csv, for anything reading the CSV back
    (validate_csv.py) and needing to compare against the taxonomy strings."""
    return str(text or '').replace('\\,', ',')


def _cat_norm(value):
    return re.sub(r'\s+', ' ', str(value or '').strip()).lower()


def _split_subcategories(raw):
    parts = [p.strip() for p in str(raw or '').split(',') if p.strip()]
    merged, i = [], 0
    while i < len(parts):
        for (first, second), combined in CATEGORY_COMMA_MERGES:
            if parts[i] == first and i + 1 < len(parts) and parts[i + 1] == second:
                merged.append(combined)
                i += 2
                break
        else:
            merged.append(parts[i])
            i += 1
    return merged


def load_category_map(client=None):
    """Read the client's taxonomy file. Returns lookup dicts whose VALUES keep
    the file's exact spelling/case/spacing."""
    path = category_map_path(client)
    combos, parents = {}, {}
    if not path:
        return combos, parents, ''
    try:
        if path.lower().endswith('.csv'):
            rows = pd.read_csv(path, dtype=str).fillna('').values.tolist()
        else:
            rows = pd.read_excel(path, dtype=str).fillna('').values.tolist()
    except Exception:
        return combos, parents, ''
    for row in rows:
        parent = str(row[0] if len(row) > 0 else '').strip()
        if not parent or _cat_norm(parent) == 'parent category':
            continue
        parents[_cat_norm(parent)] = parent
        for sub in _split_subcategories(row[1] if len(row) > 1 else ''):
            combos[_cat_norm(f'{parent} > {sub}')] = f'{parent} > {sub}'
            combos.setdefault(_cat_norm(sub), f'{parent} > {sub}')
    return combos, parents, os.path.basename(path)


CATEGORY_COMBOS, CATEGORY_PARENTS, CATEGORY_MAP_FILE = load_category_map()


# The operator's own category code, which is the most reliable signal there is
# about what a product IS - it was typed by someone holding the box. Used only
# as a FALLBACK, because a real listing title ('Front Load', '1.5 Ton') is more
# specific than a prefix that only says "washing machine".
# PRODUCT_PLAN item 2: the table itself lives in clients/<slug>/client.toml
# ([sku.input_codes]); this is a per-client view of it, rebound by configure().
SKU_PREFIX_CATEGORY = dict(CLIENT.sku.input_codes)


def sku_prefix_category(raw_sku):
    """(parent, sub) implied by a leading category code, or ('', '')."""
    token = re.split(r'[-\s_]+', compact(raw_sku).upper())[0].strip()
    return SKU_PREFIX_CATEGORY.get(token, ('', ''))


# P1-14: phrases that mean "this product needs no stabilizer" - an AC feature -
# and therefore CANCEL the stabilizer keyword rather than confirming it.
NOT_A_STABILIZER_RE = (
    r'stabili[sz]er[\s-]*free'
    r'|no[\s-]+stabili[sz]er'
    r'|(?:with|works?)?\s*without\s+(?:a\s+|any\s+)?stabili[sz]er'
    r'|stabili[sz]er\s+(?:is\s+)?not\s+(?:required|needed)'
)


def detect_category(raw_item_name, raw_categories='', detected_brand='',
                    raw_sku=''):
    """Guess (parent, subcategory); canonicalize_category() then snaps the
    guess to the exact strings of the taxonomy file.

    `raw_sku` is consulted only when the title yields nothing. It used not to
    be passed at all, which made every SKU-code rule in here ('wm-' in t,
    'wh-' in t) dead in the real pipeline: measured on a live crawl,
    'KENSTAR-WH MAXIMUS 3L' - a water heater whose SKU literally starts WH -
    fell through to the bare 'Home Appliances' catch-all."""
    t = ' '.join([
        compact(raw_item_name).lower(),
        compact(raw_categories).lower().replace(',', ' '),
        compact(detected_brand).lower(),
    ])

    def has(*phrases):
        return any(p in t for p in phrases)

    def word(w):
        return re.search(r'\b' + w + r'\b', t) is not None

    # P1-15: a reviewer's explicit pick outranks every keyword rule below.
    # GUI Step 2 writes the exact 'Parent > Sub' string out of
    # category_map.xlsx into `categories`, and without this a misfiled product
    # could not be corrected short of rewriting its title - the rules would
    # just overrule the human every rebuild. ONLY the full 'parent > sub' form
    # counts: a bare parent from an ERP export, or a retailer breadcrumb like
    # 'Home > Kitchen > Mixers', is not a pick from the taxonomy and still goes
    # through the rules.
    # The hint is normalised first: a hand-typed 'Home Appliances >Refrigerators'
    # missed the lookup on the space alone, and a categories cell round-tripped
    # from a built CSV still carries the importer escape ('Juicers\, Mixers &
    # Grinders'), so the reviewer's own pick fell through to the keyword rules.
    hint = re.sub(r'\s*>\s*', ' > ',
                  _cat_norm(unescape_category_from_csv(raw_categories)))
    if ' > ' in hint:
        exact = CATEGORY_COMBOS.get(hint)
        if exact:
            parent, sub = exact.split(' > ', 1)
            return parent, sub

    # --- accessories that NAME another category, checked FIRST ----------
    # 'V-Guard VG 400 Voltage Stabilizer for Air Conditioner' was filed as an
    # Air Conditioner, because the AC block only had to see the words "air
    # conditioner" anywhere in the title. A stabilizer is the appliance being
    # sold; the AC is what it is FOR. Same shape as the dishwasher exclusion
    # below - decide what it is not, before deciding what it is.
    # P1-14: 'Stabilizer Free Operation' is the opposite claim - an AC feature
    # meaning the unit needs NO stabilizer - and it is in the marketing name of
    # most Indian inverter ACs (Voltas Vertis, Lloyd, Blue Star). Matching the
    # bare word filed a 37,000-rupee AC under Small Appliances, so exclude the
    # phrase before trusting the word.
    # The same claim is written several ways: 'Stabilizer Free Operation',
    # 'with No Stabilizer Required', 'Works without Stabilizer', 'Stabilizer
    # Not Required'. All of them mean the product is NOT a stabilizer.
    if (has('stabilizer', 'stabiliser', 'voltage regulator')
            and not re.search(NOT_A_STABILIZER_RE, t)):
        return 'Small Appliances', 'Voltage Stabilizers & Regulators'

    # --- Washing Machines (dishwashers excluded first) ---
    if has('dishwasher', 'dish washer'):
        return 'Home Appliances', ''

    # A dispenser is a dispenser however the listing describes its bottle or
    # its chiller. Checked BEFORE the washing-machine and refrigerator rules
    # because real retail copy for a bottled water dispenser contains both
    # "Top Load" (how the bottle sits) and "Refrigerator" (the cooling
    # cabinet). Measured on a live crawl: three sibling Haier HWD-* dispensers
    # landed in three different categories - Washing Machines > Top load,
    # Home Appliances > Refrigerators, and the correct Water Dispensers - off
    # nothing but which word their retailer happened to use.
    if has('water dispenser', 'dispenser'):
        return 'Home Appliances', 'Water Dispensers'
    if has('semi auto', 'semi automatic', 'semi-automatic') or word('semi'):
        return 'Washing Machines', 'semi auto'
    if has('front load', 'front-load', 'front loading') or word('fl'):
        return 'Washing Machines', 'Front Load'
    if has('top load') or word('tl'):
        return 'Washing Machines', 'Top load'
    if has('washing machine', 'washer', 'wash') or 'wm-' in t:
        return 'Washing Machines', 'Top load'

    # --- Water Heaters (before generic 'heater') ---
    if has('geyser accessorie', 'geyser accessory'):
        return 'Water Heaters', 'Geyser & Accessories'
    if has('geyser', 'water heater', 'immersion rod') or 'wh-' in t:
        return 'Water Heaters', 'Geysers'

    # --- Air Conditioner ---
    # Inverter-vs-fixed-speed is the axis this taxonomy splits on, and it beats
    # form factor: retail titles read "1.5 Ton 5 Star AI DUAL Inverter Split AC",
    # so testing for the literal phrase 'inverter ac' never matched and filed
    # essentially EVERY inverter unit under Split AC. Checked with AC context so
    # an inverter washing machine or fridge is not dragged in, and non-inverter
    # first because 'Non-Inverter' contains 'inverter'.
    # Deliberately narrow: this ONLY promotes a genuine inverter unit ahead of
    # the form-factor rules. A 'Fixed Speed Split AC' still lands on Split AC
    # exactly as before, because form factor beating Non-Inverter was the
    # existing, intended precedence - only the inverter case was broken.
    ac_context = has('air conditioner', 'split ac', 'window ac') or word('ac')
    if (ac_context and word('inverter')
            and not re.search(r'non[\s-]?inverter|fixed\s*speed', t)):
        return 'Air Conditioner', 'Inverter AC'
    if has('inverter ac', 'inverter air conditioner', 'air conditioner inverter'):
        return 'Air Conditioner', 'Inverter AC'
    if has('split ac') or word('split'):
        return 'Air Conditioner', 'Split AC'
    if has('window ac') or word('window'):
        return 'Air Conditioner', 'Window AC'
    if word('ac') or has('air conditioner', 'conditioner'):
        return 'Air Conditioner', 'Non-Inverter AC'

    # --- Cooling & Freezing ---
    if has('deep freezer') or word('df'):
        return 'Cooling & Freezing', 'Deep Freezers'
    if has('commercial freezer', 'visi cooler', 'visicooler'):
        return 'Cooling & Freezing', 'Commercial Freezers'
    if has('water cooler'):
        return 'Cooling & Freezing', 'Water Coolers'
    if has('air cooler') or word('cooler'):
        return 'Cooling & Freezing', 'Air Coolers'

    # --- Kitchen Appliances ---
    if has('chimney'):
        return 'Kitchen Appliances', 'Chimneys'
    if has('microwave') or word('otg') or has('oven'):
        return 'Kitchen Appliances', 'Microwave Ovens'
    if has('induction', 'cooktop'):
        return 'Kitchen Appliances', 'Induction Cooktops'
    if has('gas stove', 'hob'):
        return 'Kitchen Appliances', 'Gas Stoves & Hobs'
    if has('juicer', 'mixer', 'grinder'):
        return 'Kitchen Appliances', 'Juicers, Mixers & Grinders'
    if has('toaster', 'sandwich maker'):
        return 'Kitchen Appliances', 'Toaster & Sandwich Makers'
    if has('airfryer', 'air fryer'):
        return 'Kitchen Appliances', 'Airfryer'
    if has('electric kettle'):
        return 'Small Appliances', 'electric kettle'
    if has('kettle'):
        return 'Kitchen Appliances', 'Kettles'

    # --- Small Appliances ---
    if IRON_WORD_RE.search(t):
        return 'Small Appliances', 'Electric Iron'
    # NO stabilizer rule here. It used to be duplicated at this point, without
    # the NOT_A_STABILIZER_RE guard, which silently cancelled the guarded rule
    # near the top: a title carrying 'Stabilizer Free Operation' fell past the
    # guarded rule and was caught by this unguarded copy instead. That shadowed
    # every rule below it - 'LG 260 L Frost Free Double Door Refrigerator with
    # Stabilizer Free Operation' (standard LG/Samsung/Whirlpool fridge
    # marketing, not AC-only) shipped as Small Appliances > Voltage Stabilizers
    # & Regulators, and so did stabilizer-free TVs and water purifiers.
    # Genuine stabilizers are already claimed by the guarded rule above, which
    # runs before everything, so this copy could only ever mis-file.

    # --- LED / TV ---
    if has('qled'):
        return 'LED', 'QLED'
    if has('oled'):
        return 'LED', 'OLED'
    if has('qned'):
        return 'LED', 'Qned'
    if has('mini led'):
        return 'LED', 'Mini LED'
    if word('led') or word('tv') or has('television'):
        # A plain LED TV is NOT a Mini LED - that is a premium backlight
        # tech the listing has to actually claim. Seen live: a Full HD
        # Foxsky filed under Mini LED because this arm was the catch-all.
        # No stated panel tech -> the bare parent category.
        return 'LED', ''

    # --- Electronics & Computing ---
    if has('laptop'):
        return 'Electronics & Computing', 'Laptops'
    if has('desktop pc', 'cpu'):
        return 'Electronics & Computing', 'Computers (CPUs)'
    if has('mobile', 'smartphone'):
        return 'Electronics & Computing', 'Mobiles'
    if has('tablet'):
        return 'Electronics & Computing', 'Tablets'

    # --- Home Appliances ---
    if has('refrigerator', 'fridge'):
        return 'Home Appliances', 'Refrigerators'
    if has('water purifier'):
        return 'Home Appliances', 'Water Purifiers'
    if has('air purifier'):
        return 'Home Appliances', 'Air Purifiers'
    # (the 'dispenser' rule that used to sit here now runs near the top, above
    #  the washing-machine and refrigerator rules - see the note there)
    if has('room heater', 'heat convector', 'heater'):
        return 'Home Appliances', 'Room Heaters'
    if has('sewing', 'stitch'):
        return 'Home Appliances', 'sewing machine'
    if has('mattress'):
        return 'Home Appliances', 'mattress'
    if has('trolly bag', 'trolley bag'):
        return 'Home Appliances', 'Trolly Bag'
    if word('fan') or word('fans'):
        # The taxonomy separates 'Electronics & Computing > Electronic Fans'
        # from 'Home Appliances > Fans', and category_seo.xlsx shows what it
        # means by it: rechargeable / USB / portable fans, not ceiling fans.
        # Nothing routed anything there, so that category could never fill.
        if has('rechargeable', 'usb', 'portable', 'battery operated'):
            return 'Electronics & Computing', 'Electronic Fans'
        return 'Home Appliances', 'Fans'

    # nothing in the listing said what this is - fall back to the operator's
    # own SKU prefix rather than dumping it in the catch-all
    parent, sub = sku_prefix_category(raw_sku)
    if parent:
        return parent, sub
    return 'Home Appliances', ''


def canonicalize_category(parent, sub):
    """Exact category string from the taxonomy file. Returns (text, mapped)."""
    if CATEGORY_COMBOS or CATEGORY_PARENTS:
        if sub:
            hit = CATEGORY_COMBOS.get(_cat_norm(f'{parent} > {sub}'))
            if hit:
                return hit, True
            hit = CATEGORY_COMBOS.get(_cat_norm(sub))
            if hit:
                return hit, True
        hit = CATEGORY_PARENTS.get(_cat_norm(parent))
        if hit:
            return hit, True
    return (f'{parent} > {sub}' if sub else parent), False


FAMILY_BY_SUB = {
    'microwave ovens': 'Microwave Ovens',
    'induction cooktops': 'Induction Cooktops',
    'kettles': 'Electric Kettles',
    'electric kettle': 'Electric Kettles',
    'electric iron': 'Electric Irons',
    'sewing machine': 'Sewing Machines',
    'water dispensers': 'Water Dispensers',
}


def family_for(parent, sub):
    """Product family used for capacity labels, configuration text and SKU
    prefixes (see SKU_PREFIX_BY_PARENT)."""
    return FAMILY_BY_SUB.get(_cat_norm(sub), parent)


# One prefix per parent category (operator decision 2026-08-11: prefixes
# encode the category, settled while the catalogue is small because changing
# them later renames live products). The fallback prefix covers anything the
# taxonomy does not know.
# PRODUCT_PLAN item 2: this is no longer a literal table. Every prefix comes
# from the client's [sku] block, which is the ONE source validate_csv.py's
# stripping regex, gui_helpers.SKU_CODES and sku_enricher.CATEGORY_CODES all
# read - they used to be four hand-synced copies. configure() rebinds all of
# these, so a client swap re-codes SKUs without a restart.
SKU_PREFIX_BY_PARENT = dict(CLIENT.sku.prefix_by_parent)
SKU_PREFIXES = CLIENT.sku.prefixes
# codes already on live products: prefixed_sku returns them untouched
SKU_NEVER_RENAME = tuple(f'{code}-' for code in CLIENT.sku.never_rename)
SKU_FALLBACK_PREFIX = CLIENT.sku.fallback_prefix


def prefixed_sku(raw_sku, category):
    sku = compact(raw_sku)
    # BUGFIX: normalize before prefixing - spaces inside SKUs become hyphens,
    # doubled hyphens collapse ('WM--HW100...' duplicates came from this)
    sku = re.sub(r'\s+', '-', sku)
    # BUGFIX: filesystem/URL-unsafe characters break image saving on Windows
    # ('WA70A4022FS/TL' made every image download fail -> images=0).
    # P1-18: this was a BLACKLIST, so anything nobody had been bitten by yet
    # survived - a ',' split the WooCommerce cell, '#' truncated the image URL
    # at the fragment, '%' started a bogus percent-escape, '&' and '+' were
    # mangled in query strings. A SKU only ever has to be a filename and a URL
    # path segment, so whitelist what is safe in both and hyphenate the rest.
    sku = re.sub(r'[^A-Za-z0-9._-]+', '-', sku)
    sku = re.sub(r'-{2,}', '-', sku).strip('-')
    if SKU_NEVER_RENAME and sku.startswith(SKU_NEVER_RENAME):
        return sku                     # live products carry these - never rename
    code = SKU_PREFIX_BY_PARENT.get(category)
    for p in SKU_PREFIXES:
        if sku.startswith(f'{p}-'):
            if code is None or p == code:
                return sku             # right code already, or category unknown
            # prefixes encode the category (decision 2026-08-11): a stale
            # legacy AP- or an ERP-supplied code that contradicts the detected
            # category is re-coded, not trusted
            sku = sku[len(p) + 1:]
            break
    code = code or SKU_FALLBACK_PREFIX
    return f'{code}-{sku}' if sku else f'{code}-UNKNOWN'


def wattage(text):
    # KI-14: '' when the title does not state a wattage. A spec we could not
    # read must be omitted, never replaced with a 'Standard ...' placeholder.
    match = re.search(r'(\d+\s*(?:W|Watt|Watts))', compact(text), re.IGNORECASE)
    return match.group(1) if match else ''


def capacity_label(category, title):
    if category == 'LED':
        return 'Screen Size'
    if category == 'Washing Machines':
        return 'Washing Capacity'
    if category == 'Water Heaters':
        return 'Tank Volume'
    if category == 'Microwave Ovens':
        return 'Cavity Volume'
    if category == 'Induction Cooktops':
        return 'Cooking Zones'
    if category == 'Sewing Machines':
        return 'Stitch Configuration'
    if category == 'Electric Kettles':
        return 'Liquid Volume'
    if category == 'Electric Irons' or IRON_WORD_RE.search(compact(title)):
        return 'Soleplate Variant'
    return 'Storage Capacity'


def capacity_value(title, category):
    text = compact(title)
    # KI-14: every branch returns '' when the title does not carry the number,
    # so the row is dropped rather than filled with an invented value.
    if category == 'Washing Machines':
        m = re.search(r'(\d+(?:\.\d+)?\s*(?:kg|kgs?|ltr|L|litre|litres))', text, re.IGNORECASE)
        return m.group(1) if m else ''
    if category == 'Water Heaters':
        m = re.search(r'(\d+(?:\.\d+)?\s*(?:L|liter|liters|litre|litres))', text, re.IGNORECASE)
        return m.group(1) if m else ''
    if category == 'Microwave Ovens':
        m = re.search(r'(\d+(?:\.\d+)?\s*(?:L|liter|liters|litre|litres))', text, re.IGNORECASE)
        return m.group(1) if m else ''
    if category == 'Induction Cooktops':
        m = re.search(r'(\d+\s*(?:Zone|Zones|Burner|Burners))', text, re.IGNORECASE)
        return m.group(1) if m else ''
    if category == 'Electric Irons' or IRON_WORD_RE.search(text):
        # KI-14: was an unconditional 'Premium Non-Stick Glide' - a soleplate
        # claim invented for every iron, whatever the listing actually said.
        m = SOLEPLATE_RE.search(text)
        if not m:
            return ''
        material = m.group(1) or m.group(2)      # either arm of the alternation
        return f'{material.title()} Soleplate' if material else ''
    if category == 'Electric Kettles':
        m = re.search(r'(\d+(?:\.\d+)?\s*(?:ml|l|L))', text, re.IGNORECASE)
        return m.group(1) if m else ''
    if category == 'Sewing Machines':
        # KI-14: was an unconditional 'Built-in Straight Stitching'
        m = re.search(r'(\d+)\s*stitch', text, re.IGNORECASE)
        if m:
            return f'{m.group(1)} Built-in Stitches'
        m = re.search(r'\b(straight|zigzag|zig-zag)\s*stitch', text, re.IGNORECASE)
        return f'{m.group(1).title()} Stitch' if m else ''
    return ''


def configuration_text(title, category):
    t = compact(title).lower()
    if category == 'Washing Machines':
        # BUGFIX: front-load machines were previously labelled 'Top Load'
        if 'semi' in t:
            return 'Semi-Automatic Top Load'
        if 'front load' in t or 'front-load' in t or 'front loading' in t:
            return 'Fully Automatic Front Load'
        return 'Fully Automatic Top Load'
    if category == 'Water Heaters':
        # KI-14: the old `... if 'storage' in t else 'Instant Water Heater'`
        # labelled every heater that did not say "storage" as INSTANT - so a
        # 'Racold 15L Water Heater', which is a storage geyser, shipped with
        # "Instant Water Heater" on the storefront. Say nothing unless the
        # listing does.
        if 'storage' in t:
            return 'Storage Water Geyser'
        if 'instant' in t:
            return 'Instant Water Heater'
        if 'immersion' in t:
            return 'Immersion Water Heater'
        return ''
    if category == 'Microwave Ovens':
        return 'Convection Microwave Oven' if 'convection' in t else 'Solo Microwave Oven'
    if category == 'Induction Cooktops':
        return 'Infrared Cooktop' if 'infrared' in t else 'Induction Cooktop Push-Button'
    if category == 'Electric Irons' or IRON_WORD_RE.search(t):
        return 'Heavy Weight Dry Iron' if 'heavy' in t else 'Steam Iron Fabric Care'
    if category == 'Electric Kettles':
        # KI-14: was an unconditional 'Stainless Steel Electric Kettle', which
        # is a material claim about a product we know nothing about - a plastic
        # kettle was being sold as stainless steel.
        m = KETTLE_BODY_RE.search(t)
        return f'{m.group(1).title()} Electric Kettle' if m else ''
    if category == 'Sewing Machines':
        return 'Manual Composite Sewing Machine' if 'manual' in t else 'Straight Stitch Sewing Frame'
    return ''                                   # KI-14: was 'Appliance Utility Core'


# ------------- the attribute workbooks (owner rule 2026-11-19) ---------------
# The client's attributes master (client.toml `attributes_master`) is the
# SOURCE the owner builds the storefront filter groups from: its
# 'Sub-Category Attributes' sheet lists the attribute values each sub-category
# needs, and its filter sheet (header 'Filter Group | Filter Values') lists the
# store's filter groups. category_map.xlsx carries copies of both sheets
# ('Sub-Category Attributes', 'Filters') and is the FALLBACK when the master is
# missing or lacks the sheet. tools\check_filter_sheet.py reports any drift
# between the two copies; the build warns about it too.
SUBCAT_SHEET = 'Sub-Category Attributes'
SUBCAT_AXES = ('Type', 'Size', 'Star Rating', 'Ton', 'Capacity', 'Storage')
# The sheet's SECOND, lower-case 'type' column (Fans: 'non BLDC, BLDC'; Water
# Purifiers: 'Under Sink, Wall Mounted'). Carried as its own axis; it is an
# OPTIONAL axis - it never creates a `_spec_gaps` entry.
SUB_TYPE_AXIS = 'Sub Type'
OPTIONAL_AXES = frozenset((SUB_TYPE_AXIS,))
FILTER_GROUP_HEADER = 'filter group'
FILTERS_SHEET_FALLBACK = 'Filters'
# a cell that went through a wrong code page ('â€”', 'Ã©', U+FFFD) is dropped,
# never matched against
_MOJIBAKE_RE = re.compile('�|â€|â‚|Ã[\u0080-¿]'
                          '|Â[\u0080-¿]')


def attributes_master_path(client=None):
    """The client's attributes master workbook, or '' when it does not exist."""
    client = client or CLIENT
    path = getattr(client, 'attributes_master', None)
    return str(path) if path and os.path.exists(str(path)) else ''


def attribute_workbooks(client=None):
    """The workbooks the attribute sheets are read from, in priority order:
    the attributes master first, then category_map.xlsx."""
    out = []
    for path in (attributes_master_path(client), category_map_path(client)):
        if path and not path.lower().endswith('.csv') and path not in out:
            out.append(path)
    return out


def _workbook_rows(path):
    """{sheet name: [row tuples of str]} for one .xlsx (raises when unreadable)."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return {ws.title: [tuple('' if c is None else str(c) for c in row)
                           for row in ws.iter_rows(values_only=True)]
                for ws in wb.worksheets}
    finally:
        wb.close()


def _first_row(rows):
    return next((r for r in rows if any(compact(c) for c in r)), ())


def find_subcategory_sheet(sheets):
    """(sheet name, rows) of the 'Sub-Category Attributes' sheet, by name, else
    by its header (a 'Parent Category' and a 'Sub-Category' column)."""
    for title, rows in sheets.items():
        if _cat_norm(title) == _cat_norm(SUBCAT_SHEET):
            return title, rows
    for title, rows in sheets.items():
        head = {_cat_norm(c) for c in _first_row(rows)}
        if 'parent category' in head and 'sub-category' in head:
            return title, rows
    return None, None


def find_filter_sheet(sheets):
    """(sheet name, rows, group column) of the filter sheet. Found by HEADER -
    a column whose header starts with 'Filter Group' - so 'Sheet2' in the
    master and 'Filters' in category_map.xlsx are both found. A sheet named
    'Filters' with another header is accepted last, column A."""
    for title, rows in sheets.items():
        for col, cell in enumerate(_first_row(rows)):
            if _cat_norm(cell).startswith(FILTER_GROUP_HEADER):
                return title, rows, col
    for title, rows in sheets.items():
        if _cat_norm(title) == _cat_norm(FILTERS_SHEET_FALLBACK):
            return title, rows, 0
    return None, None, None


def subcategory_records(rows):
    """Data rows of a 'Sub-Category Attributes' sheet as dicts keyed by
    'Parent Category', 'Sub-Category', each axis in SUBCAT_AXES and
    SUB_TYPE_AXIS (the second 'type' column). Cells are compacted strings."""
    head = _first_row(rows)
    if not head:
        return []
    start = rows.index(head) + 1
    columns = {}
    for col, cell in enumerate(head):
        name = compact(cell)
        low = name.lower()
        if low == 'type':
            key = 'Type' if 'Type' not in columns.values() else SUB_TYPE_AXIS
        else:
            key = next((k for k in ('Parent Category', 'Sub-Category') + SUBCAT_AXES
                        if k.lower() == low), None)
        if key and key not in columns.values():
            columns[col] = key
    out = []
    for row in rows[start:]:
        record = {key: compact(row[col]) if col < len(row) else ''
                  for col, key in columns.items()}
        if any(record.values()):
            out.append(record)
    return out


def _subcategory_sheet(client=None):
    """(records, source path, problems) from the first workbook that has the
    'Sub-Category Attributes' sheet - the master first."""
    problems = []
    for path in attribute_workbooks(client):
        name = os.path.basename(path)
        try:
            sheets = _workbook_rows(path)
        except Exception as exc:
            problems.append(f'{name}: {type(exc).__name__}: {exc}')
            continue
        _title, rows = find_subcategory_sheet(sheets)
        if rows is None:
            # NEVER swallow this silently: with no axes, missing_spec_axes()
            # returns [] for every product, so `_spec_gaps` is blank and
            # validate_csv reports no missing_required_attribute - a build
            # that looks perfect while checking nothing. The usual cause is
            # the workbook having been rewritten as a single sheet; restore
            # it from backups\category_map_<ts>.xlsx.
            problems.append(f"{name}: no '{SUBCAT_SHEET}' sheet")
            continue
        return subcategory_records(rows), path, problems
    return [], '', problems


def _index_subcategories(records, cell_fn):
    """{normalised sub-category: {axis: cell_fn(axis, cell)}} with the keying
    rules below. Axes for which cell_fn returns None are left out."""
    out = {}
    # A parent is only safe to inherit from when it declares exactly ONE
    # attribute row (the LED shape: one row listing every sub-category).
    # 'Home Appliances' has 12 heterogeneous rows and Fans is the first, so
    # inheriting "whichever row came first" gave every bare-parent product
    # the ceiling-fan axes - build_tags shipped the tag 'Table' on a
    # dishwasher and 'Wall' on a water softener, and missing_spec_axes
    # invented gaps ('Type, Size, Star Rating, Capacity') that validate_csv
    # then reported as missing_required_attribute.
    parent_rows = {}
    for row in records:
        if row.get('Sub-Category'):
            key = _cat_norm(row.get('Parent Category'))
            parent_rows[key] = parent_rows.get(key, 0) + 1
    for row in records:
        sub = row.get('Sub-Category', '')
        if not sub:
            continue
        axes = {}
        for axis in SUBCAT_AXES + (SUB_TYPE_AXIS,):
            value = cell_fn(axis, row.get(axis, ''))
            if value is not None:
                axes[axis] = value
        # P1-17: the merged cell as written ('Juicers, Mixers & Grinders')
        # is itself a taxonomy sub-category, and a product filed under the
        # whole string used to find nothing here
        out[_cat_norm(sub)] = axes
        # the LED row lists several sub-categories in one cell
        for one in re.split(r',', sub):
            one = compact(one)
            if one:
                out[_cat_norm(one)] = axes
        # a product filed under the bare parent (split_hierarchy turns
        # 'LED' into ('LED', 'LED')) keeps the same required axes as the
        # parent's sub-categories - but ONLY when the parent has a single
        # attribute row, so "the parent's axes" is not ambiguous.
        # setdefault so a real sub named like a parent still wins.
        parent_name = row.get('Parent Category', '')
        if parent_name and parent_rows.get(_cat_norm(parent_name)) == 1:
            out.setdefault(_cat_norm(parent_name), axes)
    return out


def _legacy_axis_values(_axis, cell):
    """The vocabulary shape attribute_axes() has always returned: a listed
    cell -> its comma parts; a free-text hint -> [] (the axis applies, the
    value must come from elsewhere); empty / N/A -> None (axis absent)."""
    cell = compact(cell)
    if not cell or cell.upper() == 'N/A':
        return None
    looks_like_hint = 'e.g' in cell.lower() or '(' in cell or len(cell) > 60
    return [] if looks_like_hint else [
        compact(v) for v in cell.split(',') if compact(v)]


def load_subcategory_attributes(client=None):
    """Read the 'Sub-Category Attributes' sheet - from the client's attributes
    master when it has one, else from category_map.xlsx. Returns
    {normalised sub-category: {axis: [allowed values]}}; the second 'type'
    column is the SUB_TYPE_AXIS axis.

    Cells come in two flavours: an enumerable vocabulary ('1.0 Ton, 1.5 Ton,
    2.0 Ton') and a free-text hint ('Dry Clothes Wash Load Weight in kg (e.g.
    7.0kg)'). Only the first is usable as a value list here; a hint just means
    the axis applies to this sub-category. (The filter-group code reads the
    same cells more deeply - see parse_axis_cell.)
    """
    global SUBCATEGORY_ATTRIBUTES_WARNING
    SUBCATEGORY_ATTRIBUTES_WARNING = ''
    records, _source, problems = _subcategory_sheet(client)
    out = _index_subcategories(records, _legacy_axis_values)
    if not out:
        SUBCATEGORY_ATTRIBUTES_WARNING = (
            "No 'Sub-Category Attributes' sheet could be read from the "
            "attributes master or the taxonomy file, so NO required "
            "attribute axes (and no filter axes) are known: "
            "`_spec_gaps` will be blank for every product and validate_csv "
            "will report no missing_required_attribute. Restore "
            "category_map.xlsx from backups\\category_map_<timestamp>.xlsx."
            + (' (' + '; '.join(problems) + ')' if problems else ''))
        _ui().warning(SUBCATEGORY_ATTRIBUTES_WARNING)
    return out


# Set by load_subcategory_attributes(); non-empty means the taxonomy's
# attribute sheet is missing/unreadable. Surfaced at import AND at build time.
SUBCATEGORY_ATTRIBUTES_WARNING = ''
SUBCATEGORY_ATTRIBUTES = load_subcategory_attributes()


def attribute_axes(sub, parent=''):
    """{axis: [allowed values]} the taxonomy declares for this product, or {}.

    Looks up the sub-category as written first, then each comma part of it (a
    merged cell like 'Juicers, Mixers & Grinders' reaches us both ways).

    A merged name whose parts are SEPARATE taxonomy rows is the union of those
    rows, not the first one: 'Juicers, Mixers & Grinders' has a 'Juicers' row
    (Type, Capacity 250-1000W) and a 'Mixers & Grinders' row (Type, Size,
    Capacity 500-1000W). Returning only the juicer row hid the 'Size' axis
    entirely and judged 'Philips HL7756 750W Mixer Grinder' against a Capacity
    vocabulary that has no 750W in it - the product was gap-reported for a
    figure printed in its own title, and shipped without the 750W tag. Each
    axis's allowed values are concatenated in first-seen order, de-duplicated;
    the stored dicts are never mutated.

    The fall back to the PARENT is deliberately limited to the bare-parent
    case - no sub-category at all, or a sub that IS the parent (split_hierarchy
    turns 'LED' into ('LED', 'LED')). An unknown sub-category must NOT inherit
    its parent's axes: it would be judged complete, or gap-reported, against
    axes nobody declared for it, and the mis-detection would be hidden.
    The parent itself is only present in SUBCATEGORY_ATTRIBUTES when it has a
    single attribute row (see load_subcategory_attributes), so a many-row
    parent like 'Home Appliances' correctly resolves to {}.
    """
    axes = SUBCATEGORY_ATTRIBUTES.get(_cat_norm(sub))
    if axes:
        return axes
    parts = [SUBCATEGORY_ATTRIBUTES.get(_cat_norm(part))
             for part in str(sub or '').split(',')]
    parts = [p for p in parts if p]
    if len(parts) == 1:
        return parts[0]
    if parts:
        merged = {}
        for one in parts:
            for axis, allowed in one.items():
                bucket = merged.setdefault(axis, [])
                for value in allowed:
                    if value not in bucket:
                        bucket.append(value)
        return merged
    if not compact(sub) or _cat_norm(sub) == _cat_norm(parent):
        return SUBCATEGORY_ATTRIBUTES.get(_cat_norm(parent)) or {}
    return {}

RESOLUTION_PATTERNS = (
    (r'\b8k\b', '8K'),
    (r'\b4k\b|ultra\s*hd|\buhd\b', '4K Ultra HD'),
    (r'full\s*hd|\bfhd\b|1080p', 'Full HD'),
    (r'hd\s*ready|\bhd\b|720p', 'HD Ready'),
)
SMART_OS_PATTERNS = (
    (r'google\s*tv', 'Google TV'),
    (r'android\s*tv', 'Android TV'),
    (r'\bwebos\b', 'WebOS'),
    (r'\btizen\b', 'Tizen'),
    (r'fire\s*tv', 'Fire TV'),
    (r'\bvidaa\b', 'VIDAA'),
)
# A bare "Smart" in a title is NOT a platform (owner decision 2026-11-19:
# write the platform when the page names one, else nothing). spec_tags()
# keeps it under its own key 'smart', which only feeds the tag "Smart TV".
SMART_WORD_RE = re.compile(r'\bsmart\b', re.IGNORECASE)

# Labels that name a TV's platform / operating system, in any wording, and the
# bare values that say "it is smart" without naming one. Such a row is never
# written - not as an attribute, not in the description spec table, not in
# the key features (see bare_platform_row).
PLATFORM_LABEL_RE = re.compile(
    r'(?:smart\s*(?:tv\s*)?platform|tv\s+smart\s+features?|'
    r'(?:smart\s*(?:tv\s*)?|tv\s+)?(?:operating\s+system|os)|platform|'
    r'smart\s*tv\s*(?:operating\s+system|os|platform|type))',
    re.IGNORECASE)
BARE_PLATFORM_VALUES = frozenset((
    'smart tv', 'smart', 'yes', 'no', 'na', 'n/a', 'none', 'not available'))


def bare_platform_row(label, value):
    """True for a platform/OS-style label whose value names no platform
    ('Smart Platform = Smart TV', 'Operating System = Yes')."""
    lab = compact(label).strip(': ')
    val = compact(value).lower().strip(' .')
    return bool(PLATFORM_LABEL_RE.fullmatch(lab)) and val in BARE_PLATFORM_VALUES
PRICE_BANDS = ((10000, 'Under 10000'), (20000, '10000 to 20000'),
               (30000, '20000 to 30000'), (50000, '30000 to 50000'),
               (100000, '50000 to 100000'), (float('inf'), 'Above 100000'))


def spec_tags(title):
    """Headline specs read straight off the listing title. Generic on purpose -
    the same numbers mean different things per category, so the caller decides
    which of these are relevant."""
    t = compact(title)
    found = {}
    m = re.search(r'(\d{2,3})\s*(?:inch|inches|")', t, re.IGNORECASE)
    if m:
        found['screen'] = f'{m.group(1)} Inch'
    m = re.search(r'(\d+(?:\.\d+)?)\s*kg\b', t, re.IGNORECASE)
    if m:
        found['weight'] = f'{m.group(1)} kg'
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:ton|tr)\b', t, re.IGNORECASE)
    if m:
        found['ton'] = f'{m.group(1)} Ton'
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:l|litre|liter|ltr)s?\b', t, re.IGNORECASE)
    if m:
        found['volume'] = f'{m.group(1)} L'
    m = re.search(r'(\d)\s*star\b', t, re.IGNORECASE)
    if m:
        found['star'] = f'{m.group(1)} Star'
    for pattern, label in RESOLUTION_PATTERNS:
        if re.search(pattern, t, re.IGNORECASE):
            found['resolution'] = label
            break
    for pattern, label in SMART_OS_PATTERNS:
        if re.search(pattern, t, re.IGNORECASE):
            found['platform'] = label
            break
    if SMART_WORD_RE.search(t):
        found['smart'] = 'Smart TV'      # a tag only, never a platform value
    return found


def price_band_tag(sale_price):
    try:
        value = float(sale_price)
    except (TypeError, ValueError):
        return ''
    if value <= 0:
        return ''
    for ceiling, label in PRICE_BANDS:
        if value < ceiling:
            return label
    return ''


CATEGORY_SEO_FILE = CLIENT.category_seo.name


def load_category_seo(client=None):
    """Curated per-category tags and SEO keywords from category_seo.xlsx.

    Hand-written, because tags exist to group products the way a customer
    searches and nobody searches a model code. Missing file is not an error -
    the derived tags below still work, they are just not topped up.
    Regenerate with tools\\build_category_seo.py."""
    path = str((client or CLIENT).category_seo)
    table = {}
    if not os.path.exists(path):
        return table
    try:
        rows = pd.read_excel(path, dtype=str).fillna('').values.tolist()
    except Exception:
        return table
    for row in rows:
        parent = compact(row[0] if len(row) > 0 else '')
        sub = compact(row[1] if len(row) > 1 else '')
        if not parent or _cat_norm(parent) == 'parent category':
            continue
        table[_cat_norm(f'{parent} > {sub}')] = {
            'tags': [compact(t) for t in str(row[2] if len(row) > 2 else '').split(',') if compact(t)],
            'focus': compact(row[3] if len(row) > 3 else ''),
            'keywords': [compact(k) for k in str(row[4] if len(row) > 4 else '').split(',') if compact(k)],
        }
    return table


CATEGORY_SEO = load_category_seo()


def configure(slug):
    """Switch this module to another client and return its ClientConfig.

    Every constant that was derived from the client at import time is rebound
    here, so a process that already imported app (wc_seo.py, the GUI, the
    tests) can build for a different store without a subprocess. Raises
    ClientConfigError for an unknown slug and leaves the current client in
    place, because a half-swapped module would write one store's images under
    another store's media base.
    """
    global CLIENT, IMAGE_DIR, MEDIA_BASE, SEO_SITE_NAME, CATEGORY_SEO_FILE
    global CATEGORY_COMBOS, CATEGORY_PARENTS, CATEGORY_MAP_FILE
    global SUBCATEGORY_ATTRIBUTES, CATEGORY_SEO
    global SKU_PREFIX_BY_PARENT, SKU_PREFIXES, SKU_PREFIX_CATEGORY
    global SKU_NEVER_RENAME, SKU_FALLBACK_PREFIX
    global FILTER_GROUPS, SUBCATEGORY_FILTER_AXES, FILTER_SHEET_WARNING
    incoming = client_config.load_client(slug)
    combos, parents, map_file = load_category_map(incoming)
    attributes = load_subcategory_attributes(incoming)
    filter_groups = load_filter_groups(incoming)
    filter_axes_table = load_subcategory_filter_axes(incoming)
    sheet_warning = filter_sheet_warning(incoming)
    seo_table = load_category_seo(incoming)

    CLIENT = incoming
    IMAGE_DIR = incoming.image_dir
    MEDIA_BASE = incoming.media_base()
    SEO_SITE_NAME = incoming.seo_site_name
    CATEGORY_SEO_FILE = incoming.category_seo.name
    CATEGORY_COMBOS, CATEGORY_PARENTS, CATEGORY_MAP_FILE = combos, parents, map_file
    SUBCATEGORY_ATTRIBUTES = attributes
    FILTER_GROUPS = filter_groups
    SUBCATEGORY_FILTER_AXES = filter_axes_table
    FILTER_SHEET_WARNING = sheet_warning
    CATEGORY_SEO = seo_table
    SKU_PREFIX_BY_PARENT = dict(incoming.sku.prefix_by_parent)
    SKU_PREFIXES = incoming.sku.prefixes
    SKU_PREFIX_CATEGORY = dict(incoming.sku.input_codes)
    SKU_NEVER_RENAME = tuple(f'{code}-' for code in incoming.sku.never_rename)
    SKU_FALLBACK_PREFIX = incoming.sku.fallback_prefix
    return CLIENT


def category_seo_for(parent, sub):
    """Curated entry for 'Parent > Sub', falling back to the parent alone."""
    return (CATEGORY_SEO.get(_cat_norm(f'{parent} > {sub}'))
            or CATEGORY_SEO.get(_cat_norm(f'{parent} > {parent}'))
            or {})


def build_tags(brand, parent, sub, title, sale_price, category_family):
    """WooCommerce product tags, driven by the taxonomy file's attribute sheet
    plus specs read off the title, then topped up with the curated per-category
    tags in category_seo.xlsx. Deterministic - the same product always produces
    the same tags, and editing either Excel changes the output."""
    tags, seen = [], set()

    def add(value):
        value = compact(value)
        if value and value.lower() not in seen and len(value) <= 60:
            seen.add(value.lower())
            tags.append(value)

    add(brand)
    add(parent)
    if _cat_norm(sub) != _cat_norm(parent):
        add(sub)
    if brand and brand != 'Authorized Brand':
        add(f'{brand} {sub or parent}')

    specs = spec_tags(title)
    axes = attribute_axes(sub, parent)
    axis_to_spec ={'Ton': 'ton', 'Star Rating': 'star',
                    'Size': 'screen', 'Storage': 'volume'}

    for axis, allowed in axes.items():
        if axis in OPTIONAL_AXES:        # tags stay as they were before it
            continue
        if axis == 'Capacity':
            # 'Capacity' means kg for washers, litres for fridges, inches for TVs
            for key in ('weight', 'volume', 'screen'):
                if key in specs:
                    add(specs[key])
                    break
        elif axis in axis_to_spec and axis_to_spec[axis] in specs:
            add(specs[axis_to_spec[axis]])
        # a listed vocabulary ('Inverter, Fixed Speed') can be matched directly
        for value in allowed:
            if value and re.search(r'\b' + re.escape(value) + r'\b', title, re.IGNORECASE):
                add(value)

    # specs worth tagging whether or not the sheet mentions them
    add(specs.get('resolution'))
    # the named platform, else the plain "Smart TV" tag (tags are search
    # groupings, not a platform claim - unchanged by the platform decision)
    add(specs.get('platform') or specs.get('smart'))
    if 'screen' in specs and _cat_norm(parent) in ('led', 'electronicscomputing'):
        add(specs['screen'])
    add(configuration_text(title, category_family))
    add(price_band_tag(sale_price))

    # curated category tags last: the product's own specs are the more specific
    # signal, and these are the broad terms customers actually search
    for tag in category_seo_for(parent, sub).get('tags', ()):
        add(tag)
    return ', '.join(tags)


# ------------- sub-category attribute completeness gate (item 7) -------------
# The 'Sub-Category Attributes' sheet already DECLARES which axes matter per
# sub-category (an AC has a Ton, a TV has a Size, a washer has a Capacity). It
# was only ever read as a source of tags. Read as a CHECKLIST it becomes a
# completeness gate: a product missing an axis its own category requires is
# flagged, in the sheet and in validate_csv.py, instead of quietly shipping
# with a hole in its filters.
#
# It FLAGS, it does not block. A missing axis is often just a terse title, and
# the human review step is where that gets decided.

# axis -> the spec_tags() keys that would satisfy it
AXIS_SPEC_KEYS = {
    'Ton': ('ton',),
    'Star Rating': ('star',),
    'Size': ('screen',),
    'Storage': ('volume',),
    'Capacity': ('weight', 'volume', 'screen', 'ton'),
    'Type': (),
}

# axis -> spec-table labels (already canonicalised) that would satisfy it
AXIS_SPEC_LABELS = {
    'Ton': ('capacity', 'tonnage'),
    'Star Rating': ('energy rating',),
    'Size': ('screen size',),
    'Storage': ('capacity',),
    'Capacity': ('capacity', 'screen size'),
    'Type': ('configuration',),
}


def missing_spec_axes(sub, title, category_family, scraped=(), parent=''):
    """Axes the taxonomy declares for this sub-category that this product
    cannot fill from anywhere. Empty tuple means complete.

    `parent` only matters for a product filed under the bare parent - see
    attribute_axes()."""
    axes = attribute_axes(sub, parent)
    if not axes:
        return []
    specs = spec_tags(title)
    labels = {label.lower() for label, _ in scraped}
    text = compact(title)
    missing = []
    for axis, allowed in axes.items():
        if axis in OPTIONAL_AXES:        # the sheet's second 'type' column
            continue
        # 1. the sheet's own vocabulary ('Inverter, Fixed Speed') is in the title
        if any(v and re.search(r'\b' + re.escape(v) + r'\b', text, re.IGNORECASE)
               for v in allowed):
            continue
        # 2. the title carries the measurement the axis names
        if any(k in specs for k in AXIS_SPEC_KEYS.get(axis, ())):
            continue
        # 3. the retailer's spec table names it
        if any(l in labels for l in AXIS_SPEC_LABELS.get(axis, ())):
            continue
        # 4. 'Type' is satisfied by a configuration we could actually determine
        if axis == 'Type' and configuration_text(title, category_family):
            continue
        missing.append(axis)
    return missing


def html_block(rows):
    content = ''.join([f'<tr><th>{compact(k)}</th><td>{compact(v)}</td></tr>' for k, v in rows])
    return compact(f'<table><tbody>{content}</tbody></table>')


# ---------------- specs scraped off the retailer page (item 9) ---------------
# sku_enricher fills a 'specs' column with the product's own specification
# table, serialised as 'Label: Value | Label: Value'. Before this, every
# WooCommerce attribute had to be inferred from the listing TITLE, so a product
# whose page listed twenty specs shipped with four.
#
# These are copied facts, not guesses, so they are safe under the KI-14 rule.
# They rank BELOW the title-derived rows, which are normalised per category: a
# scraped row only ever fills a label nothing else supplied.

# retailer wording -> the label this catalogue uses, so the same fact does not
# appear twice under two names
SPEC_LABEL_CANON = {
    'display size': 'Screen Size', 'screen size': 'Screen Size',
    'screen size (inch)': 'Screen Size', 'display': 'Screen Size',
    'star rating': 'Energy Rating', 'energy rating': 'Energy Rating',
    'bee star rating': 'Energy Rating', 'bee rating': 'Energy Rating',
    'display resolution': 'Resolution', 'resolution': 'Resolution',
    'supported resolution': 'Resolution',
    'operating system': 'Smart Platform', 'smart tv platform': 'Smart Platform',
    'os': 'Smart Platform', 'platform': 'Smart Platform',
    'power consumption': 'Power Rating', 'wattage': 'Power Rating',
    'power requirement': 'Power Rating', 'motor power': 'Power Rating',
    'washing capacity': 'Capacity', 'total capacity': 'Capacity',
    'storage capacity': 'Capacity', 'capacity': 'Capacity',
    'product type': 'Configuration', 'type': 'Configuration',
    'washing method': 'Configuration', 'loading type': 'Configuration',
    'brand': 'Brand', 'model name': 'Model', 'model number': 'Model',
    'model': 'Model', 'colour': 'Colour', 'color': 'Colour',
    'warranty': 'Warranty', 'warranty period': 'Warranty',
    'product warranty': 'Warranty', 'refresh rate': 'Refresh Rate',
}

# Invoice facts, seller lines and offers dressed as spec rows ('SuperDost
# Cashback: 100 for new SuperDost Users* + 3% or 250 Cashback') are refused by
# the content firewall imported from sku_enricher (spec_row_ok: the merged
# label deny list + content_ok on label AND value), so an already-enriched
# sheet carrying the junk is cleaned here without a re-scrape.


# The product's own size and weight, in every retailer wording, get ONE
# catalogue name each (owner decision 2026-11-19). The box's size and weight
# ('Package Dimensions', 'Shipping Weight') never get here: the firewall's
# label deny list refuses them on the raw label first.
_DIMENSIONS_LABEL_RE = re.compile(
    r'(?:product|item|unit|overall)?\s*dimensions?'
    r'(?:\s*[(\[]?\s*(?:in\s+)?(?:[lwhdx×*/\s]|cm|mm|inch(?:es)?|in)*[)\]]?)?',
    re.IGNORECASE)
_WEIGHT_LABEL_RE = re.compile(
    r'(?:item|product|net|unit)?\s*weight'
    r'(?:\s*[(\[]?\s*(?:in\s+)?(?:kgs?|g|grams?)[)\]]?)?', re.IGNORECASE)


def canon_spec_label(label):
    text = compact(label).strip(': ')
    low = text.lower()
    if low in SPEC_LABEL_CANON:
        return SPEC_LABEL_CANON[low]
    if _DIMENSIONS_LABEL_RE.fullmatch(low):
        return 'Dimensions'
    if _WEIGHT_LABEL_RE.fullmatch(low):
        return 'Weight'
    return text


# ----------------- filter groups (owner rule 2026-11-19) ---------------------
# "The sub-category attribute sheet is what we use to make the filter groups.
# If anything scraped matches these filter groups, we include our attribute
# value." So a product's filter attributes are NAMED by the filter group and
# carry the SHEET's spelling of the value, never the retailer's.
#
# Nothing is invented: a value is only written when the scraped spec table or
# the title states it. A stated value that snaps to an allowed value is
# written in the sheet's spelling.
#
# Owner decision 2026-11-19: a scraped value is never replaced, rounded or
# dropped because the list does not hold it; only its spelling is made
# regular. So a stated value that does not snap is WRITTEN as the filter
# attribute (global, first in order) and `_filter_unmatched` gets the note
# "<Group>: '<value>' is a new value, add it to the sheet":
#   - numeric group: a number in the axis unit (scraped label first, else the
#     title) as number + the list's own suffix ('1.8 T' -> '1.8 Ton', '24
#     inch' -> '24"'). A value inside a band writes the band label, and the
#     exact source row stays as its own attribute (_repeats_filter);
#   - text group: a value under a label DEDICATED to the group (never the
#     title, never a general feature list) as scraped, when it passes the
#     content firewall and is at most NEW_TEXT_VALUE_MAX characters.
# Never a value: 'yes', 'no', 'n/a', 'not available', negatives ('without
# heater'), and for TV Smart Features a bare 'Smart TV' (the platform is
# written only when the page names one). No value is ever rounded to the
# nearest allowed one.

# unit -> regex of its spellings (order matters: longer spellings first)
_UNIT_SPELLINGS = (
    ('m3h', r'm\s*(?:³|3)\s*/\s*h(?:r|our)?|cmh'),
    ('kva', r'kva'),
    ('tb', r'tb'),
    ('gb', r'gb'),
    ('ton', r'tonnes?|tons?|tr|t'),
    ('l', r'litres?|liters?|ltrs?|ltr|lt|l'),
    ('kg', r'kilograms?|kgs?'),
    ('inch', r'inches|inch|in(?!\s*\d)|"|″|”|\'\''),
    ('star', r'stars?'),
    ('w', r'watts?|w'),
    ('mm', r'mm'),
    ('cm', r'cm'),
    ('spm', r'spm'),
    ('rpm', r'rpm'),
    ('hz', r'hertz|hz'),
)
_NUMBER = r'(\d+(?:\.\d+)?)'
_QUANTITY_RE = re.compile(
    _NUMBER + r'\s*-?\s*(?:' + '|'.join(f'(?P<{unit}>{spelling})'
                                        for unit, spelling in _UNIT_SPELLINGS)
    + r')(?![a-z0-9])', re.IGNORECASE)
# how a free-form value is written: number + this suffix
UNIT_DISPLAY = {
    'm3h': ' m³/h', 'kva': ' kVA', 'gb': 'GB', 'ton': ' Ton', 'l': 'L',
    'kg': ' kg', 'inch': ' Inch', 'star': ' Star', 'w': 'W', 'mm': 'mm',
    'cm': ' cm', 'spm': ' SPM', 'rpm': ' RPM', 'hz': 'Hz',
}
_STAR_WORDS = {'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5'}

# Wording that unambiguously means an allowed value (normalised spelling of
# the allowed value -> extra phrases). Kept SHORT on purpose: an alias is a
# claim that two wordings mean the same thing.
FILTER_VALUE_ALIASES = {
    'non inverter': ('fixed speed',),
    'fully automatic front load': ('front load', 'front loading',
                                   'front loader', 'front opener'),
    'fully automatic top load': ('top load', 'top loading', 'top loader',
                                 'top opener'),
    'non electrical (gravity)': ('gravity', 'non electric'),
    'semi automatic': ('semi auto', 'twin tub'),
    'front load': ('front loading', 'front loader'),
    'top load': ('top loading', 'top loader'),
    # directly-fed groups (DIRECT_FILTER_GROUPS); '3840 x 2160' is folded to
    # '3840x2160' by _norm_filter_text
    '4k uhd': ('4k', '4k ultra hd', 'ultra hd', 'uhd', '3840x2160'),
    'full hd': ('fhd', '1920x1080'),
    'hd ready': ('1366x768',),
    'built in heater': ('inbuilt heater', 'in built heater'),
    'inverter tech': ('inverter', 'digital inverter', 'smart inverter',
                      'inverter motor', 'inverter technology'),
    'steam wash': ('steam', 'hygiene steam'),
    'smart wi fi': ('wifi', 'wi fi', 'wi fi enabled'),
    'direct drive': ('inverter direct drive',),
    'grey': ('gray',),
    'frost free': ('no frost',),
}


def _quantities(text):
    """[(number, unit, matched text)] for every number + unit in `text`, in
    order. TB is folded into GB (1 TB = 1024 GB) so storage lists compare."""
    text = compact(text)
    text = re.sub(r'\b(one|two|three|four|five)\b(?=\s*-?\s*stars?\b)',
                  lambda m: _STAR_WORDS[m.group(1).lower()], text,
                  flags=re.IGNORECASE)
    out = []
    for m in _QUANTITY_RE.finditer(text):
        unit = next(u for u, _s in _UNIT_SPELLINGS if m.group(u) is not None)
        number = float(m.group(1))
        if unit == 'tb':
            unit, number = 'gb', number * 1024
        out.append((number, unit, m.group(0)))
    return out


def _bare_number(text):
    m = re.fullmatch(r'\s*' + _NUMBER + r'\s*', str(text or ''))
    return float(m.group(1)) if m else None


def _single_quantity(text):
    """(number, unit or None) when `text` is exactly one quantity, else None."""
    text = compact(text)
    found = _quantities(text)
    if len(found) == 1 and compact(found[0][2]).lower() == text.lower():
        return found[0][0], found[0][1]
    number = _bare_number(text)
    return (number, None) if number is not None else None


def _allowed_entry(value):
    """Classify one allowed value: ('exact', n, unit, text),
    ('band', lo, hi, lo_incl, hi_incl, unit, text) or ('text', norm, text)."""
    text = compact(value)
    low = text.lower()
    for pattern, shape in (
            (r'(?:below|under|up\s*to|less\s+than)\s+(.+)', 'lt'),
            (r'(?:above|over|more\s+than)\s+(.+)', 'gt'),
            (r'(.+?)\s*(?:&|and)\s*below', 'le'),
            (r'(.+?)\s*(?:&|and)\s*above', 'ge'),
            (r'(.+?)\s*\+', 'ge')):
        m = re.fullmatch(pattern, low)
        q = _single_quantity(m.group(1)) if m else None
        if q:
            n, unit = q
            lo, hi, lo_in, hi_in = {
                'lt': (None, n, False, False), 'gt': (n, None, False, False),
                'le': (None, n, False, True), 'ge': (n, None, True, False)}[shape]
            return ('band', lo, hi, lo_in, hi_in, unit, text)
    m = re.fullmatch(r'(.+?)\s*-\s*(.+)', low)
    if m:
        first, second = _single_quantity(m.group(1)), _single_quantity(m.group(2))
        if first and second and (first[1] is None or second[1] is None
                                 or first[1] == second[1]):
            unit = second[1] or first[1]
            return ('band', first[0], second[0], True, True, unit, text)
    q = _single_quantity(low)
    if q and q[1]:
        return ('exact', q[0], q[1], text)
    return ('text', _norm_filter_text(text), text)


def _in_band(entry, number):
    _kind, lo, hi, lo_in, hi_in, _unit, _text = entry
    if lo is not None and (number < lo or (number == lo and not lo_in)):
        return False
    if hi is not None and (number > hi or (number == hi and not hi_in)):
        return False
    return True


def _norm_filter_text(text):
    """Case, hyphens, '&'/'and' and the spacing around '+', '/', '&' folded."""
    text = compact(text).lower()
    text = re.sub(r'[-_–—]', ' ', text)
    text = re.sub(r'\band\b', '&', text)
    text = re.sub(r'\s*([+/&])\s*', r'\1', text)
    text = re.sub(r'(\d)\s*[x×*]\s*(?=\d)', r'\1x', text)   # 3840 x 2160
    return re.sub(r'\s+', ' ', text).strip()


def _text_hits(raw, entries):
    """Allowed text values stated in `raw` as whole words / phrases. A match
    that sits inside a longer match of ANOTHER value is dropped ('Inverter'
    inside 'Non-Inverter', 'BLDC' inside 'non BLDC')."""
    norm = _norm_filter_text(raw)
    found = []
    for entry in entries:
        _kind, value_norm, text = entry
        if not value_norm:
            continue
        for phrase in (value_norm,) + FILTER_VALUE_ALIASES.get(value_norm, ()):
            for m in re.finditer(r'(?<![a-z0-9])' + re.escape(phrase)
                                 + r'(?![a-z0-9])', norm):
                found.append((m.start(), m.end(), text))
    kept = [hit for hit in found
            if not any(o[2] != hit[2] and o[0] <= hit[0] and hit[1] <= o[1]
                       and (o[1] - o[0]) > (hit[1] - hit[0]) for o in found)]
    texts = {hit[2] for hit in kept}
    return [entry[2] for entry in entries if entry[2] in texts]


def snap_filter_matches(raw, allowed, unit=None):
    """[(allowed spelling, kind)] that `raw` states; kind is 'exact', 'band'
    or 'text'. Numbers compare numerically (1 == 1.0) in the axis unit, and
    are NEVER rounded: '1.2 Ton' against 1 / 1.5 / 2 Ton matches nothing."""
    entries = [_allowed_entry(v) for v in allowed or () if compact(v)]
    numeric = [e for e in entries if e[0] in ('exact', 'band')]
    texts = [e for e in entries if e[0] == 'text']
    if numeric:
        axis_unit = unit or next((e[2] if e[0] == 'exact' else e[5]
                                  for e in numeric
                                  if (e[2] if e[0] == 'exact' else e[5])), None)
        found = [(n, u) for n, u, _t in _quantities(raw)
                 if axis_unit is None or u == axis_unit]
        if not found and _bare_number(raw) is not None:
            found = [(_bare_number(raw), axis_unit)]
        for number, _u in found:
            for e in numeric:
                if e[0] == 'exact' and abs(e[1] - number) < 1e-6:
                    return [(e[3], 'exact')]
        if found:
            for e in numeric:
                if e[0] == 'band' and _in_band(e, found[0][0]):
                    return [(e[6], 'band')]
    if texts:
        return [(text, 'text') for text in _text_hits(raw, texts)]
    return []


def snap_filter_values(raw, allowed, unit=None):
    """Every allowed spelling `raw` states (a list, possibly empty)."""
    return [text for text, _kind in snap_filter_matches(raw, allowed, unit)]


def snap_filter_value(raw, allowed, unit=None):
    """`raw` (a scraped value or a title) snapped onto `allowed`: the ALLOWED
    spelling of what it states, several joined by ', ' when the text clearly
    states several textual values, or None. Pure; no I/O."""
    hits = snap_filter_values(raw, allowed, unit)
    return ', '.join(hits) if hits else None


def normalise_quantity(raw, unit):
    """A free-form value for a hint axis: the first `unit` quantity in `raw`
    written as number + UNIT_DISPLAY suffix ('1200 MM' -> '1200mm'), or None."""
    for number, found_unit, _text in _quantities(raw):
        if found_unit == unit:
            return f'{number:g}{UNIT_DISPLAY.get(unit, "")}'
    return None


def clean_filter_cell(cell):
    """A filter-sheet / list cell -> its values: split on ',', stripped,
    empties dropped (trailing commas), 'etc.' dropped, a '1 Star to 5 Star'
    range expanded, N/A and mojibake cells -> []."""
    text = compact(cell)
    if not text or text.upper() == 'N/A' or _MOJIBAKE_RE.search(text):
        return []
    expanded = _expand_range(text)
    if expanded:
        return expanded
    return [v for v in (compact(p) for p in text.split(','))
            if v and v.lower().rstrip('.') != 'etc']


def _expand_range(text):
    m = re.fullmatch(r'(\d+)\s*([a-z]+)\s+to\s+(\d+)\s*([a-z]+)', compact(text),
                     re.IGNORECASE)
    if not m or m.group(2).lower() != m.group(4).lower():
        return []
    lo, hi = int(m.group(1)), int(m.group(3))
    if not 0 < hi - lo <= 20:
        return []
    return [f'{n} {m.group(2)}' for n in range(lo, hi + 1)]


AxisSpec = namedtuple('AxisSpec', 'values hint unit free')
AxisSpec.__doc__ = ("One sub-category sheet cell: allowed values, the hint "
                    "text (cell was free text), the unit of its numbers, and "
                    "free=True when only a free-form number + unit can be "
                    "written (a hint with an 'e.g.' example and no list).")


def _values_unit(values):
    for value in values:
        entry = _allowed_entry(value)
        if entry[0] == 'exact':
            return entry[2]
        if entry[0] == 'band' and entry[5]:
            return entry[5]
    return None


def _embedded_list(text, hint_unit):
    """The value list a hint cell carries, if any: a tail after the hint
    ('...(e.g. 8L)- 6, 7, 8'), a list inside parentheses ('(Small, Medium,
    Jumbo)'), or a comma list around parenthesised names ('Electrical
    (RO+UV+UF), Non-Electrical (Gravity)'). Bare numbers inherit the unit."""
    items = []
    tail = re.search(r'\)\s*-?\s*([^()]+)$', text)
    if tail and re.search(r'\d', tail.group(1)) and ',' in tail.group(1):
        items = tail.group(1).split(',')
    if not items:
        for m in re.finditer(r'\(([^()]*)\)', text):
            inner = m.group(1)
            if ',' in inner and 'e.g' not in inner.lower():
                items = inner.split(',')
                break
    if not items and 'e.g' not in text.lower():
        outside = re.sub(r'\([^()]*\)', '', text)
        if ',' in outside:
            items = text.split(',')
    items = [compact(i) for i in items if compact(i)]
    split = []
    for item in items:                    # '75 cm 90 cm' is two values
        found = _quantities(item)
        split.extend([compact(t) for _n, _u, t in found] if len(found) > 1
                     else [item])
    items = split
    if items:
        last = re.fullmatch(_NUMBER + r'\s*(.+)', items[-1])
        suffix = last.group(2) if last and _bare_number(items[-1]) is None else ''
        out = []
        for item in items:
            if _bare_number(item) is not None:
                if suffix:
                    item = f'{item} {suffix}'
                elif hint_unit:
                    item = f'{item}{UNIT_DISPLAY.get(hint_unit, "")}'
            out.append(item)
        items = out
    return items


def parse_axis_cell(cell):
    """One 'Sub-Category Attributes' cell -> AxisSpec, or None (empty / N/A /
    mojibake: the axis does not apply)."""
    text = compact(cell)
    if not text or text.upper() == 'N/A' or _MOJIBAKE_RE.search(text):
        return None
    expanded = _expand_range(text)
    if expanded:
        return AxisSpec(tuple(expanded), '', _values_unit(expanded), False)
    if not ('e.g' in text.lower() or '(' in text or len(text) > 60):
        values = clean_filter_cell(text)
        return AxisSpec(tuple(values), '', _values_unit(values), False)
    example = re.search(r'e\.g\.?\s*([^)]*)\)', text, re.IGNORECASE)
    example_unit = None
    if example:
        found = _quantities(example.group(1))
        example_unit = found[0][1] if found else None
    values = _embedded_list(text, example_unit)
    unit = _values_unit(values) or example_unit
    return AxisSpec(tuple(values), text, unit, bool(not values and example_unit))


def _filter_axis_cell(_axis, cell):
    return parse_axis_cell(cell)


def load_subcategory_filter_axes(client=None):
    """{normalised sub-category: (parent, {axis: AxisSpec})} from the same
    'Sub-Category Attributes' sheet load_subcategory_attributes reads."""
    records, _source, _problems = _subcategory_sheet(client)
    axes = _index_subcategories(records, _filter_axis_cell)
    parents = {}
    for row in records:
        sub = row.get('Sub-Category', '')
        for key in [sub] + [compact(p) for p in sub.split(',')]:
            if key:
                parents.setdefault(_cat_norm(key), row.get('Parent Category', ''))
        parents.setdefault(_cat_norm(row.get('Parent Category')),
                           row.get('Parent Category', ''))
    return {key: (parents.get(key, ''), value) for key, value in axes.items()}


def read_filter_groups(path):
    """{filter group: [allowed values]} from ONE workbook's filter sheet, or
    None when the workbook has no filter sheet. Raises when unreadable."""
    _title, rows, col = find_filter_sheet(_workbook_rows(path))
    if rows is None:
        return None
    head = _first_row(rows)
    groups = {}
    for row in rows[rows.index(head) + 1:]:
        name = compact(row[col]) if col < len(row) else ''
        if not name or _MOJIBAKE_RE.search(name):
            continue
        cell = row[col + 1] if col + 1 < len(row) else ''
        groups[name] = clean_filter_cell(cell)
    return groups


def load_filter_groups(client=None):
    """{filter group name: ordered list of allowed values} for the client.

    Primary source: the attributes master's filter sheet (found by its
    'Filter Group' header, not by its sheet name). Fallback: the 'Filters'
    sheet of category_map.xlsx. {} when neither has one. Cells are cleaned by
    clean_filter_cell: trailing commas, N/A and mojibake cells dropped, a
    '1 Star to 5 Star' range expanded."""
    for path in attribute_workbooks(client):
        try:
            groups = read_filter_groups(path)
        except Exception:
            continue
        if groups is not None:
            return groups
    return {}


def attribute_sheet_differences(client=None):
    """Human-readable differences between the attributes master and
    category_map.xlsx copies of the shared sheets ('Sub-Category Attributes'
    and the filter sheet). [] when there is only one file, or no drift."""
    client = client or CLIENT
    master, catmap = attributes_master_path(client), category_map_path(client)
    if not master or not catmap or catmap.lower().endswith('.csv'):
        return []
    try:
        a, b = _workbook_rows(master), _workbook_rows(catmap)
    except Exception as exc:
        return [f'cannot compare the attribute workbooks: {exc}']
    out = []

    def cells(rows):
        return [tuple(compact(c) for c in r) for r in rows
                if any(compact(c) for c in r)]

    for label, finder in (
            (SUBCAT_SHEET, lambda s: find_subcategory_sheet(s)[1]),
            ('filter sheet', lambda s: find_filter_sheet(s)[1])):
        ra, rb = finder(a), finder(b)
        if ra is None and rb is None:
            continue
        if ra is None or rb is None:
            out.append(f"{label}: only in "
                       f"{os.path.basename(master if ra is not None else catmap)}")
            continue
        ca, cb = cells(ra), cells(rb)
        for i in range(max(len(ca), len(cb))):
            row_a = ca[i] if i < len(ca) else ()
            row_b = cb[i] if i < len(cb) else ()
            width = max(len(row_a), len(row_b))
            row_a += ('',) * (width - len(row_a))
            row_b += ('',) * (width - len(row_b))
            if row_a != row_b:
                diff = [j for j in range(width) if row_a[j] != row_b[j]]
                out.append(f'{label} row {i + 1}: master {row_a[diff[0]]!r} '
                           f'vs category_map {row_b[diff[0]]!r}'
                           + (f' (+{len(diff) - 1} more cells)'
                              if len(diff) > 1 else ''))
    return out


def filter_sheet_warning(client=None):
    diffs = attribute_sheet_differences(client)
    if not diffs:
        return ''
    return (f'The attributes master and category_map.xlsx disagree in '
            f'{len(diffs)} place(s) (the master wins; run '
            f'tools\\check_filter_sheet.py): ' + '; '.join(diffs[:3])
            + (' ...' if len(diffs) > 3 else ''))


# ---- the axis <-> filter group table ----------------------------------------
# Which store filter group (the filter sheet) each sub-category axis feeds,
# read off the real sheets. (parent or sub-category, axis, unit of the axis's
# values or None for any, filter groups). An axis with SEVERAL groups (AC
# Type -> technology AND type) sends each value to the group whose list holds
# it. A group missing from the client's filter sheet is simply skipped.
FILTER_AXIS_GROUPS = (
    ('air conditioner', 'Ton', None, ('AC Capacity',)),
    ('air conditioner', 'Star Rating', None, ('AC Star Rating',)),
    ('air conditioner', 'Type', None, ('AC Technology', 'AC Type', 'AC Mode')),
    ('led', 'Size', 'inch', ('TV Screen Size',)),
    ('led', 'Capacity', 'inch', ('TV Screen Size',)),
    ('led', 'Type', None, ('TV Smart Features',)),
    ('washing machines', 'Type', None, ('WM Function Type',)),
    ('washing machines', 'Capacity', 'kg', ('WM Washing Capacity',)),
    ('refrigerators', 'Type', None, ('Fridge Type',)),
    ('refrigerators', 'Capacity', 'l', ('Fridge Capacity',)),
    ('water purifiers', 'Type', None, ('WP Technology',)),
    ('water purifiers', 'Capacity', 'l', ('WP Storage Capacity',)),
)
# ---- filter groups no sub-category axis feeds (2026-11-19) ------------------
# Some store filter groups have no column in the sub-category sheet (TV
# resolution, WM colour, ...). They are fed straight from the scraped spec
# table (`labels`, canonical lower case, read first) and, when `use_title`,
# from the title, snapped onto the group's list in the client's filter sheet
# with the same snap functions. `where` is matched like FILTER_AXIS_GROUPS
# (normalised parent / sub-category / sheet parent). A group missing from the
# client's filter sheet is skipped. These groups are OPTIONAL: they never add
# to `_spec_gaps`. `note_labels`: the dedicated labels whose stated value that
# does not snap is written as scraped, with a "new value" note in
# `_filter_unmatched` (never the title; never a general feature list). `pick`: 'highest' keeps one value (the best stated
# resolution); 'specific' drops plain 'LED' when QLED/OLED/... is also hit.
DirectGroup = namedtuple('DirectGroup',
                         'where group labels use_title note_labels pick')
_TV_RES_LABELS = ('resolution', 'display resolution', 'screen resolution')
_DEFROST_LABELS = ('defrost system', 'defrosting type', 'defrost type',
                   'defrosting')
DIRECT_FILTER_GROUPS = (
    DirectGroup('led', 'TV Resolution', _TV_RES_LABELS, True, _TV_RES_LABELS,
                'highest'),
    DirectGroup('led', 'TV Display Tech',
                ('display technology', 'display type', 'panel type',
                 'screen type'), True,
                ('display technology', 'display type'), 'specific'),
    # numeric (Hz): a title only feeds it with a number directly followed by
    # Hz/Hertz, matched exactly - never rounded
    DirectGroup('led', 'TV Refresh Rate', ('refresh rate',), True,
                ('refresh rate',), None),
    DirectGroup('washing machines', 'WM Features',
                ('special features', 'features', 'key features', 'technology',
                 'motor type'), True, (), None),
    DirectGroup('washing machines', 'WM Color', ('colour', 'color'), True,
                ('colour', 'color'), None),
    DirectGroup('refrigerators', 'Fridge Defrosting', _DEFROST_LABELS, True,
                _DEFROST_LABELS, None),
)
_RESOLUTION_RANK = ('hd ready', 'full hd', '4k uhd', '8k')       # low -> high
# a scraped part that says the feature is ABSENT: 'No', 'Not available',
# 'without heater', 'Wi-Fi: No'
_NEGATIVE_PART_RE = re.compile(
    r'^\s*(?:no|none|nil|not|without|n/?a|absent)\b'
    r'|[:\-]\s*(?:no|none|not available|absent)\s*$', re.IGNORECASE)
# An axis no filter-sheet group covers is still a filter (the owner builds
# filters from the sub-category sheet); it is named from the axis and the unit
# of its values, so 'Capacity' in W is 'Power Rating', not 'Capacity'.
AXIS_FILTER_NAMES = {
    'Type': 'Product Type', 'Star Rating': 'Energy Rating', 'Ton': 'Tonnage',
    'Storage': 'Storage', 'Size': 'Size', 'Capacity': 'Capacity',
}
UNIT_FILTER_NAMES = {
    'w': 'Power Rating', 'm3h': 'Airflow', 'kva': 'Load Capacity',
    'spm': 'Stitch Speed', 'rpm': 'Motor Speed', 'mm': 'Sweep Size',
}
SUB_TYPE_FILTER_NAMES = {'fans': 'Motor Type',
                         'water purifiers': 'Installation Type'}
# spec-table labels (canonical, lower case) that can carry each kind of value
UNIT_SPEC_LABELS = {
    'w': ('power rating',), 'mm': ('sweep size', 'sweep', 'blade sweep'),
    'inch': ('screen size',), 'star': ('energy rating',),
    'ton': ('capacity', 'tonnage'), 'l': ('capacity',), 'kg': ('capacity',),
    'm3h': ('air flow', 'airflow', 'suction power', 'suction capacity', 'cadr'),
    'kva': ('capacity', 'load capacity', 'output capacity'),
    'spm': ('stitch speed',), 'rpm': ('speed', 'motor speed'),
}
GROUP_SPEC_LABELS = {
    'TV Smart Features': ('smart platform',),
    'AC Technology': ('compressor', 'compressor type'),
    'WP Technology': ('purification technology', 'filtration technology',
                      'purification method'),
    'Fridge Type': ('door type',),
}
SUB_TYPE_SPEC_LABELS = ('motor type', 'motor', 'installation type',
                        'mounting type', 'installation')

FilterHit = namedtuple('FilterHit', 'group value axis labels kind')
FilterHit.__doc__ = ("One filter attribute for one product: group (the "
                     "attribute name), value (allowed spelling), axis, the "
                     "spec labels that fed it, and kind 'exact' / 'band' / "
                     "'text' / 'free' / 'new' (a value the lists do not "
                     "hold, written as scraped - owner decision 2026-11-19).")
# the `axis` of a FilterHit from DIRECT_FILTER_GROUPS (no sub-category axis)
DIRECT_AXIS = '(direct)'


def filter_axes(sub, parent='', table=None):
    """(sheet parent, {axis: AxisSpec}) for this product - the same lookup
    rules as attribute_axes() (merged names are the union of their parts,
    bare parent only when the parent has a single row)."""
    table = SUBCATEGORY_FILTER_AXES if table is None else table
    hit = table.get(_cat_norm(sub))
    if hit and hit[1]:
        return hit
    parts = [table.get(_cat_norm(p)) for p in str(sub or '').split(',')]
    parts = [p for p in parts if p and p[1]]
    if len(parts) == 1:
        return parts[0]
    if parts:
        merged = {}
        for _parent, axes in parts:
            for axis, spec in axes.items():
                if axis not in merged:
                    merged[axis] = spec
                else:
                    old = merged[axis]
                    merged[axis] = AxisSpec(
                        old.values + tuple(v for v in spec.values
                                           if v not in old.values),
                        old.hint or spec.hint, old.unit or spec.unit,
                        old.free and spec.free)
        return parts[0][0], merged
    if not compact(sub) or _cat_norm(sub) == _cat_norm(parent):
        return table.get(_cat_norm(parent)) or ('', {})
    return '', {}


def _group_lookup(groups):
    return {name.lower(): name for name in groups}


def _category_keys(sub, parent, sheet_parent):
    """The normalised names a filter table's `where` is matched against."""
    return {_cat_norm(parent), _cat_norm(sub), _cat_norm(sheet_parent)}


def _direct_plan(sub, parent='', table=None, groups=None):
    """[(DirectGroup, group name as the client's sheet spells it, allowed
    values)] for one product: the DIRECT_FILTER_GROUPS whose `where` matches
    and whose group the client's filter sheet holds (with values)."""
    groups = FILTER_GROUPS if groups is None else groups
    lookup = _group_lookup(groups)
    sheet_parent, _axes = filter_axes(sub, parent, table)
    keys = _category_keys(sub, parent, sheet_parent)
    out = []
    for entry in DIRECT_FILTER_GROUPS:
        name = lookup.get(entry.group.lower())
        if entry.where in keys and name and groups.get(name):
            out.append((entry, name, list(groups[name])))
    return out


def _direct_parts(raw):
    """A scraped value / title split into its listed parts (',', ';', '|')."""
    return [p for p in (compact(x) for x in re.split(r'[,;|]', raw or '')) if p]


def _direct_matches(part, options):
    """snap_filter_matches for one part, but [] when the part says the
    value is ABSENT ('No Wi-Fi', 'Wi-Fi: No', 'without heater'). A negative
    word that is part of the value's own name ('No Frost' = Frost Free) is
    not a negation: the part is negative only when it snaps the same with
    the negative word removed."""
    found = snap_filter_matches(part, options)
    m = _NEGATIVE_PART_RE.search(part)
    if found and m:
        rest = compact(part[:m.start()] + ' ' + part[m.end():])
        if snap_filter_matches(rest, options) == found:
            return []
    return found


def _snap_direct(raw, options, pick=None):
    """[(allowed spelling, kind)] that `raw` states for a direct group, in
    the sheet's order, after the group's `pick` rule."""
    found = []
    for part in _direct_parts(raw):
        for text, kind in _direct_matches(part, options):
            if text not in [t for t, _k in found]:
                found.append((text, kind))
    if pick == 'specific' and len(found) > 1:
        found = [f for f in found if _norm_filter_text(f[0]) != 'led']
    if pick == 'highest' and len(found) > 1:
        def rank(item):
            norm = _norm_filter_text(item[0])
            return (_RESOLUTION_RANK.index(norm) if norm in _RESOLUTION_RANK
                    else -1, options.index(item[0]))
        found = [max(found, key=rank)]
    order = {v: i for i, v in enumerate(options)}
    return sorted(found, key=lambda f: order.get(f[0], len(order)))


def _direct_filter_hits(sub, parent, title, scraped, table, groups, done):
    """([FilterHit], [notes]) for the DIRECT_FILTER_GROUPS of one product.
    Scraped values under the group's labels first, then the title; the first
    source that gives a value wins. A value the list does not hold is WRITTEN
    as scraped (owner decision 2026-11-19, see _new_filter_value) with a
    "new value" note, but only from a dedicated label (note_labels) or, for a
    numeric group, a number in the group's unit - never a text value from the
    title, never from a negative ('No')."""
    hits, unmatched = [], []
    for entry, group, options in _direct_plan(sub, parent, table, groups):
        if group.lower() in done:
            continue
        labels = frozenset(entry.labels)
        sources = [(compact(v), compact(label).lower()) for label, v in scraped
                   if compact(label).lower() in labels and compact(v)]
        if entry.use_title and compact(title):
            sources.append((compact(title), None))
        unit = _values_unit(options)
        numeric = bool(unit)
        dedicated = frozenset(entry.note_labels)
        evidence = ''
        for raw, label in sources:
            value, kind, new = None, None, []
            if not numeric and label is not None and label in dedicated:
                matched, new = _text_parts(raw, (options,), group, label)
                if entry.pick == 'highest' and matched:
                    matched = [t for t, _k in _snap_direct(
                        ', '.join(matched), options, 'highest')]
                    new = []
                elif entry.pick == 'specific' and len(matched) > 1:
                    matched = [t for t in matched
                               if _norm_filter_text(t) != 'led']
                if matched or new:
                    value = ', '.join(matched + new)
                    kind = 'new' if new else 'text'
            else:
                found = _snap_direct(raw, options, entry.pick)
                if found:
                    value, kind = ', '.join(t for t, _k in found), found[0][1]
                elif numeric:
                    quantity = _new_quantity(raw, unit, options)
                    if quantity:
                        value, kind, new = quantity, 'new', [quantity]
            if value:
                hits.append(FilterHit(group, value, DIRECT_AXIS, labels, kind))
                done.add(group.lower())
                for part in new:
                    note = new_value_note(group, part)
                    if note not in unmatched:
                        unmatched.append(note)
                evidence = ''
                break
            if (not evidence and label in entry.note_labels
                    and not _only_empty_words(raw, group)):
                evidence = raw
        if evidence:
            note = f"{group}: '{evidence[:60]}' not in list"
            if note not in unmatched:
                unmatched.append(note)
    return hits, unmatched


# ---- new values (owner decision 2026-11-19) ---------------------------------
# A scraped value is never replaced, rounded or dropped because the filter
# list does not hold it; only its spelling is made regular. These words are
# never a filter value for any group (they say nothing about the product), and
# 'Smart TV' is not a TV platform: the platform is written only when the page
# names one (Android TV, Google TV, WebOS, Tizen, ...).
NEVER_FILTER_VALUES = frozenset(('yes', 'no', 'na', 'n/a', 'not available'))
NEVER_FILTER_VALUES_BY_GROUP = {
    'tv smart features': frozenset(('smart tv', 'smart')),
}
# a text value written as scraped is at most this long
NEW_TEXT_VALUE_MAX = 40
# spec labels that are too general to be DEDICATED to one text group: the
# canonical 'Configuration' also holds 'Product Type' ('Refrigerator'), and
# 'Motor' holds '75 W copper motor'
GENERAL_TEXT_LABELS = frozenset(('configuration', 'type', 'motor'))


def new_value_note(group, value):
    """The `_filter_unmatched` note for a value written but not in the sheet."""
    return f"{group}: '{value}' is a new value, add it to the sheet"


def _empty_word(group, text):
    low = compact(text).lower().strip(' .')
    return (low in NEVER_FILTER_VALUES
            or low in NEVER_FILTER_VALUES_BY_GROUP.get(group.lower(), ()))


def _only_empty_words(raw, group):
    """True when every part of `raw` is a negative or a never-a-value word."""
    return all(_NEGATIVE_PART_RE.search(p) or _empty_word(group, p)
               for p in _direct_parts(raw))


def _list_suffix(options, unit):
    """How the group's own list writes `unit` after a number: '1.5 Ton' ->
    ' Ton', '32"' -> '"', '60Hz' -> 'Hz', '6kg & below' -> 'kg'. Exact values
    first, then bands; UNIT_DISPLAY when the list has none."""
    for wanted in ('exact', 'band'):
        for value in options:
            text = compact(value)
            if _allowed_entry(text)[0] != wanted:
                continue
            for m in re.finditer(_NUMBER + r'(\s*[^\d\s&+\-,.]+)', text):
                suffix = m.group(2)
                found = _quantities('1' + suffix)
                if (len(found) == 1 and found[0][1] == unit
                        and not re.search(r'tb', suffix, re.IGNORECASE)):
                    return suffix
    return UNIT_DISPLAY.get(unit, '')


def _new_quantity(raw, unit, options):
    """The first quantity in `unit` that `raw` states, written as number +
    the list's own suffix ('0.8 T' against '1 Ton, ...' -> '0.8 Ton'), or
    None. The number is the stated one: never rounded."""
    if not unit:
        return None
    for number, found_unit, _text in _quantities(raw):
        if found_unit == unit:
            return f'{number:g}{_list_suffix(options, unit)}'
    return None


def _text_parts(raw, option_lists, group, label):
    """([allowed spellings], [new parts]) for a scraped value under a label
    dedicated to a text group. Each listed part ('A, B; C') snaps onto the
    first list that holds it; a part that snaps nowhere is kept as scraped
    (whitespace compacted) when it is not a negative, not a never-a-value
    word, passes the content firewall and is at most NEW_TEXT_VALUE_MAX
    characters."""
    matched, new = [], []
    for part in _direct_parts(raw):
        found = []
        for options in option_lists:
            found = _direct_matches(part, options) if options else []
            if found:
                break
        if found:
            matched.extend(t for t, _k in found if t not in matched)
            continue
        if (_NEGATIVE_PART_RE.search(part) or _empty_word(group, part)
                or len(part) > NEW_TEXT_VALUE_MAX
                or not content_ok(part) or not spec_row_ok(label, part)):
            continue
        if part.lower() not in [n.lower() for n in new]:
            new.append(part)
    return matched, new


def _dedicated_labels(group, mapped, labels):
    """The spec labels whose unlisted text value may be written for `group`:
    the group's own name and GROUP_SPEC_LABELS; on a one-group axis also the
    axis's labels, minus the general ones. Never the title."""
    own = {group.lower(), *GROUP_SPEC_LABELS.get(group, ())}
    if len(mapped) > 1:
        return frozenset(own)
    return frozenset((set(labels) | own) - GENERAL_TEXT_LABELS)


def _filter_plan(sub, parent='', table=None, groups=None):
    """[(axis, spec, [mapped filter-sheet groups], derived name)] for one
    product. Mapped groups are those of FILTER_AXIS_GROUPS present in the
    client's filter sheet; `derived` names the axis when no group maps, and
    receives the values only the sub-category row lists on a multi-group
    axis."""
    groups = FILTER_GROUPS if groups is None else groups
    lookup = _group_lookup(groups)
    sheet_parent, axes = filter_axes(sub, parent, table)
    keys = _category_keys(sub, parent, sheet_parent)
    plan = []
    for axis, spec in axes.items():
        mapped = []
        for where, ax, unit, names in FILTER_AXIS_GROUPS:
            if ax == axis and where in keys and unit in (None, spec.unit):
                mapped = [lookup[n.lower()] for n in names if n.lower() in lookup]
                break
        plan.append((axis, spec, mapped, _derived_filter_name(axis, spec, sub)))
    return plan


def _derived_filter_name(axis, spec, sub):
    if axis == SUB_TYPE_AXIS:
        return SUB_TYPE_FILTER_NAMES.get(_cat_norm(sub), 'Sub Type')
    if axis in ('Capacity', 'Size') and spec.unit in UNIT_FILTER_NAMES:
        return UNIT_FILTER_NAMES[spec.unit]
    if axis == 'Capacity' and spec.unit == 'gb':
        return 'RAM'
    if spec.unit == 'inch' and axis in ('Capacity', 'Size'):
        words = (spec.hint + ' ' + ' '.join(spec.values)).lower()
        return 'Screen Size' if re.search(r'screen|display', words) else 'Size'
    return woo_attribute_name(AXIS_FILTER_NAMES.get(axis, axis))


def _axis_source_labels(axis, spec, groups):
    if axis == SUB_TYPE_AXIS:
        return set(SUB_TYPE_SPEC_LABELS)
    labels = {axis.lower(), *AXIS_SPEC_LABELS.get(axis, ())}
    labels |= set(UNIT_SPEC_LABELS.get(spec.unit, ()))
    for group in groups:
        labels |= {group.lower(), *GROUP_SPEC_LABELS.get(group, ())}
    if spec.unit == 'gb':
        if axis == 'Capacity':
            labels = {'ram', 'memory', 'ram size'}
        else:
            labels |= {'storage', 'internal storage', 'rom'}
    return labels


def _evidence(raw, spec, numeric):
    """What a source states for an axis, for `_filter_unmatched`: a scraped
    value as written; for a title, only a number in the axis unit."""
    if numeric and spec.unit:
        for _n, unit, text in _quantities(raw):
            if unit == spec.unit:
                return compact(text)
    return ''


def filter_attributes(sub, parent, title, scraped=(), table=None, groups=None):
    """The product's filter attributes: ([FilterHit], [notes]).

    Per axis of the product's sub-category row: the value comes from the
    scraped spec table (the labels that carry that axis), else from the
    title; it is snapped onto the filter group's list first (its spelling
    wins), then onto the sub-category row's list. Snapped -> a FilterHit
    named by the filter group.

    Owner decision 2026-11-19: a stated value the lists do not hold is
    WRITTEN, never replaced, rounded or dropped; only its spelling is made
    regular. A number in the axis unit (scraped label first, else the title)
    is written as number + the list's own suffix ('0.8 T' -> '0.8 Ton'); a
    text value under a label dedicated to the group is written as scraped
    (see _text_parts). Each such value gets a note "<Group>: '<value>' is a
    new value, add it to the sheet". A stated value that cannot be written
    (no unit, firewall, too long) gets the note "... not in list". Nothing
    stated -> nothing (that is what `_spec_gaps` reports)."""
    groups = FILTER_GROUPS if groups is None else groups
    hits, unmatched, done = [], [], set()
    for axis, spec, mapped, derived in _filter_plan(sub, parent, table, groups):
        labels = _axis_source_labels(axis, spec, mapped)
        scraped_rows = [(compact(v), compact(label).lower())
                        for label, v in scraped
                        if compact(label).lower() in labels and compact(v)]
        sources = scraped_rows + [(compact(title), None)]
        numeric = bool(spec.unit) or any(
            _allowed_entry(v)[0] != 'text'
            for g in mapped for v in groups.get(g, ()))
        targets = mapped or [derived]
        if not mapped and not spec.values and not spec.free:
            continue                     # a hint with no list and no format
        written = False
        evidence = ''
        for group in targets:
            if group.lower() in done:
                continue
            options = list(groups.get(group, ())) if mapped else []
            # one group per axis: the row's own list is a valid fallback;
            # several groups: each takes only values from its OWN list
            subvals = list(spec.values) if len(targets) == 1 else []
            listed = options + [v for v in subvals if v not in options]
            unit = spec.unit or _values_unit(listed)
            group_numeric = bool(unit)
            dedicated = _dedicated_labels(group, mapped, labels)
            for raw, label in sources:
                value, kind, new = None, None, []
                if (listed and not group_numeric and label is not None
                        and label in dedicated):
                    matched, new = _text_parts(raw, (options, subvals),
                                               group, label)
                    if matched or new:
                        value = ', '.join(matched + new)
                        kind = 'new' if new else 'text'
                else:
                    found = snap_filter_matches(raw, options, spec.unit) or \
                        snap_filter_matches(raw, subvals, spec.unit)
                    if found:
                        value = ', '.join(t for t, _k in found)
                        kind = found[0][1]
                    elif listed and group_numeric:
                        quantity = _new_quantity(raw, unit, listed)
                        if quantity:
                            value, kind, new = quantity, 'new', [quantity]
                if value:
                    hits.append(FilterHit(group, value, axis,
                                          frozenset(labels), kind))
                    done.add(group.lower())
                    for part in new:
                        note = new_value_note(group, part)
                        if note not in unmatched:
                            unmatched.append(note)
                    written = True
                    break
                if not evidence:
                    if label is None:
                        evidence = _evidence(raw, spec, numeric)
                    elif not _only_empty_words(raw, group):
                        evidence = raw
        if len(targets) > 1 and spec.values and derived.lower() not in done:
            # values only the sub-category row lists ('Hot & Cold') go to the
            # axis's own name, unless a mapped group already holds them
            for raw, _is_scraped in sources:
                extra = [t for t in snap_filter_values(raw, spec.values, spec.unit)
                         if not any(snap_filter_values(t, groups.get(g, ()))
                                    for g in mapped)]
                if extra:
                    hits.append(FilterHit(derived, ', '.join(extra), axis,
                                          frozenset(labels), 'text'))
                    done.add(derived.lower())
                    written = True
                    break
        if written:
            continue
        group = targets[0]
        if (spec.free and not mapped and spec.unit
                and group.lower() not in done):
            for raw, _is_scraped in sources:
                value = normalise_quantity(raw, spec.unit)
                if value:
                    hits.append(FilterHit(group, value, axis,
                                          frozenset(labels), 'free'))
                    done.add(group.lower())
                    written = True
                    break
        if (not written and evidence and group.lower() not in done
                and not (spec.free and not mapped)):
            name = group if group == axis else f'{group} ({axis})'
            note = f"{name}: '{evidence[:60]}' not in list"
            if note not in unmatched:
                unmatched.append(note)
    # the filter groups no sub-category axis feeds (DIRECT_FILTER_GROUPS)
    direct, direct_unmatched = _direct_filter_hits(sub, parent, title, scraped,
                                                   table, groups, done)
    hits.extend(direct)
    unmatched.extend(n for n in direct_unmatched if n not in unmatched)
    return hits, unmatched


def filter_allowed_for(sub, parent='', table=None, groups=None):
    """{filter group name: set of allowed lower-case spellings, or None for
    a free-form group} for one product. A value is VALID when the filter
    sheet's group lists it or the product's sub-category row does."""
    groups = FILTER_GROUPS if groups is None else groups
    out = {}
    for axis, spec, mapped, derived in _filter_plan(sub, parent, table, groups):
        own = {v.lower() for v in spec.values}
        if mapped:
            for group in mapped:
                bucket = out.setdefault(group, set())
                if bucket is not None:
                    bucket |= {v.lower() for v in groups.get(group, ())}
                    if len(mapped) == 1:
                        bucket |= own
            if len(mapped) > 1 and own:
                bucket = out.setdefault(derived, set())
                if bucket is not None:
                    bucket |= own
        elif spec.free:
            out[derived] = None
        elif own:
            bucket = out.setdefault(derived, set())
            if bucket is not None:
                bucket |= own
    for _entry, group, options in _direct_plan(sub, parent, table, groups):
        bucket = out.setdefault(group, set())
        if bucket is not None:
            bucket |= {v.lower() for v in options}
    return out


def filter_group_names(client=None):
    """Every attribute name a filter attribute can carry for this client: the
    filter-sheet groups the axis table maps, the derived names of the axes no
    group covers, and the DIRECT_FILTER_GROUPS the client's sheet holds.
    wc_publish.py sends these as GLOBAL attributes."""
    if client is None or client is CLIENT:
        table, groups = SUBCATEGORY_FILTER_AXES, FILTER_GROUPS
    else:
        table, groups = load_subcategory_filter_axes(client), load_filter_groups(client)
    names = set()
    for key, (parent, _axes) in table.items():
        for _axis, spec, mapped, derived in _filter_plan(key, parent, table,
                                                         groups):
            if mapped:
                names.update(mapped)
                if len(mapped) > 1 and spec.values:
                    names.add(derived)
            elif spec.values or spec.free:
                names.add(derived)
    # the directly-fed groups the client's filter sheet holds
    lookup = _group_lookup(groups)
    for entry in DIRECT_FILTER_GROUPS:
        name = lookup.get(entry.group.lower())
        if name and groups.get(name):
            names.add(name)
    return sorted(names)


FILTER_GROUPS = load_filter_groups()
SUBCATEGORY_FILTER_AXES = load_subcategory_filter_axes()
FILTER_SHEET_WARNING = filter_sheet_warning()


def parse_scraped_specs(cell, brand=''):
    """'Label: Value | Label: Value' -> [(canonical label, value)].

    The inverse of sku_enricher.format_specs. Every row passes the content
    firewall on its raw AND its canonical label; `brand` is the product's own
    brand, which is never treated as a retailer name.
    """
    rows, seen = [], set()
    for chunk in str(cell or '').split('|'):
        if ':' not in chunk:
            continue
        raw_label, value = chunk.split(':', 1)
        label, value = canon_spec_label(raw_label), compact(value)
        key = label.lower()
        if not label or not value or key in seen:
            continue
        if len(label) > 40 or len(value) > 120 or label.lower() == value.lower():
            continue
        if not (spec_row_ok(compact(raw_label), value, brand=brand)
                and spec_row_ok(label, value, brand=brand)):
            continue                      # retailer content, not a product fact
        if bare_platform_row(raw_label, value) or bare_platform_row(label, value):
            continue                      # 'Operating System: Smart TV' names no platform
        seen.add(key)
        rows.append((label, value))
    return rows


# WooCommerce turns every attribute name into a GLOBAL attribute taxonomy
# whose slug is capped at 28 characters and must not collide with a term
# WordPress reserves for itself - break either rule and the importer rejects
# the whole product row ('Slug "..." is too long' / 'reserved term').
# Scraped retailer spec labels break both, so every label goes through
# woo_attribute_name() before it becomes an attribute or a spec row.
WOO_ATTR_SLUG_MAX = 28
# Known scraped labels that need a human rename, not a mechanical trim.
ATTRIBUTE_RENAMES = {
    'display backlight configuration': 'Backlight Type',
    'picture quality enhancement technology': 'Picture Technology',
    'motion enhancement technology': 'Motion Technology',
    'category': 'Product Category',
}
# WordPress query vars + built-in taxonomies (what WooCommerce's
# wc_check_if_attribute_name_is_reserved refuses).
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
# Words a spec-table label can lose without losing its meaning.
_ATTR_FILLER_WORDS = frozenset((
    'technology', 'technologies', 'configuration', 'specification',
    'specifications', 'feature', 'features', 'capability', 'capabilities',
    'system', 'details', 'information',
))


def woo_slug(label):
    """How WooCommerce derives an attribute taxonomy slug from its name."""
    return re.sub(r'[^a-z0-9]+', '-', compact(label).lower()).strip('-')


def woo_attribute_name(label):
    """Shorten/rename a label until its WooCommerce slug is importable.

    Curated renames first; otherwise drop filler words, then trailing words,
    until the slug fits WOO_ATTR_SLUG_MAX. A reserved slug gets a 'Product '
    prefix. The loop re-checks both rules because fixing one can expose the
    other (shortening can land ON a reserved word; prefixing adds length)."""
    label = compact(label)
    if not label:
        return label
    label = ATTRIBUTE_RENAMES.get(label.lower(), label)
    for _ in range(3):
        if len(woo_slug(label)) > WOO_ATTR_SLUG_MAX:
            words = [w for w in label.split()
                     if w.lower() not in _ATTR_FILLER_WORDS]
            label = ' '.join(words) or label
            while len(woo_slug(label)) > WOO_ATTR_SLUG_MAX:
                words = label.split()
                label = (' '.join(words[:-1]) if len(words) > 1
                         else label[:WOO_ATTR_SLUG_MAX])
        if woo_slug(label) in RESERVED_ATTRIBUTE_SLUGS:
            label = f'Product {label}'
        else:
            break
    return label


def product_specs(title, brand, category, scraped=(), filters=()):
    """The (label, value) rows shown on the product page and exported as
    WooCommerce attributes.

    KI-14: only facts actually present in the listing title get a row. Anything
    that could not be read is omitted rather than filled with 'Standard ...',
    and the labels follow the product's own category - the previous version
    named every attribute 'WM ...' (washing machine) regardless of what the
    product was.

    `scraped` carries the retailer's own spec table. It fills labels the title
    could not supply; it never replaces one it could.

    Every row passes the content firewall (content_ok on label and value).
    """
    return [(label, value) for label, value, _origin
            in _spec_rows(title, brand, category, scraped, filters)]


def _repeats_filter(label, value, filters):
    """True when (label, value) states the same fact as a filter attribute
    already written: its label is one the hit was read from and its value
    snaps EXACTLY to the hit's value. A band hit ('Below 200L', '6.1-7kg')
    NEVER swallows its source row: the exact figure ('190 L', '7 kg') is
    different information and stays as its own (non-global) attribute."""
    low = compact(label).lower()
    for hit in filters:
        if hit.kind == 'band' or not (low in hit.labels
                                      or low == hit.group.lower()):
            continue
        if hit.kind == 'new' or (hit.axis == DIRECT_AXIS
                                 and hit.kind == 'text'):
            # a value written as scraped ('0.8 Ton', 'VIDAA') is matched
            # part by part, like a direct group's value
            # a direct group's source row ('Resolution: 4K Ultra HD',
            # 'Colour: Gray') is the same fact when every part of it snaps
            # onto the hit's own values; a row that also says more
            # ('Steam Wash, Child Lock') is kept
            stated = [v for v in hit.value.split(', ') if v]
            snapped = [_snap_direct(p, stated) for p in _direct_parts(value)]
            if snapped and all(snapped) and {t for s in snapped
                                             for t, _k in s} == set(stated):
                return True
            continue
        if hit.kind == 'text':
            if _norm_filter_text(value) == _norm_filter_text(hit.value):
                return True
        elif snap_filter_value(value, [hit.value]) == hit.value:
            return True
    return False


def _spec_rows(title, brand, category, scraped=(), filters=()):
    """product_specs() rows tagged with where each came from: 'filter' (a
    filter attribute, see filter_attributes - always FIRST, so the
    MAX_ATTRIBUTES cap can never cut one), 'brand', 'core' (derived from the
    title) or 'scraped' (the retailer's spec table). key_points() ranks on
    the tag; the attribute list ignores it."""
    specs = spec_tags(title)
    rows, used = [], set()
    origin = ['filter']

    def add(label, value):
        label, value = woo_attribute_name(label), compact(value)
        # one row per LABEL: a WooCommerce product with two attributes of the
        # same name renders both, and the second is always the worse one
        if not (label and value) or label.lower() in used:
            return
        # a platform/OS label with a bare 'Smart TV' / 'Smart' / 'Yes' names
        # no platform: write nothing (owner decision 2026-11-19)
        if bare_platform_row(label, value):
            return
        # the content firewall: no retailer-owned text in any attribute
        if not (content_ok(label, brand=brand) and content_ok(value, brand=brand)):
            return
        # a row the filter attribute already carries is not written twice
        if origin[0] != 'filter' and _repeats_filter(label, value, filters):
            return
        used.add(label.lower())
        rows.append((label, value, origin[0]))

    for hit in filters:
        add(hit.group, hit.value)
    origin[0] = 'brand'
    add('Brand', brand)
    origin[0] = 'core'

    cap_label = capacity_label(category, title)
    cap_value = capacity_value(title, category)
    if not cap_value:
        # the category-specific reader found nothing - fall back to whatever
        # measurement the title does carry, labelled for what it actually is
        for key, label in (('volume', 'Capacity'), ('weight', 'Capacity'),
                           ('screen', 'Screen Size'), ('ton', 'Capacity')):
            if key in specs:
                cap_label, cap_value = label, specs[key]
                break
    add(cap_label, cap_value)

    add('Screen Size', specs.get('screen'))
    add('Resolution', specs.get('resolution'))
    add('Smart Platform', specs.get('platform'))
    add('Energy Rating', specs.get('star'))
    add('Configuration', configuration_text(title, category))
    add('Power Rating', wattage(title))

    origin[0] = 'scraped'
    for label, value in scraped:
        if len(rows) >= MAX_ATTRIBUTES:
            break
        add(label, value)
    return rows


def axis_labels(sub, parent=''):
    """The sub-category's attribute whitelist as spec labels, in sheet order:
    [(axis, {lower-case labels that satisfy it})]. Empty when the taxonomy
    declares no axes for this product."""
    return [(axis, {axis.lower(), *AXIS_SPEC_LABELS.get(axis, ())})
            for axis in attribute_axes(sub, parent)
            if axis not in OPTIONAL_AXES]


def key_points(tagged_rows, sub='', parent=''):
    """At most KEY_POINTS_MAX relevant (label, value) points for the three
    description fields, from _spec_rows() output. The Brand row is excluded
    (the templates add their own).

    Relevance order: (0) the filter attributes (origin 'filter'), in sheet
    axis order; (1) rows whose label is on the sub-category's attribute
    whitelist, in sheet order; (2) the core rows derived from the title, in
    product_specs order; (3) other scraped rows - left OUT when the
    sub-category has a whitelist. Fewer than KEY_POINTS_MAX relevant points
    means fewer points: the list is never padded."""
    candidates = [(label, value, origin) for label, value, origin in tagged_rows
                  if origin != 'brand']
    whitelist = axis_labels(sub, parent)
    picked = []

    def take(row):
        if row not in picked and len(picked) < KEY_POINTS_MAX:
            picked.append(row)

    for row in candidates:
        if row[2] == 'filter':
            take(row)
    for _axis, labels in whitelist:
        for row in candidates:
            if row[0].lower() in labels:
                take(row)
    for row in candidates:
        if row[2] == 'core':
            take(row)
    if not whitelist:
        for row in candidates:
            take(row)
    return [(label, value) for label, value, _origin in picked]


def _firewall_rows(rows, brand):
    """Last gate before a description is rendered: drop any ROW (not the whole
    field) whose label or value still fails content_ok."""
    return [(label, value) for label, value in rows
            if content_ok(label, brand=brand) and content_ok(value, brand=brand)]


def build_descriptions(sku, title, brand, category, scraped=(), sub='',
                       parent='', filters=()):
    """Returns (key-features HTML, short description, long description, specs).

    The spec rows (all of them - they become the WooCommerce attributes) are
    returned directly instead of being recovered by regexing the HTML this
    function just produced - that round-trip through a string was where the
    wrong attribute names came from.

    The three description fields show only key_points(): at most
    KEY_POINTS_MAX relevant points for this `sub`-category. Nothing is written
    that was not scraped or read off the title (no invented "230 V / 50 Hz").
    """
    tagged = _spec_rows(title, brand, category, scraped, filters)
    rows = [(label, value) for label, value, _origin in tagged]
    points = _firewall_rows(key_points(tagged, sub, parent), brand)
    brand_rows = _firewall_rows([(label, value) for label, value, origin
                                 in tagged if origin == 'brand'], brand)

    # Paragraph, not a bullet list: the storefront renders this inline under the
    # title, where a <ul> broke the flow. Still one sentence per VERIFIED spec
    # pair - nothing is written here that product_specs() did not produce, so a
    # missing spec stays missing instead of being smoothed over with prose.
    feature_rows = brand_rows + points
    configuration_points = compact(
        '<p class="product-key-features">'
        + ' '.join(f'{label}: {value}.' for label, value in feature_rows)
        + '</p>'
    ) if feature_rows else ''
    short_desc = html_block(_firewall_rows([
        ('Brand', brand),
        ('Product SKU', sku),
    ] + points, brand))
    long_desc = html_block(_firewall_rows([
        ('Brand Designation', brand),
        ('Model Identifier Code', sku),
        ('Title', title),
        ('Categories', category),
    ] + points, brand))
    return configuration_points, short_desc, long_desc, rows


# -------------------------- SEO meta (item 8, OPT-IN) -------------------------
# OFF BY DEFAULT. Turn it on with the checkbox in the app or `--seo` on
# build_csv.py. Nothing about the existing output changes while it is off - the
# columns are simply absent.
#
# Meta title and description are NOT WooCommerce fields; core needs only a
# name, a SKU and a price. They belong to an SEO plugin and are stored as
# ordinary WordPress post meta, which is why they ride as `Meta: <key>`
# columns: that prefix is what tells the WooCommerce CSV importer to write post
# meta, so no plugin-specific tooling is needed. wc_publish.py sends the same
# keys through the REST API's meta_data array.
#
# Everything generated here is DERIVED FROM THE ROW - the title, the category,
# the specs, the two prices. No LLM, no invented claims (no "free delivery",
# no "best price"), and the same product always produces the same text.
#
# `slug` is the one column the CSV importer cannot use (it has no slug field);
# it is sent on the API route only.

SEO_SITE_NAME = CLIENT.seo_site_name
SEO_TITLE_MAX = 60           # what Google renders before truncating
SEO_DESC_MAX = 155
SEO_SLUG_MAX = 70

SEO_META_KEYS = {
    'yoast': {
        'title': 'Meta: _yoast_wpseo_title',
        'description': 'Meta: _yoast_wpseo_metadesc',
        'focus': 'Meta: _yoast_wpseo_focuskw',
    },
    'rankmath': {
        'title': 'Meta: rank_math_title',
        'description': 'Meta: rank_math_description',
        'focus': 'Meta: rank_math_focus_keyword',
    },
}

# marketing parentheses that eat the character budget without adding a keyword
SEO_NOISE_RE = re.compile(r'\((?:[^()]*\b(?:20\d\d|model|pack|combo)\b[^()]*)\)',
                          re.IGNORECASE)


def seo_plugin_keys(plugin):
    """Column names for one plugin, or both sets for 'both'."""
    if plugin == 'both':
        return [SEO_META_KEYS['yoast'], SEO_META_KEYS['rankmath']]
    return [SEO_META_KEYS.get(plugin, SEO_META_KEYS['yoast'])]


def seo_columns(plugin):
    names = ['slug', 'category focus keyword', 'category seo keywords']
    for keys in seo_plugin_keys(plugin):
        names.extend([keys['title'], keys['description'], keys['focus']])
    return names


def trim_to(text, limit):
    """Cut on a word boundary, never mid-word and never leaving a dangling
    separator. A meta description chopped at exactly 155 bytes reads like a
    fault; this reads like a sentence that ended."""
    text = compact(text)
    if len(text) <= limit:
        return text
    cut = text[:limit + 1]
    space = cut.rfind(' ')
    out = (cut[:space] if space > limit * 0.6 else text[:limit]).rstrip()
    return out.rstrip(' ,;:-|/&').rstrip()


def model_code(sku):
    """The manufacturer's model code inside a prefixed SKU, for the slug and
    the focus keyword. 'WM-SAMSUNG-WW80T504DAB' -> 'WW80T504DAB'."""
    text = re.sub(r'^(?:' + '|'.join(re.escape(p) for p in SKU_PREFIXES) + r')-',
                  '', compact(sku))
    tokens = [t for t in re.split(r'[-\s_/]+', text)
              if len(t) >= 4 and re.search(r'\d', t) and re.search(r'[A-Za-z]', t)]
    tokens.sort(key=len, reverse=True)
    return tokens[0] if tokens else ''


def slugify(text):
    text = re.sub(r'[^a-z0-9]+', '-', compact(text).lower())
    return re.sub(r'-{2,}', '-', text).strip('-')


def seo_slug(sku, title):
    """A readable, stable URL. The model code is appended when the title does
    not already carry it, so two variants of one product cannot collide and
    have WordPress silently append '-2'."""
    base = slugify(SEO_NOISE_RE.sub('', compact(title))) or slugify(sku)
    code = slugify(model_code(sku))
    if code and code not in base:
        base = f'{base}-{code}'
    if len(base) <= SEO_SLUG_MAX:
        return base
    # trim whole words off the middle, keeping the model code at the end
    parts = base.split('-')
    if code:
        tail = code.split('-')
        head = parts[:len(parts) - len(tail)]
        while head and len('-'.join(head + tail)) > SEO_SLUG_MAX:
            head.pop()
        return '-'.join(head + tail).strip('-') or code
    while parts and len('-'.join(parts)) > SEO_SLUG_MAX:
        parts.pop()
    return '-'.join(parts).strip('-')


def seo_title(title, category_leaf=''):
    """<= 60 characters, brand-led, ending in the store name when it fits."""
    head = compact(SEO_NOISE_RE.sub('', compact(title)))
    tail = f' | {SEO_SITE_NAME}'
    if len(head) + len(tail) <= SEO_TITLE_MAX:
        return head + tail
    # the store name is worth less than the product words; drop it before
    # mangling the product name, and only then trim
    if len(head) <= SEO_TITLE_MAX:
        return head
    return trim_to(head, SEO_TITLE_MAX)


def seo_description(title, brand, category_hierarchy, spec_rows,
                    regular_price, sale_price):
    """<= 155 characters of facts already in the row: what it is, what it
    costs, its headline specs. Nothing here is a claim we cannot back.

    Assembled by PRIORITY, not by order. 155 characters is not enough for
    everything, and the price is worth more in a search result than a third
    spec - so each slot below offers progressively shorter variants and the
    first one that still fits is taken. A slot that cannot fit is skipped
    rather than ending the description, which is why the store name can appear
    on a short product and the price never gets crowded out by specs.
    """
    leaf = compact(str(category_hierarchy).split('>')[-1]) or 'appliance'

    sale = format_price_output(sale_price)
    regular = format_regular_price_output(regular_price)
    price_bit = ''
    if single_price_mode():
        # [pricing] single_price: ONE price, never an MRP next to it, never a
        # saving. The pair is normalised here too, so a caller that passes
        # the raw two values still gets one number.
        one, _ = single_price_pair(regular_price, sale_price)
        one = format_regular_price_output(one)
        if one:
            price_bit = f'Price Rs {one:,}.'
    elif sale:
        price_bit = f'Price Rs {sale:,}'
        if regular and regular > sale:
            price_bit += f' (MRP Rs {regular:,})'
        price_bit += '.'

    highlights = [f'{label} {value}' for label, value in spec_rows
                  if label in ('Screen Size', 'Capacity', 'Washing Capacity',
                               'Tank Volume', 'Energy Rating', 'Resolution',
                               'Smart Platform', 'Configuration')]

    slots = [
        [f'{compact(title)}.'],
        [price_bit] if price_bit else [],
        [', '.join(highlights[:n]) + '.' for n in (3, 2, 1) if highlights[:n]],
        [f'Shop {leaf} at {SEO_SITE_NAME}.', f'At {SEO_SITE_NAME}.'],
    ]

    out = ''
    for variants in slots:
        for variant in variants:
            candidate = f'{out} {variant}'.strip() if out else variant
            if len(candidate) <= SEO_DESC_MAX:
                out = candidate
                break
    return out or trim_to(f'{compact(title)}.', SEO_DESC_MAX)


def split_hierarchy(category_hierarchy):
    """'LED > Mini LED' -> ('LED', 'Mini LED'). A bare parent returns itself."""
    parts = [compact(p) for p in str(category_hierarchy or '').split('>')]
    parts = [p for p in parts if p]
    if not parts:
        return '', ''
    return parts[0], (parts[-1] if len(parts) > 1 else parts[0])


def seo_focus_keyword(brand, sku, category_hierarchy):
    """One phrase a shopper would actually type. Model code when there is one,
    because that is the query that converts; otherwise the curated category
    phrase from category_seo.xlsx, which is a real search term - 'brand + leaf'
    produced things like 'haier qned' that nobody types."""
    code = model_code(sku)
    brand = compact(brand)
    if brand.lower() == 'authorized brand':
        brand = ''
    if code:
        return ' '.join(p for p in (brand, code) if p).lower().strip()
    parent, sub = split_hierarchy(category_hierarchy)
    curated = category_seo_for(parent, sub).get('focus', '')
    leaf = curated or sub
    return ' '.join(p for p in (brand, leaf) if p).lower().strip()


def seo_fields(sku, title, brand, category_hierarchy, spec_rows,
               regular_price, sale_price, plugin='yoast'):
    """{column name: value} for one product."""
    meta_title = seo_title(title, category_hierarchy)
    meta_desc = seo_description(title, brand, category_hierarchy, spec_rows,
                                regular_price, sale_price)
    focus = seo_focus_keyword(brand, sku, category_hierarchy)
    parent, sub = split_hierarchy(category_hierarchy)
    curated = category_seo_for(parent, sub)
    out = {
        'slug': seo_slug(sku, title),
        # category-level terms, the same for every product in the category.
        # They are what the CATEGORY page should rank for, so they are carried
        # here rather than merged into the product's own focus keyword - two
        # products competing for one phrase is what suppressed focus keywords
        # across the catalogue last time.
        'category focus keyword': curated.get('focus', ''),
        'category seo keywords': ', '.join(curated.get('keywords', ())),
    }
    for keys in seo_plugin_keys(plugin):
        out[keys['title']] = meta_title
        out[keys['description']] = meta_desc
        out[keys['focus']] = focus
    return out


def template_csv_bytes():
    buffer = io.StringIO()
    pd.DataFrame(columns=REQUIRED_INPUT_COLUMNS).to_csv(buffer, index=False, quoting=1)
    return buffer.getvalue().encode('utf-8')


def split_image_urls(row):
    raw = ''
    if 'image_url' in row and compact(row.get('image_url')):
        raw = row.get('image_url')
    elif 'images' in row and compact(row.get('images')):
        raw = row.get('images')
    elif 'Images' in row and compact(row.get('Images')):
        raw = row.get('Images')
    elif 'url_link' in row and compact(row.get('url_link')):
        raw = row.get('url_link')
    # normalise the other separators to commas first, then split URL-safely
    normalised = re.sub(r'<br\s*/?>|\r?\n', ',', str(raw or ''))
    return [compact(x) for x in split_image_list(normalised) if compact(x)]


def flatten_on_white(image):
    """RGB copy of the image with any transparency composited onto white.

    A transparent PNG and an opaque JPEG side by side on the storefront would
    otherwise render as two different backgrounds."""
    if (image.mode in ('RGBA', 'LA', 'PA')
            or (image.mode == 'P' and 'transparency' in image.info)):
        rgba = image.convert('RGBA')
        white = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(white, rgba).convert('RGB')
    return image if image.mode == 'RGB' else image.convert('RGB')


def _min_channel(rgb):
    """'L' image of each pixel's darkest channel (255 = every channel white)."""
    r, g, b = rgb.split()
    return ImageChops.darker(ImageChops.darker(r, g), b)


def white_share(image):
    """Share (0.0-1.0) of the outer BORDER_PCT strip on all four sides whose
    pixels have every channel >= WHITE_MIN, after transparency is composited
    onto white.

    Measure the SOURCE photo, never the finished canvas: layout_on_canvas()
    pads with white, so every finished file would score as white."""
    mins = _min_channel(flatten_on_white(image))
    w, h = mins.size
    bx = max(1, round(w * BORDER_PCT / 100))
    by = max(1, round(h * BORDER_PCT / 100))
    # top and bottom take the full width; the sides skip those rows so no
    # corner pixel is counted twice
    strips = [(0, 0, w, min(by, h)), (0, max(h - by, by), w, h),
              (0, by, min(bx, w), h - by), (max(w - bx, bx), by, w, h - by)]
    total = white = 0
    for box in strips:
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        hist = mins.crop(box).histogram()
        total += sum(hist)
        white += sum(hist[WHITE_MIN:])
    return white / total if total else 0.0


def is_white_background(image):
    """True when the photo sits on a white (or transparent) backdrop: at least
    WHITE_SHARE of its border strip is white (see white_share). A lifestyle
    shot fails because its scene runs to the frame edge."""
    return white_share(image) >= WHITE_SHARE


_BBOX_LUT = [255 if (255 - v) > BBOX_TOL else 0 for v in range(256)]


def product_bbox(rgb):
    """Box around the non-white pixels of an RGB image, or None when there is
    no usable product (all white, or only a speck under BBOX_MIN_PCT)."""
    box = _min_channel(rgb).point(_BBOX_LUT).getbbox()
    if not box:
        return None
    w, h = rgb.size
    if ((box[2] - box[0]) < w * BBOX_MIN_PCT / 100
            or (box[3] - box[1]) < h * BBOX_MIN_PCT / 100):
        return None
    return box


def canvas_scale(width, height, white):
    """Scale for a product of width x height pixels on the CANVAS_EDGE square.

    A white-background product fits inside the canvas minus MARGIN_PCT on each
    side; a lifestyle shot fills the canvas. Downscaling is unlimited;
    upscaling is capped at MAX_UPSCALE, so a product that would need more sits
    smaller, still centred, on the same canvas."""
    margin = MARGIN_PCT / 100 if white else 0.0
    inner = CANVAS_EDGE * (1 - 2 * margin)
    return min(inner / width, inner / height, MAX_UPSCALE)


def layout_on_canvas(image, white):
    """Finish one photo in the house layout: a CANVAS_EDGE x CANVAS_EDGE white
    square with the product centred, never cropped.

    white=True trims the photo to its product box (falling back to the whole
    photo when the box is empty) and keeps MARGIN_PCT of white on the limiting
    side. white=False (a lifestyle shot) is never trimmed - the whole frame is
    fitted inside the canvas. Returns RGB."""
    rgb = flatten_on_white(image)
    if white:
        box = product_bbox(rgb)
        if box:
            rgb = rgb.crop(box)
    width, height = rgb.size
    edge = CANVAS_EDGE
    scale = canvas_scale(width, height, white)
    new_w = max(1, min(edge, round(width * scale)))
    new_h = max(1, min(edge, round(height * scale)))
    if (new_w, new_h) != (width, height):
        rgb = rgb.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new('RGB', (edge, edge), SQUARE_BACKGROUND)
    canvas.paste(rgb, ((edge - new_w) // 2, (edge - new_h) // 2))
    return canvas


def filter_dropped_urls(urls, dropped_cell):
    """Remove reviewer-rejected image URLs (comma-separated cell written by
    the GUI's card review) before download. Survivor order is preserved."""
    dropped = {u.strip() for u in split_image_list(str(dropped_cell or ''))
               if u.strip()}
    if not dropped:
        return urls
    return [u for u in urls if u.strip() not in dropped]


IMAGE_ACCEPT = 'image/avif,image/webp,image/*,*/*;q=0.8'
# HTTP codes that mean "this client was refused", not "this image is gone":
# a browser-impersonating retry often gets through (image.haier.com resets
# every python-requests TLS handshake but answers curl_cffi with 200).
_RETRY_STATUS = (403, 429)


def fetch_image_bytes(url):
    """Return the body of `url`. Plain requests with BROWSER_UA first; on a
    connection error or HTTP 403/429, retry ONCE with curl_cffi impersonating
    Chrome. Raises on failure (never returns empty bytes silently)."""
    try:
        response = requests.get(url, headers={'User-Agent': BROWSER_UA},
                                timeout=30)
        status = getattr(response, 'status_code', 200)
        if status not in _RETRY_STATUS:
            response.raise_for_status()
            return response.content
        first_error = requests.HTTPError(f'HTTP {status}', response=response)
    except requests.ConnectionError as exc:
        first_error = exc
    try:
        from curl_cffi import requests as creq
    except ImportError:
        raise first_error
    response = creq.get(url, impersonate='chrome', timeout=30,
                        headers={'Accept': IMAGE_ACCEPT})
    if response.status_code >= 400:
        raise requests.HTTPError(
            f'HTTP {response.status_code} (after browser retry; first try: '
            f'{first_error})')
    return response.content


SUPERSEDED_DIR_NAME = '_superseded'


def retire_stale_images(sku, written_indexes, root=None, today=None):
    """MOVE (never delete) every `<sku>_<n>.webp` in the upload folder that
    this build did NOT write to `<root>\\_superseded\\<YYYY-MM-DD>\\` (owner
    decision 2026-11-19). Runs also when the build saved 0 images for the SKU.

    The SKU is matched exactly: 'TV-X_1.webp' belongs to TV-X, never to TV-X2
    or TV-X-B ('<sku>_' followed by digits only). Case-insensitive, because
    NTFS is (see process_inventory's P2-30). A name already in the target gets
    a numeric suffix ('TV-X_1-2.webp'). Only the top level of `root` is
    scanned, so _superseded itself is never touched again. Every consumer of
    the upload folder (pack, validator, wc_publish, GUI) opens files BY NAME
    from the CSV, so nothing reads _superseded. Returns the moved names."""
    root = str(IMAGE_DIR if root is None else root)
    if not os.path.isdir(root):
        return []
    keep = {f'{sku}_{i}.webp'.lower() for i in written_indexes}
    own = re.compile(re.escape(str(sku)) + r'_\d+\.webp', re.IGNORECASE)
    stale = sorted(name for name in os.listdir(root)
                   if own.fullmatch(name) and name.lower() not in keep
                   and os.path.isfile(os.path.join(root, name)))
    if not stale:
        return []
    day = today or datetime.now().strftime('%Y-%m-%d')
    target_dir = os.path.join(root, SUPERSEDED_DIR_NAME, day)
    os.makedirs(target_dir, exist_ok=True)
    moved = []
    for name in stale:
        stem, ext = os.path.splitext(name)
        target, n = os.path.join(target_dir, name), 1
        while os.path.exists(target):
            n += 1
            target = os.path.join(target_dir, f'{stem}-{n}{ext}')
        try:
            shutil.move(os.path.join(root, name), target)
            moved.append(name)
        except OSError as exc:
            _ui().warning(f'{sku}: could not move old image {name} ({exc})')
    if moved:
        safe_print(f'{sku}: moved {len(moved)} old image(s) to '
                   f'{SUPERSEDED_DIR_NAME}\\{day}: {", ".join(moved)}')
    return moved


def download_urls_for_sku(sku, urls, flags=None):
    """Download and convert images, then retire this SKU's old files (see
    retire_stale_images). Returns the list of indexes saved."""
    saved = _download_urls_for_sku(sku, urls, flags=flags)
    retire_stale_images(sku, saved)
    return saved


def _download_urls_for_sku(sku, urls, flags=None):
    """Download and convert images. Returns the list of indexes actually
    saved, so the exported Images column never references files that were
    not downloaded (previously failures were silently mapped anyway).

    Quality rules, all enforced here so the browser app and build_csv.py cannot
    diverge: ask the CDN for its largest rendition and refuse anything below
    MIN_IMAGE_EDGE as unusable on a product page.

    SQUARE_IMAGES=True (the house layout): every photo is finished by
    layout_on_canvas() on the one CANVAS_EDGE white square. Of the photos with
    a white background, the one with the HIGHEST white share (white_share,
    measured on the source; ties -> earlier URL) becomes `<sku>_1.webp` - the
    featured image - and the rest keep their URL order as _2.._n, numbered
    contiguously so _1 always exists. When no photo has a white background
    the first one stays _1 and FLAG_MAIN_NOT_WHITE is appended to `flags`
    (a list the caller owns) for validate_csv to report.

    SQUARE_IMAGES=False: the old path - downscale past MAX_IMAGE_EDGE only,
    each file keeps its URL position as its index."""
    root = str(IMAGE_DIR)
    os.makedirs(root, exist_ok=True)
    rejected = []

    # Save all images into a single root folder (no per-SKU subfolders)
    def _fetch(pair):
        index, url = pair
        image = Image.open(io.BytesIO(fetch_image_bytes(largest_rendition(url))))
        if max(image.size) < MIN_IMAGE_EDGE:
            # a soft, blocky product photo is worse than one image fewer
            rejected.append(f'{os.path.basename(url)[:40]} '
                            f'({image.size[0]}x{image.size[1]})')
            raise ValueError('below the minimum resolution')
        if image.mode == 'P':
            # palette images: keep transparency when present (silences the
            # PIL 'Palette images with Transparency' warning correctly)
            image = image.convert('RGBA' if 'transparency' in image.info else 'RGB')
        elif image.mode not in ('RGB', 'RGBA'):
            image = image.convert('RGBA' if 'A' in image.getbands() else 'RGB')
        if SQUARE_IMAGES:
            # the file name depends on which photo wins the main slot, so
            # encode here (in parallel) and write after every result is in.
            # Whiteness is measured on the SOURCE, before layout_on_canvas()
            # pads it with white - measured after, every photo would pass.
            share = white_share(image)
            white = share >= WHITE_SHARE
            buf = io.BytesIO()
            layout_on_canvas(image, white).save(
                buf, format='WEBP', quality=WEBP_QUALITY, method=WEBP_METHOD)
            return index, white, share, buf.getvalue()
        # downscale only - enlarging a small source invents detail and looks
        # worse than the original did
        if max(image.size) > MAX_IMAGE_EDGE:
            ratio = MAX_IMAGE_EDGE / max(image.size)
            image = image.resize((max(1, round(image.size[0] * ratio)),
                                  max(1, round(image.size[1] * ratio))),
                                 Image.LANCZOS)
        target = os.path.join(root, f'{sku}_{index}.webp')
        image.save(target, format='WEBP', quality=WEBP_QUALITY,
                   method=WEBP_METHOD)
        return index

    if not urls:
        return []

    results = []
    failed = []     # (url, reason) for every failure that is NOT a resolution reject
    workers = min(8, max(1, len(urls)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch, (i, u)): u
                   for i, u in enumerate(urls, start=1)}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except ValueError as exc:
                if str(exc) != 'below the minimum resolution':
                    failed.append((futures[future], f'{type(exc).__name__}: {exc}'))
            except Exception as exc:
                failed.append((futures[future], f'{type(exc).__name__}: {exc}'))
    if rejected:
        _ui().warning(f'{sku}: skipped {len(rejected)} image(s) below '
                      f'{MIN_IMAGE_EDGE}px - ' + '; '.join(sorted(rejected)[:3]))
    if failed:
        _ui().warning(f'{sku}: image download failed for {len(failed)} of '
                      f'{len(urls)} URL(s) - ' + '; '.join(
                          f'{os.path.basename(u)[:40]} ({why[:80]})'
                          for u, why in failed[:3]))
        if flags is not None:
            flags.append(f'image download failed: {len(failed)} of {len(urls)}')
    if not SQUARE_IMAGES:
        return sorted(results)

    # threads finish in any order: sort by URL position so the main-image
    # choice and the numbering are the same on every run
    results.sort(key=lambda r: r[0])
    if not results:
        return []
    # main image = the WHITEST white-background photo (highest border white
    # share); a tie goes to the earlier URL position. The rest keep URL order.
    whites = [r for r in results if r[1]]
    main = max(whites, key=lambda r: (r[2], -r[0])) if whites else None
    if main is None:
        main = results[0]
        if flags is not None:
            flags.append(FLAG_MAIN_NOT_WHITE)
    ordered = [main] + [r for r in results if r is not main]
    for number, (_, _, _, data) in enumerate(ordered, start=1):
        with open(os.path.join(root, f'{sku}_{number}.webp'), 'wb') as fh:
            fh.write(data)
    return list(range(1, len(ordered) + 1))


def format_price_output(value):
    # None (not '') for missing values keeps the price columns numeric, which
    # fixes the noisy pyarrow 'Could not convert' errors in the preview table
    if value == '' or value is None:
        return None
    try:
        number = float(value)
        if number.is_integer():
            return int(number)
        return round(number, 2)
    except Exception:
        return None


def format_regular_price_output(value):
    if value == '' or value is None:
        return None
    try:
        number = float(value)
        return int(round(number))
    except Exception:
        return None


def require_input_columns(df):
    missing = [col for col in REQUIRED_INPUT_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError('Missing required columns: ' + ', '.join(missing))


def read_uploaded_inventory(uploaded_file):
    file_name = (getattr(uploaded_file, 'name', '') or '').lower()

    if file_name.endswith(('.xlsx', '.xls')):
        return pd.read_excel(io.BytesIO(uploaded_file.getvalue()))

    raw_bytes = uploaded_file.getvalue()

    # latin1 decodes ANY byte sequence, so the loop below could never fail and
    # the ValueError at the end was unreachable: an .xlsx or a PNG renamed to
    # .csv came back as a garbage one-column frame, and the user got
    # "Missing required columns: sku, title, ..." instead of being told the
    # file was not a CSV. A text file has no NUL bytes; UTF-16/32 does, so
    # those are recognised by their BOM first.
    bom = raw_bytes[:4]
    utf_wide = bom.startswith((b'\xff\xfe', b'\xfe\xff'))
    if b'\x00' in raw_bytes[:8192] and not utf_wide:
        raise ValueError('That file is not a text CSV (it contains binary '
                         'data). If it is an Excel workbook, upload it as '
                         '.xlsx; otherwise save it as UTF-8 CSV.')

    encodings = ['utf-8-sig', 'utf-8', 'utf-16', 'cp1252', 'latin1']
    separators = [None, ',', ';', '\t', '|']

    for encoding in encodings:
        try:
            decoded_text = raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue

        for separator in separators:
            try:
                if separator is None:
                    parsed = pd.read_csv(io.StringIO(decoded_text), sep=None, engine='python')
                else:
                    parsed = pd.read_csv(io.StringIO(decoded_text), sep=separator)

                # a real header row has words in it; noise decoded through
                # latin1 gives columns of control characters
                if not any(re.search(r'[A-Za-z0-9]', str(c))
                           for c in parsed.columns):
                    continue
                if len(parsed.columns) > 1 or len(parsed) > 0:
                    return parsed
            except Exception:
                continue

    raise ValueError('Unable to decode uploaded CSV. Save it as UTF-8 or UTF-16 and try again.')


def filter_active_inventory_rows(df):
    candidate_columns = [
        'sku',
        'title',
        'categories',
        'Brands',
        'regular price',
        'sale price',
        'url_link',
        'image_url',
        'Product Configuration',
        'configuration',
        'Images',
    ]
    present_columns = [column for column in candidate_columns if column in df.columns]
    if not present_columns:
        return df.iloc[0:0].copy()

    cleaned = df.copy()
    for column in present_columns:
        cleaned[column] = cleaned[column].map(compact)

    active_mask = cleaned[present_columns].ne('').any(axis=1)
    return cleaned[active_mask].copy()


def single_price_mode(client=None):
    """True when the client's client.toml sets [pricing] single_price."""
    cfg = client or CLIENT
    pricing = getattr(cfg, 'pricing', None)
    return bool(getattr(pricing, 'single_price', False))


def single_price_pair(regular_price, sale_price):
    """(price, '') for single-price mode: the HIGHER of the MRP and the
    selling price, whichever exist. ('', '') when neither exists. The price
    goes in 'regular price'; 'sale price' is always empty."""
    values = []
    for raw in (regular_price, sale_price):
        if raw == '' or raw is None:
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        if number > 0:
            values.append(number)
    if not values:
        return '', ''
    best = max(values)
    return (int(best) if best.is_integer() else round(best, 2)), ''


def row_context(row, estimate_mrp=False):
    """Everything process_inventory derives from ONE input row before it starts
    writing output columns: brand, category, family, prefixed sku, scraped
    specs, attribute rows and the two prices.

    Pure - no image download, no Streamlit, no per-batch bookkeeping (the
    duplicate/unmapped/gap counters stay in process_inventory). It exists so
    the GUI's preview panes and the tests can ask "what would the build make of
    this row?" without running a build, and so there is exactly ONE definition
    of that answer.

    Returns {raw_sku, title, brand, parent, sub, category_hierarchy,
    category_mapped, category_family, sku, scraped, dynamic_attrs,
    regular_price, sale_price}.
    """
    raw_sku = row.get('sku')
    title = compact(row.get('title'))
    brand = compact(row.get('Brands')) or infer_brand(title, raw_sku)
    # The content firewall, ONCE, here: the name, SEO title/description and
    # tags are all built from this title, so every later use is clean. The
    # product's own brand is never stripped as a "retailer" (Croma kettles).
    title = compact(content_clean(title, brand=brand)) or title
    parent_guess, sub_guess = detect_category(title, row.get('categories'),
                                              brand, raw_sku)
    category_hierarchy, category_mapped = canonicalize_category(parent_guess, sub_guess)
    category_family = family_for(parent_guess, sub_guess)
    # the PARENT names the prefix, the family names capacity/config text -
    # passing the family here sent every FAMILY_BY_SUB product (microwaves,
    # kettles, irons, dispensers...) to the AP- fallback while its parent
    # siblings got the real code
    sku = prefixed_sku(raw_sku, parent_guess)

    sale_price = number_or_blank(row.get('sale price'))
    regular_price = number_or_blank(row.get('regular price'))
    if single_price_mode():
        # [pricing] single_price: ONE price, the higher of the two. No
        # struck-through MRP, no sale price, no estimated MRP.
        regular_price, sale_price = single_price_pair(regular_price,
                                                      sale_price)
    else:
        # sanity: an MRP at or below the selling price is bogus data
        if (regular_price != '' and sale_price != ''
                and regular_price <= sale_price):
            regular_price = ''
        # MRP estimation is now opt-in: fabricated regular prices are not real
        if estimate_mrp and regular_price == '' and sale_price != '':
            regular_price = round(float(sale_price) * 1.25, 2)

    scraped = parse_scraped_specs(row.get('specs'), brand=brand)
    # filter attributes: named by the store's filter group, value snapped
    # onto the sheet's own spelling (see filter_attributes)
    filters, filter_unmatched = filter_attributes(sub_guess, parent_guess,
                                                  title, scraped)
    dynamic_attrs = product_specs(title, brand, category_family, scraped,
                                  filters)

    return {
        'raw_sku': raw_sku,
        'title': title,
        'brand': brand,
        'parent': parent_guess,
        'sub': sub_guess,
        'category_hierarchy': category_hierarchy,
        'category_mapped': category_mapped,
        'category_family': category_family,
        'sku': sku,
        'scraped': scraped,
        'filters': filters,
        'filter_unmatched': filter_unmatched,
        'dynamic_attrs': dynamic_attrs,
        'regular price': regular_price,
        'sale price': sale_price,
    }


def process_inventory(df, media_base=None, estimate_mrp=False,
                      seo=False, seo_plugin='yoast'):
    """`seo` is OPT-IN. While it is off the output is byte-identical to what
    this function produced before the SEO columns existed."""
    require_input_columns(df)
    working = df.copy()
    working['title'] = working['title'].map(compact)
    working = working[working['title'] != '']
    if media_base is None:
        media_base = MEDIA_BASE

    processed_rows = []
    seen_skus = set()            # upper-cased keys (P2-30)
    seen_skus_exact = set()      # as written, to spot case-ONLY collisions
    case_only_duplicates = set()
    skipped_duplicates = 0
    unmapped_categories = set()
    gap_counts = {}
    not_white_main = []
    unmatched_rows = 0
    ui = _ui()
    if SUBCATEGORY_ATTRIBUTES_WARNING:
        ui.warning(SUBCATEGORY_ATTRIBUTES_WARNING)
    if FILTER_SHEET_WARNING:
        ui.warning(FILTER_SHEET_WARNING)
    progress = ui.progress(0)
    tracker = ui.empty()

    total = len(working)
    for idx, (_, row) in enumerate(working.iterrows(), start=1):
        ctx = row_context(row, estimate_mrp=estimate_mrp)
        title = ctx['title']
        brand = ctx['brand']
        parent_guess, sub_guess = ctx['parent'], ctx['sub']
        category_hierarchy = ctx['category_hierarchy']
        if not ctx['category_mapped']:
            unmapped_categories.add(category_hierarchy)
        category_family = ctx['category_family']
        sku = ctx['sku']

        # BUGFIX: identical SKUs used to produce duplicate WooCommerce rows.
        # P2-30: compare case-INSENSITIVELY. WooCommerce's SKU uniqueness query
        # and NTFS both fold case, so 'ha-x1' and 'HA-X1' are one product and
        # one '<sku>_1.webp' file. The stored SKU keeps its original case - a
        # live WM-/WH- key whose case changed would duplicate on upsert.
        sku_key = sku.upper()
        if sku_key in seen_skus:
            skipped_duplicates += 1
            if sku not in seen_skus_exact:
                case_only_duplicates.add(sku)
            continue
        seen_skus.add(sku_key)
        seen_skus_exact.add(sku)

        sale_price = ctx['sale price']
        regular_price = ctx['regular price']

        tracker.text(f'Compiling and downloading SKU: {sku} ({idx}/{total})')
        progress.progress(idx / max(total, 1))

        urls = filter_dropped_urls(split_image_urls(row),
                                   row.get('dropped_images'))
        image_flags = []
        saved_indexes = download_urls_for_sku(sku, urls, flags=image_flags)
        if FLAG_MAIN_NOT_WHITE in image_flags:
            not_white_main.append(sku)

        # BUGFIX: only map images that were really downloaded (previously a
        # minimum of 2 URLs was invented even when nothing was saved)
        mapped_images = ','.join([f'{media_base}/{sku}_{i}.webp' for i in saved_indexes])
        scraped = ctx['scraped']
        # key points are ranked by this sub-category's attribute whitelist;
        # build_descriptions drops any rendered row that fails content_ok
        # filter attributes come FIRST in dynamic_attrs (never cut by the
        # MAX_ATTRIBUTES cap) and are named by the store's filter group
        config, short_desc, long_desc, dynamic_attrs = build_descriptions(
            sku, title, brand, category_family, scraped,
            sub=sub_guess, parent=parent_guess, filters=ctx['filters'])
        filter_unmatched = ctx['filter_unmatched']
        if filter_unmatched:
            unmatched_rows += 1

        gaps = missing_spec_axes(sub_guess, title, category_family, scraped,
                                 parent=parent_guess)
        for axis in gaps:
            gap_counts[axis] = gap_counts.get(axis, 0) + 1

        output_row = {
            'sku': sku,
            'cross reference tag': f'{brand} | {category_hierarchy.lower().replace(" ", "_")}',
            'configuration': compact(config),
            'short description': compact(short_desc),
            'description': compact(long_desc),
            'title': title,
            # the importer splits this cell on ',' unless escaped as '\,'
            'categories': escape_category_for_csv(category_hierarchy),
            # the price band follows the price the shopper pays: the sale
            # price, or in single-price mode the one price ('regular price')
            'Tags': build_tags(brand, parent_guess, sub_guess, title,
                               regular_price if single_price_mode()
                               else sale_price, category_family),
            'regular price': format_regular_price_output(regular_price),
            'sale price': format_price_output(sale_price),
            'Brands': brand,
            '_consolidated_sku': sku,
            'Images': mapped_images,
            # axes this product's own sub-category declares but nothing could
            # fill. Blank means complete. validate_csv.py reports these.
            '_spec_gaps': ', '.join(gaps),
            # a filter value WAS stated (spec table or title) but is not in
            # the filter sheet / sub-category lists, so no filter attribute
            # was written for it. Blank means none. validate_csv.py warns.
            '_filter_unmatched': '; '.join(filter_unmatched),
            # image problems to review (blank = none); validate_csv.py reports
            # them as warnings that never block the import
            '_image_flags': '; '.join(image_flags),
        }

        if seo:
            output_row.update(seo_fields(
                sku, title, brand, category_hierarchy, dynamic_attrs,
                regular_price, sale_price, seo_plugin))

        # only a FILTER attribute is a global (taxonomy) attribute; every other
        # spec label stays a per-product custom attribute, so the store does
        # not grow one global taxonomy per scraped label
        filter_names = {woo_attribute_name(hit.group).lower()
                        for hit in ctx['filters']}
        for index, (attr_name, attr_value) in enumerate(dynamic_attrs, start=1):
            output_row[f'Attribute {index} name'] = attr_name
            output_row[f'Attribute {index} value(s)'] = attr_value
            output_row[f'Attribute {index} visible'] = '1'
            output_row[f'Attribute {index} global'] = (
                '1' if attr_name.lower() in filter_names else '0')

        processed_rows.append(output_row)

    if skipped_duplicates:
        ui.warning(f'Skipped {skipped_duplicates} duplicate SKU row(s) after normalization.')
    if case_only_duplicates:
        ui.warning('Some SKUs differed only by letter case and were treated as '
                   'the same product (WooCommerce and Windows both ignore case): '
                   + ', '.join(sorted(case_only_duplicates)))
    if unmapped_categories:
        ui.warning('Categories NOT found in the category map (left as detected - '
                   'add them to category_map.xlsx or fix the product data): '
                   + ', '.join(sorted(unmapped_categories)))
    if gap_counts:
        ui.warning(
            'Missing attributes their sub-category requires (see _spec_gaps): '
            + ', '.join(f'{axis} x{n}' for axis, n
                        in sorted(gap_counts.items(), key=lambda kv: -kv[1]))
            + '. These products will be missing storefront filters.')
    if unmatched_rows:
        ui.warning(f'{unmatched_rows} product(s) state a filter value that is '
                   'not in the filter sheet (see _filter_unmatched). A "new '
                   'value" was written as scraped; add it to the attributes '
                   'master if it is real.')
    if not_white_main:
        ui.warning(f'{len(not_white_main)} product(s) have no white-background '
                   'photo, so the featured image is a lifestyle shot (see '
                   '_image_flags): ' + ', '.join(not_white_main[:10])
                   + (' ...' if len(not_white_main) > 10 else ''))

    attribute_columns = []
    for row in processed_rows:
        for key in row.keys():
            if key.startswith('Attribute ') and key not in attribute_columns:
                attribute_columns.append(key)

    def attribute_sort_key(column_name):
        match = re.search(r'Attribute (\d+) (name|value\(s\)|visible|global)', column_name)
        if not match:
            return (10**9, 10**9)
        attribute_index = int(match.group(1))
        field_order = {'name': 0, 'value(s)': 1, 'visible': 2, 'global': 3}[match.group(2)]
        return (attribute_index, field_order)

    ordered_columns = (EXPORTED_OUTPUT_COLUMNS
                       + (seo_columns(seo_plugin) if seo else [])
                       + sorted(attribute_columns, key=attribute_sort_key))
    return pd.DataFrame(processed_rows).reindex(columns=ordered_columns)


def main():
    st.set_page_config(page_title='BranchIQ Master Enterprise Engine', layout='wide')
    if 'final_wordpress_df' not in st.session_state:
        st.session_state['final_wordpress_df'] = None

    st.title('BranchIQ Master Enterprise Engine')
    st.caption(f'Client: {CLIENT.name} ({CLIENT.slug}) - media base {MEDIA_BASE}')
    if CATEGORY_MAP_FILE:
        st.caption(f'Category map loaded: {CATEGORY_MAP_FILE} '
                   f'({len(CATEGORY_PARENTS)} parent categories, '
                   f'{len(CATEGORY_COMBOS)} lookup entries)')
    else:
        st.warning(f'{CLIENT.category_map} not found - category names '
                   'will use built-in spellings instead of your taxonomy.')
    st.download_button(
        label='Download input_template.csv',
        data=template_csv_bytes(),
        file_name='input_template.csv',
        mime='text/csv',
        width='stretch',
    )

    uploaded_file = st.file_uploader('Upload inventory CSV or Excel', type=['csv', 'xlsx', 'xls'])
    media_folder = st.text_input(
        'WordPress uploads folder (YYYY/MM) - set this to the month you will actually upload the images',
        value=f'{datetime.now():%Y/%m}',
    )
    estimate_mrp = st.checkbox(
        'Estimate regular price as sale price x 1.25 when missing (leave off to keep it blank)',
        value=False,
    )
    # OFF by default: SEO meta belongs to a plugin, and turning it on changes
    # what the importer writes. Nothing else in the output moves when it is off.
    seo = st.checkbox(
        'Add SEO meta columns (meta title, meta description, focus keyword, slug)',
        value=False,
        help='Written as "Meta: ..." columns, which is what tells the '
             'WooCommerce CSV importer to store them as post meta. '
             'Deterministic - built from the title, category, specs and price.',
    )
    seo_plugin = 'yoast'
    if seo:
        seo_plugin = st.radio(
            'Which SEO plugin does the store use?',
            options=['yoast', 'rankmath', 'both'],
            horizontal=True,
            format_func=lambda v: {'yoast': 'Yoast SEO',
                                   'rankmath': 'Rank Math',
                                   'both': 'Write both sets'}[v],
        )
    if uploaded_file is not None:
        try:
            incoming = read_uploaded_inventory(uploaded_file)
            total_rows = len(incoming)
            incoming = filter_active_inventory_rows(incoming)
            active_rows = len(incoming)
            st.info(f'Loaded {active_rows} active rows from {total_rows} total rows')
            if total_rows > 0 and active_rows == 0:
                st.warning('The uploaded file has rows, but none contain usable product data in the core columns. If you uploaded the blank template, fill in product rows first.')

            # Files coming from sku_enricher carry a status column - only
            # verified listings should become live products by default.
            if 'status' in incoming.columns:
                verified_only = st.checkbox(
                    'Import only verified rows (status starts with FOUND)',
                    value=True,
                )
                if verified_only:
                    status_upper = incoming['status'].map(compact).str.upper()
                    keep = status_upper.str.startswith('FOUND')
                    excluded = int((~keep).sum())
                    incoming = incoming[keep].copy()
                    if excluded:
                        st.info(f'Excluded {excluded} unverified row(s) '
                                f'(CHECK MANUALLY / CLOSE MATCH / NOT FOUND). '
                                f'Untick the box to include them.')

            if st.button('Generate file & apply changes', type='primary'):
                try:
                    folder = compact(media_folder).strip('/')
                    if not folder:
                        folder = f'{datetime.now():%Y/%m}'
                    st.session_state['final_wordpress_df'] = process_inventory(
                        incoming,
                        media_base=CLIENT.media_base(folder),
                        estimate_mrp=estimate_mrp,
                        seo=seo,
                        seo_plugin=seo_plugin,
                    )
                except Exception as exc:
                    st.error(str(exc))
        except Exception as exc:
            st.error(str(exc))

    if st.session_state['final_wordpress_df'] is not None:
        result = st.session_state['final_wordpress_df']
        st.dataframe(result, width='stretch')

        # QA runs on generation, not as a step someone has to remember
        with st.expander('Quality check', expanded=True):
            try:
                import contextlib
                import validate_csv
                # validate_export narrates to stdout for its CLI callers; in the
                # browser that is invisible noise, so only its result is used
                with contextlib.redirect_stdout(io.StringIO()):
                    issues = validate_csv.validate_export(
                        result.astype(str).fillna(''), 'generated file',
                        write_issues=False)
                if not issues:
                    st.success('No issues found - this file looks import-ready.')
                else:
                    st.warning(f'{len(issues)} row(s) need a look before publishing.')
                    st.dataframe(pd.DataFrame(issues), width='stretch')
            except Exception as exc:
                st.info(f'Quality check unavailable ({type(exc).__name__}). '
                        f'Run validate_csv.py on the downloaded file.')
        buffer = io.StringIO()
        result.to_csv(buffer, index=False, quoting=1)
        st.download_button(
            label='Download wordpress_ready_clean_repaired.csv',
            data=buffer.getvalue(),
            file_name='wordpress_ready_clean_repaired.csv',
            mime='text/csv',
            width='stretch',
        )


if __name__ == '__main__':
    main()
