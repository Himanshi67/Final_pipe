---
name: fix-issue
description: Pick a bug from KNOWN_ISSUES.md and fix + verify it. Use when the user says "fix the next issue", "fix KI-3", or reports a symptom matching a known issue.
---

# Fix a known issue

1. Read `KNOWN_ISSUES.md` ONLY. Pick the entry the user named, else the
   highest-severity `open` one. Confirm the choice with the user if ambiguous.
2. Read ONLY the cited line ranges ±20 lines of the target file — never the whole
   script (`sku_enricher.py` is ~1,180 lines; the anchors exist so you don't pay
   for it).
3. Implement the minimal fix per the **Fix sketch**. No drive-by refactors; match the
   file's style (plain functions, no classes, `log()` for output, comments only for
   non-obvious constraints).
4. Dispatch the **verifier** agent with: the issue's **What** + **Accept** lines and
   the changed file. Acceptance runs happen on SCRATCH COPIES of spreadsheets, never
   live batch files.
5. On PASS:
   - Update the issue's Status to `fixed (YYYY-MM-DD)`.
   - If user-facing (new flag / changed behavior), add one line to `README.md` and
     `PIPELINE_GUIDE.md` §3.
   - Run **/sync-copies** — the enricher exists in 4 copies.
6. Commit only when the user asks.
