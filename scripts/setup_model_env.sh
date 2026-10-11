#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BASE_PYTHON="${JEPA_VJEPA_BASE_PYTHON:-$PROJECT_ROOT/../jepa-risk-detection/.venv/bin/python}"
VENV="$PROJECT_ROOT/.venv-vjepa"

if [ ! -x "$BASE_PYTHON" ]; then
  printf 'Pinned Python 3.12 base interpreter is unavailable: %s\n' "$BASE_PYTHON" >&2
  exit 1
fi
if [ ! -x "$VENV/bin/python" ]; then
  if [ -e "$VENV" ]; then
    printf 'Refusing to replace an existing non-venv path: %s\n' "$VENV" >&2
    exit 1
  fi
  "$BASE_PYTHON" -m venv "$VENV"
fi

"$VENV/bin/python" - <<'PY'
import sys
if sys.version_info[:2] != (3, 12):
    raise SystemExit(f"expected Python 3.12, found {sys.version.split()[0]}")
print(f"venv_python={sys.version.split()[0]}")
PY

cd "$PROJECT_ROOT"
"$VENV/bin/python" -m pip install --disable-pip-version-check --no-cache-dir \
  --index-url https://pypi.org/simple -e '.[model,test]'
"$VENV/bin/python" - <<'PY'
from importlib.metadata import version
import torch
for package in ("numpy", "Pillow", "torch", "torchvision", "transformers", "opencv-python-headless", "pytest"):
    print(f"{package}={version(package)}")
print(f"mps_built={torch.backends.mps.is_built()}")
print(f"mps_available={torch.backends.mps.is_available()}")
PY
