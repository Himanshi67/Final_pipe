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

## Deliberate limits

- **Drafts by default.** `--status publish` exists and is not the default.
- **The verifier has no bypass in the normal path.** `--allow-unverified`
  exists for inspecting a failure, and it refuses to publish.
- **No claims the data cannot support.** "Free delivery", "best price",
  "lowest", "guaranteed" and friends are rejected by the verifier whether a
  human or a model wrote them.
- **Prices go stale.** A brief records when it was built, and the renderer
  stamps the post with that date. Re-run rather than editing an old post.
