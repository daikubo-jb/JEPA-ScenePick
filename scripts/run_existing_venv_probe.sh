#!/bin/sh
set -eu
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PDEV_ROOT=$(CDPATH= cd -- "$PROJECT_ROOT/.." && pwd)
PYTHONDONTWRITEBYTECODE=1 HF_HOME=/tmp/jepa-hf-readonly HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH="$PROJECT_ROOT/src:$PDEV_ROOT/jepa-risk-detection/src${PYTHONPATH:+:$PYTHONPATH}" "$PDEV_ROOT/jepa-risk-detection/.venv/bin/python" -B "$PROJECT_ROOT/scripts/raw_vjepa_probe.py"
