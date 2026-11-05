#!/bin/sh
# Opt-in, current execution only. Does not modify any persistent launcher.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${MARKET_PYTHON:-"$HOME/venv/bin/python"}
exec "$PYTHON" "$HERE/guarded_market_kit.py" join --dir "$HERE" --market https://market.ourolabs.ai --name mjforge --tunnel
