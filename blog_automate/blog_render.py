"""blog_render.py - brief -> finished post HTML. Templates only, no LLM.

This is the baseline, not a fallback. It runs by default, it is fully
deterministic, and everything it writes is a restatement of the brief. When the
optional prose layer is on, its paragraphs are dropped into the same skeleton -
so the tables, prices and links are identical either way and only the narrative
changes.
"""

import html
import re

from blog_brief import headline, rupees

MAX_BLURB_CHARS = 900


def esc(value):
    return html.escape(str(value or ""), quote=True)


def product_link(product, site_url=""):
    """A product's storefront URL. Built from its slug when the SEO pass gave
    it one; otherwise the post links nowhere rather than to a guessed URL that
    would 404."""
    slug = product.get("slug")
    if not (slug and site_url):
        return ""
    return f"{site_url.rstrip('/')}/product/{slug}/"


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


def product_block(product, blurb="", site_url="", axes=()):
    link = product_link(product, site_url)
    name = esc(product["name"])
    heading = f"<a href='{esc(link)}'>{name}</a>" if link else name
    parts = [f"<h3>{heading}</h3>"]
    if product["image"]:
        parts.append(f"<p><img src='{esc(product['image'])}' alt='{name}' "
                     f"loading='lazy' /></p>")
    parts.append(price_line(product))
    if blurb:
        parts.append(f"<p>{esc(blurb)}</p>")
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
    rows = [f"<tr><th>Price</th>"
            + "".join(f"<td>{esc(p['price_text'])}</td>" for p in products)
            + "</tr>"]
    for label in axes:
        cells = "".join(f"<td>{esc(p['specs'].get(label, ''))}</td>" for p in products)
        rows.append(f"<tr><th>{esc(label)}</th>{cells}</tr>")
    return (f"<table class='blog-compare'><thead><tr><th></th>{head}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


def render(brief, prose=None, site_url=""):
    """The post. `prose` is the optional generated narrative; when it is None
    the deterministic text is used instead."""
    prose = prose or {}
    intro = prose.get("intro") or default_intro(brief)
    conclusion = prose.get("conclusion") or default_conclusion(brief)
    blurbs = prose.get("blurbs") or {}

    parts = [f"<p class='blog-intro'>{esc(intro)}</p>"]

    if brief["kind"] == "comparison":
        table = comparison_table(brief)
        if table:
            parts.append("<h2>Side by side</h2>")
            parts.append(table)

    if brief["kind"] == "guide" and brief["common_specs"]:
        parts.append("<h2>What the specifications mean</h2>")
        parts.append("<ul>" + "".join(
            f"<li><strong>{esc(label)}</strong> — stated on every model below.</li>"
            for label in brief["common_specs"]) + "</ul>")

    if brief["products"]:
        parts.append("<h2>The products</h2>")
        axes = brief["common_specs"] if brief["kind"] == "comparison" else ()
        for product in brief["products"]:
            blurb = blurbs.get(product["sku"], "")[:MAX_BLURB_CHARS]
            parts.append(product_block(product, blurb, site_url, axes))

    parts.append(f"<p class='blog-outro'>{esc(conclusion)}</p>")
    # the freshness stamp is always the deterministic one, never generated -
    # a wrong date on a price is worse than no date
    if prose.get("conclusion"):
        parts.append(f"<p class='blog-stamp'><em>{esc(default_conclusion(brief))}</em></p>")
    return "\n".join(parts)


def excerpt(brief, prose=None):
    text = (prose or {}).get("intro") or default_intro(brief)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:155].rsplit(" ", 1)[0] if len(text) > 155 else text


def post_payload(brief, prose=None, site_url="", status="draft"):
    title = (prose or {}).get("title") or headline(brief)
    return {
        "title": title,
        "content": render(brief, prose, site_url),
        "excerpt": excerpt(brief, prose),
        "status": status,
    }
