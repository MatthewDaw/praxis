#!/usr/bin/env bash
# Validates the ticket-9e4a939e acceptance condition:
#   "given a pull request that breaks a frontend test, the CI run reports a failing
#   vitest job; given the workflow file, a job invoking vitest exists and is not
#   gated away for frontend-only changes."
#
# Two independent checks:
#   1. STATIC: the CI workflow file defines a job that runs the frontend test
#      script (vitest) and is gated ONLY on the frontend changes-filter output
#      (so a frontend-only PR still runs it -- not additionally gated behind
#      another stack, e.g. requiring python/infra changes too).
#   2. DYNAMIC: running the real frontend test command against a deliberately
#      broken test fails (nonzero exit) -- proving a broken frontend test is
#      genuinely caught, not silently swallowed.
set -euo pipefail
cd "$(dirname "$0")/.."

WORKFLOW=".github/workflows/ci.yml"

if [ ! -f "$WORKFLOW" ]; then
  echo "FAIL: $WORKFLOW not found" >&2
  exit 1
fi

# --- 1. static: an ungated vitest/test job exists -------------------------------
# Find job blocks (top-level "  <name>:" under "jobs:") and inspect each one.
job_names=$(awk '
  /^jobs:/ { in_jobs=1; next }
  in_jobs && /^[A-Za-z]/ { in_jobs=0 }
  in_jobs && /^  [A-Za-z0-9_-]+:$/ { gsub(/^  /,""); gsub(/:$/,""); print }
' "$WORKFLOW")

found_ungated_vitest_job=0
for job in $job_names; do
  block=$(awk -v job="$job" '
    $0 ~ "^  "job":$" { grab=1; print; next }
    grab && /^  [A-Za-z0-9_-]+:$/ { exit }
    grab { print }
  ' "$WORKFLOW")

  code_lines=$(echo "$block" | grep -v '^\s*#')
  if echo "$code_lines" | grep -Eq 'npm run test\b|npx vitest|vitest run'; then
    # Gating: the job must not be conditioned on any OTHER changed-stack output
    # in addition to frontend (that would gate it away for frontend-only PRs).
    cond_line=$(echo "$block" | grep -E '^\s*if:' || true)
    if echo "$cond_line" | grep -q 'frontend'; then
      if echo "$cond_line" | grep -Eq "outputs\.(python|infra|go)\s*=="; then
        echo "SKIP: job '$job' runs vitest but is gated behind another stack too: $cond_line"
        continue
      fi
      found_ungated_vitest_job=1
      echo "OK: job '$job' runs vitest, gated only on frontend changes"
      break
    elif [ -z "$cond_line" ]; then
      found_ungated_vitest_job=1
      echo "OK: job '$job' runs vitest, unconditioned (always runs)"
      break
    fi
  fi
done

if [ "$found_ungated_vitest_job" -ne 1 ]; then
  echo "FAIL: no vitest job in $WORKFLOW that runs (ungated) for frontend-only changes" >&2
  exit 1
fi

# --- 2. dynamic: vitest genuinely fails on a broken frontend test ---------------
cd frontend-react
TMP_TEST="src/__ci_break_check__.test.ts"
cleanup() { rm -f "$TMP_TEST"; }
trap cleanup EXIT

cat > "$TMP_TEST" <<'EOF'
import { describe, it, expect } from "vitest";

describe("__ci_break_check__ (transient, deleted by the validation script)", () => {
  it("intentionally fails to prove vitest reports a broken frontend test", () => {
    expect(true).toBe(false);
  });
});
EOF

if npx vitest run "$TMP_TEST" >/tmp/ci_vitest_break_check.log 2>&1; then
  echo "FAIL: expected vitest to report a failing job for a broken test, but it passed" >&2
  cat /tmp/ci_vitest_break_check.log >&2
  exit 1
fi

echo "OK: vitest correctly reports a failing job for a broken frontend test"
