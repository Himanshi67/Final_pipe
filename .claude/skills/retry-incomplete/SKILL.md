---
name: retry-incomplete
description: Recover rows that have a url_link but missing price/images in an existing _filled.xlsx. Use when the user says rows are "half-filled", "no MRP", or "missing images".
---

# Recover half-filled rows

Context: historically, rows with a `url_link` were skipped forever on resume (KI-2) —
`--retry-missing` never reached them. Since `--retry-incomplete` was added, the direct
path is:

    .venv\Scripts\python.exe sku_enricher.py "<original input>.xlsx" --retry-incomplete

This re-attempts only rows whose status is `FOUND … - no price shown` /
`- only N image(s)` or whose sale-price / image_url cell is empty. Existing good cells
are preserved (the writer only overwrites fields the new lookup returned). Note the
input is the ORIGINAL list (the script resumes into `<stem>_filled.xlsx` itself) —
never pass the `_filled` file as input.

## Old builds without the flag
(e.g. copies in `Sku_enrichment\` before a sync): work on a COPY of the `_filled.xlsx`,
clear `url_link`+`status` on incomplete rows, then run `--retry-missing`. Write this
to the scratchpad as `reset_incomplete.py` and run it with `.venv\Scripts\python.exe`:

    import openpyxl, sys
    src = sys.argv[1]                      # path to the COPY, never the original
    wb = openpyxl.load_workbook(src); ws = wb.active
    hdr = {str(c.value).strip().lower(): c.column for c in ws[1] if c.value}
    u, s = hdr["url_link"], hdr["status"]
    p, i = hdr["sale price"], hdr["image_url"]
    n = 0
    for r in range(2, ws.max_row + 1):
        st = str(ws.cell(r, s).value or "")
        half = (st.startswith("FOUND") and " - " in st) or (
            ws.cell(r, u).value and (not ws.cell(r, p).value or not ws.cell(r, i).value))
        if ws.cell(r, u).value and half:
            ws.cell(r, u).value = None; ws.cell(r, s).value = None; n += 1
    wb.save(src); print(n, "rows reset")

## Cautions
- Never run the reset on the only copy of a batch result.
- KI-4: a `<stem>_filled_recovery.xlsx` may hold NEWER data than the `_filled.xlsx` —
  compare timestamps before choosing which file to recover.
- Report filled-counts (price / images) before vs after so the user sees the gain.
