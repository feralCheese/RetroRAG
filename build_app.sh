#!/usr/bin/env bash
# Build a standalone copy in dist/RetroRAG for this OS/CPU (Linux x86-64, Linux ARM64 / Raspberry Pi, macOS).
# Build on the kind of machine you want to run it on - PyInstaller does not cross-compile.
set -e
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || ./setup.sh
.venv/bin/python -m pip install "pyinstaller>=6.0"
.venv/bin/python -m PyInstaller --noconfirm --clean retrorag.spec
echo "RetroRAG portable mode" > dist/RetroRAG/portable.flag
echo "Adding the built-in embedding model so the build works offline..."
PYTHONPATH="$(pwd)" .venv/bin/python -m retrorag download-model --to dist/RetroRAG/models/nomic-embed-text-v1.5
echo "Built: $(pwd)/dist/RetroRAG  - run dist/RetroRAG/RetroRAG (app) or dist/RetroRAG/retrorag-cli (CLI / MCP)"
