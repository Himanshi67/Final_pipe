"""Unit tests for the POSIX process helpers in gui_helpers.py (the Linux /
Render container path of gui.py). Pure functions, synthetic input only - they
run on Windows too, so the container behaviour is pinned without a Linux box."""
import sys
from pathlib import Path

from gui_helpers import (posix_cmdline_is_enricher, posix_stat_state,
                         venv_python)


# ---------------------------------------------------------- posix_stat_state

def test_stat_state_running_and_sleeping():
    assert posix_stat_state("1234 (python) R 1 1234 1234 0 -1") == "R"
    assert posix_stat_state("1234 (python3.14) S 1 1234 1234 0 -1\n") == "S"


def test_stat_state_zombie_and_dead():
    assert posix_stat_state("77 (python) Z 1 77 77 0 -1") == "Z"
    assert posix_stat_state("77 (python) X 1 77 77 0 -1") == "X"


def test_stat_state_comm_with_spaces_and_parens():
    # comm may hold spaces and ')' - the state follows the LAST ')'
    assert posix_stat_state("42 (my (odd) proc) S 1 42 42 0") == "S"
    assert posix_stat_state("42 (a) Z (b)) R 1 42 42 0") == "R"
    assert posix_stat_state("42 (x y z) Z 1 42") == "Z"


def test_stat_state_malformed():
    assert posix_stat_state("") is None
    assert posix_stat_state(None) is None
    assert posix_stat_state("garbage without parens") is None
    assert posix_stat_state("42 (python)") is None
    assert posix_stat_state("42 (python)   ") is None
    assert posix_stat_state("42 (python) 17 1 42") is None


# ------------------------------------------------- posix_cmdline_is_enricher

def test_cmdline_matches_the_enricher():
    argv = b"/usr/local/bin/python\0sku_enricher.py\0runs/x/input.xlsx\0" \
           b"--client\0shreejee\0"
    assert posix_cmdline_is_enricher(argv) is True
    assert posix_cmdline_is_enricher(
        b"python\0/app/sku_enricher.py\0in.xlsx\0") is True
    # a Windows-style path still names the script
    assert posix_cmdline_is_enricher("python\0C:\\x\\sku_enricher.py\0") is True


def test_cmdline_rejects_other_processes():
    assert posix_cmdline_is_enricher(
        b"python\0-m\0streamlit\0run\0gui.py\0") is False
    assert posix_cmdline_is_enricher(b"python\0build_csv.py\0a.xlsx\0") is False
    # the name inside another argument is not the script being run
    assert posix_cmdline_is_enricher(b"python\0my_sku_enricher.py\0") is False
    assert posix_cmdline_is_enricher(b"python\0sku_enricher.py.bak\0") is False
    assert posix_cmdline_is_enricher(b"") is False
    assert posix_cmdline_is_enricher(None) is False


# --------------------------------------------------------------- venv_python

def test_venv_python_falls_back_when_no_venv(tmp_path):
    # tmp_path has no .venv: the fallback (default sys.executable) is used
    assert venv_python(tmp_path) == Path(sys.executable)
    assert venv_python(tmp_path, fallback="/usr/local/bin/python") == \
        Path("/usr/local/bin/python")


def test_venv_python_prefers_the_venv_file(tmp_path):
    exe = tmp_path / ".venv" / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert venv_python(tmp_path) == exe
    assert venv_python(tmp_path, fallback="/x/python") == exe
