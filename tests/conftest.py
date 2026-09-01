r"""pytest wiring for tests\ - everything the standalone scripts do for themselves.

Every suite here is ALSO a standalone script
(`.venv\Scripts\python.exe tests\test_x.py`, the convention
docs\PRODUCTION_READINESS.md documents), so nothing in this file may be
required for that mode; it only supplies what the pytest entry point needs:

  * the repo root on sys.path, so `import app` / `import gui_helpers` resolve
    under `pytest.exe` as well as `python -m pytest` from the repo root
    (the scripts do their own sys.path.insert; this is for the ones that
    do not, e.g. test_gui_helpers.py).
  * a `tmp` fixture - tests\test_gui_ui.py's own script runner hands its
    tests a scratch Path called `tmp`; this makes the same signature work
    under pytest instead of erroring with "fixture 'tmp' not found".
  * collect_ignore for the qa_* operator scripts. They are QA runbooks, not
    unit tests: they need a live network, a hardcoded absolute repo path and
    the gitignored inputs\LED.xlsx. Their names are referenced in docs, so
    they keep them; pytest simply never collects them. (Default `python_files`
    would not match `qa_*.py` either - this makes the exclusion explicit and
    survives someone widening that setting.)
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# the streamlit-importing suites set this too; harmless when already set
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

collect_ignore = ["qa_pipeline.py", "qa_deep.py", "qa_cli.py"]


@pytest.fixture
def tmp(tmp_path):
    """Alias for pytest's tmp_path, under the name test_gui_ui.py asks for."""
    return tmp_path
