#!/bin/sh
# Rebuild the catalog after adding files (e.g. a DriveThruRPG sync or files
# dropped into RPG-Library/_Inbox/).
set -eu
cd "$(dirname "$0")"
./.venv/bin/python catalog.py \
  --library "${RPG_LIBRARY:-/mnt/disk5/RPG-Library}" \
  --db "${RPG_DB:-$(pwd)/library.db}" \
  --moves /mnt/disk5/rpg-tools/shelf-moves.json \
  --provenance "${RPG_PROVENANCE:-$(pwd)/provenance.json}"
