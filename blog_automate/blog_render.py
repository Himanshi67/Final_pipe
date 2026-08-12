"""blog_render.py - brief -> finished post HTML. Templates only, no LLM.

This is the baseline, not a fallback. It runs by default, it is fully
deterministic, and everything it writes is a restatement of the brief. When the
optional prose layer is on, its paragraphs are dropped into the same skeleton -
so the tables, prices and links are identical either way and only the narrative
changes.
"""

import html
import json
import re

from blog_brief import compact, headline, singular, spec_values

MAX_BLURB_CHARS = 900
EXCERPT_CHARS = 155
SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")


def esc(value):
    return html.escape(str(value or ""), quote=True)


def sentence_trim(text, limit):
    """Cut at the last sentence that fits, never mid-sentence.

    A hard slice produced blurbs ending 'which makes it a good choice for a'
    - the truncation read as a bug in the post rather than a length cap."""
    text = compact(text)
    if len(text) <= limit:
        return text
    window = text[:limit]
    ends = [m.end() for m in SENTENCE_END_RE.finditer(window)]
    if ends:
        return window[:ends[-1]].strip()
    if " " in window:
        return window.rsplit(" ", 1)[0].rstrip(" ,;:-") + "..."
    return window


def paragraphs(text):
    """Blank-line-separated prose stays separated. Everything the model wrote
    used to collapse into one <p>."""
    blocks = [compact(block) for block in re.split(r"\n\s*\n|\r\n\s*\r\n", str(text or ""))]
    return [block for block in blocks if block]


def prose_html(text, css_class=""):
    attr = f" class='{css_class}'" if css_class else ""
    return "".join(f"<p{attr}>{esc(block)}</p>" for block in paragraphs(text))


def product_link(product, site_url=""):
    """A product's storefront URL. Built from its slug when the SEO pass gave
    it one; otherwise the post links nowhere rather than to a guessed URL that
    would 404."""
    slug = product.get("slug")
    if not (slug and site_url):
        return ""
    return f"{site_url.rstrip('/')}/product/{slug}/"


def missing_links(brief, site_url=""):
    """SKUs this post cannot link to. A buying guide that links to nothing is
    broken in a way nothing else in the run would report."""
    return [p["sku"] for p in brief["products"] if not product_link(p, site_url)]


def external_images(brief):
    """Image URLs the post will hotlink. They point at the store's uploads
    folder, which only exists AFTER the WooCommerce import has run - publish
    the post first and every one of them is a 404."""
    return [(p["sku"], p["image"]) for p in brief["products"] if p["image"]]


def default_blurb(product, leaf=""):
    """The sentence used when no prose was generated for this product.

    Purely a restatement of the brief, like everything else here."""
    kind = compact(leaf) or compact(product["category"]).split(">")[-1].strip()
    subject = compact(f"{product['brand']} {singular(kind)}").strip() or product["name"]
    axes = [(label, value) for label, value in product["specs"].items() if value][:2]
    if not axes:
        return f"{subject}, listed as {product['name']}."
    stated = " and ".join(f"{value} {label.lower()}" for label, value in axes)
    return f"{subject} with {stated}, as stated on the product listing."


def spec_table(product, axes=()):
    rows = []
    labels = axes or list(product["specs"])
    for label in labels:
        value = product["specs"].get(label)
        if value:
            rows.append(f"<tr><th>{esc(label)}</th><td>{esc(value)}</td></tr>")
    if not rows:
        return ""
    return "<table class='blog-specs'><tbody>" + "".join(rows) + "</tbody></table>"


def price_line(product):
    if product["price"] is None:
        return ""
    if product["mrp"]:
        return (f"<p class='blog-price'><strong>{esc(product['price_text'])}</strong> "
                f"<span class='blog-mrp'>MRP {esc(product['mrp_text'])}</span></p>")
    return f"<p class='blog-price'><strong>{esc(product['price_text'])}</strong></p>"


def product_block(product, blurb="", site_url="", axes=(), leaf=""):
    link = product_link(product, site_url)
    name = esc(product["name"])
    heading = f"<a href='{esc(link)}'>{name}</a>" if link else name
    parts = [f"<h3>{heading}</h3>"]
    if product["image"]:
        parts.append(f"<p><img src='{esc(product['image'])}' alt='{name}' "
                     f"loading='lazy' /></p>")
    parts.append(price_line(product))
    text = compact(blurb) or default_blurb(product, leaf)
    parts.append(prose_html(sentence_trim(text, MAX_BLURB_CHARS)))
    parts.append(spec_table(product, axes))
    return "".join(p for p in parts if p)


def default_intro(brief):
    """Facts, arranged into a sentence. Deliberately plain - this is the
    version that ships when nobody has asked for generated prose."""
    leaf = brief["category_leaf"] or "catalogue"
    count = brief["product_count"]
    # "N products from the <leaf> range" stays grammatical whatever the leaf
    # is; "N mini led" and "N washing machines products" do not
    noun = "product" if count == 1 else "products"
    bits = [f"{count} {noun} from the {leaf} range at {brief['site_name']}."]
    if brief["price_low"] is not None and brief["price_high"] is not None:
        if brief["price_low"] == brief["price_high"]:
            bits.append(f"They are priced at {brief['price_low_text']}.")
        else:
            bits.append(f"Prices run from {brief['price_low_text']} to "
                        f"{brief['price_high_text']}.")
    if brief["brands"]:
        bits.append("Brands covered: " + ", ".join(brief["brands"]) + ".")
    return " ".join(bits)


