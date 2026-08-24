#!/usr/bin/env bash
# Run the custom flow. Works both from a plain shell and from inside the
# OpenLane nix-shell.
#
#   ./run.sh                    full run
#   ./run.sh --list             show the step plan
#   ./run.sh --sweep-only       synthesis sweep only
#   ./run.sh --resume-from 35   re-enter mid-flow
set -euo pipefail

CUSTOM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# NOTE: do NOT use $OPENLANE_DIR to locate shell.nix -- inside the OpenLane
# nix-shell that variable points at the INSTALLED PACKAGE
# (.../site-packages/openlane), which contains no shell.nix.
OPENLANE_SRC="${OPENLANE_SRC:-/Users/prasanna/openlane2}"

# nix-shell/nix-daemon aren't on PATH in a fresh non-login shell on this
# machine even though Nix itself is installed; source the profile script so
# `nix-shell` resolves below.
if ! command -v nix-shell >/dev/null 2>&1; then
    if [[ -e '/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh' ]]; then
        # shellcheck disable=SC1091
        source '/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh'
    fi
fi

if command -v openroad >/dev/null 2>&1 && python3 -c "import openlane" 2>/dev/null; then
    exec python3 "$CUSTOM_DIR/run_custom_flow.py" "$@"
fi

if [[ ! -f "$OPENLANE_SRC/shell.nix" ]]; then
    echo "ERROR: not inside an OpenLane environment, and no shell.nix at:" >&2
    echo "         $OPENLANE_SRC/shell.nix" >&2
    exit 1
fi

q=""
for a in "python3" "$CUSTOM_DIR/run_custom_flow.py" "$@"; do
    q+="'${a//\'/\'\\\'\'}' "
done
exec nix-shell "$OPENLANE_SRC/shell.nix" --run "$q"
