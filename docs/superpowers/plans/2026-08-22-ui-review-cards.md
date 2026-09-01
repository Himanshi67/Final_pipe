# Card-Mode Review (UI Refit Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A one-SKU-at-a-time card review mode (full image gallery, editable fields, approve/skip queue), per-image keep/drop that the CSV build honors, and validation issues that deep-link back to the review step.

**Architecture:** Pure logic (URL/spec parsing, prefixed-SKU back-mapping) goes into `gui_helpers.py` with unit tests; `gui.py` gains a `card_review()` view toggled from step 2 and a `jump_step` navigation mechanism; `app.py` gains one small pure filter (`filter_dropped_urls`) applied at the image-download call site. No pipeline semantics change: statuses, FOUND-gating, and `write_edits_back`'s by-SKU matching stay as they are.

**Tech Stack:** Python 3.13 (`.venv\Scripts\python.exe` ONLY), Streamlit, pandas, openpyxl, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-22-ui-refit.md` (Phase 2). Prerequisite: Phase 1 is merged (commits `3d03967`…`8697d2d`) — `gui_helpers.py`, the review filters, and `category_options()` exist.

## Global Constraints

- Python is ALWAYS `.venv\Scripts\python.exe` — never bare `python`, never "activate". Shell is PowerShell.
- Run tests as: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v` (18 tests pass before this plan starts).
- Git: stage by explicit path only. NEVER `git add -A`, `git add .`, or `git commit -a`. Never touch `_push_export\`.
- Do NOT edit `Sku_enrichment\` or `_push_export\` (mirrors). This plan touches `gui.py`, `gui_helpers.py`, `app.py` — none of them is mirrored (only `sku_enricher.py` is), so no /sync-copies step.
- Tests use synthetic DataFrames and `tmp_path` workbooks only — never live spreadsheets in `runs\` or `batches\`. Tests that import `app` or `gui` must set `os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")` first and do the import INSIDE the test function.
- Line anchors are as of commit `8697d2d` — verify by content, not number, before editing.
- End commit messages with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

Facts this plan is built on (verified in code):

- Review-sheet `image_url` cell: comma-separated URLs; `app.split_image_list` (app.py:19) splits on `,(?=\s*(?:https?://|//))`.
- `specs` cell format: `'Label: Value | Label: Value'` (sku_enricher.py:2138–2148); labels never contain `:` or `|`.
- CSV/issues SKUs are `app.prefixed_sku` output (app.py:601–626): whitespace/specials → `-`, dash-runs collapsed, then an optional `XX-` category prefix (SKUs already starting `WM-`/`WH-` untouched).
- `validation_issues.csv` columns: `row,sku,title,reasons,first_image`; the non-product-photo reason starts with `image_not_a_product_photo` and carries a parenthetical suffix — match with `startswith`.
- Images download at app.py:1731–1732 (`urls = split_image_urls(row)` → `download_urls_for_sku(sku, urls)`), files named `{prefixed_sku}_{n}.webp`, n from `enumerate(urls, start=1)` — so filtering must happen BEFORE the download call, and dropping a URL renumbers later files for that SKU (harmless: the `Images` column is rebuilt from `saved_indexes` at app.py:1736 in the same build).
- The step radio (gui.py:1017) has NO key today; programmatic jumps need one.

---

### Task 1: Parsing helpers — `parse_image_urls`, `parse_specs_cell`, `raw_sku_for_csv_sku`

**Files:**

- Modify: `gui_helpers.py` (append)
- Test: `tests\test_gui_helpers.py` (append)

**Interfaces:**

- Consumes: nothing new.
- Produces:
  - `parse_image_urls(cell) -> list[str]` — URL-boundary-aware split of a comma-separated cell; `[]` for blank/None.
  - `parse_specs_cell(cell) -> list[tuple[str, str]]` — `'Label: Value | …'` → `[(label, value)]`; tolerant of blanks and `:` inside values.
  - `raw_sku_for_csv_sku(csv_sku: str, raw_skus: list) -> str | None` — maps a prefixed CSV/issues SKU back to the raw review-sheet SKU by sanitized-suffix match (longest raw wins), `None` when nothing matches.

- [ ] **Step 1: Write the failing tests**

