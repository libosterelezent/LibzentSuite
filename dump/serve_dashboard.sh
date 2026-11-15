#!/usr/bin/env bash
# serve_dashboard.sh — interrupt-resistant dashboard launcher.
#
# Why this exists: ad-hoc "nohup ... & disown" starts inside agent tool
# calls died whenever the tool call was aborted (the abort signals the
# whole process group), and combined pkill+start one-liners could match
# their own wrapper shell. This script:
#   * manages the server by PIDFILE (never pattern-matches itself),
#   * starts it under setsid — its own session/process group, immune to
#     tool-call aborts and terminal hangs,
#   * waits for HTTP readiness before returning (short foreground run),
#   * is idempotent: restart|stop|status subcommands.
set -u

PORT="${LZ_DASH_PORT:-8070}"
PIDFILE="/tmp/opencode/sm-dashboard.pid"
LOG="/tmp/opencode/sm-dashboard.log"
URL="http://127.0.0.1:${PORT}/"
cd "$(dirname "$0")/.." || exit 1

alive() {  # is the pidfile's process actually running?
  [ -f "$PIDFILE" ] || return 1
  local pid; pid="$(cat "$PIDFILE" 2>/dev/null)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

stop_server() {
  if alive; then
    local pid; pid="$(cat "$PIDFILE")"
    kill "$pid" 2>/dev/null
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
    kill -9 "$pid" 2>/dev/null || true
    echo "stopped pid $pid"
  else
    echo "not running"
  fi
  rm -f "$PIDFILE"
}

start_server() {
  if alive; then
    echo "already running (pid $(cat "$PIDFILE")) — $URL"
    return 0
  fi
  rm -f "$PIDFILE"
  # setsid: new session — an aborted launching tool call cannot signal it
  setsid nohup python3 main.py --dashboard --headless --port "$PORT" \
    </dev/null >>"$LOG" 2>&1 &
  local pid=$!
  echo "$pid" > "$PIDFILE"
  # readiness wait (bounded — the server keeps booting even if we exit)
  for _ in $(seq 1 100); do
    if curl -sf -o /dev/null --max-time 1 "$URL"; then
      echo "up: pid $pid — $URL (log: $LOG)"
      return 0
    fi
    kill -0 "$pid" 2>/dev/null || { echo "DIED during boot — last log lines:"; tail -5 "$LOG"; return 1; }
    sleep 0.25
  done
  echo "still booting after 25s — pid $pid (log: $LOG)"
  return 0
}

case "${1:-restart}" in
  start) start_server ;;
  stop) stop_server ;;
  restart) stop_server; start_server ;;
  status)
    if alive; then
      pid="$(cat "$PIDFILE")"
      code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$URL" || echo 000)"
      echo "running (pid $pid) http=$code log=$LOG"
    else
      echo "not running"
    fi
    ;;
  *) echo "usage: $0 {start|stop|restart|status}" >&2; exit 2 ;;
esac
