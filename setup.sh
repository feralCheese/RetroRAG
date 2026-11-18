#!/usr/bin/env bash
# RetroRAG setup for Linux, macOS and Raspberry Pi OS (64-bit). Run once on each machine:
#   ./setup.sh            full install with the desktop app
#   ./setup.sh --server   headless server only (no GUI) - e.g. a Raspberry Pi serving MCP/REST on your network
set -e
cd "$(dirname "$0")"
SERVER=0
[ "${1:-}" = "--server" ] && SERVER=1
OS="$(uname -s)"

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10 or newer was not found."
  if [ "$OS" = "Darwin" ]; then echo "  Install it with:  brew install python"; else echo "  Install it with:  sudo apt install python3 python3-venv"; fi
  exit 1
fi

# a .venv copied from another machine does not work: rebuild it if broken
if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import pymupdf' 2>/dev/null; then
  echo "Existing .venv is broken or came from another machine - recreating it..."
  rm -rf .venv
fi
if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment with $PY ..."
  "$PY" -m venv .venv || { echo "Could not create the virtual environment. On Debian/Ubuntu/Pi OS: sudo apt install python3-venv"; exit 1; }
fi

echo "Installing packages (a few minutes the first time)..."
.venv/bin/python -m pip install --upgrade pip >/dev/null
if [ "$SERVER" = 1 ]; then
  .venv/bin/python -m pip install -r requirements-server.txt
else
  .venv/bin/python -m pip install -r requirements.txt
fi

echo
if .venv/bin/python -c 'from retrorag.ocr import find_tesseract; import sys; sys.exit(0 if find_tesseract() else 1)'; then
  echo "Tesseract OCR: found"
else
  echo "OCR needs Tesseract. Install it with:"
  if [ "$OS" = "Darwin" ]; then echo "    brew install tesseract"; else echo "    sudo apt install tesseract-ocr      (Fedora: sudo dnf install tesseract)"; fi
fi
if [ "$SERVER" = 0 ] && [ "$OS" = "Linux" ] && command -v ldconfig >/dev/null 2>&1; then
  missing=""
  ldconfig -p | grep -q 'libEGL.so.1' || missing="$missing libegl1"
  ldconfig -p | grep -q 'libxcb-cursor.so.0' || missing="$missing libxcb-cursor0"
  [ -n "$missing" ] && echo "The desktop app (Qt) also needs:  sudo apt install$missing"
fi

if [ -t 0 ]; then
  read -r -p "Portable mode - keep settings and the default library inside this folder? [y/N] " ans
  case "$ans" in [Yy]*) echo "RetroRAG portable mode" > portable.flag ;; *) rm -f portable.flag ;; esac
fi
echo
if [ "$SERVER" = 1 ]; then
  echo "Setup complete. Start the server with:  ./retrorag-cli.sh api --host 0.0.0.0"
else
  echo "Setup complete. Start RetroRAG with:  ./retrorag.sh"
fi
