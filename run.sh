#!/bin/sh
# LAN-only RPG library server
cd "$(dirname "$0")"
export RPG_LIBRARY="${RPG_LIBRARY:-/mnt/disk5/RPG-Library}"
export RPG_DB="${RPG_DB:-$(pwd)/library.db}"
exec ./.venv/bin/uvicorn app:app --host 0.0.0.0 --port "${PORT:-8765}"