def default_conclusion(brief):
    return (f"Prices and specifications above were recorded on "
            f"{brief['built_at']} and can change. "
            f"Check the product page for the current price.")


def comparison_table(brief):
    products = brief["products"]
    axes = brief["common_specs"]
    if len(products) < 2:
        return ""
    head = "".join(f"<th>{esc(p['name'])}</th>" for p in products)
    rows = ["<tr><th>Price</th>"
            + "".join(f"<td>{esc(p['price_text'])}</td>" for p in products)
            + "</tr>"]
    for label in axes:
        cells = "".join(f"<td>{esc(p['specs'].get(label, ''))}</td>" for p in products)
        rows.append(f"<tr><th>{esc(label)}</th>{cells}</tr>")
    return ("<div class='blog-table-scroll'>"
            f"<table class='blog-compare'><thead><tr><th></th>{head}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def spec_glossary(brief):
    """For a guide: what each shared axis actually varies across in THIS set.

    'stated on every model below' told the reader nothing. The values come
    from the brief, so this stays a restatement rather than advice."""
    items = []
    for label in brief["common_specs"]:
        values = spec_values(brief["products"], label)
        if not values:
            continue
        if len(values) == 1:
            detail = f"every model here states {values[0]}"
        else:
            detail = "the models here state " + ", ".join(values[:6])
            if len(values) > 6:
                detail += f" and {len(values) - 6} more"
        items.append(f"<li><strong>{esc(label)}</strong> — {esc(detail)}.</li>")
    return "<ul>" + "".join(items) + "</ul>" if items else ""


def schema_ld(brief, title, site_url=""):
    """schema.org ItemList so a roundup can be understood as a list of products
    rather than as undifferentiated text. Prices come from the brief, so the
    markup cannot disagree with the visible table.

    WordPress strips <script> for users without unfiltered_html; the post is
    complete without it, which is why nothing else depends on it."""
    items = []
    for position, product in enumerate(brief["products"], start=1):
        entry = {
            "@type": "Product",
            "name": product["name"],
            "sku": product["sku"],
        }
        if product["brand"]:
            entry["brand"] = {"@type": "Brand", "name": product["brand"]}
        if product["image"]:
            entry["image"] = product["image"]
        if product["price"] is not None:
            entry["offers"] = {
                "@type": "Offer",
                "price": str(product["price"]),
                "priceCurrency": "INR",
            }
            link = product_link(product, site_url)
            if link:
                entry["offers"]["url"] = link
        items.append({"@type": "ListItem", "position": position, "item": entry})

    payload = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": title,
        "numberOfItems": len(items),
        "itemListElement": items,
    }
    body = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return f"<script type='application/ld+json'>{body}</script>"


def render(brief, prose=None, site_url="", schema=True, title=""):
    """The post. `prose` is the optional generated narrative; when it is None
    the deterministic text is used instead."""
    prose = prose or {}
    intro = compact(prose.get("intro")) or default_intro(brief)
    conclusion = compact(prose.get("conclusion")) or default_conclusion(brief)
    blurbs = prose.get("blurbs") or {}
    leaf = brief["category_leaf"]

    parts = [prose_html(intro, "blog-intro")]

    if brief["kind"] == "comparison":
        table = comparison_table(brief)
        if table:
            parts.append("<h2>Side by side</h2>")
            parts.append(table)

    if brief["kind"] == "guide":
        glossary = spec_glossary(brief)
        if glossary:
            parts.append("<h2>What the specifications mean</h2>")
            parts.append(glossary)

    if brief["products"]:
        parts.append("<h2>The products</h2>")
        axes = brief["common_specs"] if brief["kind"] == "comparison" else ()
        for product in brief["products"]:
            parts.append(product_block(product, blurbs.get(product["sku"], ""),
                                       site_url, axes, leaf))

    parts.append(prose_html(conclusion, "blog-outro"))
    # the freshness stamp is always the deterministic one, never generated -
    # a wrong date on a price is worse than no date
    if compact(prose.get("conclusion")):
        parts.append(f"<p class='blog-stamp'><em>{esc(default_conclusion(brief))}</em></p>")
    if schema:
        parts.append(schema_ld(brief, title or headline(brief), site_url))
    return "\n".join(p for p in parts if p)


def excerpt(brief, prose=None):
    text = compact(compact((prose or {}).get("intro")) or default_intro(brief))
    return sentence_trim(text, EXCERPT_CHARS).rstrip(" ,;:")


def post_tags(brief):
    """Post tags, straight from the brief: the brands covered and the leaf
    category. Tags were never sent, so every generated post landed untagged."""
    tags = [brief["category_leaf"]] if brief["category_leaf"] else []
    tags += [b for b in brief["brands"] if b]
    seen, out = set(), []
    for tag in tags:
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            out.append(tag)
    return out


def post_payload(brief, prose=None, site_url="", status="draft", schema=True):
    title = compact((prose or {}).get("title")) or headline(brief)
    return {
        "title": title,
        "content": render(brief, prose, site_url, schema=schema, title=title),
        "excerpt": excerpt(brief, prose),
        "status": status,
        "tags": post_tags(brief),
    }
