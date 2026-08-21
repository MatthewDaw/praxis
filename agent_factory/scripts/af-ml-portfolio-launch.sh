#!/usr/bin/env bash
# Detach exactly one canonical portfolio controller. All policy remains in Python.
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: af-ml-portfolio-launch.sh --config OPERATOR.json run [options]" >&2
  exit 2
fi

RUNTIME_LOG="${AF_ML_PORTFOLIO_LOG:-portfolio-controller.log}"
PYTHON_BIN="${AF_ML_PORTFOLIO_PYTHON:-python}"
if command -v setsid >/dev/null 2>&1; then
  nohup setsid "$PYTHON_BIN" -m knowledge.ml_registry.cli.portfolio "$@" \
    </dev/null >>"$RUNTIME_LOG" 2>&1 &
else
  nohup "$PYTHON_BIN" -m knowledge.ml_registry.cli.portfolio "$@" \
    </dev/null >>"$RUNTIME_LOG" 2>&1 &
fi
printf '%s\n' "$!"
