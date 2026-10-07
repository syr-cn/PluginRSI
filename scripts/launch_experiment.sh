#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PLUGINRSI_PYTHON:-$ROOT/.venv/bin/python3}"
LOG_DIR="${PLUGINRSI_LAUNCH_LOG_DIR:-$ROOT/logs/launcher}"
mkdir -p "$LOG_DIR"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
"$PYTHON_BIN" -u "$ROOT/scripts/experiment.py" "$@" 2>&1 \
  | tee -a "$LOG_DIR/$(date +%Y%m%d_%H%M%S)-$$.log"
