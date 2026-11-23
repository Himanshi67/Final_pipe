"""gui_helpers.py - pure, streamlit-free helpers behind gui.py.

Unit-tested in tests/test_gui_helpers.py. Keep streamlit (and app.py)
imports OUT of this module so the tests never boot the app."""

import hashlib
import hmac
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

_URL_RE = re.compile(r"^https?://", re.I)


def csv_signature(path) -> str | None:
    """Identity of the CSV file on disk: `"<mtime_ns>:<size>"`, or None when
    it is missing (or unreadable).

    Step 5 pins its "a dry run succeeded" flag to this string instead of a
    bare bool: a rebuild in Step 3 rewrites wordpress_ready.csv, the stored
    signature stops matching, and Commit re-locks. Size catches every scope
    change (different row count -> different length); mtime_ns catches a
    same-length rebuild after a review edit. The one gap is a same-length
    rewrite landing inside the filesystem's mtime tick (on this Windows box
    ~1 in 4 back-to-back writes reuse st_mtime_ns) - a real Step-3 build
    downloads images and takes seconds, far longer than that tick.
    Deliberately NOT a content hash: this runs on every render."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def flag_value(flags, name):
    """Integer value following `name` in an argv-style flag list; 0 if the
    flag is absent, last, or its value is not a number."""
    flags = list(flags or [])
    try:
        return int(str(flags[flags.index(name) + 1]))
    except (ValueError, IndexError):
        return 0


def limit_from_flags(flags):
    """The `--limit N` cap as a positive int, or None when there is none.

    Knowable from argv ALONE - no spreadsheet needed - which is the whole
    point: gui.eligible_total can apply it on a FIRST launch, before
    <stem>_filled.xlsx exists. Without it the documented `--limit 5` smoke
    run on a 300-row sheet wrote enricher_total=300 and the panel showed
    '3 / 300' with a ~50x ETA (P2-28 on the most common launch path)."""
    limit = flag_value(flags, "--limit")
    return limit if limit > 0 else None


def sanity_issues(df, min_price=0):
    """Row-level problems worth fixing BEFORE the CSV build.

    df is the review dataframe from gui.load_filled_df: prices already
    numeric (NaN when missing), url_link a string. Returns
    [{"sku": ..., "problem": ...}, ...] in row order."""
    issues = []
    for _, row in df.iterrows():
        sku = str(row.get("sku", "")).strip()

        def add(problem):
            issues.append({"sku": sku, "problem": problem})

        mrp = row.get("regular price")
        sale = row.get("sale price")
        url = str(row.get("url_link", "") or "").strip()

        if pd.notna(mrp) and mrp <= 0:
            add(f"non-positive MRP ({mrp:.0f})")
        if pd.notna(sale) and sale <= 0:
            add(f"non-positive sale price ({sale:.0f})")
        if pd.notna(mrp) and pd.notna(sale) and mrp > 0 and sale > mrp:
            add(f"sale price {sale:.0f} above MRP {mrp:.0f}")
        if min_price and pd.notna(sale) and 0 < sale < min_price:
            add(f"sale price {sale:.0f} below batch min price {min_price}")
        if url and not _URL_RE.match(url):
            add("url_link is not an http(s) URL")
    return issues


def filter_review_df(df, query="", brands=None):
    """Search + brand filter for the review table. Case-insensitive
    substring match of `query` against sku and title (regex-escaped), then
    an exact Brands membership filter. Original index is preserved so
    edits still map back by SKU."""
    out = df
    q = str(query or "").strip()
    if q:
        title = (out["title"] if "title" in out.columns
                 else pd.Series("", index=out.index))
        hay = (out["sku"].astype(str) + " " + title.astype(str)).str.lower()
        out = out[hay.str.contains(re.escape(q.lower()), na=False)]
    if brands:
        out = out[out["Brands"].astype(str).isin(list(brands))]
    return out


def _price_series(s):
    """'22,599' / '₹22,599' → 22599.0; blanks → NaN. Same stripping the
    review loader applies (gui.load_filled_df)."""
    return pd.to_numeric(
        s.astype(str).str.replace(",", "", regex=False)
         .str.replace("₹", "", regex=False).str.strip(),
        errors="coerce")


def batch_summary(result):
    """Aggregate stats over app.process_inventory() output (string cells).
    Answers 'is this batch import-ready?' at a glance."""
    imgs = result["Images"].astype(str).str.strip().map(
        lambda v: len([u for u in v.split(",") if u.strip()]) if v else 0)
    sale = _price_series(result["sale price"])
    has_price = sale.notna().any()
    return {
        "products": len(result),
        "zero_image": int((imgs == 0).sum()) if len(result) else 0,
        "avg_images": round(float(imgs.mean()), 1) if len(result) else 0.0,
        "spec_gap_rows": int((result["_spec_gaps"].astype(str)
                              .str.strip() != "").sum()),
        # filter values stated but not in the filter sheet (app.py writes
        # them to _filter_unmatched); 0 for output that predates the column
        "filter_unmatched_rows": int((result["_filter_unmatched"].astype(str)
                                      .str.strip().replace("nan", "") != "")
                                     .sum()) if "_filter_unmatched" in result
                                 else 0,
        "missing_brand": int((result["Brands"].astype(str)
                              .str.strip() == "").sum()),
        "price_min": int(sale.min()) if has_price else 0,
        "price_max": int(sale.max()) if has_price else 0,
        # the CSV cell escapes a comma inside a category NAME as '\,' (the
        # WooCommerce importer would otherwise split it); show the readable
        # form in the summary
        "category_counts": result["categories"].astype(str).str.strip()
                                 .str.replace("\\,", ",", regex=False)
                                 .replace("", "(none)")
                                 .value_counts().to_dict(),
    }


# split only at commas that start a new URL - same rule as
# app.split_image_list, so commas inside a URL survive
_IMG_SPLIT_RE = re.compile(r",(?=\s*(?:https?://|//))")

# mirror of app.prefixed_sku's sanitization - keep the two in step or the
# review back-map (raw_sku_for_csv_sku) stops recognising its own SKUs.
# P1-18: a whitelist, exactly as app.prefixed_sku now does it.
_SKU_SANITIZE = (
    (re.compile(r"\s+"), "-"),
    (re.compile(r"[^A-Za-z0-9._-]+"), "-"),
    (re.compile(r"-{2,}"), "-"),
)


def parse_image_urls(cell):
    """Comma-separated image URLs from a review-sheet cell -> list."""
    text = str(cell or "").strip()
    if not text:
        return []
    return [u.strip() for u in _IMG_SPLIT_RE.split(text) if u.strip()]


def parse_specs_cell(cell):
    """'Label: Value | Label: Value' (sku_enricher.format_specs) ->
    [(label, value)]. format_specs strips ':' and '|' out of labels, so
    splitting on ' | ' then the FIRST ':' is lossless."""
    pairs = []
    for part in str(cell or "").split(" | "):
        if ":" in part:
            label, value = part.split(":", 1)
            if label.strip() and value.strip():
                pairs.append((label.strip(), value.strip()))
    return pairs


# The same codes app.SKU_PREFIXES carries, read from the SAME source of truth
# (the client's [sku] table) instead of hand-copied - the two used to drift.
# app itself is still NOT imported here: that would drag Streamlit and the
# taxonomy workbook into every gui_helpers test, while client_config is
# stdlib-only. tests/test_gui_helpers.py still pins the two lists together.
SKU_CODES = ("WM", "WH", "TV", "AC", "CF", "KA", "HA", "SA", "EC", "AP")
_SKU_CODE_RE = None
_SPECIFIC_CODE_RE = None


def configure_sku_codes(prefixes):
    """Rebind SKU_CODES and its two regexes for another client's scheme.

    gui.switch_client() calls this the moment the sidebar's Client selector
    moves, so the review helpers stop stripping the previous store's codes.
    `prefixes` is client_config.SkuScheme.prefixes: the taxonomy parents'
    codes in TOML order, with the fallback code LAST (that ordering is what
    lets the 'specific code' regex know which one to leave out)."""
    global SKU_CODES, _SKU_CODE_RE, _SPECIFIC_CODE_RE
    codes = tuple(c for c in
                  (str(p or "").strip().upper() for p in (prefixes or ()))
                  if c)
    if not codes:
        raise ValueError("configure_sku_codes: no SKU prefixes given")
    SKU_CODES = codes
    _SKU_CODE_RE = re.compile(
        r"^(?:" + "|".join(re.escape(c) for c in codes) + r")-")
    # every code EXCEPT the fallback (the last one): a raw carrying one of
    # these states a category, so re-coding it means the sheet contradicted
    # the detected one.
    _SPECIFIC_CODE_RE = re.compile(
        r"^(?:" + "|".join(re.escape(c) for c in codes[:-1] or codes) + r")-")


try:                                   # the client's own scheme, at import
    import client_config as _client_config
    configure_sku_codes(_client_config.load_client().sku.prefixes)
except Exception:                      # no clients/ dir yet, bad toml, …
    configure_sku_codes(SKU_CODES)     # the historical Shree Jee tuple above


def _strip_code(s):
    """Remove one leading '<CODE>-' category prefix, if there is one."""
    return _SKU_CODE_RE.sub("", str(s or ""), count=1)


def _sanitized_sku(raw):
    # app.compact() treats a NaN cell as empty; str(nan) would sanitize to the
    # literal 'nan' here while app.prefixed_sku made 'AP-UNKNOWN' from the same
    # cell, so the back-map matched a junk row to nothing at best (NaN != NaN).
    s = "" if raw is None or raw != raw else str(raw).strip()
    for rx, rep in _SKU_SANITIZE:
        s = rx.sub(rep, s)
    return s.strip("-")


def raw_sku_for_csv_sku(csv_sku, raw_skus):
    """Map a CSV/validation sku (app.prefixed_sku output) back to the raw
    sku the review sheet holds. The prefixed form is sanitize(raw) with an
    optional 'XX-' category prefix, so match sanitized suffixes; the
    longest raw wins; None when nothing matches.

    P1-13: the raw sku may ALREADY carry a category code, and prefixed_sku
    re-codes a code that contradicts the detected category ('AP-X200' under
    Kitchen Appliances becomes 'KA-X200'). Neither the exact nor the suffix
    rule sees through that, so a re-coded row silently lost its link back to
    the review sheet; compare with both codes stripped as well.

    The rules are RANKED, not OR'd: an exact preimage beats a suffix
    match, which beats a code-stripped match, and only inside one rank does
    the longest raw win. Flattening them let a code-stripped guess outrank a
    true suffix match - with raws 'X200' and 'AC-X200' on one sheet, the CSV
    sku 'HA-X200' (from 'X200') came back as 'AC-X200', a different product -
    and made an exact hit lose to whatever came first in the list.

    The code-stripped rank is split again, and ties inside it refuse to
    guess. Two raws that already carry DIFFERENT category codes over the same
    stem ('SA-X200' and 'AP-X200', CSV sku 'KA-X200') were previously settled
    by list order, so reversing the sheet changed which product a card showed
    - one card wore the other's validation reasons and 'Fix in review' jumped
    the operator to the wrong SKU. 'AP-' is the documented fallback code for
    'the taxonomy did not know' (app.SKU_PREFIX_BY_PARENT), so an AP- raw is
    the one prefixed_sku re-codes as a matter of course and it wins over a raw
    whose specific code contradicts the CSV's; when the tie is between two
    specific codes nothing local can separate them, and None (no flags, no
    jump) is safer than picking someone else's product."""
    c = str(csv_sku or "").strip()
    c_bare = _strip_code(c)
    best, best_rank, best_len = None, (-1, -1), -1
    tied = 0
    for raw in raw_skus:
        s = _sanitized_sku(raw)
        if not s:
            continue
        if c == s:
            rank = (3, 0)
        elif c.endswith("-" + s):
            rank = (2, 0)
        elif c_bare == _strip_code(s):
            # sub-rank: a re-coded AP- (or uncoded) raw beats a raw whose own
            # specific code contradicts the CSV sku's
            rank = (1, 0 if _SPECIFIC_CODE_RE.match(s) else 1)
        else:
            continue
        if (rank, len(s)) > (best_rank, best_len):
            best, best_rank, best_len, tied = raw, rank, len(s), 1
        elif (rank, len(s)) == (best_rank, best_len) and raw != best:
            tied += 1
    if best_rank[0] == 1 and tied > 1:
        return None                    # genuinely ambiguous - do not guess
    return best


