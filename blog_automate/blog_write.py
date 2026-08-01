"""blog_write.py - the OPTIONAL prose layer. Claude writes narrative only.

The model is handed the brief and nothing else, and is asked for prose only:
an intro, one short blurb per product, a conclusion. It never supplies a
price, a spec, a product name, or a link - the renderer already has those from
the brief, and blog_verify.py rejects any number the brief does not contain.

That division is the whole point of the hybrid design: a model that is never
asked for a fact cannot get a fact wrong, and the verifier catches it if it
volunteers one anyway.

Needs ANTHROPIC_API_KEY. Without it, blog_run.py falls back to the
deterministic renderer and says so.
"""

import json
import os

MODEL = "claude-opus-5"
MAX_TOKENS = 16000

# Kept identical across runs so it caches: the brief varies per post, the
# instructions do not. Prompt caching is a prefix match, so the stable half
# must come first - which it does, since `system` renders before `messages`.
SYSTEM_PROMPT = """You write product blog posts for an Indian home-appliance \
retailer. You are one half of a two-part system: a deterministic program \
supplies every fact, and you supply only the prose that connects them.

WHAT YOU ARE GIVEN
A JSON brief containing the products, their prices, and their specifications. \
It is the complete set of facts available. There is nothing else.

WHAT YOU WRITE
- intro: 2-4 sentences introducing what the post covers.
- blurbs: one per product, keyed by SKU, 2-3 sentences each, describing what \
that product's stated specifications mean for someone using it.
- conclusion: 1-2 sentences closing the post.
- title: a plain, accurate headline.

HARD RULES - these are checked mechanically after you write, and a violation \
means the post is discarded:

1. Never state a number that is not in the brief. Not a price, not a capacity, \
not a rating, not a year, not a count. If you want to say a TV is large, say \
it is large - do not reach for an inch figure the brief did not give you.
2. Never name a product, brand, or model the brief does not list.
3. Never make a claim the brief cannot support. No "best price", no "lowest", \
no "cheapest", no "free delivery", no "guaranteed", no "limited time", no \
"#1", no award or ranking, no comparison to a competitor's pricing.
4. Never invent availability, delivery, warranty, or return terms.
5. Do not restate prices or specification values in your prose. The template \
prints those from the brief in its own tables - repeating them just creates \
two places for them to disagree.

STYLE
Plain, useful, specific. Write for someone deciding between these products. \
Explain what a specification means in practice rather than repeating its \
value. No hype, no exclamation marks, no "in today's fast-paced world". \
Indian English. Do not address the reader as "dear customer"."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "intro": {"type": "string"},
        "blurbs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["sku", "text"],
                "additionalProperties": False,
            },
        },
        "conclusion": {"type": "string"},
    },
    "required": ["title", "intro", "blurbs", "conclusion"],
    "additionalProperties": False,
}


def brief_for_model(brief):
    """The brief, trimmed to what prose actually needs. Images and slugs are
    the renderer's business and would only be noise here."""
    return {
        "kind": brief["kind"],
        "category": brief["category_leaf"],
        "site_name": brief["site_name"],
        "product_count": brief["product_count"],
        "price_low": brief["price_low"],
        "price_high": brief["price_high"],
        "brands": brief["brands"],
        "products": [
            {
                "sku": p["sku"],
                "name": p["name"],
                "brand": p["brand"],
                "price": p["price"],
                "mrp": p["mrp"],
                "specs": p["specs"],
            }
            for p in brief["products"]
        ],
    }


def available():
    return bool(os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def write(brief, effort="medium"):
    """Returns {'title','intro','blurbs':{sku: text},'conclusion'}.

    Raises RuntimeError with a readable message on any API problem, so
    blog_run.py can fall back to the deterministic renderer rather than dying.
    """
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "The anthropic package is not installed. Run:\n"
            "  .venv\\Scripts\\python.exe -m pip install anthropic\n"
            "or drop --llm to use the deterministic renderer.")

    client = anthropic.Anthropic()
    payload = json.dumps(brief_for_model(brief), indent=2)

    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            # a policy decline would otherwise just stop the run; this re-runs
            # the same request on Anthropic's recommended fallback instead
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            thinking={"type": "adaptive"},
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
            messages=[{
                "role": "user",
                "content": f"Write the post for this brief.\n\n{payload}",
            }],
        )
    except Exception as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}")

    # a refusal returns HTTP 200 with an empty or partial content list, so
    # reading content[0] first would raise IndexError instead of explaining
    if response.stop_reason == "refusal":
        detail = getattr(response, "stop_details", None)
        category = getattr(detail, "category", None) if detail else None
        raise RuntimeError(
            f"The model declined this brief"
            + (f" (category: {category})" if category else "")
            + ". Nothing was written.")

    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        raise RuntimeError("The model returned no text.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"The model's output was not valid JSON: {exc}")

    known = {p["sku"] for p in brief["products"]}
    blurbs = {}
    for item in data.get("blurbs", []):
        sku = str(item.get("sku", "")).strip()
        # a blurb for a SKU that is not in the brief is dropped here rather
        # than shown; the verifier would reject it anyway
        if sku in known:
            blurbs[sku] = str(item.get("text", "")).strip()

    return {
        "title": str(data.get("title", "")).strip(),
        "intro": str(data.get("intro", "")).strip(),
        "blurbs": blurbs,
        "conclusion": str(data.get("conclusion", "")).strip(),
        "_usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_read": getattr(response.usage, "cache_read_input_tokens", 0),
        },
        "_model": response.model,
    }
