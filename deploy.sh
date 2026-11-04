#!/usr/bin/env bash
# Deploy the Great Library application to the LAN host.
#
#   ./deploy.sh --dry-run    show what would change, then stop
#   ./deploy.sh              sync, back up the old code, restart, health-check
#
# The library data (library.db, covers/, thumbnails/, inbox-originals/,
# provenance.json, logs, locks) lives only on the host and is never synced.
# The restart goes through the app's own endpoint, so no sudo is required.
set -euo pipefail

HOST="${DEPLOY_HOST:-bruce@192.168.44.50}"
DIR="${DEPLOY_DIR:-/mnt/disk5/rpg-app}"
SERVICE="${DEPLOY_SERVICE:-rpg-library}"
BASE_URL="${DEPLOY_URL:-http://127.0.0.1:8765}"
BACKUP_ROOT="${DEPLOY_BACKUP_ROOT:-/mnt/disk4/RPG-Library-Backups}"

DRY_RUN=0
PRUNE=0
BACKUP=1
RESTART=1

usage() {
  cat <<'EOF'
Usage: ./deploy.sh [options]

  -n, --dry-run     show the rsync changes without applying them
      --prune       also delete files that no longer exist in the repository
      --no-backup   skip the pre-deploy code backup on the host
      --no-restart  sync only; do not restart or health-check
  -h, --help        show this help

Environment overrides:
  DEPLOY_HOST, DEPLOY_DIR, DEPLOY_SERVICE, DEPLOY_URL, DEPLOY_BACKUP_ROOT
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -n|--dry-run)  DRY_RUN=1 ;;
    --prune)       PRUNE=1 ;;
    --no-backup)   BACKUP=0 ;;
    --no-restart)  RESTART=0 ;;
    -h|--help)     usage; exit 0 ;;
    *)             echo "deploy: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

backup_dir=""
die() { echo "deploy: $*" >&2; exit 1; }
rollback_hint() {
  [ -n "$backup_dir" ] || return 0
  echo "deploy: previous code: $backup_dir/app-code.tar.gz" >&2
  echo "deploy: roll back with: ssh $HOST \"tar xzf $backup_dir/app-code.tar.gz -C $DIR\"" >&2
}
on_exit() {
  local code=$?
  [ "$code" -eq 0 ] || rollback_hint
}
trap on_exit EXIT

# Paths that belong to the host's data, not to the application.
EXCLUDES=(
  --exclude '.git/'
  --exclude '.openbad/'
  --exclude '.venv/'
  --exclude '__pycache__/'
  --exclude '*.pyc'
  --exclude 'library.db*'
  --exclude 'covers/'
  --exclude 'thumbnails/'
  --exclude 'inbox-originals/'
  --exclude 'provenance.json*'
  --exclude 'job-*.json'
  --exclude '*.log'
  --exclude '*.lock'
)

RSYNC=(rsync -rltz --itemize-changes "${EXCLUDES[@]}")
[ "$PRUNE" = 1 ] && RSYNC+=(--delete)
[ "$DRY_RUN" = 1 ] && RSYNC+=(--dry-run)

echo "deploy: $ROOT -> $HOST:$DIR"

echo -n "deploy: checking host ... "
ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" \
  "test -d '$DIR' && systemctl is-active --quiet '$SERVICE'" \
  || die "host unreachable, $DIR missing, or $SERVICE not active"
echo "ok"

if [ "$DRY_RUN" = 1 ]; then
  "${RSYNC[@]}" ./ "$HOST:$DIR/"
  echo "deploy: dry run, nothing changed"
  exit 0
fi

if [ "$BACKUP" = 1 ]; then
  stamp="$(date +%Y%m%d-%H%M%S)"
  backup_dir="$BACKUP_ROOT/$stamp-pre-deploy"
  echo -n "deploy: backing up host code ... "
  ssh -o BatchMode=yes "$HOST" "set -e
    mkdir -p '$backup_dir'
    cd '$DIR'
    tar czf '$backup_dir/app-code.tar.gz' \
      --exclude=.venv --exclude=library.db --exclude=covers \
      --exclude=thumbnails --exclude=inbox-originals --exclude=__pycache__ \
      --exclude='*.log' --exclude='provenance.json*' ." \
    || die "backup failed; nothing was synced"
  echo "$backup_dir"
fi

echo "deploy: syncing ..."
"${RSYNC[@]}" ./ "$HOST:$DIR/"

local_sum="$(sha256sum app.py | cut -d' ' -f1)"
remote_sum="$(ssh -o BatchMode=yes "$HOST" "sha256sum '$DIR/app.py'" | cut -d' ' -f1)"
[ "$local_sum" = "$remote_sum" ] || die "app.py checksum mismatch after sync"
echo "deploy: synced (app.py sha256 ${local_sum:0:12})"

if [ "$RESTART" = 0 ]; then
  echo "deploy: --no-restart, service left running the old code"
  exit 0
fi

echo "deploy: restarting $SERVICE ..."
ssh -o BatchMode=yes "$HOST" bash -s -- "$SERVICE" "$BASE_URL" <<'REMOTE'
set -u
service="$1"
base="$2"
before="$(systemctl show "$service" -p MainPID --value)"

curl -fsS -X POST -H 'X-Library-Request: 1' \
  "$base/api/restart?confirm=1" >/dev/null \
  || { echo "restart request rejected" >&2; exit 1; }

for _ in $(seq 1 30); do
  sleep 1
  if systemctl is-active --quiet "$service" && curl -sf -o /dev/null "$base/"; then
    echo "deploy: restarted, pid $before -> $(systemctl show "$service" -p MainPID --value)"
    exit 0
  fi
done

echo "deploy: $service did not become healthy within 30s" >&2
systemctl status "$service" --no-pager -n 15 >&2 || true
exit 1
REMOTE

echo "deploy: done"
