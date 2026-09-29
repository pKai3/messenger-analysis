#!/bin/sh
# The past calendar year compared with the past 90 days.
set -eu
launcher_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$launcher_dir/run-preset.sh" year-vs-90days 1y 90d "$@"