def classify(status) -> str:
    s = str(status or "").strip()
    u = s.upper()
    # two-pass flow (--discover): a link is waiting to be looked at, and the
    # row is deliberately NOT importable yet. These must not read as failures.
    if u.startswith("NEEDS CRAWL"):
        return "review" if "CHECK LINK" in u else "pending"
    if u.startswith("NEEDS LINK"):
        return "failed"      # nothing found - a human has to paste a URL
    if u.startswith("FOUND"):
        return "review" if " - " in s else "found"
    if u.startswith(("CLOSE", "CHECK")):
        return "review"
    if u.startswith("SKIPPED"):
        return "skipped"
    return "failed"          # NOT FOUND / ERROR / blank (not processed)


BADGE = {"found": "🟢", "review": "🟡", "failed": "🔴", "skipped": "⚪",
         "pending": "🔵"}

_ROW_RE = re.compile(r"^\[(\d+)\] Row \d+", re.M)
_STATUS_RE = re.compile(r"^    -> (.+)$", re.M)
_SERPER_RE = re.compile(r"^\s*\[(\d+) serper\.dev call\(s\) this run", re.M)
# P2-27: the running tally above is printed only every 10 rows
# (sku_enricher.py `if RUN_STATS["serper_calls"] and done % 10 == 0`), so a
# short run has NO bracketed line at all and a 25-row run's last one is stale.
# The end-of-run summary uses a different grammar and has to be matched too.
# `Budget of N serper credit(s) reached - stopping, progress saved.` - the
# exact line sku_enricher.py logs when --budget trips.
_BUDGET_RE = re.compile(
    r"^Budget of \d+ serper credit\(s\) reached", re.M)
_SERPER_FINAL_RE = re.compile(
    r"^serper\.dev calls this run: (\d+)", re.M)


def parse_enricher_log(text):
    """Progress facts from the enricher's log. Grammar pinned by
    sku_enricher.py's log() calls - see the plan header for line formats."""
    done_hits = _ROW_RE.findall(text or "")
    buckets = {}
    for status in _STATUS_RE.findall(text or ""):
        b = classify(status)
        buckets[b] = buckets.get(b, 0) + 1
    serper = _SERPER_RE.findall(text or "")
    serper_final = _SERPER_FINAL_RE.findall(text or "")
    return {
        # sku_enricher's --budget stop (docs/CLIENT_CONFIG.md item 4). The run
        # exits 0 and the sheet is saved, so nothing else in the log says the
        # batch is UNFINISHED - without this the panel reads like a clean end.
        "stopped_by_budget": bool(_BUDGET_RE.search(text or "")),
        "done": int(done_hits[-1]) if done_hits else 0,
        "buckets": buckets,
        # the summary line is authoritative when present, but a run killed
        # mid-flight has only the tally - take whichever is higher
        "serper_calls": max(int(serper[-1]) if serper else 0,
                            int(serper_final[-1]) if serper_final else 0),
        "cached_searches": (text or "").count("(cached search)"),
        "cached_pages": (text or "").count("(cached page"),
    }


def _cell_text(value) -> str:
    """A sheet cell as text. pd.read_excel(dtype=str) leaves a blank cell as
    NaN, and `str(nan or "")` is the string 'nan' - which read as a url_link
    and made every blank row look linked."""
    if value is None or value != value:
        return ""
    return str(value).strip()


def _cell_filled(value) -> bool:
    """openpyxl truthiness of a sheet cell: None, NaN, '' and 0 are empty
    (the enricher tests `not ws.cell(...).value`)."""
    if value is None or value != value:
        return False
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, (int, float)):
        return value != 0
    return bool(str(value).strip())


def eligible_rows(df, flags) -> int:
    """How many rows this invocation will actually process - the ETA total.

    P2-28: every start_enricher call used to pass the FULL sheet row count,
    so a --retry-incomplete run over 5 of 40 rows showed '3 / 40' and
    '~37 min left' and then stopped at 5/40. This mirrors sku_enricher.py's
    per-row skip rules (the `for r in range(2, ws.max_row + 1)` loop, whose
    `continue`s all fire BEFORE `done += 1`), one branch per flag:

      --crawl-urls       a link is required; a FOUND row is already crawled
      --discover         only rows with no link and nothing tried yet
      --retry-incomplete a linked row whose FOUND carries a ' - ' suffix, or
                         that has no sale price / no image_url
      --retry-missing    rows with no link that were tried and failed
      plain              untouched rows only

    A row whose status mentions 'blocked' is transient and stays eligible;
    --force takes every row (bar a crawl-urls row with no link); --limit
    caps the count exactly as the enricher's `if args.limit and done >=
    args.limit: break` does. `df` is the filled sheet as strings."""
    flags = list(flags or [])
    force = "--force" in flags
    crawl = "--crawl-urls" in flags
    discover = "--discover" in flags
    retry_incomplete = "--retry-incomplete" in flags
    retry_missing = "--retry-missing" in flags

    def cell(row, name):
        return row[name] if name in row.index else None

    n = 0
    for _, row in df.iterrows():
        if not _cell_filled(cell(row, "sku")):
            continue
        url = _cell_text(cell(row, "url_link"))
        status = _cell_text(cell(row, "status"))
        if crawl:
            if not url:
                continue                  # gets a NEEDS LINK stamp, not a crawl
            if not force and status.startswith("FOUND"):
                continue
        elif discover:
            if not force and url:
                continue
            if not force and status and not retry_missing:
                continue
        elif not force:
            if url:
                incomplete = ((status.startswith("FOUND") and " - " in status)
                              or not _cell_filled(cell(row, "sale price"))
                              or not _cell_filled(cell(row, "image_url")))
                if not (retry_incomplete and incomplete):
                    continue
            elif status and not retry_missing:
                if "blocked" not in status.lower():
                    continue
        n += 1
    limit = limit_from_flags(flags)
    return min(n, limit) if limit else n


