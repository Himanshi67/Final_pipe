import streamlit as st
import pandas as pd
import re
import io
import os
import sys
from datetime import datetime
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image

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
EXPORTED_OUTPUT_COLUMNS = ['sku', 'cross reference tag', 'configuration', 'short description', 'description', 'title', 'categories', 'Tags', 'regular price', 'sale price', 'Brands', '_consolidated_sku', 'Images', '_spec_gaps']
IMAGE_ROOT = 'product_images_for_upload'
MAX_ATTRIBUTES = 15   # each one costs 4 CSV columns, so the sheet stays usable
# The short description renders right under the price; a 15-row table there
# pushes the buy button off the screen. The essentials (brand, capacity,
# screen, resolution, rating...) always rank first in product_specs, so the
# cap keeps the best rows. The LONG description still carries everything.
SHORT_DESC_MAX_SPECS = 6

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
# Only governs the non-square path: squared catalogue masters are finished at
# CATALOG_EDGE below, which supersedes this cap.
MAX_IMAGE_EDGE = 2400
# Main product images ship at ONE of two fixed square renditions - 1200x1200
# when the source square is big enough, 1000x1000 otherwise - because the
# catalogue grid only lines up when every tile is the same shape and scale.
# The fallback IS MIN_IMAGE_EDGE by construction: anything that passed the
# floor can always be finished at 1000x1000 without upscaling.
CATALOG_EDGE = 1200
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


def build_media_base():
    return f"https://shreejeeelectronics.in/wp-content/uploads/{datetime.now():%Y/%m}"


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
    if re.fullmatch(r'[A-Za-z]{2,}', token) and token.upper() not in ('WM', 'WH', 'AP'):
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


def load_category_map():
    """Read the taxonomy file that sits next to app.py. Returns lookup dicts
    whose VALUES keep the file's exact spelling/case/spacing."""
    base = os.path.dirname(os.path.abspath(__file__))
    path = ''
    for name in CATEGORY_MAP_CANDIDATES:
        candidate = os.path.join(base, name)
        if os.path.exists(candidate):
            path = candidate
            break
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
SKU_PREFIX_CATEGORY = {
    'WM': ('Washing Machines', ''),
    'WH': ('Water Heaters', 'Geysers'),
    'SM': ('Home Appliances', 'sewing machine'),
    'HWD': ('Home Appliances', 'Water Dispensers'),
    'REF': ('Home Appliances', 'Refrigerators'),
    'FRIDGE': ('Home Appliances', 'Refrigerators'),
    'MWO': ('Kitchen Appliances', 'Microwave Ovens'),
    'IC': ('Kitchen Appliances', 'Induction Cooktops'),
    'ICT': ('Kitchen Appliances', 'Induction Cooktops'),
    'DI': ('Small Appliances', 'Electric Iron'),
    'DF': ('Cooling & Freezing', 'Deep Freezers'),
    'AC': ('Air Conditioner', ''),
    'TV': ('LED', ''),
}


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
# them later renames live products). AP- stays the fallback for anything the
# taxonomy does not know. Keep validate_csv.py's prefix-stripping regex and
# sku_enricher.CATEGORY_CODES in sync with this map.
SKU_PREFIX_BY_PARENT = {
    'Washing Machines': 'WM',          # live products carry these two -
    'Water Heaters': 'WH',             # never change them
    'LED': 'TV',
    'Air Conditioner': 'AC',
    'Cooling & Freezing': 'CF',
    'Kitchen Appliances': 'KA',
    'Home Appliances': 'HA',
    'Small Appliances': 'SA',
    'Electronics & Computing': 'EC',
}
SKU_PREFIXES = tuple(SKU_PREFIX_BY_PARENT.values()) + ('AP',)


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
    if sku.startswith(('WM-', 'WH-')):
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
    code = code or 'AP'
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


