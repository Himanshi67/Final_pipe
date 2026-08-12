# blog_automate — hybrid blog generation for shreejeeelectronics.in

Isolated from the product pipeline. Nothing in the parent folder imports
anything from here; the only thing this borrows from the parent is
`wc_publish.py`'s session and CDN-challenge handling, because that logic was
expensive to get right and duplicating it would mean duplicating its bugs.

## The hybrid model

The product pipeline is deliberately LLM-free: the same SKU always produces the
same output. A blog post cannot be *fully* deterministic and still read well —
but the part that can go wrong is not the prose, it is the **facts**. So the two
are separated, and only the prose is generated:

```
  catalogue data
        |
        v
  blog_brief.py     the BRIEF: every fact the post may state, as data.
        |           Deterministic. No LLM. This is the source of truth.
        |
        +---------------------------+
        |                           |
        v                           v
  blog_render.py              blog_write.py      (optional, --llm)
  templates only              Claude writes ONLY prose, from the brief
  no LLM, ships today         and nothing else
        |                           |
        |                           v
        |                    blog_verify.py
        |                    every number and product name in the prose
        |                    must trace back to the brief, or the post
        |                    is held. No exceptions, no overrides by default.
        |                           |
        +---------------------------+
                    |
                    v
             blog_publish.py
             WordPress REST API. DRAFT unless --status publish.
```

**Why this shape:** an LLM that is never given a number cannot get a number
wrong, and a verifier that rejects any number not in the brief catches the case
where it invents one anyway. The deterministic renderer is not a fallback for
when the LLM is unavailable — it is the baseline the LLM has to beat, and it is
what runs by default.

## What the verifier actually checks

Not just "is this number in the brief" — that was checkable but not
attributable, and three of its holes were wide enough to drive a wrong price
through:

| Check | Why it exists |
|---|---|
| Numbers are scoped **per product** | The pooled set made every number valid everywhere, so a blurb could quote another product's price and pass. |
| Digits **and** number words | `NUMBER_RE` only saw digits, so "forty-three inches" was invisible. |
| A unit makes a small number a claim | 0–10 used to be unconditionally safe, so "5 star", "2 year warranty" and "8 kg" all passed unchecked. `one of these` is still ordinary English. |
| Aggregates are not measurements | A roundup of 2 products made "2 Ton" verifiable, because 2 was in the brief as the product count. The count and the price span are excluded wherever they cannot apply. |
| Model codes, any case | `Gls12i3fwaev` used to slip past an ALL-CAPS-only pattern. `3-star` is not a false positive. |
| Banned claims vs the brief | A product genuinely titled "… Best Selling …" is the store's own words — that is a warning, not a hold. Invented hype is still a hold. |

Run the suite after any change here:

```
.venv\Scripts\python.exe tests\test_blog_automate.py
```

## Post kinds

| Kind | What it is | Needs |
|---|---|---|
| `roundup` | "Top N <category> under Rs X" | a category, optionally a price cap |
| `comparison` | "A vs B", spec-by-spec | two or more SKUs |
| `guide` | Category buying guide | a category |

All three are built from the same brief structure, so a new kind is a new
template plus a new brief builder — not a new pipeline.

## Commands

```
:: what would be written, from the catalogue CSV - no LLM, no network
.venv\Scripts\python.exe blog_automate\blog_run.py roundup ^
    --source wordpress_ready_clean_repaired.csv --category LED --limit 5

:: same post, with Claude writing the prose (facts still come from the brief)
.venv\Scripts\python.exe blog_automate\blog_run.py roundup ^
    --source wordpress_ready_clean_repaired.csv --category LED --llm

:: a spec-by-spec comparison
.venv\Scripts\python.exe blog_automate\blog_run.py comparison ^
    --source wordpress_ready_clean_repaired.csv --sku AP-A --sku AP-B --llm

:: publish (DRAFT by default; nothing is sent without --commit)
.venv\Scripts\python.exe blog_automate\blog_run.py roundup --source out.csv ^
    --category LED --llm --publish --commit --cookie-file ..\cookie.txt
```

Every run writes the brief, the post HTML and the verification result into
`blog_automate\out\` so a human can read them before anything is published.

## Credentials

- `ANTHROPIC_API_KEY` — only for `--llm`. Without it the deterministic
  renderer runs and says so.
- `WP_APP_USER` / `WP_APP_PASSWORD` — WordPress Application Password, for
  publishing. Same variables `wc_publish.py` uses.
- `WC_URL` — the store URL.

Never in the repo, never in chat. Same rule as the rest of the project.

## What holds a publish

Everything here is a warning on a build-only run — you will often generate a
post before the products are imported — and a refusal on `--publish`.

| Hold | Override | Why |
|---|---|---|
| Products cannot be linked | `--allow-no-links` | No `WC_URL`, or the CSV has no `slug` column because `build_csv.py` ran without `--seo`. A buying guide that links to nothing is broken. |
| Images 404 (`--check-images`) | `--allow-missing-images` | Posts hotlink `wp-content/uploads/…`, which only fills up **after** the WooCommerce import. Publish first and every image is a 404. |
| A `--sku` is not in the CSV | — | Fix the SKU. It used to be dropped silently. |
| `--llm` fell back to templates | `--allow-fallback` | A missing key, an API error or a failed fact check used to publish the template post anyway — a different post from the one that was asked for. |
| Prose failed the fact check | — | `--allow-unverified` keeps it for inspection and can never publish. |
| The post was edited in WordPress | `--force-update` | An update overwrites title, body and excerpt wholesale, so a human's edits would be lost. |

## Deliberate limits

- **Drafts by default.** `--status publish` exists and is not the default.
- **The verifier has no bypass in the normal path.** `--allow-unverified`
  exists for inspecting a failure, and it refuses to publish.
- **No claims the data cannot support.** "Free delivery", "best price",
  "lowest", "guaranteed" and friends are rejected by the verifier whether a
  human or a model wrote them.
- **Prices go stale.** A brief records when it was built, and the renderer
  stamps the post with that date. Re-run rather than editing an old post.
- **A price is refused rather than guessed.** A cell holding a range
  (`28000-32000`), a zero, or anything that is not one number leaves the
  product out of the post with a reason. An unreadable *sale* price no longer
  falls back to the MRP — that quoted the struck-through price as the selling
  price, which is a wrong price rather than a missing one.
- **`--category` matches whole words.** `--category LED` no longer also
  matches "Bladeless" and "Sealed".

## Not covered

- No topic planner, scheduling, or interlinking between generated posts. What
  to write about is still a human decision.
- Deduplication against existing articles is by slug only. A post about the
  same products under a genuinely different title will not be spotted.
- Brand detection is a curated vocabulary (extend it with `known_brands.txt`
  next to this file, one per line, or `$BLOG_EXTRA_BRANDS`). An invented
  lower-case brand outside that list is not caught; an invented *model code*
  is.
