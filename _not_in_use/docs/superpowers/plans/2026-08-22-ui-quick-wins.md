# GUI Quick Wins (UI Refit Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Five review/build-step improvements to the Streamlit wizard: inline data-sanity warnings, search + brand filtering, a category dropdown bound to the taxonomy, auto-validation after the CSV build, and a batch summary dashboard.

**Architecture:** All testable logic goes into a new streamlit-free module `gui_helpers.py` at repo root (pure pandas functions, unit-tested); `gui.py` only gains thin wiring. `gui.py` keeps delegating to `app.process_inventory()` and `validate_csv.validate_export()` — no pipeline logic moves into the GUI.

**Tech Stack:** Python 3.13 (`.venv\Scripts\python.exe` ONLY), Streamlit, pandas, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-22-ui-refit.md` (this plan implements Phase 1)

## Global Constraints

- Python is ALWAYS `.venv\Scripts\python.exe` — never bare `python`, never "activate". Shell is PowerShell.
- Run tests as: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
- Git: stage by explicit path only. NEVER `git add -A`, `git add .`, or `git commit -a`. Never touch `_push_export\` from this repo.
- Do NOT edit `Sku_enrichment\` or `_push_export\` (mirrors). `gui.py` and `gui_helpers.py` are not mirrored — no sync step needed.
- New files stay at repo root (`gui_helpers.py`) or under `tests\` / `docs\`. Never move existing `.py` files.
- No LLM calls. No network calls in tests. Tests use synthetic DataFrames only — never open live spreadsheets in `runs\` or `batches\`.
- Never Read/Glob `.venv\`, `__pycache__\`, `product_images_for_upload\`, `.cache\`.
- `gui.py` line numbers below are as of commit abd6f5c — verify anchors by content, not number, before editing.
- End commit messages with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: `gui_helpers.py` — `flag_value()` and `sanity_issues()`

**Files:**

- Create: `gui_helpers.py`
- Test: `tests\test_gui_helpers.py`

**Interfaces:**

- Consumes: nothing (first task).
- Produces:
  - `flag_value(flags: list, name: str) -> int` — the integer following `name` in an argv-style list (e.g. `["--min-price", "8000"]`), `0` if absent or unparsable.
  - `sanity_issues(df: pandas.DataFrame, min_price: int = 0) -> list[dict]` — each dict is `{"sku": str, "problem": str}`. Expects the review dataframe shape produced by `gui.load_filled_df` (gui.py:426–444): columns `sku` (str), `regular price` / `sale price` (float, NaN when missing), `url_link` (str).

- [ ] **Step 1: Write the failing tests**

Create `tests\test_gui_helpers.py`:

```python
"""Unit tests for gui_helpers.py — pure functions, synthetic frames only."""
import math

import pandas as pd

from gui_helpers import flag_value, sanity_issues


def _df(rows):
    base = {"sku": "S1", "regular price": math.nan,
            "sale price": math.nan, "url_link": ""}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_flag_value_present():
    assert flag_value(["--discover", "--min-price", "8000"], "--min-price") == 8000


def test_flag_value_absent_trailing_or_bad():
    assert flag_value([], "--min-price") == 0
    assert flag_value(["--min-price"], "--min-price") == 0
    assert flag_value(["--min-price", "cheap"], "--min-price") == 0


def test_sale_above_mrp_flagged():
    out = sanity_issues(_df([{"regular price": 100.0, "sale price": 150.0}]))
    assert len(out) == 1 and out[0]["sku"] == "S1"
    assert "above MRP" in out[0]["problem"]


def test_non_positive_prices_flagged():
    out = sanity_issues(_df([{"regular price": 0.0},
                             {"sku": "S2", "sale price": -5.0}]))
    assert {o["sku"] for o in out} == {"S1", "S2"}


def test_below_min_price_flagged_only_when_set():
    rows = [{"sale price": 4000.0}]
    assert sanity_issues(_df(rows)) == []                      # min_price off
    out = sanity_issues(_df(rows), min_price=8000)
    assert len(out) == 1 and "below batch min price" in out[0]["problem"]


