#!/bin/sh
set -eu
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PDEV_ROOT=$(CDPATH= cd -- "$PROJECT_ROOT/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1
export HF_HOME=/tmp/jepa-hf-readonly
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export PYTHONPATH="$PROJECT_ROOT/tests/offline_guard:$PROJECT_ROOT/.runtime-deps:$PROJECT_ROOT/src:$PDEV_ROOT/jepa-risk-detection/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PDEV_ROOT/jepa-risk-detection/.venv/bin/python" -B "$PROJECT_ROOT/scripts/vjepa_synthetic_integration.py" "$@"
