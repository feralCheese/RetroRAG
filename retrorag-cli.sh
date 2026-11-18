#!/usr/bin/env bash
# Command-line access, e.g.:  ./retrorag-cli.sh search "SID filter" --system C64
#                             ./retrorag-cli.sh api --host 0.0.0.0     (REST + MCP HTTP server, no GUI)
DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}" exec "$DIR/.venv/bin/python" -m retrorag "$@"
