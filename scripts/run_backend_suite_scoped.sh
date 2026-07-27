#!/usr/bin/env bash
# Runs knowledge/serve/tests deselecting the documented pre-existing baseline failures
# (docs/known-baseline-failures-backend-suite.md), so a ticket's backend-suite validation grades
# on whether it regressed the suite, not on unrelated pre-existing breakage (see that doc).
set -euo pipefail
cd "$(dirname "$0")/.."
mapfile -t DESELECTED < knowledge/serve/tests/known_baseline_failures.txt
ARGS=()
for t in "${DESELECTED[@]}"; do
  ARGS+=("--deselect=$t")
done
exec uv run --group dev pytest knowledge/serve/tests -q "${ARGS[@]}"
