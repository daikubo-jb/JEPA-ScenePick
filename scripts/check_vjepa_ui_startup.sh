#!/bin/sh
set -eu
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
MODEL_PYTHON="$PROJECT_ROOT/.venv-vjepa/bin/python"

if [ ! -x "$MODEL_PYTHON" ]; then
  printf 'Project V-JEPA environment is unavailable; run scripts/setup_model_env.sh first.\n' >&2
  exit 1
fi

cd "$PROJECT_ROOT"
HF_HOME=/tmp/jepa-hf-readonly HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PROJECT_ROOT/src" \
  "$MODEL_PYTHON" -B - <<'PY'
from jepasearch.model import vjepa2_readiness

status = vjepa2_readiness()
if not status["available"]:
    raise SystemExit("V-JEPA UI startup readiness is blocked: " + " ".join(status["blockers"]))
print({
    "vjepa2_available": True,
    "local_pin_registered": status["local_pin_registered"],
    "provider_authentication": status["provider_authentication"],
    "dependencies": status["dependencies"],
    "checkpoint_bytes_read": False,
    "model_weights_loaded": False,
})
PY
