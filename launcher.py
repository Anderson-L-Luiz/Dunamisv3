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

Requires Python 3.10+ already installed and on PATH.
"""
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQ = ROOT / "requirements.txt"
PORT = int(os.environ.get("DUNAMIS_PORT", "6970"))
COOKIES = Path(os.path.expanduser("~")) / ".dunamis" / "gemini_cookies.json"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd))
    return subprocess.call([str(c) for c in cmd], **kw)


def ensure_venv():
    if venv_python().exists():
        return
    print("[1/3] Creating virtual environment (.venv)...")
    if run([sys.executable, "-m", "venv", str(VENV)]) != 0:
        sys.exit("Could not create a virtual environment. Is Python 3.10+ installed?")


def ensure_deps():
    marker = VENV / ".deps_ok"
    if marker.exists() and marker.stat().st_mtime >= REQ.stat().st_mtime:
        return
    print("[2/3] Installing dependencies (first run can take a few minutes)...")
    py = str(venv_python())
    run([py, "-m", "pip", "install", "--upgrade", "pip"])
    if run([py, "-m", "pip", "install", "-r", str(REQ)]) != 0:
        sys.exit("Dependency install failed. Check your internet connection and retry.")
    # Browser used only for the one-time Google login.
    if run([py, "-m", "playwright", "install", "chromium"]) != 0:
        print("  (note: 'playwright install chromium' failed — login window may not open; "
              "the rest still works if you already have cookies.)")
    marker.write_text("ok")


def ensure_login():
    if COOKIES.is_file():
        return
    print("[3/3] First run — opening a window to sign in to your Google account...")
    run([str(venv_python()), "-m", "dunamis.login"], cwd=str(ROOT))


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
