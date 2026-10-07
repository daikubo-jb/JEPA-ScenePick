#!/bin/sh
set -eu
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUTPUT_NAME=${1:-synthetic-ui-review-final.png}
case "$OUTPUT_NAME" in
  *[!A-Za-z0-9._-]*|*..*) echo "Output must be a simple filename under screenshots/" >&2; exit 2 ;;
esac
OUTPUT="$PROJECT_ROOT/screenshots/$OUTPUT_NAME"
if [ -e "$OUTPUT" ] || [ -L "$OUTPUT" ]; then
  echo "Refusing to overwrite existing screenshot: $OUTPUT" >&2
  exit 2
fi
mkdir -p "$PROJECT_ROOT/screenshots"
TEMP_ROOT=$(mktemp -d /tmp/jepa-ui-screen.XXXXXX)
SERVER_PID=
cleanup() {
  if [ -n "$SERVER_PID" ]; then kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; fi
  rm -rf "$TEMP_ROOT"
}
trap cleanup EXIT INT TERM
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PROJECT_ROOT/.runtime-deps:$PROJECT_ROOT/src"
python3 -u "$PROJECT_ROOT/scripts/serve_synthetic_ui_demo.py" --port "$PORT" > "$TEMP_ROOT/server.log" 2>&1 &
SERVER_PID=$!
URL="http://127.0.0.1:$PORT/"
READY=0
for attempt in $(seq 1 80); do
  if curl --silent --show-error --fail "$URL" -o /dev/null 2>/dev/null; then READY=1; break; fi
  sleep 0.25
done
if [ "$READY" -ne 1 ]; then
  cat "$TEMP_ROOT/server.log" >&2
  echo "Synthetic UI server failed to start" >&2
  exit 1
fi
mkdir -p "$TEMP_ROOT/swift-module-cache"
CLANG_MODULE_CACHE_PATH="$TEMP_ROOT/swift-module-cache" swift "$PROJECT_ROOT/scripts/capture_webkit_snapshot.swift" "$URL" "$OUTPUT"
CLANG_MODULE_CACHE_PATH="$TEMP_ROOT/swift-module-cache" swift "$PROJECT_ROOT/scripts/qa_synthetic_ui_viewports.swift" "$URL"
/usr/bin/sips -g pixelWidth -g pixelHeight "$OUTPUT" | tail -n 2
printf 'Synthetic-only screenshot saved: screenshots/%s\n' "$OUTPUT_NAME"
