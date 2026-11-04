#!/bin/sh
# Manage the RPG Library web app with NO sudo required.
#
#   ./manage.sh start|stop|restart|status|logs
#
# Runs the server detached (setsid + nohup), so it survives closing the
# terminal and survives VS Code restarting. The trade-off versus the systemd
# service: it does NOT start automatically on boot.
#
# If the systemd service is running you do not want this as well -- both would
# bind port 8765. This script detects that and tells you.
set -eu
cd "$(dirname "$0")"

PIDFILE="$(pwd)/rpg-library.pid"
LOGFILE="$(pwd)/uvicorn.log"
STOPFLAG="$(pwd)/.rpg-library.stopping"
PORT="${PORT:-8765}"
UVICORN_ARGS="app:app --host 0.0.0.0 --port $PORT"
VENV="./.venv/bin/uvicorn"

export RPG_LIBRARY="${RPG_LIBRARY:-/mnt/disk5/RPG-Library}"
export RPG_DB="${RPG_DB:-$(pwd)/library.db}"

systemd_active() {
    systemctl is-active --quiet rpg-library 2>/dev/null
}
pid_running() {
    [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}
port_in_use() {
    ss -ltn 2>/dev/null | grep -q ":$PORT "
}

do_start() {
    if pid_running; then
        echo "already running (pid $(cat "$PIDFILE"))"; return 0
    fi
    if systemd_active; then
        echo "ERROR: the systemd service 'rpg-library' is running and owns port $PORT." >&2
        echo "       Either use it:   sudo systemctl restart rpg-library" >&2
        echo "       Or retire it:    sudo systemctl disable --now rpg-library" >&2
        return 1
    fi
    if port_in_use; then
        echo "ERROR: something else is already listening on port $PORT." >&2
        return 1
    fi
    echo "starting on port $PORT (supervised; POST /api/restart will bounce it) ..."
    rm -f "$STOPFLAG"
    # supervise.sh relaunches uvicorn whenever it exits, which is what makes
    # the app's own /api/restart work without any privileges.
    setsid nohup sh ./supervise.sh "$STOPFLAG" "$LOGFILE" "$PORT" \
        > /dev/null 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    sleep 4
    if pid_running; then
        echo "started (supervisor pid $(cat "$PIDFILE"))  ->  http://localhost:$PORT/"
    else
        echo "FAILED to start; last lines of $LOGFILE:" >&2
        tail -15 "$LOGFILE" >&2
        rm -f "$PIDFILE"
        return 1
    fi
}

do_stop() {
    if systemd_active; then
        echo "note: the systemd service is active; this script did not start it." >&2
        echo "      stop it with: sudo systemctl stop rpg-library" >&2
    fi
    if pid_running; then
        pid=$(cat "$PIDFILE")
        # tell the supervisor to stop relaunching, then stop it
        touch "$STOPFLAG"
        pkill -P "$pid" 2>/dev/null || true
        kill "$pid" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.5
        done
        kill -9 "$pid" 2>/dev/null || true
        rm -f "$PIDFILE"
        echo "stopped (supervisor pid $pid)"
    else
        echo "not running"
        rm -f "$PIDFILE"
    fi
}

do_status() {
    if systemd_active; then echo "systemd service : active"; else echo "systemd service : inactive"; fi
    if pid_running; then echo "manage.sh       : running (pid $(cat "$PIDFILE"))"; else echo "manage.sh       : stopped"; fi
    if port_in_use; then echo "port $PORT       : listening"; else echo "port $PORT       : free"; fi
}

case "${1:-status}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; do_start ;;
    status)  do_status ;;
    logs)    tail -n 100 -f "$LOGFILE" ;;
    *) echo "usage: $0 {start|stop|restart|status|logs}" >&2; exit 2 ;;
esac
