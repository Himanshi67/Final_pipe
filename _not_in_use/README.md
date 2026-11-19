# _not_in_use

Files that no script, test, skill or current doc uses. Nothing here was deleted.
Each file keeps its original path below this folder, so a restore is one command.
Run the restore commands from the repo root in PowerShell.

Two files are local only. `.gitignore` ignores them at this path too, because they
hold private business content and the origin remote is public.

| Original path | Moved on | Why it is not in use | What would make it useful again | How to restore |
|---|---|---|---|---|
| `docs\archive\scraper2-uncommitted.patch` | 2026-11-19 | Uncommitted diff from the old `scraper (2)\` app. The present `app.py` already has each useful part: month-based media base (`build_media_base`), WebP save, `image_url` column, encoding fallback for uploads, active-row filter, whole-rupee regular price, AC tonnage read from the title. The rest guessed values (`Standard Cooling Capacity`, `Non-Inverter AC` as a default, keyword category tree), which the project rejects (KI-14). | Only as history of the old app. Nothing in it is worth porting. | `Move-Item _not_in_use\docs\archive\scraper2-uncommitted.patch docs\archive\` |
| `docs\handover-audit-productization.md` (local only, git-ignored) | 2026-11-19 | Session handover of 2026-09-01. Its open items (P1 list, unproven findings, critic pass) are closed. `docs\NEXT_STEPS.md` records the closure. Its decisions are in `docs\PRODUCT_PLAN.md` and `CLAUDE.md`. | Only to read the reasons of that session again. | `Move-Item _not_in_use\docs\handover-audit-productization.md docs\` |
| `docs\handover-phase0-closeout.md` (local only, git-ignored) | 2026-11-19 | Session handover of 2026-09-02. Its two owed checks (gui re-verify, completeness-critic pass) ran later (`docs\NEXT_STEPS.md`, status block of 2026-09-02). Its pending owner decisions are in the "Open from this pass" note of `docs\NEXT_STEPS.md`. | Only to read the reasons of that session again. | `Move-Item _not_in_use\docs\handover-phase0-closeout.md docs\` |
| `docs\superpowers\plans\2026-08-22-ui-quick-wins.md` | 2026-11-19 | Step-by-step build plan for GUI refit phase 1. The phase shipped. The spec `docs\superpowers\specs\2026-08-22-ui-refit.md` stays in place and points here. | Only to see how phase 1 was built. | `Move-Item _not_in_use\docs\superpowers\plans\2026-08-22-ui-quick-wins.md docs\superpowers\plans\` |
| `docs\superpowers\plans\2026-08-22-ui-review-cards.md` | 2026-11-19 | Build plan for GUI refit phase 2. The phase shipped. | Only to see how phase 2 was built. | `Move-Item _not_in_use\docs\superpowers\plans\2026-08-22-ui-review-cards.md docs\superpowers\plans\` |
| `docs\superpowers\plans\2026-08-22-ui-telemetry-publish.md` | 2026-11-19 | Build plan for GUI refit phase 3. The phase shipped. | Only to see how phase 3 was built. | `Move-Item _not_in_use\docs\superpowers\plans\2026-08-22-ui-telemetry-publish.md docs\superpowers\plans\` |
| `docs\superpowers\plans\2026-08-22-ui-run-manager.md` | 2026-11-19 | Build plan for GUI refit phase 4 (run manager, category-map editor, SEO preview). `gui.py` has all three. | Only to see how phase 4 was built. | `Move-Item _not_in_use\docs\superpowers\plans\2026-08-22-ui-run-manager.md docs\superpowers\plans\` |

If you restore a plan, also change its link in the spec back to `docs/superpowers/plans/`.
If you restore a handover, the old ignore lines in `.gitignore` still cover it.
