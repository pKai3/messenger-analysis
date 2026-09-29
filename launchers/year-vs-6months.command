#!/bin/sh
# The past calendar year compared with the past six calendar months.
set -eu
launcher_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$launcher_dir/run-preset.sh" year-vs-6months 1y 6m "$@"
