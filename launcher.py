#!/usr/bin/env python3
"""
Dunamis v3 — cross-platform one-click launcher.

Double-click the launcher for your OS (Dunamis.bat / Dunamis.command /
Dunamis-Linux.sh) — it calls this. On first run it:
  1. creates a private virtual environment (.venv),
  2. installs dependencies + the login browser,
  3. opens a window to sign in to your Google account (one time),
then starts the server and opens the web chat in your browser. Subsequent
runs skip straight to starting the server.

Requires Python 3.11+ already installed and on PATH.
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

MIN_PY = (3, 11)  # gemini_webapi needs StrEnum (Python 3.11+)

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQ = ROOT / "requirements.txt"
PORT = int(os.environ.get("DUNAMIS_PORT", "6970"))
COOKIES = Path(os.path.expanduser("~")) / ".dunamis" / "gemini_cookies.json"


def _py_version(exe) -> tuple:
    try:
        out = subprocess.check_output(
            [str(exe), "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
            stderr=subprocess.DEVNULL, text=True).strip()
        return tuple(int(x) for x in out.split("."))
    except Exception:
        return (0, 0)


def _find_newer_python():
    """Locate a Python >= MIN_PY interpreter, returning its executable path."""
    tried = []
    if os.name == "nt":
        for v in ("3.13", "3.12", "3.11"):
            tried.append(["py", "-" + v])
    for name in ("python3.13", "python3.12", "python3.11"):
        tried.append([name])
    for cmd in tried:
        try:
            exe = subprocess.check_output(
                cmd + ["-c", "import sys;print(sys.executable)"],
                stderr=subprocess.DEVNULL, text=True).strip()
            if exe and _py_version(exe) >= MIN_PY:
                return exe
        except Exception:
            continue
    return None


def ensure_python_version():
    """We need Python 3.11+. If we're older, re-exec with a newer one if we can find it."""
    if sys.version_info[:2] >= MIN_PY:
        return
    newer = _find_newer_python()
    if newer and os.path.abspath(newer) != os.path.abspath(sys.executable):
        print(f"Python {sys.version_info.major}.{sys.version_info.minor} is too old; "
              f"re-launching with {newer} (need 3.11+)...")
        os.execv(newer, [newer, os.path.abspath(__file__)])
    sys.exit("Python 3.11+ is required.\n"
             "  Windows/macOS: install from https://www.python.org/downloads/\n"
             "  Ubuntu 22.04:  sudo apt install python3.12 python3.12-venv  (or python3.11)")


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd))
    return subprocess.call([str(c) for c in cmd], **kw)


def ensure_venv():
    vpy = venv_python()
    if vpy.exists() and _py_version(vpy) >= MIN_PY:
        return
    if VENV.exists():
        print("Recreating virtual environment (needs Python 3.11+)...")
        shutil.rmtree(VENV, ignore_errors=True)
    print("[1/3] Creating virtual environment (.venv)...")
    if run([sys.executable, "-m", "venv", str(VENV)]) != 0:
        sys.exit("Could not create a virtual environment. Is Python 3.11+ installed?")


def ensure_deps():
    if not REQ.is_file():
        sys.exit(f"requirements.txt is missing next to launcher.py ({REQ}). "
                 "Restore it (re-download / re-clone the repo) and retry.")
    py = str(venv_python())
    marker = VENV / ".deps_ok"
    if not (marker.exists() and marker.stat().st_mtime >= REQ.stat().st_mtime):
        print("[2/3] Installing dependencies (first run can take a few minutes)...")
        run([py, "-m", "pip", "install", "--upgrade", "pip"])
        if run([py, "-m", "pip", "install", "-r", str(REQ)]) != 0:
            sys.exit("Dependency install failed. Check your internet connection and retry.")
        marker.write_text("ok")
    # Browser for the one-time login. Own marker so a failed install is retried
    # on the next launch instead of being permanently skipped.
    pw_marker = VENV / ".pw_ok"
    if not pw_marker.exists():
        if run([py, "-m", "playwright", "install", "chromium"]) == 0:
            pw_marker.write_text("ok")
        else:
            print("  (note: 'playwright install chromium' failed — the login window may not "
                  "open. It'll retry next launch; existing cookies still work.)")


def ensure_login():
    if COOKIES.is_file():
        return
    print("[3/3] First run — opening a window to sign in to your Google account...")
    rc = run([str(venv_python()), "-m", "dunamis.login"], cwd=str(ROOT))
    if rc != 0 or not COOKIES.is_file():
        print("\nLogin wasn't completed - no cookies were saved. The server will still")
        print("start; just click 'Log in' in the web chat when it opens.\n")


def open_browser_later(url, delay=3.5):
    def _open():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


def main():
    os.chdir(ROOT)
    ensure_python_version()
    ensure_venv()
    ensure_deps()
    ensure_login()
    url = f"http://localhost:{PORT}/"
    print(f"\nStarting Dunamis v3 — the chat will open at {url}\n"
          f"(Keep this window open. Close it to stop the server.)\n")
    open_browser_later(url)
    try:
        run([str(venv_python()), "-m", "dunamis.server", "--port", str(PORT)], cwd=str(ROOT))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
