#!/usr/bin/env bash
# Development helper: run the daemon / panel from the checkout, tracked by pidfiles.
#   tools/dev.sh restart | start | stop | panel | panel-close | log
# With the user service installed, start/stop/restart/log act on the service.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN="${XDG_RUNTIME_DIR:-/tmp}/tuxlens"
LOG="$RUN/dev-daemon.log"
mkdir -p "$RUN"

stop_pidfile() {
  local file="$RUN/$1.pid" sig="$2"
  [ -f "$file" ] || return 0
  local pid; pid="$(cat "$file")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "-$sig" "$pid"
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid"
  fi
  rm -f "$file"
}

start_daemon() {
  cd "$ROOT" || exit 1
  setsid python3 -m tuxlensd >"$LOG" 2>&1 &
  echo $! >"$RUN/dev-daemon.pid"
  for _ in $(seq 1 25); do [ -S "$RUN/control.sock" ] && break; sleep 0.2; done
}

# Close a panel started by this script (never touches the service).
if [ "${1:-}" = "panel-close" ]; then stop_pidfile dev-panel TERM; exit 0; fi

# Installed as a user service? Then drive that instead of a private copy.
if systemctl --user cat tuxlens.service >/dev/null 2>&1; then
  case "${1:-restart}" in
    start|stop|restart) systemctl --user "${1:-restart}" tuxlens.service
                        for _ in $(seq 1 25); do [ -S "$RUN/control.sock" ] && break; sleep 0.2; done
                        exit 0 ;;
    log) journalctl --user -u tuxlens -n "${2:-30}" --no-pager -o cat; exit 0 ;;
  esac
fi

case "${1:-restart}" in
  start)   start_daemon ;;
  stop)    stop_pidfile dev-panel TERM; stop_pidfile dev-daemon INT ;;
  restart) stop_pidfile dev-daemon INT; start_daemon ;;
  panel)   stop_pidfile dev-panel TERM
           setsid "$ROOT/panel/tuxlens-panel" >"$RUN/dev-panel.log" 2>&1 &
           echo $! >"$RUN/dev-panel.pid" ;;
  log)     tail -n "${2:-30}" "$LOG" ;;
  *)       echo "usage: $0 restart|start|stop|panel|log" >&2; exit 2 ;;
esac
