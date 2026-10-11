#!/bin/sh
set -eu
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PDEV_ROOT=$(CDPATH= cd -- "$PROJECT_ROOT/.." && pwd)
export HF_HOME=/tmp/jepa-vjepa-offline
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PROJECT_ROOT/src:$PDEV_ROOT/jepa-risk-detection/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PROJECT_ROOT/.venv-vjepa/bin/python" -B "$PROJECT_ROOT/scripts/vjepa_synthetic_integration.py"