Append to `tests\test_gui_helpers.py`:

```python
from gui_helpers import (parse_image_urls, parse_specs_cell,
                         raw_sku_for_csv_sku)


def test_parse_image_urls_basic_and_blank():
    cell = "https://a/1.jpg, https://a/2.jpg,https://a/3.jpg"
    assert parse_image_urls(cell) == [
        "https://a/1.jpg", "https://a/2.jpg", "https://a/3.jpg"]
    assert parse_image_urls("") == []
    assert parse_image_urls(None) == []


def test_parse_image_urls_comma_inside_url_survives():
    cell = "https://a/im,g/1.jpg, https://b/2.jpg"
    assert parse_image_urls(cell) == ["https://a/im,g/1.jpg", "https://b/2.jpg"]


def test_parse_specs_cell_pairs_and_colon_in_value():
    cell = "Capacity: 8 kg | Ratio: 16:9 | Warranty: 2 years"
    assert parse_specs_cell(cell) == [("Capacity", "8 kg"), ("Ratio", "16:9"),
                                      ("Warranty", "2 years")]
    assert parse_specs_cell("") == []
    assert parse_specs_cell("no separator here") == []


def test_raw_sku_untouched_and_prefixed():
    assert raw_sku_for_csv_sku("WM-HW100", ["WM-HW100", "X200"]) == "WM-HW100"
    assert raw_sku_for_csv_sku("AP-X200", ["WM-HW100", "X200"]) == "X200"


def test_raw_sku_sanitized_spaces():
    assert raw_sku_for_csv_sku("AP-AB-100", ["AB 100"]) == "AB 100"


def test_raw_sku_longest_match_wins_and_none():
    assert raw_sku_for_csv_sku("AP-AB-100", ["100", "AB 100"]) == "AB 100"
    assert raw_sku_for_csv_sku("AP-ZZZ", ["AB 100"]) is None
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
Expected: 18 pass, 6 FAIL with `ImportError: cannot import name 'parse_image_urls'`

- [ ] **Step 3: Implement**

Append to `gui_helpers.py`:

```python
# split only at commas that start a new URL - same rule as
# app.split_image_list, so commas inside a URL survive
_IMG_SPLIT_RE = re.compile(r",(?=\s*(?:https?://|//))")

# mirror of app.prefixed_sku's sanitization (app.py:605-612)
_SKU_SANITIZE = (
    (re.compile(r"\s+"), "-"),
    (re.compile(r'[\\/:*?"<>|()\[\]{}]+'), "-"),
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


def _sanitized_sku(raw):
    s = str(raw or "").strip()
    for rx, rep in _SKU_SANITIZE:
        s = rx.sub(rep, s)
    return s.strip("-")


def raw_sku_for_csv_sku(csv_sku, raw_skus):
    """Map a CSV/validation sku (app.prefixed_sku output) back to the raw
    sku the review sheet holds. The prefixed form is sanitize(raw) with an
    optional 'XX-' category prefix, so match sanitized suffixes; the
    longest raw wins; None when nothing matches."""
    c = str(csv_sku or "").strip()
    best, best_len = None, -1
    for raw in raw_skus:
        s = _sanitized_sku(raw)
        if s and (c == s or c.endswith("-" + s)) and len(s) > best_len:
            best, best_len = raw, len(s)
    return best
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
Expected: 24 passed

- [ ] **Step 5: Commit**

```powershell
git add gui_helpers.py tests\test_gui_helpers.py
git commit -m @'
feat: image/spec cell parsers and prefixed-sku back-mapping

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 2: `dropped_images` plumbing — sheet column + build-time filter

**Files:**

- Modify: `gui.py` — `EDITABLE_COLS` (~line 435) and `write_edits_back()` (~line 488)
- Modify: `app.py` — new `filter_dropped_urls()` next to `split_image_urls` (~line 1495) and the call site in `process_inventory` (~line 1731)
- Test: `tests\test_gui_helpers.py` (append)

**Interfaces:**

- Consumes: `gui.write_edits_back(P, original, edited_parts)` (writes only `EDITABLE_COLS` present in the sheet headers, matched by SKU); `app.split_image_list` (app.py:19).
- Produces:
  - Sheet contract: `dropped_images` column in `input_filled.xlsx` — comma-separated URLs the reviewer rejected. Written by the GUI, honored by the build.
  - `app.filter_dropped_urls(urls: list, dropped_cell) -> list` — pure.
  - `write_edits_back` now CREATES a missing header on demand (today an edit to a column the enricher never wrote is silently skipped by the `if col in headers` guard).

- [ ] **Step 1: Write the failing tests**

Append to `tests\test_gui_helpers.py`:

```python
import os


def test_write_edits_back_creates_missing_column(tmp_path):
    import openpyxl
    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    import gui
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["sku", "title"])
    ws.append(["S1", "Old"])
    f = tmp_path / "input_filled.xlsx"
    wb.save(f)
    orig = pd.DataFrame([{"sku": "S1", "title": "Old", "dropped_images": ""}])
    part = pd.DataFrame([{"sku": "S1", "title": "Old",
                          "dropped_images": "https://x/1.jpg"}])
    changed = gui.write_edits_back({"filled": f}, orig, [part])
    assert changed == 1
    ws2 = openpyxl.load_workbook(f).active
    hdrs = [c.value for c in ws2[1]]
    assert "dropped_images" in hdrs
    assert ws2.cell(row=2, column=hdrs.index("dropped_images") + 1
                    ).value == "https://x/1.jpg"


