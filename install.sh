#!/usr/bin/env sh
# POSIX convenience wrapper. install.py is the cross-platform installer.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$HERE/install.py" "$@"