def test_bad_url_flagged_empty_url_ignored():
    out = sanity_issues(_df([{"url_link": "www.amazon.in/dp/X"},
                             {"sku": "S2", "url_link": "https://ok.example"},
                             {"sku": "S3", "url_link": ""}]))
    assert len(out) == 1 and out[0]["sku"] == "S1"


def test_clean_row_no_issues():
    out = sanity_issues(_df([{"regular price": 22599.0, "sale price": 18999.0,
                              "url_link": "https://www.croma.com/x"}]),
                        min_price=8000)
    assert out == []


def test_nan_prices_are_not_issues():
    assert sanity_issues(_df([{}]), min_price=8000) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gui_helpers'`

- [ ] **Step 3: Write the implementation**

Create `gui_helpers.py`:

```python
"""gui_helpers.py - pure, streamlit-free helpers behind gui.py.

Unit-tested in tests/test_gui_helpers.py. Keep streamlit (and app.py)
imports OUT of this module so the tests never boot the app."""

import re

import pandas as pd

_URL_RE = re.compile(r"^https?://", re.I)


def flag_value(flags, name):
    """Integer value following `name` in an argv-style flag list; 0 if the
    flag is absent, last, or its value is not a number."""
    flags = list(flags or [])
    try:
        return int(str(flags[flags.index(name) + 1]))
    except (ValueError, IndexError):
        return 0


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```powershell
git add gui_helpers.py tests\test_gui_helpers.py
git commit -m @'
feat: add gui_helpers with review-sheet sanity checks

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 2: Sanity warning strip in Step 2 (review)

**Files:**

- Modify: `gui.py` — imports (~line 33) and `step_review()` (~line 517, insert after the metrics row ending `m5.metric("✔ Confirmed", ...)` at ~line 539)

**Interfaces:**

- Consumes: `gui_helpers.flag_value`, `gui_helpers.sanity_issues` (Task 1 signatures).
- Produces: UI only — a warning strip; no new symbols other tasks rely on.

- [ ] **Step 1: Add the import**

In `gui.py`, after `import streamlit as st` (line 35), add:

```python
from gui_helpers import flag_value, sanity_issues
```

(Root scripts import each other by module name; `gui_helpers.py` sits next to `gui.py`, so no path games.)

- [ ] **Step 2: Wire the strip into `step_review`**

In `step_review()`, directly after the five-metric row (the line `m5.metric("✔ Confirmed", int(df["confirmed"].sum()))`, ~line 539) and BEFORE the `st.caption("Edit title, prices, ...")` call, insert:

```python
    probs = sanity_issues(
        df, min_price=flag_value(load_meta(P).get("enricher_flags", []),
                                 "--min-price"))
    if probs:
        st.warning(f"⚠ {len(probs)} data problem(s) in this sheet — fix them "
                   "here before building the CSV.")
        with st.expander("Show problems", expanded=len(probs) <= 10):
            st.dataframe(pd.DataFrame(probs), hide_index=True,
                         use_container_width=True)
```

- [ ] **Step 3: Syntax check + full helper suite**

Run: `.venv\Scripts\python.exe -m py_compile gui.py; .venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -q`
Expected: no output from py_compile (exit 0); 8 passed

- [ ] **Step 4: Commit**

```powershell
git add gui.py
git commit -m @'
feat: inline price/URL sanity warnings in the review step

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 3: Search + brand filter over the review table

**Files:**

- Modify: `gui_helpers.py` (add `filter_review_df`)
- Modify: `gui.py` — imports, `step_review()` (filter controls + filtered tabs + merge-save fix)
- Test: `tests\test_gui_helpers.py` (append)

**Interfaces:**

- Consumes: nothing new.
- Produces: `filter_review_df(df: pandas.DataFrame, query: str = "", brands: list | None = None) -> pandas.DataFrame` — rows whose `sku` or `title` contains `query` case-insensitively (regex-safe) AND whose `Brands` is in `brands` (when non-empty). Original index preserved.

**CRITICAL BEHAVIOR CHANGE this task must also make:** `save_confirmed` (gui.py:118–122) REPLACES the whole confirmed dict, and the save handler currently rebuilds it from the visible editor parts. That is safe today only because the "All rows" tab always contains every SKU. Once the table is filterable, the handler MUST merge into the existing confirmed set, or saving while filtered silently un-confirms every hidden row. Step 4 below makes that change; do not skip it.

- [ ] **Step 1: Write the failing tests**

Append to `tests\test_gui_helpers.py`:

```python
from gui_helpers import filter_review_df


