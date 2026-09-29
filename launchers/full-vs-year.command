#!/bin/sh
# Full available history compared with the past calendar year.
set -eu
launcher_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$launcher_dir/run-preset.sh" full-vs-year all 1y "$@"