def test_app_filter_dropped_urls():
    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    import app
    urls = ["https://a/1.jpg", "https://a/2.jpg", "https://a/3.jpg"]
    assert app.filter_dropped_urls(urls, "https://a/2.jpg") == [
        "https://a/1.jpg", "https://a/3.jpg"]
    assert app.filter_dropped_urls(urls, " https://a/1.jpg , https://a/3.jpg"
                                   ) == ["https://a/2.jpg"]
    assert app.filter_dropped_urls(urls, "") == urls
    assert app.filter_dropped_urls(urls, None) == urls
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
Expected: 24 pass, 2 FAIL (`changed == 1` assertion fails — the column is skipped; `AttributeError: module 'app' has no attribute 'filter_dropped_urls'`)

- [ ] **Step 3: Implement the gui.py side**

3a. Add `"dropped_images"` to `EDITABLE_COLS` (gui.py ~line 435):

```python
EDITABLE_COLS = ["title", "regular price", "sale price", "url_link",
                 "image_url", "categories", "Brands", "dropped_images"]
```

3b. In `write_edits_back()`, directly after the `if not sku_c: raise RuntimeError(...)` guard, insert:

```python
    # the review GUI can introduce columns the enricher never wrote
    # (dropped_images) - create the header on demand, else the
    # `col in headers` guard below silently discards the edit
    needed = {c for edit in edits.values() for c in edit}
    for col in sorted(needed - set(headers)):
        new_c = ws.max_column + 1
        ws.cell(row=1, column=new_c, value=col)
        headers[col] = new_c
```

(`load_filled_df` already back-fills every `EDITABLE_COLS` column with `""` on read, so the new column needs no loader change.)

- [ ] **Step 4: Implement the app.py side**

4a. Directly above `def download_urls_for_sku` (app.py ~line 1500), add:

```python
def filter_dropped_urls(urls, dropped_cell):
    """Remove reviewer-rejected image URLs (comma-separated cell written by
    the GUI's card review) before download. Survivor order is preserved."""
    dropped = {u.strip() for u in split_image_list(str(dropped_cell or ''))
               if u.strip()}
    if not dropped:
        return urls
    return [u for u in urls if u.strip() not in dropped]
```

4b. At the call site in `process_inventory` (app.py ~line 1731), change:

```python
        urls = split_image_urls(row)
        saved_indexes = download_urls_for_sku(sku, urls)
```

to:

```python
        urls = filter_dropped_urls(split_image_urls(row),
                                   row.get('dropped_images'))
        saved_indexes = download_urls_for_sku(sku, urls)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -v`
Expected: 26 passed

- [ ] **Step 6: Syntax check + commit**

Run: `.venv\Scripts\python.exe -m py_compile gui.py app.py`
Expected: exit 0