def _rdf():
    return pd.DataFrame([
        {"sku": "WM-IFB-01", "title": "IFB 8kg Front Load", "Brands": "IFB"},
        {"sku": "WM-LG-02", "title": "LG 6.5kg Top Load", "Brands": "LG"},
        {"sku": "AC-LG-03", "title": "LG 1.5 Ton Split AC", "Brands": "LG"},
    ])


def test_filter_noop_returns_all_rows():
    assert len(filter_review_df(_rdf())) == 3
    assert len(filter_review_df(_rdf(), "", [])) == 3


def test_filter_query_matches_sku_and_title_case_insensitive():
    assert list(filter_review_df(_rdf(), "ifb")["sku"]) == ["WM-IFB-01"]
    assert list(filter_review_df(_rdf(), "top load")["sku"]) == ["WM-LG-02"]


def test_filter_query_is_regex_safe():
    assert list(filter_review_df(_rdf(), "1.5 Ton")["sku"]) == ["AC-LG-03"]
    assert len(filter_review_df(_rdf(), "1x5 Ton")) == 0   # '.' not a wildcard


def test_filter_by_brand_and_combined():
    assert len(filter_review_df(_rdf(), brands=["LG"])) == 2
    assert list(filter_review_df(_rdf(), "AC", ["LG"])["sku"]) == ["AC-LG-03"]


def test_filter_preserves_original_index():
    out = filter_review_df(_rdf(), brands=["LG"])
    assert list(out.index) == [1, 2]


def test_filter_without_title_column():
    df = pd.DataFrame([{"sku": "X1", "Brands": "LG"}])
    assert len(filter_review_df(df, "x1")) == 1
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
Expected: 8 pass, 6 FAIL with `ImportError: cannot import name 'filter_review_df'`

- [ ] **Step 3: Implement `filter_review_df`**

Append to `gui_helpers.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
Expected: 14 passed

- [ ] **Step 5: Wire controls into `step_review` and fix the save merge**

5a. Add `import hashlib` to gui.py's stdlib imports (alphabetical, after `import ctypes`), and extend the helper import to:

```python
from gui_helpers import filter_review_df, flag_value, sanity_issues
```

5b. In `step_review()`, AFTER the sanity strip from Task 2 and BEFORE `st.caption("Edit title, prices, ...")`, insert:

```python
    fc1, fc2 = st.columns([2, 1])
    query = fc1.text_input("🔍 Search SKU / title",
                           key=f"flt_q_{P['run'].name}")
    brand_opts = sorted(b for b in df["Brands"].astype(str).str.strip()
                        .unique() if b)
    brands = fc2.multiselect("Brand", brand_opts,
                             key=f"flt_b_{P['run'].name}")
    fdf = filter_review_df(df, query, brands)
    if len(fdf) < len(df):
        st.caption(f"Showing **{len(fdf)} of {len(df)}** rows. Save edits "
                   "before changing the filter — unsaved edits in hidden "
                   "rows are lost on rerender.")
    # a new filter must reset the editors, else stale edit state maps onto
    # different rows; hashing the filter into the key does that
    fsig = hashlib.md5(f"{query}|{'|'.join(brands)}".encode()).hexdigest()[:6]
```

5c. Change the five tab blocks (gui.py:550–563) to use `fdf` and the `fsig` keys — the metrics above stay on the full `df`:

```python
    parts = []
    with tabs[0]:
        parts.append(review_editor(fdf[fdf["_bucket"] == "review"],
                                   f"ed_review_{fsig}"))
    with tabs[1]:
        if pending or int(n.get("failed", 0)):
            st.caption("Paste a product URL into **url_link** for each row "
                       "here, save, then press **Crawl all info**.")
        parts.append(review_editor(fdf[fdf["_bucket"] == "failed"],
                                   f"ed_failed_{fsig}"))
    with tabs[2]:
        parts.append(review_editor(fdf[fdf["_bucket"] == "pending"],
                                   f"ed_pending_{fsig}"))
    with tabs[3]:
        parts.append(review_editor(fdf[fdf["_bucket"] == "found"],
                                   f"ed_found_{fsig}"))
    with tabs[4]:
        parts.append(review_editor(fdf, f"ed_all_{fsig}"))