def load_subcategory_attributes():
    """Read the 'Sub-Category Attributes' sheet of the taxonomy file, if it has
    one. Returns {normalised sub-category: {axis: [allowed values]}}.

    Cells come in two flavours: an enumerable vocabulary ('1.0 Ton, 1.5 Ton,
    2.0 Ton') and a free-text hint ('Dry Clothes Wash Load Weight in kg (e.g.
    7.0kg)'). Only the first is usable as a value list; a hint just means the
    axis applies to this sub-category and the value must be read off the title.
    """
    global SUBCATEGORY_ATTRIBUTES_WARNING
    SUBCATEGORY_ATTRIBUTES_WARNING = ''
    base = os.path.dirname(os.path.abspath(__file__))
    out = {}
    problems = []
    for name in CATEGORY_MAP_CANDIDATES:
        path = os.path.join(base, name)
        if not os.path.exists(path) or path.lower().endswith('.csv'):
            continue
        try:
            table = pd.read_excel(path, sheet_name='Sub-Category Attributes',
                                  dtype=str).fillna('')
        except Exception as exc:
            # NEVER swallow this silently: with no axes, missing_spec_axes()
            # returns [] for every product, so `_spec_gaps` is blank and
            # validate_csv reports no missing_required_attribute - a build
            # that looks perfect while checking nothing. The usual cause is
            # the workbook having been rewritten as a single sheet; restore
            # it from backups\category_map_<ts>.xlsx.
            problems.append(f'{name}: {type(exc).__name__}: {exc}')
            continue
        # A parent is only safe to inherit from when it declares exactly ONE
        # attribute row (the LED shape: one row listing every sub-category).
        # 'Home Appliances' has 12 heterogeneous rows and Fans is the first, so
        # inheriting "whichever row came first" gave every bare-parent product
        # the ceiling-fan axes - build_tags shipped the tag 'Table' on a
        # dishwasher and 'Wall' on a water softener, and missing_spec_axes
        # invented gaps ('Type, Size, Star Rating, Capacity') that validate_csv
        # then reported as missing_required_attribute.
        parent_rows = {}
        for _, row in table.iterrows():
            if not compact(row.get('Sub-Category')):
                continue
            key = _cat_norm(compact(row.get('Parent Category')))
            parent_rows[key] = parent_rows.get(key, 0) + 1
        for _, row in table.iterrows():
            sub = compact(row.get('Sub-Category'))
            if not sub:
                continue
            axes = {}
            for axis in ('Type', 'Size', 'Star Rating', 'Ton', 'Capacity', 'Storage'):
                cell = compact(row.get(axis))
                if not cell or cell.upper() == 'N/A':
                    continue
                looks_like_hint = ('e.g' in cell.lower() or '(' in cell
                                   or len(cell) > 60)
                values = [] if looks_like_hint else [
                    compact(v) for v in cell.split(',') if compact(v)]
                axes[axis] = values
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
            parent_name = compact(row.get('Parent Category'))
            if parent_name and parent_rows.get(_cat_norm(parent_name)) == 1:
                out.setdefault(_cat_norm(parent_name), axes)
        break
    if not out:
        SUBCATEGORY_ATTRIBUTES_WARNING = (
            "No 'Sub-Category Attributes' sheet could be read from the "
            "taxonomy file, so NO required attribute axes are known: "
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
    (r'\bsmart\b', 'Smart TV'),
)
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


CATEGORY_SEO_FILE = 'category_seo.xlsx'


