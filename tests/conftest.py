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
# Tier 0 (url_index.py) refreshes retailer sitemaps over the network at the
# start of every enricher run. Suites that drive sku_enricher.main() stub the
# page fetcher and serper only, so keep tier 0 off for them; tests# test_url_index.py turns it back on per test with fixtures and a temp DB.
os.environ.setdefault("PIPELINE_URL_INDEX", "off")
# Card review (gui.py) downloads every kept photo to preview the final 1500 x
# 1500 canvas. The seeded test rows carry example.com image URLs, so the
# toggle defaults to OFF under pytest; tests\test_review_image_preview.py
# turns it on per test with app.fetch_image_bytes stubbed.
os.environ.setdefault("PIPELINE_REVIEW_IMAGE_PREVIEW", "off")

collect_ignore = ["qa_pipeline.py", "qa_deep.py", "qa_cli.py"]


@pytest.fixture
def tmp(tmp_path):
    """Alias for pytest's tmp_path, under the name test_gui_ui.py asks for."""
    return tmp_path
