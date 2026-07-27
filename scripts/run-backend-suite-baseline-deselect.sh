#!/usr/bin/env bash
# Run the knowledge/serve backend suite excluding the currently-documented
# pre-existing/environmental failures (see
# docs/known-baseline-failures-backend-suite.md) so a ticket's backend-suite
# validation is graded on whether it regressed the suite, not on unrelated
# baseline breakage. The deselect list lives in
# scripts/backend-suite-known-baseline-deselect.txt so it is reviewable and
# reusable across tickets instead of hardcoded inline.
set -euo pipefail
cd "$(dirname "$0")/.."

deselect_args=()
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  deselect_args+=(--deselect "$line")
done < scripts/backend-suite-known-baseline-deselect.txt

uv run --no-sync pytest knowledge/serve/tests -q "${deselect_args[@]}"