def load_category_seo():
    """Curated per-category tags and SEO keywords from category_seo.xlsx.

    Hand-written, because tags exist to group products the way a customer
    searches and nobody searches a model code. Missing file is not an error -
    the derived tags below still work, they are just not topped up.
    Regenerate with tools\\build_category_seo.py."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        CATEGORY_SEO_FILE)
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
    add(specs.get('platform'))
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

# facts that belong on an invoice, not a product page
SPEC_LABEL_DROP = {
    'price', 'mrp', 'discount', 'offer', 'seller', 'delivery', 'availability',
    'stock', 'sku', 'asin', 'ean', 'upc', 'hsn', 'gst', 'tax', 'rating',
    'ratings', 'reviews', 'best sellers rank', 'date first available',
    'in the box', 'sales package',
}

# an offer dressed as a spec row ('SuperDost Cashback: 100 for new SuperDost
# Users* + 3% or 250 Cashback'). Checked against the label AND the value, so
# already-enriched sheets carrying the junk are cleaned here without a
# re-scrape. Mirrors sku_enricher.SPEC_PROMO_RE - kept in both places for the
# same reason parse_scraped_specs itself is (see its docstring).
PROMO_SPEC_RE = re.compile(
    r"cash\s?back|coupon|voucher|promo|superdost|super\s?coin|bank offer|"
    r"exchange offer|no.?cost emi|instant discount|%\s*off\b|flat\s*off|"
    r"free gift|t&c|terms apply", re.I)


def canon_spec_label(label):
    text = compact(label).strip(': ')
    return SPEC_LABEL_CANON.get(text.lower(), text)


def parse_scraped_specs(cell):
    """'Label: Value | Label: Value' -> [(canonical label, value)].

    The inverse of sku_enricher.format_specs. Kept here rather than imported:
    validate/app must stay runnable without the scraper's dependencies.
    """
    rows, seen = [], set()
    for chunk in str(cell or '').split('|'):
        if ':' not in chunk:
            continue
        raw_label, value = chunk.split(':', 1)
        label, value = canon_spec_label(raw_label), compact(value)
        key = label.lower()
        if not label or not value or key in seen or key in SPEC_LABEL_DROP:
            continue
        if len(label) > 40 or len(value) > 120 or label.lower() == value.lower():
            continue
        if PROMO_SPEC_RE.search(label) or PROMO_SPEC_RE.search(value):
            continue                      # an offer, not a product fact
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


def product_specs(title, brand, category, scraped=()):
    """The (label, value) rows shown on the product page and exported as
    WooCommerce attributes.

    KI-14: only facts actually present in the listing title get a row. Anything
    that could not be read is omitted rather than filled with 'Standard ...',
    and the labels follow the product's own category - the previous version
    named every attribute 'WM ...' (washing machine) regardless of what the
    product was.

    `scraped` carries the retailer's own spec table. It fills labels the title
    could not supply; it never replaces one it could.
    """
    specs = spec_tags(title)
    rows, used = [], set()

    def add(label, value):
        label, value = woo_attribute_name(label), compact(value)
        # one row per LABEL: a WooCommerce product with two attributes of the
        # same name renders both, and the second is always the worse one
        if label and value and label.lower() not in used:
            used.add(label.lower())
            rows.append((label, value))

    add('Brand', brand)

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

    for label, value in scraped:
        if len(rows) >= MAX_ATTRIBUTES:
            break
        add(label, value)
    return rows


def build_descriptions(sku, title, brand, category, scraped=()):
    """Returns (key-features HTML, short description, long description, specs).

    The spec rows are returned directly instead of being recovered by regexing
    the HTML this function just produced - that round-trip through a string was
    where the wrong attribute names came from.
    """
    rows = product_specs(title, brand, category, scraped)
    extra = [(label, value) for label, value in rows if label != 'Brand']

    # Paragraph, not a bullet list: the storefront renders this inline under the
    # title, where a <ul> broke the flow. Still one sentence per VERIFIED spec
    # pair - nothing is written here that product_specs() did not produce, so a
    # missing spec stays missing instead of being smoothed over with prose.
    configuration_points = compact(
        '<p class="product-key-features">'
        + ' '.join(f'{label}: {value}.' for label, value in rows)
        + '</p>'
    ) if rows else ''
    short_desc = html_block([
        ('Brand', brand),
        ('Product SKU', sku),
    ] + extra[:SHORT_DESC_MAX_SPECS])
    long_desc = html_block([
        ('Brand Designation', brand),
        ('Model Identifier Code', sku),
        ('Title', title),
        ('Categories', category),
    ] + extra + [
        ('Standard Operational Grid', '230 V / 50 Hz'),
    ])
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

SEO_SITE_NAME = 'Shree Jee Electronics'
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
    text = re.sub(r'^(?:' + '|'.join(SKU_PREFIXES) + r')-', '', compact(sku))
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
    if sale:
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


def pad_to_square(image):
    """Centre the photo on a square white canvas without cropping or upscaling.

    Returns RGB: the alpha channel is composited onto white rather than kept,
    because a transparent PNG and an opaque JPEG side by side on the storefront
    render as two different backgrounds. Already-square photos come back
    unchanged apart from that flattening."""
    side = max(image.size)
    source = image if image.mode == 'RGBA' else image.convert('RGBA')
    canvas = Image.new('RGB', (side, side), SQUARE_BACKGROUND)
    canvas.paste(source,
                 ((side - image.size[0]) // 2, (side - image.size[1]) // 2),
                 source)                      # alpha as mask -> transparent = white
    return canvas


def filter_dropped_urls(urls, dropped_cell):
    """Remove reviewer-rejected image URLs (comma-separated cell written by
    the GUI's card review) before download. Survivor order is preserved."""
    dropped = {u.strip() for u in split_image_list(str(dropped_cell or ''))
               if u.strip()}
    if not dropped:
        return urls
    return [u for u in urls if u.strip() not in dropped]


def download_urls_for_sku(sku, urls):
    """Download and convert images. Returns the list of indexes actually
    saved, so the exported Images column never references files that were
    not downloaded (previously failures were silently mapped anyway).

    Quality rules, all enforced here so the browser app and build_csv.py cannot
    diverge: ask the CDN for its largest rendition, refuse anything below
    MIN_IMAGE_EDGE as unusable on a product page, never upscale, and finish
    every main product image on a fixed 1:1 canvas - 1200x1200 when the source
    allows, 1000x1000 otherwise - so every catalogue tile is the same shape
    AND the same scale."""
    root = os.path.join(os.getcwd(), IMAGE_ROOT)
    os.makedirs(root, exist_ok=True)
    rejected = []

    # Save all images into a single root folder (no per-SKU subfolders)
    def _fetch(pair):
        index, url = pair
        response = requests.get(largest_rendition(url),
                                headers={'User-Agent': BROWSER_UA}, timeout=30)
        response.raise_for_status()
        image = Image.open(io.BytesIO(response.content))
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
        # downscale only - enlarging a small source invents detail and looks
        # worse than the original did
        if SQUARE_IMAGES:
            # pad first so the resize measures the finished square, then land
            # on exactly one of the two catalogue renditions
            image = pad_to_square(image)
            side = image.size[0]
            edge = CATALOG_EDGE if side >= CATALOG_EDGE else MIN_IMAGE_EDGE
            if side != edge:
                image = image.resize((edge, edge), Image.LANCZOS)
        elif max(image.size) > MAX_IMAGE_EDGE:
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

    saved_indexes = []
    workers = min(8, max(1, len(urls)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_fetch, (i, u)) for i, u in enumerate(urls, start=1)]
        for future in as_completed(futures):
            try:
                saved_indexes.append(future.result())
            except Exception:
                pass
    if rejected:
        _ui().warning(f'{sku}: skipped {len(rejected)} image(s) below '
                      f'{MIN_IMAGE_EDGE}px - ' + '; '.join(rejected[:3]))
    return sorted(saved_indexes)


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
    # sanity: an MRP at or below the selling price is bogus data
    if regular_price != '' and sale_price != '' and regular_price <= sale_price:
        regular_price = ''
    # MRP estimation is now opt-in: fabricated regular prices are not real
    if estimate_mrp and regular_price == '' and sale_price != '':
        regular_price = round(float(sale_price) * 1.25, 2)

    scraped = parse_scraped_specs(row.get('specs'))
    dynamic_attrs = product_specs(title, brand, category_family, scraped)

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
    seen_skus = set()
    skipped_duplicates = 0
    unmapped_categories = set()
    gap_counts = {}
    ui = _ui()
    if SUBCATEGORY_ATTRIBUTES_WARNING:
        ui.warning(SUBCATEGORY_ATTRIBUTES_WARNING)
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

        # BUGFIX: identical SKUs used to produce duplicate WooCommerce rows
        if sku in seen_skus:
            skipped_duplicates += 1
            continue
        seen_skus.add(sku)

        sale_price = ctx['sale price']
        regular_price = ctx['regular price']

        tracker.text(f'Compiling and downloading SKU: {sku} ({idx}/{total})')
        progress.progress(idx / max(total, 1))

        urls = filter_dropped_urls(split_image_urls(row),
                                   row.get('dropped_images'))
        saved_indexes = download_urls_for_sku(sku, urls)

        # BUGFIX: only map images that were really downloaded (previously a
        # minimum of 2 URLs was invented even when nothing was saved)
        mapped_images = ','.join([f'{media_base}/{sku}_{i}.webp' for i in saved_indexes])
        scraped = ctx['scraped']
        config, short_desc, long_desc, dynamic_attrs = build_descriptions(
            sku, title, brand, category_family, scraped)

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
            'Tags': build_tags(brand, parent_guess, sub_guess, title,
                               sale_price, category_family),
            'regular price': format_regular_price_output(regular_price),
            'sale price': format_price_output(sale_price),
            'Brands': brand,
            '_consolidated_sku': sku,
            'Images': mapped_images,
            # axes this product's own sub-category declares but nothing could
            # fill. Blank means complete. validate_csv.py reports these.
            '_spec_gaps': ', '.join(gaps),
        }

        if seo:
            output_row.update(seo_fields(
                sku, title, brand, category_hierarchy, dynamic_attrs,
                regular_price, sale_price, seo_plugin))

        for index, (attr_name, attr_value) in enumerate(dynamic_attrs, start=1):
            output_row[f'Attribute {index} name'] = attr_name
            output_row[f'Attribute {index} value(s)'] = attr_value
            output_row[f'Attribute {index} visible'] = '1'
            output_row[f'Attribute {index} global'] = '1'

        processed_rows.append(output_row)

    if skipped_duplicates:
        ui.warning(f'Skipped {skipped_duplicates} duplicate SKU row(s) after normalization.')
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
    if CATEGORY_MAP_FILE:
        st.caption(f'Category map loaded: {CATEGORY_MAP_FILE} '
                   f'({len(CATEGORY_PARENTS)} parent categories, '
                   f'{len(CATEGORY_COMBOS)} lookup entries)')
    else:
        st.warning('category_map.xlsx not found next to app.py - category names '
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
                        media_base=f'https://shreejeeelectronics.in/wp-content/uploads/{folder}',
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
