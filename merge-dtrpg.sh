#!/bin/sh
# Fold the DriveThruRPG download shelf into the main library shelves.
#
# The library is ONE library with several SOURCES; DriveThruRPG is a provenance,
# not a shelf. Run this AFTER dedupe.sh (so the dedupe can still tell which copy
# is the download and which is the curated shelf copy) and BEFORE rescan.sh.
set -eu
cd "$(dirname "$0")"
exec ./.venv/bin/python /mnt/disk5/rpg-tools/05_merge_dtrpg.py \
  --library "${RPG_LIBRARY:-/mnt/disk5/RPG-Library}" \
  --log "${DTRPG_MERGE_LOG:-/mnt/disk5/rpg-tools/dtrpg-merge.json}" \
  --provenance "${RPG_PROVENANCE:-$(pwd)/provenance.json}" \
  "$@"
