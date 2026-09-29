#!/bin/sh
# The past 90 days compared with the past 30 days.
set -eu
launcher_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$launcher_dir/run-preset.sh" 90days-vs-30days 90d 30d "$@"