def eta_text(done, total, started_epoch, now_epoch):
    """Remaining-time estimate from this run's average pace; '' when it
    cannot be estimated honestly."""
    if not done or not total or not started_epoch or done >= total:
        return ""
    per = (now_epoch - started_epoch) / done
    if per <= 0:
        return ""
    minutes = int(per * (total - done) // 60)
    return f"~{minutes} min left" if minutes >= 1 else "under a minute left"


_PUB_ACTION_RE = re.compile(r"^  (CREATE|UPDATE) (\S+)$")
_PUB_ID_RE = re.compile(r"^    -> id (\d+)\s+(\S+)")
_PUB_FAIL_RE = re.compile(r"^    !! (.+)")
_PUB_SUMMARY_RE = re.compile(
    r"^Done: (\d+) created, (\d+) updated, (\d+) failed\.")


def parse_publish_output(text):
    """wc_publish.py stdout -> (rows, totals). Grammar pinned by
    wc_publish.py's print calls; dry runs emit action lines but no
    outcome, so result stays ''. Taxonomy side-effect lines ('~ would
    create …') are deeper-indented and never match the action regex.

    P2-34: a >150-SKU publish is chunked by gui.run_publish_pinned and the
    chunk outputs are concatenated, so there is one 'Done:' line PER chunk.
    The totals are SUMMED, not overwritten - keeping only the last chunk's
    line hid every failure the earlier chunks reported."""
    rows, totals, current = [], {}, None
    for ln in (text or "").splitlines():
        m = _PUB_ACTION_RE.match(ln)
        if m:
            current = {"sku": m.group(2), "action": m.group(1).lower(),
                       "result": "", "link": "", "error": ""}
            rows.append(current)
            continue
        if current is not None:
            m = _PUB_ID_RE.match(ln)
            if m:
                current["result"], current["link"] = "ok", m.group(2)
                continue
            m = _PUB_FAIL_RE.match(ln)
            if m:
                current["result"], current["error"] = "failed", m.group(1)
                continue
        m = _PUB_SUMMARY_RE.match(ln)
        if m:
            for i, key in enumerate(("created", "updated", "failed"), 1):
                totals[key] = totals.get(key, 0) + int(m.group(i))
    return rows, totals


def run_stage(meta, files, csv_sig=None):
    """Highest pipeline stage a run has reached, from its meta dict and
    {'input','filled','csv'} existence flags.

    P3-54: "published" is only true of the CSV that was actually published.
    A rebuild in Step 3 rewrites wordpress_ready.csv but leaves
    published/published_live from the earlier publish, so the Home table kept
    claiming a 43-row rebuild was live when 12 rows were. `published_csv`
    (csv_signature at publish time) pins it: when the caller passes the
    CURRENT signature and it differs, the run falls back to what the
    build/validate keys say. A meta from before that key existed has no
    signature to compare and stays "published" - never demote real history on
    missing data."""
    if meta.get("published"):
        pub_sig = meta.get("published_csv")
        if not pub_sig or csv_sig is None or pub_sig == csv_sig:
            return "published"
    if meta.get("validated"):
        return "validated"
    if files.get("csv"):
        return "built"
    if files.get("filled"):
        return "enriched"
    if files.get("input"):
        return "uploaded"
    return "created"


def image_basenames_from_csv(csv_df):
    """Basenames of every image URL the CSV references. The Images column
    lists exactly the files the build downloaded, so these names are safe
    to copy out of product_images_for_upload when archiving a run."""
    names = []
    if "Images" not in csv_df.columns:
        return names
    for cell in csv_df["Images"].astype(str):
        for u in cell.split(","):
            u = u.strip()
            if u:
                names.append(u.rsplit("/", 1)[-1])
    return names


# mirror of app.CATEGORY_COMMA_MERGES / app._split_subcategories, copied for
# the same reason SKU_CODES is (importing app boots Streamlit and reads the
# taxonomy workbook). tests/test_gui_helpers.py pins the copy against app.
CATEGORY_COMMA_MERGES = (
    (("Juicers", "Mixers & Grinders"), "Juicers, Mixers & Grinders"),
)


def split_subcategories(raw):
    """Sub-categories out of one category-map cell, EXACTLY as
    app._split_subcategories does it: split on ',' only, then re-join the
    taxonomy names that carry a comma of their own.

    P2-29: also splitting on ';' and '|', and never merging, made the map
    editor's removal warning name combos that do not exist - deleting
    'Juicers, Mixers & Grinders' was reported as removing 'Juicers' plus
    'Mixers & Grinders', neither of which any sheet can be grepped for."""
    parts = [p.strip() for p in str(raw or "").split(",") if p.strip()]
    merged, i = [], 0
    while i < len(parts):
        for (first, second), combined in CATEGORY_COMMA_MERGES:
            if (parts[i] == first and i + 1 < len(parts)
                    and parts[i + 1] == second):
                merged.append(combined)
                i += 2
                break
        else:
            merged.append(parts[i])
            i += 1
    return merged


def sheet_combos(df):
    """'Parent > Sub' combos from a category-map dataframe - a light
    mirror of app.load_category_map for DIFFING edits only (the build
    still snaps via app.py). Col 0 parent, col 1 subcategories."""
    combos = set()
    for _, row in df.iterrows():
        vals = row.tolist()
        parent = str(vals[0] if vals else "").strip()
        if not parent or parent.lower() == "parent category":
            continue
        for sub in split_subcategories(vals[1] if len(vals) > 1 else ""):
            combos.add(f"{parent} > {sub}")
    return combos


def found_status(status) -> bool:
    """True iff this row's enricher status is importable.

    A tick only counts on a FOUND row. Any suffix qualifies - 'FOUND
    (amazon.in) - no price shown' and 'FOUND (x) - VERIFY: ...' included;
    they sit in the review bucket precisely so a human vets them, and once
    vetted they are legitimate imports. Everything else is never
    importable: NEEDS CRAWL / NEEDS LINK (two-pass, half-done), CLOSE
    MATCH / CHECK MANUALLY (unverified page), NOT FOUND, ERROR, SKIPPED
    and blank. The remediation path for those is not a tick: fix
    url_link and press 'Crawl all info' (--crawl-urls re-crawls every
    non-FOUND row that has a link and stamps it FOUND)."""
    return str(status or "").strip().upper().startswith("FOUND")


_FP_COLS = ("title", "regular price", "sale price", "url_link", "image_url")


def _fp_value(v) -> str:
    """One fingerprint field as a comparable string.

    The same cell reaches this from two readers: gui.load_filled_df (dtype=str
    then pd.to_numeric on the price columns -> 22599.0 / NaN) and step 3's
    plain pd.read_excel ('22,599' / NaN). Both must hash the same, so a blank
    of any flavour is "" and anything numeric is normalised through float."""
    try:
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    if not s:
        return ""
    try:
        f = float(s.replace(",", "").replace("₹", "").strip())
    except ValueError:
        return s
    return str(int(f)) if f == int(f) else repr(f)


def row_fingerprint(row) -> str:
    """sha1 of the fields a re-enrichment rewrites: title, both prices,
    url_link, image_url. Stored beside every tick so a ✔ made on a
    'FOUND - no price shown' row does not survive --retry-incomplete
    silently re-scraping that row (P3-53). Missing columns count as ""."""
    get = row.get if hasattr(row, "get") else (lambda k, d=None: row[k])
    joined = "|".join(_fp_value(get(c, "")) for c in _FP_COLS)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def confirmed_from_state(state) -> set:
    """The confirmed SKUs in a review_state.json dict.

    Values are {"confirmed": bool, "fp": "<sha1>"} since P3-53, plus
    "skipped" / "skip_reason" since the review-states change; a bare bool is
    the legacy format and still counts. An entry that says BOTH confirmed and
    skipped (hand-edited or corrupt file) counts as skipped only - the safe
    reading, because a skipped row must never become buildable."""
    out = set()
    for sku, v in (state or {}).get("confirmed", {}).items():
        ok = v.get("confirmed") if isinstance(v, dict) else v
        if isinstance(v, dict) and v.get("skipped"):
            continue
        if ok:
            out.add(str(sku))
    return out


# ------------------------------------------------------- review states ----
# A review row is in exactly ONE of three states: approved, skipped, or not
# reviewed. review_state.json keeps them in the same per-sku entry:
#     {"confirmed": bool, "skipped": bool, "skip_reason": str, "fp": sha1}
# Files written before this change hold only {confirmed, fp} (or a bare
# bool); a missing key reads as False / "" - there is no migration step.

APPROVE, SKIP, CLEAR = "approve", "skip", "clear"


def skipped_from_state(state) -> dict:
    """The skipped SKUs in a review_state.json dict, as {sku: reason}.

    A legacy entry (bare bool, or a dict without "skipped") is never
    skipped. The reason is "" when the reviewer gave none."""
    out = {}
    for sku, v in (state or {}).get("confirmed", {}).items():
        if isinstance(v, dict) and v.get("skipped"):
            out[str(sku)] = str(v.get("skip_reason") or "").strip()
    return out


def review_decision(confirmed, skipped, sku, decision, reason=""):
    """Apply one reviewer decision under the three-state rule.

    confirmed  set of approved SKUs
    skipped    {sku: reason} of skipped SKUs
    decision   APPROVE - approved, any skip cleared
               SKIP    - skipped (with `reason`), any approval cleared
               CLEAR   - back to not reviewed
    Returns (confirmed, skipped) as NEW objects; the inputs are untouched.
    The FOUND gate is not applied here - callers check found_status()
    before they approve (merge_review_flags and the card button do)."""
    conf = {str(s) for s in (confirmed or set())}
    skips = {str(k): str(v or "") for k, v in (skipped or {}).items()}
    sku = str(sku)
    if decision == APPROVE:
        conf.add(sku)
        skips.pop(sku, None)
    elif decision == SKIP:
        conf.discard(sku)
        skips[sku] = str(reason or "").strip()
    elif decision == CLEAR:
        conf.discard(sku)
        skips.pop(sku, None)
    else:
        raise ValueError(f"unknown review decision: {decision!r}")
    return conf, skips


def review_entries(old_entries, confirmed_flags, skipped, fps):
    """The new review_state.json "confirmed" map.

    old_entries      the map as it is on disk (fp fallback for rows the
                     caller did not hash)
    confirmed_flags  {sku: bool} - every sku the caller knows a tick for
    skipped          {sku: reason} - the COMPLETE skip map to persist
    fps              {sku: sha1} fingerprints of the rows as just saved

    Enforces the three-state rule on write: a sku in `skipped` is stored
    with confirmed False, whatever the flag map says (skip wins - a skip is
    the reviewer's explicit no, and the build must never pick it up). A
    skipped sku the flag map does not mention still gets its entry."""
    old = old_entries or {}
    skips = {str(k): str(v or "").strip() for k, v in (skipped or {}).items()}
    out = {}
    keys = [str(s) for s in (confirmed_flags or {})]
    keys += [s for s in skips if s not in set(keys)]
    flags = {str(k): bool(v) for k, v in (confirmed_flags or {}).items()}
    for sku in keys:
        prev = old.get(sku)
        fp = (fps or {}).get(sku,
                             prev.get("fp") if isinstance(prev, dict) else None)
        is_skip = sku in skips
        out[sku] = {"confirmed": bool(flags.get(sku, False)) and not is_skip,
                    "skipped": is_skip,
                    "skip_reason": skips.get(sku, ""),
                    "fp": fp}
    return out


def stale_confirmations(state, df) -> set:
    """Confirmed SKUs whose sheet row changed since they were ticked.

    A stored fp of None (legacy tick, or one saved without a frame to hash)
    is never stale - there is nothing to compare. A sku the sheet no longer
    holds is not stale either: it cannot be built, and the FOUND gate in
    select_build_rows already covers that case.

    Skips are NEVER stale: a changed row keeps its skip. Dropping it would
    turn the reviewer's explicit 'no' back into 'not reviewed', and the
    explicit 'All FOUND rows' scope would then build it unseen - so the safe
    choice is to keep the skip until a human clears it."""
    if df is None or "sku" not in getattr(df, "columns", []):
        return set()
    ticks = {}                          # the ticks that CAN go stale
    for sku, v in (state or {}).get("confirmed", {}).items():
        if not isinstance(v, dict) or not v.get("confirmed") \
                or v.get("skipped"):
            continue
        if v.get("fp"):
            ticks[str(sku)] = v.get("fp")
    if not ticks:
        return set()
    # hash only the rows a tick names (first row per sku, as before): step 2
    # calls this on every rerun, and hashing all 300 rows cost ~0.1 s a click
    current = {}
    for _, row in df[df["sku"].astype(str).isin(ticks)].iterrows():
        sku = str(row["sku"])
        if sku not in current:
            current[sku] = row_fingerprint(row)
    return {sku for sku, fp in ticks.items()
            if sku in current and current[sku] != fp}


def merge_confirm_flags(existing, original, parts):
    """Merge the ✔ column of several data_editor frames into one flag map.

    existing  set of SKUs already persisted as confirmed (rows the search
              filter hid from every editor keep their tick).
    original  the review DataFrame as loaded - columns sku, confirmed
              (bool, from the persisted state) and status.
    parts     the frames the editors returned (columns sku, confirmed), in
              priority order; LATER parts win.

    A part's row counts as an EDIT only when its confirmed value differs
    from that sku's value in `original`. Every row appears in two editors
    (its bucket tab and the All tab), so without that test the untouched
    copy would silently overwrite a tick made in the other tab. A sku the
    original sheet does not know is ignored.

    Finally the FOUND gate: a flag left True on a row whose original
    status is not found_status() is forced back to False and the sku
    reported. Returns (flags, rejected) - rejected sorted - and never
    mutates its inputs."""
    orig_conf, orig_status = {}, {}
    for _, row in original.iterrows():
        sku = str(row["sku"])
        if sku in orig_conf:
            continue
        orig_conf[sku] = bool(row.get("confirmed", False))
        orig_status[sku] = row.get("status", "")

    flags = {str(sku): True for sku in existing}
    for part in parts or []:
        for _, row in part.iterrows():
            sku = str(row["sku"])
            if sku not in orig_conf:
                continue
            now = bool(row.get("confirmed", False))
            if now != orig_conf[sku]:
                flags[sku] = now

    rejected = []
    for sku, ok in flags.items():
        # a sku the sheet no longer holds cannot be built either way; only
        # judge rows whose status we actually know
        if ok and sku in orig_status and not found_status(orig_status[sku]):
            rejected.append(sku)
    for sku in rejected:
        flags[sku] = False
    return flags, sorted(rejected)


def merge_review_flags(existing_conf, existing_skips, original, parts):
    """merge_confirm_flags for BOTH review columns (✔ and Skip).

    existing_conf   set of SKUs persisted as approved
    existing_skips  {sku: reason} persisted as skipped
    original        the review frame as loaded - sku, confirmed, skipped,
                    status (a frame without 'skipped' reads as all False)
    parts           the editor frames, LATER parts win (same as
                    merge_confirm_flags)

    A part's Skip value counts as an edit only when it differs from that
    sku's value in `original`. Then the three-state rule:
      - ✔ newly ticked on a skipped row (skip untouched)  -> approved,
        skip cleared
      - Skip newly ticked                                 -> skipped,
        approval cleared
      - both newly ticked in the same save                -> skipped (the
        safe reading of a contradictory edit)
    The FOUND gate applies to the approval only; a skip is allowed on any
    row. A skip a reviewer unticks loses its reason. Returns
    (confirm_flags, skips, rejected) and never mutates its inputs."""
    flags, rejected = merge_confirm_flags(existing_conf, original, parts)

    orig_conf, orig_skip = {}, {}
    for _, row in original.iterrows():
        sku = str(row["sku"])
        if sku in orig_skip:
            continue
        orig_conf[sku] = bool(row.get("confirmed", False))
        orig_skip[sku] = bool(row.get("skipped", False))

    skips = {str(k): str(v or "") for k, v in (existing_skips or {}).items()}
    skip_changed, conf_changed = set(), set()
    for part in parts or []:
        has_skip = "skipped" in part.columns
        for _, row in part.iterrows():
            sku = str(row["sku"])
            if sku not in orig_skip:
                continue
            if bool(row.get("confirmed", False)) != orig_conf[sku]:
                conf_changed.add(sku)
            if not has_skip:
                continue
            now = bool(row.get("skipped", False))
            if now != orig_skip[sku]:
                skip_changed.add(sku)
                if now:
                    skips.setdefault(sku, "")
                else:
                    skips.pop(sku, None)

    for sku in list(skips):
        if sku in skip_changed:
            flags[sku] = False                # a new skip clears the tick
        elif flags.get(sku) and sku in conf_changed:
            skips.pop(sku)                    # a new tick clears the skip
        else:
            flags[sku] = False                # an old skip still wins
    return flags, skips, rejected


def select_build_rows(df, scope, confirmed, skipped=None):
    """Rows that may reach the CSV build, plus the SKUs refused.

    scope 'confirmed'  -> a row must be ticked AND found_status(); a ticked
                          NEEDS CRAWL / NOT FOUND / CLOSE MATCH row is
                          dropped and named in `skipped`. No approvals ->
                          an empty frame (never a fallback to all rows).
    scope 'all_found'  -> every found_status() row, ticked or not;
                          skipped is always [].

    `skipped` (a set or a {sku: reason} map) names rows the reviewer
    skipped. They are excluded in BOTH scopes and are not reported in the
    refused list - a skip is a decision, not an error.

    A frame with no 'status' column is not an enricher sheet (a hand-made
    or already-filtered input), so nothing can be judged: keep everything
    for 'all_found', keep the ticked rows for 'confirmed', skipped = [].
    Returns a copy; the input frame is untouched."""
    skip = {str(s) for s in (skipped or ())}
    conf = {str(s) for s in (confirmed or set())} - skip
    skus = df["sku"].astype(str) if "sku" in df.columns else None
    keep = ~skus.isin(skip) if skus is not None else None
    if "status" not in df.columns:
        if scope == "confirmed" and skus is not None:
            return df[skus.isin(conf)].copy(), []
        return (df[keep].copy() if keep is not None else df.copy()), []
    is_found = df["status"].map(found_status)
    if keep is None:
        keep = is_found | True
    if scope == "confirmed":
        ticked = skus.isin(conf) if skus is not None else is_found & False
        return (df[ticked & is_found].copy(),
                sorted(set(df.loc[ticked & ~is_found, "sku"].astype(str))))
    return df[is_found & keep].copy(), []


def review_counts(df, confirmed, skipped) -> dict:
    """The review summary: every unique sku on the sheet in ONE bucket.

    approved      ticked, not skipped, status FOUND (what 'Confirmed rows
                  only' builds)
    skipped       skipped by the reviewer, whatever the status
    not_reviewed  FOUND, neither ticked nor skipped
    not_found     status is not FOUND and not skipped (cannot be approved
                  until crawled; a stray legacy tick on one lands here too)
    A frame without a status column treats every row as FOUND."""
    conf = {str(s) for s in (confirmed or set())}
    skip = {str(s) for s in (skipped or ())}
    out = {"approved": 0, "skipped": 0, "not_reviewed": 0, "not_found": 0}
    if df is None or "sku" not in getattr(df, "columns", []):
        return out
    has_status = "status" in df.columns
    seen = set()
    for _, row in df.iterrows():
        sku = str(row["sku"])
        if sku in seen:
            continue
        seen.add(sku)
        found = found_status(row["status"]) if has_status else True
        if sku in skip:
            out["skipped"] += 1
        elif not found:
            out["not_found"] += 1
        elif sku in conf:
            out["approved"] += 1
        else:
            out["not_reviewed"] += 1
    return out


def review_counts_text(counts) -> str:
    """'Approved N · Skipped N · Not reviewed N · Not FOUND N'."""
    c = counts or {}
    return (f"Approved {c.get('approved', 0)} · Skipped {c.get('skipped', 0)}"
            f" · Not reviewed {c.get('not_reviewed', 0)}"
            f" · Not FOUND {c.get('not_found', 0)}")


def card_queue_index(confirmed, i) -> int:
    """Where the card-review pointer really belongs, given the queue's ✔ column.

    `confirmed` is the queue's confirmed column in queue order (unconfirmed
    rows sort first); `i` is the index the session state remembers. The
    result is clamped into range and, whenever it lands on an already
    confirmed card while unconfirmed cards remain, snapped forward to the
    first unconfirmed one.

    That snap is the P1-9 tail case: approving the LAST unconfirmed card
    leaves it at position i (the unconfirmed rows shrink to 0..i-1 and the
    confirmed group starts at i), so 'stay at i' re-shows the card just
    approved and 'i + 1' clamps straight back onto it - Approve and Skip
    would dead-loop there while earlier cards were still unconfirmed. Only
    when nothing is left unconfirmed does the pointer rest on a confirmed
    card."""
    flags = [bool(c) for c in confirmed]
    if not flags:
        return 0
    i = max(0, min(int(i), len(flags) - 1))
    if flags[i] and not all(flags):
        return flags.index(False)
    return i


# ------------------------------------------------- review instrumentation --
# Phase 1 item 5: review_state.json carries an "events" list beside
# "confirmed", and the GUI renders runs\<name>\review_metrics.json from it.
# Everything here is pure - the clock reading and the session state live in
# gui.py, these functions only ever see plain values.

# a row that took more than a bare tick: someone had to change something
_HANDS_ON_ACTIONS = frozenset({"edit", "url_pasted", "drop_image"})

# buckets a row lands in when the enricher could not finish the job alone
_NEEDS_HUMAN_BUCKETS = frozenset({"review", "failed"})


def review_event(sku, action, mode, secs, at=None) -> dict:
    """One review_state.json event: who was reviewed, what happened, how long.

    `secs` is wall time attributed to THIS event (see clock_elapsed); it is
    clamped at 0 so a clock that jumped backwards can never subtract from
    the run total. `at` defaults to now, and is a parameter so tests (and a
    replay) can pin it."""
    try:
        s = float(secs)
    except (TypeError, ValueError):
        s = 0.0
    if s != s or s < 0:          # NaN or negative
        s = 0.0
    return {"sku": str(sku), "action": str(action), "mode": str(mode),
            "at": str(at) if at else datetime.now().isoformat(
                timespec="seconds"),
            "secs": round(s, 1)}


def clock_elapsed(state, key, now, cap=600.0) -> float:
    """Seconds since `state[key]` was stamped, capped so idling never counts.

    `state` is anything with .get (streamlit's session_state included). No
    previous stamp, a junk stamp or a clock that went backwards all read as
    0.0 - a missing measurement is worth less than a wrong one. The cap
    (600 s by default) is what keeps a reviewer who went to lunch from
    booking ten hours against one SKU."""
    try:
        prev = state.get(key)
    except Exception:
        prev = None
    if prev is None:
        return 0.0
    try:
        prev = float(prev)
        delta = float(now) - prev
    except (TypeError, ValueError):
        return 0.0
    if delta != delta or delta <= 0:
        return 0.0
    return min(delta, float(cap))


def append_events(state, events) -> dict:
    """Append events to state["events"], creating the list if absent.

    Returns the same dict (mutated) so a caller can save_json it straight
    away. A legacy state - confirmed only, no events key - simply grows one;
    confirmed_from_state ignores it, so the file format stays compatible."""
    if not isinstance(state, dict):
        state = {}
    evs = state.get("events")
    if not isinstance(evs, list):
        evs = []
    for e in (events or []):
        if isinstance(e, dict):
            evs.append(dict(e))
    state["events"] = evs
    return state


def review_metrics(state, df) -> dict:
    """How much human time this run's review has cost, from the event log.

    review_minutes  total attributed seconds / 60
    rows_touched    distinct SKUs with at least one event
    min_per_sku     review_minutes / rows_touched (0.0 with nothing touched)
    needs_human_pct rows of the SHEET that needed more than a bare tick -
                    a hands-on event (edit / url_pasted / drop_image) or a
                    status in the review/failed bucket - over all rows
    actions         {action: count}

    A legacy state with no "events" returns zeros rather than raising, so
    the caption can be rendered against any review_state.json."""
    events = (state or {}).get("events")
    if not isinstance(events, list):
        events = []
    secs = 0.0
    touched, hands_on, actions = set(), set(), {}
    for e in events:
        if not isinstance(e, dict):
            continue
        sku = str(e.get("sku", ""))
        action = str(e.get("action", ""))
        try:
            v = float(e.get("secs", 0) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v == v and v > 0:
            secs += v
        if sku:
            touched.add(sku)
            if action in _HANDS_ON_ACTIONS:
                hands_on.add(sku)
        if action:
            actions[action] = actions.get(action, 0) + 1

    minutes = round(secs / 60.0, 1)
    rows = 0
    needs_human = 0
    if df is not None and "sku" in getattr(df, "columns", []):
        seen = set()
        has_status = "status" in df.columns
        for _, row in df.iterrows():
            sku = str(row["sku"])
            if sku in seen:
                continue
            seen.add(sku)
            rows += 1
            bucket = classify(row["status"]) if has_status else ""
            if sku in hands_on or bucket in _NEEDS_HUMAN_BUCKETS:
                needs_human += 1
    return {"review_minutes": minutes,
            "rows_touched": len(touched),
            "min_per_sku": round(minutes / len(touched), 2) if touched
            else 0.0,
            "needs_human_pct": round(100.0 * needs_human / rows, 1)
            if rows else 0.0,
            "actions": actions}


def review_caption(metrics) -> str:
    """The one-line summary the GUI, the archive README and the enricher
    report all print. Empty string when nothing has been reviewed yet."""
    if not isinstance(metrics, dict) or not metrics:
        return ""
    return (f"Review so far: {metrics.get('review_minutes', 0)} min · "
            f"{metrics.get('min_per_sku', 0)} min/SKU · "
            f"needs-human {metrics.get('needs_human_pct', 0)}%")


# ------------------------------------------------------ deliverable pack --
# Item 6 of docs/CLIENT_CONFIG.md. One coverage counter and ONE README
# renderer, shared by gui.archive_run and build_csv.py --pack, so the two
# hand-over routes cannot drift apart.

_DIGITS_RE = re.compile(r"\d+")


def _norm_suffix(text) -> str:
    """A status suffix with its numbers collapsed to N, so 'only 2 image(s)'
    and 'only 3 image(s)' land in the SAME coverage bucket instead of
    producing one table row per SKU."""
    return _DIGITS_RE.sub("N", str(text or "").strip())


def coverage_summary(df) -> dict:
    """Count every row of a _filled sheet into one status bucket.

    The two dict buckets keep the human-readable reason: `found_incomplete`
    is keyed by the text after the first ' - ' on a FOUND row ('only N
    image(s)', 'price from flipkart.com', 'VERIFY: ...'), `not_found` by the
    reason after 'NOT FOUND - ' (a bare 'NOT FOUND' becomes 'unspecified').

    Statuses are matched with startswith, never ==: both the bare and the
    '(site)'-suffixed forms occur. Every row lands in exactly one bucket, so
    the counts always sum to `rows` - `other` is the catch-all that keeps
    that promise for a status shape nobody has seen yet.
    """
    out = {"rows": 0, "found_clean": 0, "found_incomplete": {},
           "close_match": 0, "check_manually": 0, "not_found": {},
           "error": 0, "skipped": 0, "needs_crawl": 0, "needs_link": 0,
           "blank": 0, "other": 0}
    if df is None:
        return out
    if "status" not in getattr(df, "columns", []):
        out["rows"] = len(df)
        out["blank"] = out["rows"]
        return out

    for raw in df["status"]:
        out["rows"] += 1
        s = "" if raw is None or raw != raw else str(raw).strip()
        u = s.upper()
        if not s:
            out["blank"] += 1
        elif u.startswith("NEEDS CRAWL"):
            out["needs_crawl"] += 1
        elif u.startswith("NEEDS LINK"):
            out["needs_link"] += 1
        elif u.startswith("FOUND"):
            if " - " in s:
                label = _norm_suffix(s.split(" - ", 1)[1])
                out["found_incomplete"][label] = (
                    out["found_incomplete"].get(label, 0) + 1)
            else:
                out["found_clean"] += 1
        elif u.startswith("CLOSE MATCH"):
            out["close_match"] += 1
        elif u.startswith("CHECK MANUALLY"):
            out["check_manually"] += 1
        elif u.startswith("SKIPPED"):
            out["skipped"] += 1
        elif u.startswith("NOT FOUND"):
            rest = s[len("NOT FOUND"):].strip()
            if rest.startswith("-"):
                rest = rest[1:].strip()
            reason = _norm_suffix(rest) or "unspecified"
            out["not_found"][reason] = out["not_found"].get(reason, 0) + 1
        elif u.startswith("ERROR"):
            out["error"] += 1
        else:
            out["other"] += 1
    return out


def coverage_total(coverage) -> int:
    """Sum of every bucket - equals coverage['rows'] on a well-formed dict."""
    total = 0
    for key, value in (coverage or {}).items():
        if key == "rows":
            continue
        total += sum(value.values()) if isinstance(value, dict) else int(value)
    return total


# ---- client hand-over guard (owner rule, 2026-11-19) --------------------
# The pack a client receives must never hold the enriched sheet
# (*_filled.xlsx: source page URLs, maker image URLs, statuses that name the
# source site), the enricher report, a log, or any text file with a link to
# a host that is not the client's own store / media host. The internal
# archive (gui.archive_run's batches\<name>\ folder) is NOT handed over and
# passes internal=True. validation_issues.csv is INTERNAL too (owner
# decision 2026-11-19, fifth round): its reasons can name a source site.
CLIENT_PACK_REFUSED_RE = re.compile(
    r"(?:_filled\.xlsx|_report\.md|\.log|(?:^|[\\/])validation_issues\.csv)$",
    re.IGNORECASE)
PACK_TEXT_SUFFIXES = (".csv", ".md", ".txt", ".json", ".tsv", ".html", ".htm",
                      ".xml")
_PACK_URL_RE = re.compile(r"(?:https?://|(?<![:\w/])//|(?<![\w.])www\.)"
                          r"([^\s/\"'<>),;|\]]+)",
                          re.IGNORECASE)


def _bare_host(host) -> str:
    host = str(host or "").strip().lower().rstrip(".")
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    return host[4:] if host.startswith("www.") else host


def client_hosts(cfg) -> set:
    """The hosts a client pack may link to: the store and the media base."""
    hosts = set()
    for url in (getattr(cfg, "store_url", ""), _safe_media_base(cfg)):
        host = _bare_host(re.sub(r"^[a-z]+://", "", str(url or ""),
                                 flags=re.IGNORECASE).split("/", 1)[0])
        if host:
            hosts.add(host)
    return hosts


def _safe_media_base(cfg) -> str:
    try:
        return cfg.media_base()
    except Exception:
        return ""


def default_client_hosts() -> set:
    """client_hosts() of the client $PIPELINE_CLIENT (else the default)
    names; an empty set (every link refused) when it cannot be loaded."""
    try:
        import client_config
        return client_hosts(client_config.load_client())
    except Exception:
        return set()


def foreign_hosts(text, allowed) -> list:
    """Hosts linked in `text` (http(s)://, //, www.) that are neither in
    `allowed` nor a sub-domain of one, in order, without repeats."""
    allowed = {_bare_host(h) for h in (allowed or ()) if h}
    out = []
    for m in _PACK_URL_RE.finditer(str(text or "")):
        host = _bare_host(m.group(1))
        if not host or "." not in host:
            continue
        if host in allowed or any(host.endswith("." + a) for a in allowed):
            continue
        if host not in out:
            out.append(host)
    return out


def pack_member_problem(name, path, allowed_hosts) -> str:
    """Why `name` (copied from `path`) must not go into a client pack, or
    ''. Refused: *_filled.xlsx, *_report.md, *.log, validation_issues.csv,
    and a text member that links to a host other than the client's own."""
    if CLIENT_PACK_REFUSED_RE.search(str(name)) or \
            CLIENT_PACK_REFUSED_RE.search(Path(path).name):
        return ("internal file (enriched sheet, enricher report, log or "
                "validation issues)")
    if str(name).lower().endswith(PACK_TEXT_SUFFIXES):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"cannot be read to check for source links ({exc})"
        hosts = foreign_hosts(text, allowed_hosts)
        if hosts:
            return ("links to a host that is not the client's: "
                    + ", ".join(hosts[:3]))
    return ""


def scan_pack_folder(dest, allowed_hosts) -> list:
    """['<file>: <why>'] for every file in a finished client pack folder
    (README included) that pack_member_problem refuses. [] = clean."""
    dest = Path(dest)
    out = []
    if not dest.is_dir():
        return out
    for path in sorted(dest.rglob("*")):
        if path.is_file():
            rel = path.relative_to(dest).as_posix()
            why = pack_member_problem(rel, path, allowed_hosts)
            if why:
                out.append(f"{rel}: {why}")
    return out


def remove_refused(dest, allowed_hosts) -> list:
    """scan_pack_folder, then DELETE each refused file from the client pack
    folder (with a printed warning) so the zip cannot carry it. Returns the
    problems found."""
    problems = scan_pack_folder(dest, allowed_hosts)
    for item in problems:
        rel = item.split(": ", 1)[0]
        try:
            (Path(dest) / rel).unlink()
        except OSError:
            pass
        print(f"[pack] REFUSED and removed from the client pack: {item}")
    return problems


def assemble_pack(dest, files, image_dir, csv_path, *, internal=False,
                  allowed_hosts=None) -> dict:
    """Copy one pack into `dest` - the single copy routine behind
    BOTH gui.archive_run and build_csv.py --pack.

    files      {destination filename: source Path}; a missing source is
               skipped, never an error (a half-done run has no CSV yet).
    image_dir  where the build left the .webp files (app.IMAGE_DIR).
    csv_path   the built CSV; its Images column names exactly the files worth
               copying. A CSV that cannot be read costs the images folder and
               nothing else - a build killed mid-to_csv leaves a 0-byte or
               ragged file and pandas raises ValueError (EmptyDataError /
               ParserError), not OSError (P2-25).
    internal   True only for the operator's own archive (never handed over):
               no member is refused. Default False = a CLIENT pack: every
               member passes pack_member_problem (no *_filled.xlsx, no
               *_report.md, no *.log, no text file that links to a host other
               than `allowed_hosts`); a refused member is skipped with a
               printed warning and listed in res['refused'].
    allowed_hosts  the client's own hosts (client_hosts(cfg)); None = the
               hosts of the client $PIPELINE_CLIENT names.

    Returns {files, images, missing, copied, csv_error, refused}. images\\ is
    created only when the CSV actually references a photo, so a pack with
    none has no empty folder in it.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    res = {"files": 0, "images": 0, "missing": 0, "copied": 0,
           "csv_error": None, "refused": []}
    if not internal and allowed_hosts is None:
        allowed_hosts = default_client_hosts()
    for name, src in (files or {}).items():
        if not src:
            continue
        src = Path(src)
        if src.exists():
            if not internal:
                why = pack_member_problem(name, src, allowed_hosts)
                if why:
                    res["refused"].append(f"{name}: {why}")
                    print(f"[pack] REFUSED {name}: {why}")
                    continue
            shutil.copy2(src, dest / name)
            res["files"] += 1

    if csv_path and Path(csv_path).exists():
        try:
            csv_df = pd.read_csv(csv_path, dtype=str).fillna("")
        except (OSError, ValueError) as exc:
            res["csv_error"] = exc.__class__.__name__
            csv_df = None
        if csv_df is not None:
            names = list(dict.fromkeys(image_basenames_from_csv(csv_df)))
            if names:
                img_dest = dest / "images"
                img_dest.mkdir(exist_ok=True)
                for name in names:
                    src = Path(image_dir) / name
                    if src.exists():
                        shutil.copy2(src, img_dest / name)
                        res["images"] += 1
                    else:
                        res["missing"] += 1
    res["copied"] = res["files"] + res["images"]
    return res


_FILE_BLURB = {
    "input_filled.xlsx": "the enriched sheet, one row per SKU with its status",
    "wordpress_ready.csv": "the WooCommerce import file",
    "validation_issues.csv": ("rows the QA pass flagged before publishing "
                              "(internal archive only)"),
    "input_filled_report.md": "the enricher's end-of-run report",
    "images": "exactly the photos the CSV references",
    "README.md": "this file",
}


def _blurb(fname) -> str:
    """What a file in the pack is FOR. The names are not fixed - the GUI
    archives wordpress_ready.csv while `build_csv.py -o out.csv --pack`
    archives out.csv, and the client's list arrives under whatever they
    called it - so the exact-name table falls back to the extension."""
    known = _FILE_BLURB.get(str(fname))
    if known:
        return known
    low = str(fname).lower()
    if low.endswith((".xlsx", ".xls")):
        return "the SKU list the batch started from (internal archive only)"
    if low.endswith(".csv"):
        return "the WooCommerce import file"
    return ""

_COVERAGE_ROWS = (
    ("found_clean", "FOUND (clean)"),
    ("found_incomplete", "FOUND (incomplete)"),
    ("close_match", "CLOSE MATCH - CHECK"),
    ("check_manually", "CHECK MANUALLY"),
    ("not_found", "NOT FOUND"),
    ("error", "ERROR"),
    ("skipped", "SKIPPED - duplicate unit"),
    ("needs_crawl", "NEEDS CRAWL (two-pass)"),
    ("needs_link", "NEEDS LINK (two-pass)"),
    ("other", "unrecognised status"),
    ("blank", "(no status)"),
)


# a site name in a status reason ('price from flipkart.com', 'all sources
# blocked (amazon.in, croma.com)'): the README never prints one
_SITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*"
    r"\.(?:com|in|net|org|co|io|shop|store|biz|info)(?:\.[a-z]{2})?\b[^\s,)]*",
    re.IGNORECASE)


def scrub_sites(text) -> str:
    """`text` with every site name / link replaced by 'a source site'."""
    text = _SITE_RE.sub("a source site", str(text or ""))
    return re.sub(r"a source site(?:,\s*a source site)+", "source sites", text)


def _coverage_table(coverage) -> str:
    cov = coverage or {}
    lines = ["| bucket | rows |", "| --- | ---: |",
             f"| **Rows** | **{cov.get('rows', 0)}** |"]
    for key, label in _COVERAGE_ROWS:
        value = cov.get(key, 0)
        if isinstance(value, dict):
            lines.append(f"| {label} | {sum(value.values())} |")
            merged = {}
            for reason, n in value.items():
                key = scrub_sites(reason)
                merged[key] = merged.get(key, 0) + n
            for reason, n in sorted(merged.items(),
                                    key=lambda kv: (-kv[1], kv[0])):
                lines.append(f"| &nbsp;&nbsp;↳ {reason} | {n} |")
        else:
            lines.append(f"| {label} | {int(value)} |")
    return "\n".join(lines)


def render_batch_readme(name, meta, coverage, review_metrics=None,
                        images=None, validation=None, source_name=None,
                        notes=()) -> str:
    """The README that ships with a hand-over pack - the SAME text for
    gui.archive_run and build_csv.py --pack.

    Pure: every fact arrives as an argument. `meta` is the run_meta.json dict
    (or a synthesised one); `meta['archived_from']` names the run folder when
    there is one, `meta['published_stale']` is the P3-54 flag that the
    publish covered an EARLIER build than the CSV in this folder, and
    `meta['files']` lists what the packer actually copied.
    """
    meta = meta or {}
    cov = coverage or {}
    stamp = f"{datetime.now():%Y-%m-%d %H:%M}"
    origin = meta.get("archived_from")
    lead = (f"Archived {stamp} from {origin}.\n\n" if origin
            else f"Packed {stamp} by build_csv.py --pack.\n\n")

    # P3-54: "Built scope: all (43 SKUs)" next to "Published: <date> (LIVE)"
    # read as 43 live products when the publish covered an earlier build.
    pub_note = " (LIVE)" if meta.get("published_live") else ""
    if meta.get("published_stale"):
        pub_note += (" — for a PREVIOUS build; the current CSV was "
                     "rebuilt after publishing")

    issues = meta.get("issue_count", "?")
    if isinstance(validation, dict) and validation.get("issue_count") is not None:
        issues = validation["issue_count"]

    img = images or {}
    img_line = (f"- Images: {img.get('count', 0)} file(s) copied"
                + (f", {img['missing']} referenced but MISSING from the "
                   f"image folder" if img.get("missing") else "")
                + "\n")

    body = [
        f"# Batch {name}\n\n",
        lead,
        f"- Source list: {source_name or meta.get('original_name', '?')}\n",
        f"- Rows enriched: {meta.get('total_rows', '?')}\n",
        f"- Built scope: {meta.get('built_scope', '?')} "
        f"({len(meta.get('built_skus') or [])} SKUs)\n",
        f"- Validation issues: {issues}\n",
        f"- Published: {meta.get('published', 'never')}{pub_note}\n",
        img_line,
    ]
    for note in notes or ():
        if note:
            body.append(note if str(note).startswith("\n") else f"- {note}\n")

    body.append("\n## Coverage\n\n")
    body.append(_coverage_table(cov) + "\n")
    if coverage_total(cov) != cov.get("rows", 0):
        body.append("\n_(the bucket counts do not add up to the row count - "
                    "the sheet changed under the packer)_\n")

    body.append("\n## Review\n\n")
    body.append((review_caption(review_metrics)
                 or "no review metrics recorded") + "\n")

    body.append("\n## Files\n\n")
    listed = list(meta.get("files") or [])
    if listed:
        for fname in listed:
            blurb = _blurb(fname)
            body.append(f"- `{fname}`" + (f" - {blurb}\n" if blurb else "\n"))
    else:
        body.append("- (nothing was copied)\n")
    return "".join(body)


# ------------- reviewer edits of the description fields (step 2) -------------
# runs\<name>\description_overrides.json, next to review_state.json. A
# separate file (not a key in review_state.json) so build_csv.py can take it
# with one flag and nothing else of the review state. Shape:
#   {"version": 1,
#    "skus": {"<raw sheet sku>": {
#        "short description": {"lines": [...], "generated": [...],
#                              "saved": "<iso time>"},
#        "description": {...},
#        "key features": {...}}}}      (added 2026-11-20: the key-features
#                                       paragraph, CSV column 'configuration')
# `lines` are the accepted "Label: Value" lines (the firewall has already
# refused the others at save time); an EMPTY list means the reviewer cleared
# the field on purpose. `generated` is what the build made at save time, so
# the GUI can say when the scrape has changed under an edit. The scraped
# columns of _filled.xlsx are never touched.

DESCRIPTION_OVERRIDES_FILE = "description_overrides.json"
KEY_FEATURES_FIELD = "key features"     # == app.KEY_FEATURES_FIELD
OVERRIDE_FIELDS = ("short description", "description", KEY_FEATURES_FIELD)


def overrides_path(run_dir) -> Path:
    return Path(run_dir) / DESCRIPTION_OVERRIDES_FILE


def load_overrides_state(path) -> dict:
    """The overrides file as a state dict. A missing or unreadable file is an
    empty state (no overrides), never an error."""
    import json
    state = {"version": 1, "skus": {}}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return state
    if isinstance(data, dict) and isinstance(data.get("skus"), dict):
        state["skus"] = {str(k): v for k, v in data["skus"].items()
                         if isinstance(v, dict)}
    return state


def save_overrides_state(path, state) -> None:
    import json
    Path(path).write_text(json.dumps(state, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def get_override(state, sku, field):
    """The stored entry {"lines", "generated", "saved"} or None."""
    entry = ((state or {}).get("skus", {}).get(str(sku)) or {}).get(field)
    if isinstance(entry, dict) and isinstance(entry.get("lines"), list):
        return entry
    return None


def set_override(state, sku, field, lines, generated, at=None) -> str:
    """Store the reviewer's accepted `lines` for one field. Returns what was
    done: "saved", or "removed" when `lines` equal the generated lines (an
    edit that changes nothing is no override). An empty `lines` list is
    stored as an explicit "publish this field empty"; the CALLER decides
    whether the reviewer really asked for that."""
    if field not in OVERRIDE_FIELDS:
        raise ValueError(f"not a description field: {field!r}")
    lines = [str(x) for x in lines]
    generated = [str(x) for x in (generated or [])]
    if lines == generated:
        reset_override(state, sku, field)
        return "removed"
    skus = state.setdefault("skus", {})
    skus.setdefault(str(sku), {})[field] = {
        "lines": lines, "generated": generated,
        "saved": str(at) if at else datetime.now().isoformat(
            timespec="seconds")}
    return "saved"


def reset_override(state, sku, field=None) -> bool:
    """'Reset to generated': drop the override of one field (or every
    description field in OVERRIDE_FIELDS when `field` is None). Returns True when something was removed."""
    skus = (state or {}).get("skus", {})
    entry = skus.get(str(sku))
    if not entry:
        return False
    fields = [field] if field else list(OVERRIDE_FIELDS)
    removed = False
    for f in fields:
        if f in entry:
            del entry[f]
            removed = True
    if not entry:
        skus.pop(str(sku), None)
    return removed


def override_is_stale(entry, generated_lines) -> bool:
    """True when the build would now generate different lines than it did
    when the reviewer saved the edit (the row was re-enriched or edited)."""
    if not entry:
        return False
    return list(entry.get("generated") or []) != [str(x) for x in
                                                  generated_lines]


def overrides_for_build(state) -> dict:
    """{sku: {field: [lines], 'price': {...}, 'attributes': {'set', 'remove'}}}
    - the form app.process_inventory takes."""
    out = {}
    for sku, fields in ((state or {}).get("skus") or {}).items():
        kept = {f: list(e["lines"]) for f, e in (fields or {}).items()
                if f in OVERRIDE_FIELDS and isinstance(e, dict)
                and isinstance(e.get("lines"), list)}
        price = get_price_override(state, sku)
        if price:
            kept[PRICE_FIELD] = dict(price["values"])
        attrs = get_attribute_override(state, sku)
        if attrs:
            kept[ATTRIBUTE_FIELD] = {"set": dict(attrs["set"]),
                                     "remove": list(attrs["remove"])}
        if kept:
            out[str(sku)] = kept
    return out


# ------------- reviewer edits of the price and the attributes (step 2) -------
# Same file, same SKU entry, two more keys (owner request 2026-11-19, late):
#   "price": {"values": {"regular price": "37790", "sale price": "18590",
#                        "publish price": "20000"},
#             "generated": {"regular price": ..., "sale price": ...},
#             "saved": "<iso time>"}
#     `values` holds only what the reviewer CHANGED: a page MRP / selling
#     price that differs from the sheet ('' = "the page has none"), and an
#     explicit publish price (absent = no override). app.apply_price_override
#     reads it; the price is still client_config.one_price unless a publish
#     price is set.
#   "attributes": {"set": {"<name>": "<value>"}, "remove": ["<name>"],
#                  "saved": "<iso time>"}
#     `set` changes a value or adds an attribute; `remove` drops one.
#     app.apply_attribute_override reads it.
# The caller validates first (app.check_price_input / check_publish_price /
# check_attribute_edit) and stores only accepted entries.
PRICE_FIELD = "price"
ATTRIBUTE_FIELD = "attributes"
PRICE_KEYS = ("regular price", "sale price", "publish price")


def _now(at=None):
    return str(at) if at else datetime.now().isoformat(timespec="seconds")


def get_price_override(state, sku):
    """The stored price entry {"values", "generated", "saved"} or None."""
    entry = ((state or {}).get("skus", {}).get(str(sku)) or {}).get(PRICE_FIELD)
    if isinstance(entry, dict) and isinstance(entry.get("values"), dict) \
            and entry["values"]:
        return entry
    return None


def _price_text(value) -> str:
    text = "" if value is None else str(value).strip()
    if text.lower() == "nan":
        return ""
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.replace(",", "")


def set_price_override(state, sku, values, generated, at=None) -> str:
    """Store the reviewer's price inputs. `values` = {"regular price",
    "sale price", "publish price"} as typed; `generated` = the sheet's
    {"regular price", "sale price"}. Only a page price that DIFFERS from the
    sheet, and a non-empty publish price, is kept. Returns "saved" or
    "removed" (nothing differs = no override)."""
    generated = {k: _price_text((generated or {}).get(k)) for k in PRICE_KEYS[:2]}
    kept = {}
    for key in PRICE_KEYS[:2]:
        if key in (values or {}):
            text = _price_text(values[key])
            if text != generated[key]:
                kept[key] = text
    publish = _price_text((values or {}).get("publish price"))
    if publish:
        kept["publish price"] = publish
    if not kept:
        reset_override(state, sku, PRICE_FIELD)
        return "removed"
    state.setdefault("skus", {}).setdefault(str(sku), {})[PRICE_FIELD] = {
        "values": kept, "generated": generated, "saved": _now(at)}
    return "saved"


def get_attribute_override(state, sku):
    """The stored attribute entry {"set", "remove", "saved"} or None."""
    entry = ((state or {}).get("skus", {}).get(str(sku)) or {}).get(
        ATTRIBUTE_FIELD)
    if isinstance(entry, dict) and (entry.get("set") or entry.get("remove")):
        entry.setdefault("set", {})
        entry.setdefault("remove", [])
        return entry
    return None


def set_attribute_override(state, sku, sets, removes, at=None) -> str:
    """Store the reviewer's attribute edits: `sets` {name: value} (changed
    or added), `removes` [names]. A name both set and removed is set. Empty
    edits remove the entry. Returns "saved" or "removed"."""
    sets = {str(k).strip(): str(v).strip() for k, v in (sets or {}).items()
            if str(k).strip() and str(v).strip()}
    low = {k.lower() for k in sets}
    kept = []
    for name in _names(removes):
        if name.lower() not in low and name.lower() not in \
                [r.lower() for r in kept]:
            kept.append(name)
    removes = kept
    if not sets and not removes:
        reset_override(state, sku, ATTRIBUTE_FIELD)
        return "removed"
    state.setdefault("skus", {}).setdefault(str(sku), {})[ATTRIBUTE_FIELD] = {
        "set": sets, "remove": removes, "saved": _now(at)}
    return "saved"


def _names(names):
    return [str(n).strip() for n in (names or ()) if str(n).strip()]


def attribute_edits(current, edited):
    """(sets, removes) from the review screen's attribute table: `current`
    = [(name, value)] as the build would write them now (reviewer edits
    included), `edited` = [(name, value)] after the reviewer's changes in the
    editor. A row whose value changed or that is new -> set; a current name
    missing from `edited`, or emptied -> remove."""
    now = {str(n).strip().lower(): (str(n).strip(), str(v).strip())
           for n, v in current}
    sets, filled = {}, set()
    for name, value in edited:
        name = "" if name is None else str(name).strip()
        value = "" if value is None else str(value).strip()
        if not name or not value or value.lower() == "nan":
            continue
        filled.add(name.lower())
        if name.lower() not in now or now[name.lower()][1] != value:
            sets[name] = value
    removes = [now[k][0] for k in now if k not in filled]
    return sets, removes


def merge_attribute_edits(entry, sets, removes):
    """The stored edits (`entry`, or None) plus a new save: a newly set name
    leaves the remove list, a newly removed name leaves the set list.
    Returns (sets, removes) to pass to set_attribute_override."""
    old_sets = dict((entry or {}).get("set") or {})
    old_removes = _names((entry or {}).get("remove"))
    new_low = {k.lower() for k in sets}
    rem_low = {r.lower() for r in _names(removes)}
    merged_sets = {k: v for k, v in old_sets.items()
                   if k.lower() not in new_low and k.lower() not in rem_low}
    merged_sets.update(sets)
    merged_removes = [r for r in old_removes if r.lower() not in new_low]
    merged_removes += [r for r in _names(removes)
                       if r.lower() not in {m.lower() for m in merged_removes}]
    return merged_sets, merged_removes


# ------------------------------------------------------------ share mode --
# A password-protected, restricted GUI for a second person (docs\
# PIPELINE_GUIDE.md "Share mode"). The three environment switches are read
# HERE and nowhere else; gui.py asks these functions on every render, so a
# test can flip the environment between runs.

SHARE_MODE_ENV = "GUI_SHARE_MODE"
SHARE_PASSWORD_ENV = "GUI_SHARE_PASSWORD"
SHARE_BUDGET_ENV = "GUI_SHARE_BUDGET"
SHARE_DEFAULT_BUDGET = 50
SHARE_MIN_PASSWORD = 12
SHARE_MAX_FAILS = 8
SHARE_LOCK_SECONDS = 15 * 60
SHARE_FAIL_DELAY = 1.5          # seconds slept on every wrong password
SHARE_BANNER = ("Share mode: publish, delete, archive and taxonomy edits "
                "are off.")

# enricher flags share mode removes: --js-fallback / --js-profile start a
# headless browser, --force re-pays every serper credit on a cold cache.
# --budget is removed too and re-added with the capped value.
_SHARE_DROP_FLAGS = ("--js-fallback", "--js-profile", "--force")
_SHARE_VALUE_FLAGS = ("--js-profile", "--budget")


class ShareModeRefused(RuntimeError):
    """An action that share mode forbids was asked for. Raised by the
    function that does the work, so a hidden control is not the only guard."""


def _env(env):
    return os.environ if env is None else env


def share_mode_on(env=None) -> bool:
    """True when GUI_SHARE_MODE is 1 / true / on / yes (any case)."""
    return str(_env(env).get(SHARE_MODE_ENV, "")).strip().lower() in (
        "1", "true", "on", "yes")


def share_password(env=None) -> str:
    """The share password exactly as set (no stripping); "" when unset."""
    return str(_env(env).get(SHARE_PASSWORD_ENV) or "")


def share_password_problem(env=None) -> str | None:
    """Why share mode must refuse to render, or None when the password is
    usable. Never includes the password itself."""
    pw = share_password(env)
    if not pw:
        return (f"Share mode is on but {SHARE_PASSWORD_ENV} is not set. "
                "Nothing is shown until a password is configured.")
    if len(pw) < SHARE_MIN_PASSWORD:
        return (f"Share mode is on but {SHARE_PASSWORD_ENV} is shorter than "
                f"{SHARE_MIN_PASSWORD} characters. Nothing is shown until a "
                "longer password is configured.")
    return None


def share_budget(env=None) -> int:
    """GUI_SHARE_BUDGET as a positive int; the default (50) when unset, not
    a number, or not above 0 (0 would mean 'unlimited' to the enricher)."""
    raw = str(_env(env).get(SHARE_BUDGET_ENV, "")).strip()
    try:
        value = int(raw)
    except ValueError:
        return SHARE_DEFAULT_BUDGET
    return value if value > 0 else SHARE_DEFAULT_BUDGET


def check_share_password(given, expected) -> bool:
    """Constant-time compare; an empty expected password never matches."""
    if not expected:
        return False
    return hmac.compare_digest(str(given or "").encode("utf-8"),
                               str(expected).encode("utf-8"))


def _flag_hits(token: str, full: str) -> bool:
    """`token` names the flag `full`, including argparse's abbreviations
    (`--forc`, `--js-f`) and the `--flag=value` form."""
    name = token.split("=", 1)[0]
    return (name.startswith("--") and len(name) >= 3
            and full.startswith(name))


def share_cap_flags(flags, budget: int) -> list:
    """The enricher flags with share mode applied: no --js-fallback,
    --js-profile or --force (any abbreviation), and exactly one
    `--budget N` where N = min(the operator's value if > 0, budget)."""
    cap = budget if budget and budget > 0 else SHARE_DEFAULT_BUDGET
    out, asked = [], 0
    tokens = [str(t) for t in (flags or [])]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        hit = next((f for f in _SHARE_DROP_FLAGS + ("--budget",)
                    if _flag_hits(tok, f)), None)
        if hit is None:
            out.append(tok)
            i += 1
            continue
        value = None
        if "=" in tok:
            value = tok.split("=", 1)[1]
        elif hit in _SHARE_VALUE_FLAGS and i + 1 < len(tokens) \
                and not tokens[i + 1].startswith("--"):
            value = tokens[i + 1]
            i += 1
        if hit == "--budget" and value is not None:
            try:
                asked = int(value)
            except ValueError:
                asked = 0
        i += 1
    n = min(asked, cap) if asked > 0 else cap
    return out + ["--budget", str(n)]


class LoginThrottle:
    """Process-wide guard against password guessing: after `max_fails`
    wrong attempts in total (all sessions together) every login is locked
    for `lock_seconds`. A success, or the end of a lock, resets the count.

    It lives in this imported module, not in gui.py: Streamlit re-executes
    the main script on every rerun, so a global there would reset."""

    def __init__(self, max_fails=SHARE_MAX_FAILS,
                 lock_seconds=SHARE_LOCK_SECONDS, clock=None):
        import threading
        import time as _time
        self.max_fails = max_fails
        self.lock_seconds = lock_seconds
        self._clock = clock or _time.monotonic
        self._lock = threading.Lock()
        self._fails = 0
        self._locked_until = 0.0

    def locked_for(self) -> float:
        """Seconds left on the lock; 0 when logins are open."""
        with self._lock:
            left = self._locked_until - self._clock()
            if left <= 0 and self._locked_until:
                self._locked_until = 0.0
                self._fails = 0
            return max(0.0, left)

    def record_failure(self):
        with self._lock:
            self._fails += 1
            if self._fails >= self.max_fails:
                self._locked_until = self._clock() + self.lock_seconds

    def record_success(self):
        with self._lock:
            self._fails = 0

    def reset(self):
        with self._lock:
            self._fails = 0
            self._locked_until = 0.0

    @property
    def failures(self) -> int:
        return self._fails


SHARE_THROTTLE = LoginThrottle()


# ------------------------------------------------------- cross-platform --
# gui.py runs on the Windows dev machine AND in a Linux container (Render).
# These pure functions hold the parts that differ, so tests on Windows can
# pin the POSIX behaviour without a Linux box.

def venv_python(repo_root, fallback=None) -> Path:
    """The interpreter gui.py launches the enricher and wc_publish.py with:
    <repo>/.venv/Scripts/python.exe when that file exists (Windows dev
    machine), otherwise `fallback` (default: sys.executable - the container
    has no .venv)."""
    venv = Path(repo_root) / ".venv" / "Scripts" / "python.exe"
    if venv.is_file():
        return venv
    return Path(fallback if fallback is not None else sys.executable)


def posix_stat_state(stat_text) -> str | None:
    """The one-letter process state from a /proc/<pid>/stat line, or None
    when the text cannot be parsed.

    Format: `pid (comm) state ppid ...`. comm may hold spaces AND
    parentheses, so the state is the first field after the LAST ')'."""
    if not stat_text:
        return None
    text = str(stat_text)
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 1:].split()
    if not fields:
        return None
    state = fields[0]
    return state if len(state) == 1 and state.isalpha() else None


ENRICHER_SCRIPT = "sku_enricher.py"


def posix_cmdline_is_enricher(cmdline) -> bool:
    """True when a /proc/<pid>/cmdline (NUL-separated argv, bytes) runs
    sku_enricher.py - one argv element whose file name is exactly that
    script. A recycled pid that runs anything else returns False."""
    if not cmdline:
        return False
    if isinstance(cmdline, str):
        cmdline = cmdline.encode("utf-8", errors="replace")
    for arg in bytes(cmdline).split(b"\0"):
        name = arg.decode("utf-8", errors="replace").replace("\\", "/")
        if name.rsplit("/", 1)[-1] == ENRICHER_SCRIPT:
            return True
    return False