```powershell
git add gui.py app.py tests\test_gui_helpers.py
git commit -m @'
feat: reviewer image drops flow from the sheet into the CSV build

write_edits_back now creates a missing header instead of silently
discarding the edit.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 3: Card review mode in Step 2

**Files:**

- Modify: `gui.py` — imports (extend the `from gui_helpers import (...)` at line 38), new `validator_flags()` + `card_review()` above `step_review()` (~line 534), mode toggle inside `step_review()`

**Interfaces:**

- Consumes: `parse_image_urls`, `parse_specs_cell`, `raw_sku_for_csv_sku` (Task 1); `write_edits_back` with `dropped_images` (Task 2); `BADGE`, `classify`, `confirmed_set`, `save_confirmed`, `category_options` (existing); the review df from `step_review` (numeric prices, `confirmed` bool, `_bucket` column).
- Produces: `card_review(P, df, cat_opts)` and `validator_flags(P, raw_sku) -> str` in gui.py. UI contract: session key `card_i_<run>` holds the queue position.

- [ ] **Step 1: Extend the helper import**

```python
from gui_helpers import (batch_summary, filter_review_df, flag_value,
                         parse_image_urls, parse_specs_cell,
                         raw_sku_for_csv_sku, sanity_issues)
```

- [ ] **Step 2: Add `validator_flags` and `card_review` above `step_review`**

```python
def validator_flags(P, raw_sku: str) -> str:
    """Reasons the last validation run flagged this review row for, if any.
    The issues CSV carries the PREFIXED sku, so map back by suffix."""
    if not P["issues"].exists():
        return ""
    try:
        idf = pd.read_csv(P["issues"], dtype=str).fillna("")
    except Exception:
        return ""
    for _, r in idf.iterrows():
        if raw_sku_for_csv_sku(str(r.get("sku", "")), [raw_sku]) == raw_sku:
            return str(r.get("reasons", ""))
    return ""


