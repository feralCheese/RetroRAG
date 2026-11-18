#!/usr/bin/env bash
# Start the RetroRAG desktop app (Linux / macOS / Raspberry Pi OS desktop).
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || ./setup.sh || exit 1
exec .venv/bin/python -m retrorag "$@"
