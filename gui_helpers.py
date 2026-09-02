"""gui_helpers.py - pure, streamlit-free helpers behind gui.py.

Unit-tested in tests/test_gui_helpers.py. Keep streamlit (and app.py)
imports OUT of this module so the tests never boot the app."""

import os
import re

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


# mirror of app.SKU_PREFIXES (SKU_PREFIX_BY_PARENT's codes + the AP fallback).
# Copied rather than imported: importing app here would drag Streamlit and the
# taxonomy workbook into every gui_helpers test. tests/test_gui_helpers.py
# pins the two lists together so the copy cannot drift.
SKU_CODES = ("WM", "WH", "TV", "AC", "CF", "KA", "HA", "SA", "EC", "AP")
_SKU_CODE_RE = re.compile(r"^(?:" + "|".join(SKU_CODES) + r")-")
# every code EXCEPT the 'AP' fallback: a raw carrying one of these states a
# category, so re-coding it means the sheet contradicted the detected one.
_SPECIFIC_CODE_RE = re.compile(
    r"^(?:" + "|".join(c for c in SKU_CODES if c != "AP") + r")-")


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


def run_stage(meta, files):
    """Highest pipeline stage a run has reached, from its meta dict and
    {'input','filled','csv'} existence flags."""
    if meta.get("published"):
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


def select_build_rows(df, scope, confirmed):
    """Rows that may reach the CSV build, plus the SKUs refused.

    scope 'confirmed'  -> a row must be ticked AND found_status(); a ticked
                          NEEDS CRAWL / NOT FOUND / CLOSE MATCH row is
                          dropped and named in `skipped`.
    scope 'all_found'  -> every found_status() row, ticked or not;
                          skipped is always [].

    A frame with no 'status' column is not an enricher sheet (a hand-made
    or already-filtered input), so nothing can be judged: keep everything
    for 'all_found', keep the ticked rows for 'confirmed', skipped = [].
    Returns a copy; the input frame is untouched."""
    conf = {str(s) for s in (confirmed or set())}
    skus = df["sku"].astype(str) if "sku" in df.columns else None
    if "status" not in df.columns:
        if scope == "confirmed" and skus is not None:
            return df[skus.isin(conf)].copy(), []
        return df.copy(), []
    is_found = df["status"].map(found_status)
    if scope == "confirmed":
        ticked = skus.isin(conf) if skus is not None else is_found & False
        return (df[ticked & is_found].copy(),
                sorted(set(df.loc[ticked & ~is_found, "sku"].astype(str))))
    return df[is_found].copy(), []


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
