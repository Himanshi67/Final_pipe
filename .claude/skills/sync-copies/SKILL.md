---
name: sync-copies
description: Sync sku_enricher.py (and shared docs) from root to Sku_enrichment/ and _push_export/, hash-verify, and commit both repos. Use after any enricher edit or when the user says "sync the copies" or "push the export".
---

# Sync the duplicate copies

Root files are the source of truth. `_push_export\` is an UNTRACKED NESTED git repo
(own remote: SKU_Scripts.git). The outer repo ALSO has a public GitHub remote
(origin: Final_pipe.git) - anything committed here can become public, so re-read
docs/NEXT_STEPS.md's security item before adding docs. Confirm with `git remote -v` before assuming a push target.

`_push_export\` keeps a FLAT layout: docs that live in `docs\` at root land at the
ROOT of `_push_export\`, not in a `docs\` subfolder there. Do not restructure it.

## Copy map
| Source (root) | Targets |
|---|---|
| `sku_enricher.py` | `Sku_enrichment\`, `_push_export\`, `_push_export\Sku_enrichment\` |
| `README.md`, `requirements.txt`, `app.py`, `validate_csv.py`, `build_csv.py`, `wc_publish.py` | `_push_export\` (only those that exist there — check first) |
| `docs\PIPELINE_GUIDE.md`, `docs\claude_prompt.md`, `docs\data_flow.svg`, `docs\HANDOFF.md`, `docs\AUTOMATION_OPPORTUNITIES.md` | `_push_export\` **root** (flatten — drop the `docs\` prefix) |
| `category_map.xlsx`, `SubCategory_Attributes_Master.xlsx` | `_push_export\` — app.py is unrunnable without the taxonomy. Both repos un-ignore these two despite ignoring `*.xlsx`. The Master workbook is a REFERENCE COPY no script reads; the enforced attributes are the `Sub-Category Attributes` sheet inside `category_map.xlsx` (`app.load_subcategory_attributes`). |
| `Sku_enrichment\README.md`, `Sku_enrichment\requirements.txt` | `_push_export\Sku_enrichment\` |

**INTENTIONALLY DIVERGENT — never overwrite from root:**
- `Sku_enrichment\README.md` (enricher-only manual, not the root quickstart)
- `Sku_enrichment\requirements.txt` (pinned, enricher-only subset)

## Procedure (PowerShell)
1. `Copy-Item` per the map (skip targets that don't exist in `_push_export\`).
2. Verify every source/target pair with `git hash-object <a> <b>` — hashes must match.
3. Outer repo: `git add` the changed TRACKED files and commit.
   **NEVER `git add _push_export`** from the outer repo.
4. Nested repo: `git -C _push_export add -A` and commit there.
5. Push only when the user explicitly asks (two remotes; confirm which).
6. Report: pairs verified (hash-equal), commits made in each repo, push status.