def card_review(P, df, cat_opts):
    """One SKU per screen: gallery with keep/drop, editable fields, an
    approve/skip queue. df is the (possibly filtered) review dataframe -
    prices numeric, confirmed bool, _bucket set."""
    run = P["run"].name
    if df.empty:
        st.info("No rows in the current filter.")
        return
    order = {"review": 0, "failed": 1, "pending": 2, "found": 3, "skipped": 4}
    queue = df.sort_values(
        by=["confirmed", "_bucket"],
        key=lambda s: s.map(order) if s.name == "_bucket" else s,
    ).reset_index(drop=True)

    ikey = f"card_i_{run}"
    i = min(st.session_state.get(ikey, 0), len(queue) - 1)
    row = queue.iloc[i]
    sku = str(row["sku"])

    def goto(j):
        st.session_state[ikey] = max(0, min(j, len(queue) - 1))
        st.rerun()

    st.progress((i + 1) / len(queue),
                text=f"Card {i + 1} / {len(queue)} — "
                     f"{int(queue['confirmed'].sum())} confirmed")
    st.subheader(f"{BADGE[row['_bucket']]} {sku}")
    st.caption(str(row["status"]))
    src = str(row.get("url_link", "") or "").strip()
    if src:
        st.markdown(f"🔗 [Open source page]({src})")
    flagged = validator_flags(P, sku)
    if flagged:
        st.warning(f"Validator flagged this SKU last run: {flagged}")

    urls = parse_image_urls(row.get("image_url", ""))
    dropped_now = set(parse_image_urls(row.get("dropped_images", "")))
    keeps = {}
    if urls:
        cols = st.columns(min(4, len(urls)))
        for j, u in enumerate(urls):
            with cols[j % len(cols)]:
                st.image(u, use_container_width=True)
                keeps[u] = st.checkbox("keep", value=u not in dropped_now,
                                       key=f"keep_{run}_{sku}_{j}")
    else:
        st.info("No images on this row.")

    e1, e2, e3 = st.columns([2, 1, 1])
    title = e1.text_input("Title", str(row.get("title", "")),
                          key=f"ct_{run}_{sku}")
    mrp_s = e2.text_input(
        "MRP ₹", "" if pd.isna(row["regular price"])
        else f"{row['regular price']:.0f}", key=f"cm_{run}_{sku}")
    sale_s = e3.text_input(
        "Sale ₹", "" if pd.isna(row["sale price"])
        else f"{row['sale price']:.0f}", key=f"cs_{run}_{sku}")
    url = st.text_input("Product URL", src, key=f"cu_{run}_{sku}")
    cat_now = str(row.get("categories", "") or "").strip()
    cat_list = [""] + list(cat_opts)
    cat = st.selectbox("Category", cat_list,
                       index=cat_list.index(cat_now) if cat_now in cat_list
                       else 0, key=f"cc_{run}_{sku}")

    pairs = parse_specs_cell(row.get("specs", ""))
    if pairs:
        with st.expander(f"Scraped specs ({len(pairs)})"):
            st.table(pd.DataFrame(pairs, columns=["spec", "value"]))

    def _num(s):
        return pd.to_numeric(str(s).replace(",", "").replace("₹", "")
                             .strip(), errors="coerce")

    def save(confirm):
        part = pd.DataFrame([{
            "sku": sku,
            "confirmed": bool(row["confirmed"]) if confirm is None
            else bool(confirm),
            "title": title.strip(),
            "regular price": _num(mrp_s),
            "sale price": _num(sale_s),
            "url_link": url.strip(),
            "image_url": str(row.get("image_url", "") or ""),
            "categories": cat if cat else cat_now,
            "Brands": str(row.get("Brands", "") or ""),
            "dropped_images": ", ".join(
                u for u in urls if not keeps.get(u, True)),
        }])
        try:
            write_edits_back(P, df, [part])
        except PermissionError:
            st.error("The filled .xlsx is open in Excel — close it and "
                     "retry.")
            return False
        if confirm is not None:
            flags = {s: True for s in confirmed_set(P)}
            flags[sku] = bool(confirm)
            save_confirmed(P, flags)
        return True

    b1, b2, b3, b4 = st.columns(4)
    # a 'failed' row has no verified product page - approving it could pull
    # an unvetted row into a confirmed-scope build
    can_approve = row["_bucket"] != "failed"
    if b1.button("✅ Approve & next", type="primary", disabled=not can_approve,
                 help=None if can_approve else "Paste a product URL and crawl "
                 "it first — this row has no verified page.",
                 key=f"ap_{run}_{sku}"):
        if save(True):
            goto(i + 1)
    if b2.button("💾 Save & next", key=f"sv_{run}_{sku}"):
        if save(None):
            goto(i + 1)
    if b3.button("→ Skip", key=f"sk_{run}_{sku}"):
        goto(i + 1)
    if b4.button("← Back", disabled=i == 0, key=f"bk_{run}_{sku}"):
        goto(i - 1)
```

- [ ] **Step 3: Toggle it from `step_review`**

In `step_review()`, directly after the `fsig = hashlib.md5(...)` line (~line 588) and before the category-options block, insert:

```python
    mode = st.radio("View", ["Table", "Card review"], horizontal=True,
                    key=f"mode_{P['run'].name}",
                    help="Card review walks the rows one SKU at a time with "
                         "the full image gallery. Bulk actions (confirm all, "
                         "crawl, retries) live in the Table view.")
```

Then, AFTER the existing `cat_opts` computation (the two lines building `cat_opts`), insert:

```python
    if mode == "Card review":
        card_review(P, fdf, cat_opts)
        return
```

(Early return: the tab editors, save button, and pass-2/retry buttons stay Table-view-only. Card mode saves per-row, so nothing is lost.)

- [ ] **Step 4: Syntax check + suite**

Run: `.venv\Scripts\python.exe -m py_compile gui.py; .venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -q`
Expected: exit 0; 26 passed

- [ ] **Step 5: Commit**

```powershell
git add gui.py
git commit -m @'
feat: card review mode - one SKU per screen with image keep/drop

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 4: Validation issues deep-link into review

**Files:**

- Modify: `gui.py` — `main()` (step radio, ~line 1017), new `fix_in_review_controls()` above `step_validate` (~line 793), call sites in `step_validate` (issues block, ~line 831) and the build epilogue's inline issues block (~line 784)

**Interfaces:**

- Consumes: `raw_sku_for_csv_sku` (Task 1), `load_filled_df`, the review filter's session key `flt_q_<run>` (Phase 1 Task 3).
- Produces: navigation contract — `st.session_state["jump_step"] = <index into steps>` set before `st.rerun()` switches the wizard to that step; the step radio gains `key="step_radio"`.