```

(The tab labels keep their full-df counts from `n` — leave the `st.tabs([...])` call unchanged.)

5d. In the save handler (`if b1.button("💾 Save edits & confirmations", ...)`), replace the flags rebuild:

```python
        flags = {}
        for part in ordered:
            for _, row in part.iterrows():
                flags[str(row["sku"])] = bool(row["confirmed"])
        save_confirmed(P, flags)
```

with a merge that keeps hidden rows confirmed:

```python
        flags = {sku: True for sku in confirmed_set(P)}   # hidden rows keep ✔
        for part in ordered:
            for _, row in part.iterrows():
                flags[str(row["sku"])] = bool(row["confirmed"])
        save_confirmed(P, flags)
```

(`write_edits_back(P, df, ordered)` needs no change: it matches by SKU against the full original `df`. The "Confirm all clean FOUND" button also needs no change: it iterates the full `df`.)

- [ ] **Step 6: Syntax check + suite**

Run: `.venv\Scripts\python.exe -m py_compile gui.py; .venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -q`
Expected: exit 0; 14 passed

- [ ] **Step 7: Commit**

```powershell
git add gui.py gui_helpers.py tests\test_gui_helpers.py
git commit -m @'
feat: search and brand filter on the review table

Saving while filtered now merges into the confirmed set instead of
replacing it, so hidden rows keep their confirmation.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 4: Category dropdown bound to the taxonomy

**Files:**

- Modify: `gui.py` — new cached `category_options()` function (place it directly above `review_editor`, ~line 447), `review_editor()` signature + column_config, `step_review()` call sites

**Interfaces:**

- Consumes: `app.CATEGORY_COMBOS` (dict, values are exact `"Parent > Sub"` strings from `category_map.xlsx` — app.py:308–339).
- Produces: `category_options() -> list[str]` in gui.py; `review_editor(df, key, cat_options=None)` — third parameter is new and optional, so any call site not updated still works.

- [ ] **Step 1: Add the cached options loader**

In `gui.py`, directly above `def review_editor(...)`:

```python
@st.cache_resource
def category_options() -> list:
    """Exact category strings from category_map.xlsx, via the same loader
    app.py snaps against — so anything picked here is a legal snap target."""
    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    import app
    return sorted(set(app.CATEGORY_COMBOS.values()))
```

(`os.environ.setdefault` mirrors what `step_build` already does before its own `import app`, gui.py:659–660.)

- [ ] **Step 2: Extend `review_editor`**

Change the signature `def review_editor(df: pd.DataFrame, key: str) -> pd.DataFrame:` to:

```python
def review_editor(df: pd.DataFrame, key: str,
                  cat_options: list | None = None) -> pd.DataFrame:
```

and add to the `column_config` dict (after the `"sale price"` entry):

```python
            **({"categories": st.column_config.SelectboxColumn(
                    "categories", options=cat_options, required=False,
                    width="medium")} if cat_options else {}),
```

- [ ] **Step 3: Compute options in `step_review` and pass them through**

In `step_review()`, after `fdf = filter_review_df(...)` from Task 3, add:

```python
    # a SelectboxColumn renders a cell as blank if its current value is not
    # in options — union in whatever the sheet already holds so nothing
    # displays empty
    cat_opts = category_options()
    cat_opts = cat_opts + sorted(
        set(df["categories"].astype(str).str.strip()) - set(cat_opts) - {""})
```

Then add `cat_opts` as the third argument to all five `review_editor(...)` calls from Task 3, e.g. `review_editor(fdf[fdf["_bucket"] == "review"], f"ed_review_{fsig}", cat_opts)`.

- [ ] **Step 4: Syntax check + suite**

Run: `.venv\Scripts\python.exe -m py_compile gui.py; .venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -q`
Expected: exit 0; 14 passed

- [ ] **Step 5: Commit**

