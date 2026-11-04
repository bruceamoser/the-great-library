#!/bin/sh
# On-demand DriveThruRPG sync via the `drpg` package.
#
# Uses a DriveThruRPG "Library App Key" (not your password):
#   https://www.drivethrurpg.com/en/account/settings  -> "Library App Keys"
#
# The key is read from a file so it never appears in shell history or logs:
#   mkdir -p ~/.config/drpg && printf '%s' 'YOUR_KEY' > ~/.config/drpg/token
#   chmod 600 ~/.config/drpg/token
#
# Usage:
#   ./sync-dtrpg.sh --dry-run        # show what would be downloaded
#   ./sync-dtrpg.sh                  # actually sync
#   ./sync-dtrpg.sh --use-checksums  # slower but more precise
#
# After a successful sync, run ./rescan.sh to update the catalog.

set -eu
cd "$(dirname "$0")"

TOKEN_FILE="${DRPG_TOKEN_FILE:-$HOME/.config/drpg/token}"
if [ -z "${DRPG_TOKEN:-}" ]; then
  if [ -f "$TOKEN_FILE" ]; then
    DRPG_TOKEN=$(cat "$TOKEN_FILE")
    export DRPG_TOKEN
  else
    echo "ERROR: no DriveThruRPG token." >&2
    echo "  Create one at https://www.drivethrurpg.com/en/account/settings" >&2
    echo "  (section: Library App Keys), then save it with:" >&2
    echo "    mkdir -p ~/.config/drpg && printf '%s' 'YOUR_KEY' > $TOKEN_FILE" >&2
    echo "    chmod 600 $TOKEN_FILE" >&2
    exit 1
  fi
fi

DEST="${RPG_LIBRARY:-/mnt/disk5/RPG-Library}/11-DriveThruRPG"
mkdir -p "$DEST"

echo "Sync target: $DEST"
exec ./.venv/bin/python -m drpg \
  --library-path "$DEST" \
  --threads "${DRPG_THREADS:-4}" \
  --no-check \
  "$@"
