r"""The GUI's Stop button must checkpoint, not kill the run mid-row.

gui.cancel_enricher() stops the enricher with
os.kill(pid, signal.CTRL_BREAK_EVENT) - CTRL_BREAK is the only console signal
Windows can aim at ONE process group, which is why the child is started with
CREATE_NEW_PROCESS_GROUP. But the CRT default for SIGBREAK is to TERMINATE, so
without sku_enricher.install_break_handler() the "progress is saved" promise on
that button is a lie. This spawns a real child in its own process group, breaks
it, and proves the signal arrives as KeyboardInterrupt.

    .venv\Scripts\python.exe tests\test_break_handler.py

Never send CTRL_BREAK_EVENT to pid 0 / your own pid: it goes to every process
sharing the console, including the test runner.
"""
import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
UNDER_PYTEST = "pytest" in sys.modules

PASS = FAIL = SKIPPED = 0

# Child: install the handler, say READY, then sit in a sleep loop the way a
# real run sits in polite_sleep() / a request, and report the interrupt.
CHILD_SRC = (
    "import sys, time\n"
    "sys.path.insert(0, %r)\n"
    "import sku_enricher\n"
    "sku_enricher.install_break_handler()\n"
    "print('READY', flush=True)\n"
    "try:\n"
    "    for _ in range(300):\n"
    "        time.sleep(0.1)\n"
    "except KeyboardInterrupt:\n"
    "    print('CAUGHT', flush=True)\n"
    "    sys.exit(0)\n"
    "print('NOT-INTERRUPTED', flush=True)\n"
    "sys.exit(3)\n"
) % str(ROOT)


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        if UNDER_PYTEST:
            raise AssertionError(f"{label}: got {got!r}, want {want!r}")


def skip(reason):
    global SKIPPED
    SKIPPED += 1
    print(f"  SKIP {reason}")
    if UNDER_PYTEST:
        import pytest
        pytest.skip(reason)


def _signal_skip_reason():
    if sys.platform != "win32":
        return "windows-only: SIGBREAK / CTRL_BREAK_EVENT do not exist here"
    if not VENV_PY.exists():
        return f"no interpreter at {VENV_PY}"
    try:
        # NOT GetConsoleWindow(): under ConPTY (Windows Terminal, VS Code, and
        # the agent shells) there is no console WINDOW but a console IS
        # attached, so that probe skipped this test on exactly the machines it
        # has to run on. GetConsoleCP() returns 0 only with no console at all.
        k32 = ctypes.windll.kernel32
        if k32.GetConsoleCP() == 0 and k32.GetConsoleWindow() == 0:
            return "needs an attached console to deliver CTRL_BREAK"
    except Exception as exc:                      # pragma: no cover
        return f"cannot query the console: {exc}"
    return None


def _reader(stream, lines):
    for line in stream:
        lines.append(line.rstrip("\r\n"))


def _wait_for(lines, token, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(token in ln for ln in lines):
            return True
        time.sleep(0.1)
    return False


def test_break_becomes_keyboard_interrupt():
    """A CTRL_BREAK aimed at the enricher's own group raises KeyboardInterrupt
    in it - the same path as Ctrl+C, which checkpoints and saves progress."""
    print("CTRL_BREAK reaches the enricher as KeyboardInterrupt")
    reason = _signal_skip_reason()
    if reason:
        skip(reason)
        return

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    child = subprocess.Popen(
        [str(VENV_PY), "-c", CHILD_SRC],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    lines = []
    threading.Thread(target=_reader, args=(child.stdout, lines),
                     daemon=True).start()
    try:
        # importing sku_enricher is slow (pandas, curl_cffi) - be generous
        if not _wait_for(lines, "READY", 30):
            check("child reached READY", "\n".join(lines) or "<no output>",
                  "READY")
            return
        os.kill(child.pid, signal.CTRL_BREAK_EVENT)
        caught = _wait_for(lines, "CAUGHT", 10)
        try:
            rc = child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            rc = None
        check("the sleep loop saw KeyboardInterrupt", caught, True)
        check("and the child exited cleanly", rc, 0)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_handler_is_installed_by_main():
    """Installed for real runs, not just importable: main() must call it
    before argument parsing, so a break during the very first row lands on
    the checkpoint path too."""
    print("main() installs the handler")
    import sku_enricher as E                                   # noqa: E402
    check("install_break_handler exists",
          callable(getattr(E, "install_break_handler", None)), True)
    src = (ROOT / "sku_enricher.py").read_text(encoding="utf-8",
                                               errors="replace")
    body = src.split("\ndef main():", 1)[-1].split("\n    ap = argparse", 1)[0]
    check("main() calls it before parsing args",
          "install_break_handler()" in body, True)


def main():
    for test in (test_handler_is_installed_by_main,
                 test_break_becomes_keyboard_interrupt):
        test()
    print(f"\n{PASS} passed, {FAIL} failed, {SKIPPED} skipped")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
