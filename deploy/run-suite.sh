#!/usr/bin/env bash
# Weekly test run on the machine that actually serves.
#
# Why not just `pytest` in the install directory: the suite has written into
# live runtime/ files three times in this project's history, twice noticed only
# on the server. A run nobody is watching is exactly the wrong place to find
# that out again, so the code is copied to a scratch directory first and the
# suite runs there. runtime/ and content/ are left behind — an isolate_* helper
# that stops isolating then damages a copy that is deleted a second later.
#
# configs/ and the venv are the point of running here at all: they are what
# differs between this machine and the laptop the suite is green on.
#
# Usage: run-suite.sh [install-dir]   (default: the parent of this script)
set -u

INSTALL_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON="$INSTALL_DIR/.venv/bin/python"
WORK="$(mktemp -d -t owui-suite-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

if [ ! -x "$PYTHON" ]; then
    echo "No venv at $PYTHON — nothing to run the suite with." >&2
    exit 2
fi

# -a keeps modes and symlinks; the excludes are the live state and the caches.
rsync -a \
    --exclude '.venv/' \
    --exclude 'runtime/' \
    --exclude 'content/' \
    --exclude '.git/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    "$INSTALL_DIR"/ "$WORK"/ || exit 2

# The end-to-end test builds its own isolated root and symlinks the default
# venv into it — without one it skips itself, and it is the test with the most
# to say about *this* machine. runtime/ is excluded above, so the directory is
# linked back in on its own; settings.json, usage.db and the content store stay
# out of reach.
if [ -d "$INSTALL_DIR/runtime/venvs" ]; then
    mkdir -p "$WORK/runtime"
    ln -s "$INSTALL_DIR/runtime/venvs" "$WORK/runtime/venvs"
fi

cd "$WORK" || exit 2
# The mtime to compare against below. Not $WORK itself: rsync was still
# writing into it a moment ago.
stamp="$WORK/.suite-started"
touch "$stamp"

# The unit caps the runtime; pytest-timeout is not a dependency of this project.
"$PYTHON" -m pytest tests/ -q 2>&1
status=$?

# The suite is not allowed to have touched the live install. Saying so out loud
# every week is the only way this stays true.
# logs/ is written by design. venvs/ is linked in above, and the end-to-end
# test may legitimately touch the default venv while installing its echo tool.
leaked="$(find "$INSTALL_DIR/runtime" -newer "$stamp" -type f 2>/dev/null \
          | grep -v '/logs/' | grep -v '/venvs/' || true)"
if [ -n "$leaked" ]; then
    echo "WARNING: the suite wrote into the live runtime/:" >&2
    echo "$leaked" >&2
    status=1
fi

if [ "$status" -eq 0 ]; then
    echo "Suite green on $(hostname) — $(date +%FT%T%z)"
else
    echo "Suite FAILED on $(hostname) (exit $status) — $(date +%FT%T%z)" >&2
fi
exit "$status"
