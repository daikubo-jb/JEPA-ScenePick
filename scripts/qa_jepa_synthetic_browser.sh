#!/bin/sh
set -eu
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PDEV_ROOT=$(CDPATH= cd -- "$PROJECT_ROOT/.." && pwd)
PORT=${JEPA_QA_PORT:-8768}
QA_TMP=$(mktemp -d "${TMPDIR:-/tmp}/jepa-vjepa-browser-qa.XXXXXX")
SERVER_PID=

cleanup() {
  if [ -n "$SERVER_PID" ]; then
    kill -TERM "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$QA_TMP"
}
trap cleanup EXIT HUP INT TERM

if /usr/bin/python3 - "$PORT" <<'PY'
import socket, sys
s = socket.socket()
try:
    sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
finally:
    s.close()
PY
then
  printf 'Loopback port %s is already in use; leaving that process untouched.\n' "$PORT" >&2
  exit 1
fi

cd "$PROJECT_ROOT"
HF_HOME=/tmp/jepa-vjepa-offline HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH="$PROJECT_ROOT/src:$PDEV_ROOT/jepa-risk-detection/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$PROJECT_ROOT/.venv-vjepa/bin/python" -B "$PROJECT_ROOT/scripts/serve_synthetic_ui_workflow.py" \
  --port "$PORT" --engine vjepa2 > "$QA_TMP/server.log" 2>&1 &
SERVER_PID=$!

ready=0
attempt=0
while [ "$attempt" -lt 60 ]; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then break; fi
  if /usr/bin/python3 - "$PORT" <<'PY'
import sys, urllib.request
try:
    urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/", timeout=0.5).close()
except Exception:
    sys.exit(1)
PY
  then
    ready=1
    break
  fi
  attempt=$((attempt + 1))
  sleep 0.5
done
if [ "$ready" -ne 1 ]; then
  cat "$QA_TMP/server.log" >&2
  printf 'Synthetic V-JEPA loopback UI did not start.\n' >&2
  exit 1
fi

printf 'synthetic_only=true browser=WKWebView origin=http://127.0.0.1:%s\n' "$PORT"
swift "$PROJECT_ROOT/scripts/qa_synthetic_ui_workflow.swift" \
  "http://127.0.0.1:$PORT/?engine=vjepa2" "$QA_TMP/references.json"
printf 'server_log: '
cat "$QA_TMP/server.log"
