#!/bin/sh
# Supervisor loop for the RPG Library web app.
#
# Relaunches uvicorn whenever it exits, until the stop-flag file appears. This
# is what makes POST /api/restart work without sudo: the app just exits, and
# this loop brings it straight back -- the same way systemd's
# Restart=on-failure does for the service.
#
# Launched by manage.sh; not usually run by hand.
set -u
cd "$(dirname "$0")"

STOPFLAG="$1"
LOG="$2"
PORT="$3"

while :; do
    ./.venv/bin/uvicorn app:app --host 0.0.0.0 --port "$PORT" >> "$LOG" 2>&1
    rc=$?
    if [ -f "$STOPFLAG" ]; then
        echo "$(date '+%F %T') supervisor: stop requested, exiting (last rc=$rc)" >> "$LOG"
        break
    fi
    echo "$(date '+%F %T') supervisor: app exited rc=$rc, relaunching in 2s" >> "$LOG"
    sleep 2
done
