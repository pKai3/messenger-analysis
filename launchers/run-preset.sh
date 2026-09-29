#!/bin/sh
# Shared runner; use the named .command launchers for normal use.
set -eu
if [ "$#" -lt 3 ]; then
    printf '%s\n' 'Usage: run-preset.sh NAME PRIMARY_WINDOW COMPARISON_WINDOW [plotter options...]' >&2
    exit 2
fi
preset=$1
window_primary=$2
window_comparison=$3
shift 3
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if [ -n "${PYTHON_BIN:-}" ]; then
    python_bin=$PYTHON_BIN
elif [ -x "$project_dir/.venv/bin/python" ]; then
    python_bin=$project_dir/.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
    python_bin=$(command -v python3)
else
    printf '%s\n' 'Python was not found. Follow the Setup instructions in README.md first.' >&2
    exit 1
fi

# Extra options come last so callers can override any preset setting.
exec "$python_bin" "$project_dir/messenger_plots.py" \
    --window-a "$window_primary" --window-b "$window_comparison" \
    --out "$project_dir/reports/$preset" "$@"
