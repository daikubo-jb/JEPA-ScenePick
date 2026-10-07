#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RISK_ROOT=$(CDPATH= cd -- "$PROJECT_ROOT/../jepa-risk-detection" && pwd)
MODEL_PYTHON="$RISK_ROOT/.venv/bin/python"
PORT=${JEPA_UI_PORT:-8766}
PYTHONPATH_VALUE="$PROJECT_ROOT/.runtime-deps:$PROJECT_ROOT/src:$RISK_ROOT/src"
PROJECT_KEY=$(printf '%s' "$PROJECT_ROOT" | shasum -a 256 | cut -c1-12)
PID_FILE="${TMPDIR:-/tmp}/jepa-video-search-ui-${PROJECT_KEY}-${PORT}.pid"
URL="http://127.0.0.1:${PORT}/"

owned_pid() {
  [ -f "$PID_FILE" ] || return 1
  pid=$(cat "$PID_FILE")
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  command=$(ps -p "$pid" -o command= 2>/dev/null || true)
  case "$command" in
    *"$PROJECT_ROOT/scripts/run_ui_server.py"*"--port $PORT --no-open"*) printf '%s\n' "$pid" ;;
    *) return 1 ;;
  esac
}

port_open() {
  python3 - "$PORT" <<'PY'
import socket, sys
s = socket.socket()
s.settimeout(0.3)
try:
    sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
finally:
    s.close()
PY
}

case "${1:-}" in
  start)
    if [ ! -x "$MODEL_PYTHON" ]; then
      printf 'Existing model Python is unavailable: %s\n' "$MODEL_PYTHON" >&2
      exit 1
    fi
    if pid=$(owned_pid); then
      printf 'Already running (pid %s): %s\n' "$pid" "$URL"
      exit 0
    fi
    rm -f "$PID_FILE"
    if port_open; then
      printf 'Port %s is already in use; no process was changed.\n' "$PORT" >&2
      exit 1
    fi
    cd "$PROJECT_ROOT"
    nohup env PYTHONDONTWRITEBYTECODE=1 HF_HOME=/tmp/jepa-hf-readonly HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
      PYTHONPATH="$PYTHONPATH_VALUE" "$MODEL_PYTHON" "$PROJECT_ROOT/scripts/run_ui_server.py" \
      --port "$PORT" --no-open </dev/null >/dev/null 2>&1 &
    pid=$!
    printf '%s\n' "$pid" > "$PID_FILE"
    ready=0
    attempt=0
    while [ "$attempt" -lt 40 ]; do
      if ! kill -0 "$pid" 2>/dev/null; then break; fi
      if python3 - "$PORT" <<'PY'
import sys, urllib.request
try:
    urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/", timeout=0.5).close()
except Exception:
    sys.exit(1)
PY
      then ready=1; break; fi
      attempt=$((attempt + 1))
      sleep 0.25
    done
    if [ "$ready" -ne 1 ]; then
      printf 'Server failed to become ready. Inspect status; no other process was stopped.\n' >&2
      exit 1
    fi
    printf 'Started pid %s: %s\n' "$pid" "$URL"
    ;;
  status)
    if pid=$(owned_pid); then
      printf 'Running (pid %s): %s\n' "$pid" "$URL"
      exit 0
    fi
    printf 'Not running: %s\n' "$URL"
    exit 1
    ;;
  stop)
    if ! pid=$(owned_pid); then
      rm -f "$PID_FILE"
      printf 'Not running (no matching project-owned server).\n'
      exit 0
    fi
    kill -TERM "$pid"
    attempt=0
    while kill -0 "$pid" 2>/dev/null && [ "$attempt" -lt 40 ]; do
      command=$(ps -p "$pid" -o command= 2>/dev/null || true)
      case "$command" in *"$PROJECT_ROOT/scripts/run_ui_server.py"*"--port $PORT --no-open"*) ;; *) break ;; esac
      attempt=$((attempt + 1))
      sleep 0.25
    done
    rm -f "$PID_FILE"
    printf 'Stopped pid %s.\n' "$pid"
    ;;
  *)
    printf 'Usage: %s {start|status|stop}\n' "$0" >&2
    exit 2
    ;;
esac
