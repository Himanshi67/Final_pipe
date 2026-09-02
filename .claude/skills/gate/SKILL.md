---
name: gate
description: Run the project's full quality gate in one command - compile every script, run the test suite, and report a single pass/fail summary. Use after any code change, before declaring work done, and before any commit.
---

# Gate — one command, one verdict

Run this after a chunk of work, not after every edit. It is the single source of
"is the pipeline still sound", and it makes NO network calls and touches NO live store.

## The gate

```bash
.venv\Scripts\python.exe -m py_compile app.py build_csv.py client_config.py gui.py \
  gui_helpers.py sku_enricher.py validate_csv.py wc_checkpoint.py wc_publish.py \
  wc_seo.py \
  && .venv\Scripts\python.exe -m pytest tests -q --no-header -p no:cacheprovider 2>&1 | tail -15
```

Report ONLY: the compile result, the final pytest counts line, and each failure as
`file::test — one-line reason`. Never paste the full log.

## Reading the result

- **Compile fails** → stop; nothing else matters until it compiles.
- **Tests fail** → re-run ONLY the failing file, alone, to get a clean signal:
  `.venv\Scripts\python.exe -m pytest tests\<file> -q --no-header 2>&1 | tail -20`
- **Count shrank with no failure line** → a collection error swallowed tests. Check for
  a module-level `sys.exit`/`raise` at import time; that once made the whole suite
  un-runnable (see `docs/NEXT_STEPS.md` P0 — local only, not in the clone).
- **Everything green** → say so with the counts as evidence. Do not claim green
  without the summary line in front of you.

## Known slow spots (not failures)

Three tests in `tests\test_bugfixes.py` spend ~76s on DNS timeouts because they point
`WC_URL` at the reserved non-resolving host `store.invalid` **on purpose** — that is how
they prove no live store is touched. Do not "fix" them by pointing them somewhere real.

## What this gate does NOT cover

- The operator QA scripts `tests\qa_cli.py` / `qa_deep.py` / `qa_pipeline.py`: they need
  a live network, Serper credits and a gitignored input sheet. Run them deliberately,
  never as part of the gate.
- Anything touching the live store. `wc_publish.py --check`, `--commit`, and `wc_seo.py
  --commit` are operator actions, never gate steps.
- A real enrichment batch. Use the `run-batch` skill for that.
