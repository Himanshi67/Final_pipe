r"""build_csv.py --pack - the headless half of PRODUCT_PLAN item 6.

The GUI's Archive button and `build_csv.py --pack` must produce the SAME
deliverable, because they share gui_helpers.assemble_pack (the copy routine)
and gui_helpers.render_batch_readme (the text). tests\test_gui_runmgr.py
covers the GUI half; this covers the CLI half, end to end, through a real
subprocess.

Offline by construction: every image_url is empty, so app.process_inventory
downloads nothing and no network call is possible (the same trick
tests\test_bugfixes.py::test_17_console_encoding uses).

    .venv\Scripts\python.exe tests\test_pack.py

Standalone script like the rest of tests\, and collected by pytest too.
"""

import os
import subprocess
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = ROOT / ".venv" / "Scripts" / "python.exe"

HEADER = ["sku", "title", "categories", "Brands", "regular price",
          "sale price", "url_link", "image_url", "status"]

# one row per build outcome: only the FOUND row is built, but ALL THREE are
# counted in the pack's Coverage table - the client paid for the whole list.
ROWS = [
    ["PACK-A", "Haier 7 kg Washing Machine", "Home Appliances > Washing "
     "Machines", "Haier", "24990", "18990", "http://x", "",
     "FOUND (amazon.in)"],
    ["PACK-B", "Haier 8 kg Washing Machine", "Home Appliances > Washing "
     "Machines", "Haier", "27990", "21990", "", "",
     "NOT FOUND - 3 page(s) checked, none matched"],
    ["PACK-C", "Haier 9 kg Washing Machine", "Home Appliances > Washing "
     "Machines", "Haier", "31990", "25990", "", "",
     "CLOSE MATCH - CHECK (croma.com)"],
]


def seed_sheet(folder: Path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADER)
    for row in ROWS:
        ws.append(row)
    path = folder / "LED_filled.xlsx"
    wb.save(path)
    return path


def run_pack(folder: Path, *extra):
    env = dict(os.environ, PYTHONIOENCODING="utf-8",
               STREAMLIT_SERVER_HEADLESS="true")
    return subprocess.run(
        [str(PY), str(ROOT / "build_csv.py"), "LED_filled.xlsx",
         "-o", "out.csv", "--pack", *extra],
        capture_output=True, text=True, cwd=str(folder), env=env, timeout=600)


def test_pack_writes_a_folder_a_readme_and_a_zip(tmp_path):
    """The whole point of item 6: after one command there is ONE file to
    hand to the client, and it explains itself."""
    seed_sheet(tmp_path)
    p = run_pack(tmp_path)
    assert p.returncode == 0, p.stdout + p.stderr

    pack = tmp_path / "out_pack"
    assert pack.is_dir(), sorted(x.name for x in tmp_path.iterdir())
    assert (pack / "out.csv").exists()               # the import file
    assert (pack / "LED_filled.xlsx").exists()       # the list it came from
    assert (pack / "README.md").exists()
    assert "Pack: " in p.stdout and "Zip:  " in p.stdout

    zip_path = tmp_path / "out_pack.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert {"out.csv", "README.md", "LED_filled.xlsx"} <= names
        assert "## Coverage" in zf.read("README.md").decode("utf-8")


def test_pack_readme_covers_the_whole_list_not_just_the_built_rows(tmp_path):
    """--pack defaults to the FOUND gate, so ONE row is built - but a
    coverage report that hid the two rows the client is not getting would be
    worse than useless."""
    seed_sheet(tmp_path)
    p = run_pack(tmp_path)
    assert p.returncode == 0, p.stdout + p.stderr
    text = (tmp_path / "out_pack" / "README.md").read_text(encoding="utf-8")

    assert text.startswith("# Batch out\n\n")
    assert "build_csv.py --pack" in text             # no run folder to name
    assert "- Source list: LED_filled.xlsx\n" in text
    assert "- Rows enriched: 3\n" in text
    assert "- Built scope: FOUND rows only (1 SKUs)\n" in text
    assert "- Images: 0 file(s) copied\n" in text    # image_url was empty
    for section in ("## Coverage", "## Review", "## Files"):
        assert f"\n{section}\n" in text, section
    assert "| **Rows** | **3** |" in text
    assert "| FOUND (clean) | 1 |" in text
    assert "| CLOSE MATCH - CHECK | 1 |" in text
    assert "↳ N page(s) checked, none matched | 1 |" in text
    # nothing wrote review_metrics.json on the headless route
    assert "no review metrics recorded" in text
    assert "- `out.csv` - the WooCommerce import file" in text


def test_pack_records_the_validation_count_it_just_measured(tmp_path):
    """build_csv knows the issue count from the pass it ran; there is no
    run_meta.json on this route to read it out of."""
    seed_sheet(tmp_path)
    p = run_pack(tmp_path)
    assert p.returncode == 0, p.stdout + p.stderr
    text = (tmp_path / "out_pack" / "README.md").read_text(encoding="utf-8")
    line = next(x for x in text.splitlines()
                if x.startswith("- Validation issues: "))
    count = line.split(": ", 1)[1]
    assert count.isdigit(), line          # a real number, never '?' / 'not run'
    if int(count):                        # the QA pass writes it next to out.csv
        assert (tmp_path / "out_pack" / "validation_issues.csv").exists()


def test_build_csv_advertises_pack_and_still_imports_gui_helpers_purely():
    """gui_helpers must stay streamlit-free: build_csv.py imports it, and a
    streamlit import at module level would boot a server in a CLI run."""
    src = (ROOT / "gui_helpers.py").read_text(encoding="utf-8")
    assert "import streamlit" not in src
    p = subprocess.run([str(PY), str(ROOT / "build_csv.py"), "--help"],
                       capture_output=True, text=True,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                       timeout=300)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "--pack" in p.stdout


# ------------------------------------------------------------------ main --

def main():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    passed = failed = 0
    for name, fn in tests:
        try:
            if fn.__code__.co_argcount:
                with tempfile.TemporaryDirectory(prefix="pack_") as td:
                    fn(Path(td))
            else:
                fn()
            print(f"PASS {name}")
            passed += 1
        except Exception:
            print(f"FAIL {name}")
            traceback.print_exc(limit=4)
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
