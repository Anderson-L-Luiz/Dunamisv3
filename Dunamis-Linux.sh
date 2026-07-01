#!/bin/bash
# Ubuntu/Linux launcher. Make it runnable once:  chmod +x Dunamis-Linux.sh
# Then double-click (choose "Run") or run:  ./Dunamis-Linux.sh
cd "$(dirname "$0")" || exit 1
PY=""
command -v python3 >/dev/null 2>&1 && PY=python3
[ -z "$PY" ] && command -v python >/dev/null 2>&1 && PY=python
if [ -z "$PY" ]; then
  echo "Python 3.11+ is required. Install it with:  sudo apt install python3 python3-venv"
  read -n 1 -s -r -p "Press any key to close..."
  exit 1
fi
exec "$PY" launcher.py