- [ ] **Step 1: Give the step radio a key and honor jumps**

In `main()`, replace:

```python
    choice = st.sidebar.radio("Steps", labels)
    idx = labels.index(choice)
```

with:

```python
    # a step can request a jump (e.g. "fix this SKU in review") by setting
    # jump_step before st.rerun(); labels are rebuilt every run, so resolve
    # the index here, never store a label string
    if "jump_step" in st.session_state:
        st.session_state["step_radio"] = labels[
            st.session_state.pop("jump_step")]
    choice = st.sidebar.radio("Steps", labels, key="step_radio")
    idx = labels.index(choice)
```

- [ ] **Step 2: Add the shared control**

Above `step_validate()`:

```python
def fix_in_review_controls(P, idf, key: str):
    """Pick a flagged SKU and jump to Step 2 with the search prefilled.
    The issues CSV holds prefixed SKUs; map back to the raw sheet sku so
    the review filter actually matches."""
    skus = [s for s in dict.fromkeys(idf["sku"].astype(str)) if s.strip()]
    if not skus:
        return
    raw = load_filled_df(P)["sku"].astype(str).tolist()
    c1, c2 = st.columns([2, 1])
    pick = c1.selectbox("Flagged SKU", skus, key=f"fix_pick_{key}")
    if c2.button("🔎 Fix in review", key=f"fix_btn_{key}"):
        st.session_state[f"flt_q_{P['run'].name}"] = (
            raw_sku_for_csv_sku(pick, raw) or pick)
        st.session_state["jump_step"] = 1        # steps[1] = Review
        st.rerun()
```

- [ ] **Step 3: Call it from both issue displays**

3a. In `step_validate()`, directly after the `st.dataframe(pd.read_csv(P["issues"], ...))` line inside the `if issues and P["issues"].exists():` block, insert:

```python
        fix_in_review_controls(
            P, pd.read_csv(P["issues"], dtype=str).fillna(""), "val")
```

3b. In the build epilogue (`step_build`, the `if v_issues:` block added in Phase 1), directly after its `st.dataframe(pd.read_csv(P["issues"], ...))` line, insert:

```python
            fix_in_review_controls(
                P, pd.read_csv(P["issues"], dtype=str).fillna(""), "build")
```

- [ ] **Step 4: Syntax check + suite**

Run: `.venv\Scripts\python.exe -m py_compile gui.py; .venv\Scripts\python.exe -m pytest tests\test_gui_helpers.py -q`
Expected: exit 0; 26 passed

- [ ] **Step 5: Commit**

```powershell
git add gui.py
git commit -m @'
feat: validation issues deep-link to the review step, pre-filtered

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
'@
```

---

### Task 5: Whole-suite regression + headless smoke boot

**Files:** none — verification only.

- [ ] **Step 1: Full test suite**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: all pass, including the pre-existing `test_two_pass.py` and `test_polite_sleep.py`. A failure there is a regression in shared code — stop and fix.

- [ ] **Step 2: Headless smoke boot**

```powershell
$out = Join-Path $env:TEMP "gui_smoke_out.txt"; $err = Join-Path $env:TEMP "gui_smoke_err.txt"
$p = Start-Process ".venv\Scripts\python.exe" -ArgumentList '-m','streamlit','run','gui.py','--server.headless','true','--server.port','8599' -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
Start-Sleep 15
(Invoke-WebRequest "http://localhost:8599" -UseBasicParsing).StatusCode
Stop-Process $p.Id -Force
Select-String -Path $err -Pattern "Traceback" -SimpleMatch
```

Expected: `200`; no Traceback lines. Delete the two temp files afterwards.

- [ ] **Step 3: Manual spot-check (report only)**

If a run with a filled sheet exists under `runs\`: open the app, Step 2 → switch to Card review; confirm the gallery renders, keep/drop boxes persist after "Save & next" (re-open the card and check), Approve is disabled on a 🔴 row, and (if `validation_issues.csv` exists) "Fix in review" jumps to Step 2 with the search prefilled. Report what was and wasn't checkable — never claim a pass for something not exercised.
