#!/bin/sh
# De-duplicate the library by content hash.
#
# Safe after a DriveThruRPG sync: 02_dedupe.py treats 11-DriveThruRPG as a
# secondary source, so where a file exists both in a curated shelf and in the
# DTRPG download, the *curated* copy is kept and the DTRPG copy removed.
#
# --min-bytes 1 is deliberate: the default 1 MB floor silently ignores small
# byte-identical files (VTT tokens/minis are 300-600 KB).
set -eu
cd "$(dirname "$0")"
exec ./.venv/bin/python /mnt/disk5/rpg-tools/02_dedupe.py \
  --root "${RPG_LIBRARY:-/mnt/disk5/RPG-Library}" \
  --log "${DEDUPE_LOG:-/mnt/disk5/rpg-tools/dedupe-latest.json}" \
  --min-bytes 1 \
  --prune-empty \
  "$@"
