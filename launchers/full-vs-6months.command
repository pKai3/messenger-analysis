#!/bin/sh
# Full available history compared with the past six calendar months.
set -eu
launcher_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$launcher_dir/run-preset.sh" full-vs-6months all 6m "$@"