```powershell
git add gui.py
git commit -m @'
feat: review-table categories become a taxonomy-bound dropdown

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 5: Auto-validate right after the CSV build

**Files:**

- Modify: `gui.py` — end of `step_build()` (~lines 697–706: the `update_meta(...)` call and the final `st.info("Continue to **Step 4 · Validate**.")`)

**Interfaces:**

- Consumes: `validate_csv.validate_export(df, csv_path, check_images=True, write_issues=True) -> list[dict]` (validate_csv.py:608) — same call step 4 makes at gui.py:733–734. `P["issues"]` is the run's `validation_issues.csv` path; `st.session_state[f"val_out_{P['run'].name}"]` is the key step 4 reads its console text from (gui.py:736, 745).
- Produces: UI only. Meta semantics change: after a build, `validated` is `True` with a fresh `issue_count`, instead of `False`-until-step-4.

- [ ] **Step 1: Replace the build epilogue**

At the end of `step_build()`, replace this block:

```python
    update_meta(P, validated=False,     # a new CSV must be re-validated
                built_scope=scope,
                built_skus=[str(s) for s in result["sku"].tolist()])
    st.info("Continue to **Step 4 · Validate**.")
```

with:

```python
    # validate the moment the file is written - step 4 stays as a re-check
    # (e.g. with image inspection toggled) but problems surface here first
    import validate_csv
    vdf = pd.read_csv(P["csv"], dtype=str).fillna("")
    vbuf = io.StringIO()
    with st.spinner("Validating the new CSV…"):
        with contextlib.redirect_stdout(vbuf):
            v_issues = validate_csv.validate_export(
                vdf, str(P["csv"]), check_images=True)
    update_meta(P, validated=True, issue_count=len(v_issues),
                built_scope=scope,
                built_skus=[str(s) for s in result["sku"].tolist()])
    st.session_state[f"val_out_{P['run'].name}"] = vbuf.getvalue()
    if v_issues:
        st.warning(f"Validation found **{len(v_issues)} issue(s)** — fix the "
                   "rows in Step 2 and rebuild, or review details in Step 4.")
        if P["issues"].exists():
            st.dataframe(pd.read_csv(P["issues"], dtype=str).fillna(""),
                         hide_index=True, use_container_width=True)
    else:
        st.success("Validation passed — continue to **Step 5 · Publish**.")
```

- [ ] **Step 2: Syntax check + suite**

Run: `.venv\Scripts\python.exe -m py_compile gui.py; .venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -q`
Expected: exit 0; 14 passed

- [ ] **Step 3: Commit**

```powershell
git add gui.py
git commit -m @'
feat: run validation automatically at the end of the CSV build

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 6: Batch summary after the build

**Files:**

- Modify: `gui_helpers.py` (add `_price_series`, `batch_summary`)
- Modify: `gui.py` — `step_build()`, directly after the `st.success(f"Wrote **{P['csv'].name}** ...")` / zero-image `st.warning` block (~line 691)
- Test: `tests\test_gui_helpers.py` (append)

**Interfaces:**

- Consumes: the `app.process_inventory()` output DataFrame — all string cells; relevant columns `Images` (comma-separated URLs), `_spec_gaps`, `Brands`, `sale price` (Indian-formatted, e.g. `"22,599"`), `categories` (app.py:83 `EXPORTED_OUTPUT_COLUMNS`).
- Produces: `batch_summary(result: pandas.DataFrame) -> dict` with keys `products` (int), `zero_image` (int), `avg_images` (float, 1dp), `spec_gap_rows` (int), `missing_brand` (int), `price_min` / `price_max` (int, 0 when no prices), `category_counts` (dict str→int, empty string shown as `"(none)"`).

- [ ] **Step 1: Write the failing tests**

Append to `tests\test_gui_helpers.py`:

```python
from gui_helpers import batch_summary


def _result():
    return pd.DataFrame([
        {"sku": "A", "Images": "https://x/1.webp, https://x/2.webp",
         "_spec_gaps": "", "Brands": "LG", "sale price": "22,599",
         "categories": "Home Appliances > Washing Machines"},
        {"sku": "B", "Images": "", "_spec_gaps": "Capacity, RPM",
         "Brands": "", "sale price": "8,999",
         "categories": "Home Appliances > Washing Machines"},
        {"sku": "C", "Images": "https://x/3.webp", "_spec_gaps": "",
         "Brands": "IFB", "sale price": "", "categories": ""},
    ])


def test_batch_summary_counts():
    s = batch_summary(_result())
    assert s["products"] == 3
    assert s["zero_image"] == 1
    assert s["avg_images"] == 1.0          # (2 + 0 + 1) / 3
    assert s["spec_gap_rows"] == 1
    assert s["missing_brand"] == 1


def test_batch_summary_prices_parse_indian_format():
    s = batch_summary(_result())
    assert s["price_min"] == 8999
    assert s["price_max"] == 22599


def test_batch_summary_categories():
    s = batch_summary(_result())
    assert s["category_counts"] == {
        "Home Appliances > Washing Machines": 2, "(none)": 1}


def test_batch_summary_empty_frame():
    s = batch_summary(_result().iloc[0:0])
    assert s["products"] == 0 and s["price_min"] == 0 and s["avg_images"] == 0.0
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
Expected: 14 pass, 4 FAIL with `ImportError: cannot import name 'batch_summary'`

- [ ] **Step 3: Implement**

Append to `gui_helpers.py`:

```python
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
        "category_counts": result["categories"].astype(str).str.strip()
                                 .replace("", "(none)")
                                 .value_counts().to_dict(),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
Expected: 18 passed

- [ ] **Step 5: Wire into `step_build`**

Extend gui.py's helper import to:

```python
from gui_helpers import (batch_summary, filter_review_df, flag_value,
                         sanity_issues)
```

In `step_build()`, after the `if with_images < len(result): st.warning(...)` block and BEFORE `with st.expander("Build log"):`, insert:

```python
    s = batch_summary(result)
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Avg images/SKU", s["avg_images"])
    k2.metric("Zero-image rows", s["zero_image"])
    k3.metric("Rows w/ spec gaps", s["spec_gap_rows"])
    k4.metric("Missing brand", s["missing_brand"])
    k5.metric("Price range ₹", f"{s['price_min']:,}–{s['price_max']:,}")
    if s["category_counts"]:
        with st.expander("Products per category", expanded=True):
            st.bar_chart(pd.Series(s["category_counts"], name="products"))
```

- [ ] **Step 6: Syntax check + suite**

Run: `.venv\Scripts\python.exe -m py_compile gui.py; .venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -q`
Expected: exit 0; 18 passed

- [ ] **Step 7: Commit**

```powershell
git add gui.py gui_helpers.py tests\test_gui_helpers.py
git commit -m @'
feat: batch summary metrics and category chart after the CSV build

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 7: Whole-suite regression + headless smoke boot

**Files:**

- No file changes — verification only.

**Interfaces:**

- Consumes: everything above.
- Produces: evidence the app still boots and nothing else regressed.

- [ ] **Step 1: Full test suite (not just the new file)**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: all pass — including the pre-existing `test_two_pass.py` and `test_polite_sleep.py`. Any failure there means a regression in shared code: stop and fix before proceeding.

- [ ] **Step 2: Boot the wizard headless and confirm it serves**

```powershell
$out = Join-Path $env:TEMP "gui_smoke_out.txt"; $err = Join-Path $env:TEMP "gui_smoke_err.txt"
$p = Start-Process ".venv\Scripts\python.exe" -ArgumentList '-m','streamlit','run','gui.py','--server.headless','true','--server.port','8599' -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
Start-Sleep 15
(Invoke-WebRequest "http://localhost:8599" -UseBasicParsing).StatusCode
Stop-Process $p.Id -Force
Select-String -Path $err -Pattern "Traceback" -SimpleMatch
```

Expected: `200` from the request; the final `Select-String` prints nothing (no traceback). Delete the two temp files afterwards.

- [ ] **Step 3: Manual spot-check (report only, no commit)**

If a past run exists under `runs\`, open the app normally, select it, and confirm: Step 2 shows the sanity strip and the search/brand filter, categories render as dropdowns, and (if a CSV was built) Step 3's rebuild path shows the summary metrics. Report what was and wasn't checkable — do not fabricate a pass for anything that couldn't be exercised.
