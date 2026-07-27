#!/usr/bin/env bash
# R44: Local-venue regression guard for the af-build skill.
# Ensures the local af-build path (inline single-agent loop without Workflow)
# hasn't been corrupted by remote-jobs / box-service changes.
#
# Three gates, all must pass:
#   1. SHA-256 of every af-build skill file matches the recorded baseline
#   2. No venue/remote/local conditional branches have leaked into the skill text
#   3. The pre-existing local af-build regression suite passes (agent_factory tests)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SKILL_DIR="$REPO_ROOT/agent_factory/skills/af-build"
BASELINE_FILE="$SKILL_DIR/.skill-baseline-sha256"
ERRORS=0

# --- Gate 1: SHA-256 baseline ------------------------------------------------

echo "=== Gate 1: SHA-256 baseline check ==="
if [ ! -f "$BASELINE_FILE" ]; then
    echo "FAIL: baseline file missing at $BASELINE_FILE"
    ERRORS=$((ERRORS + 1))
else
    for skill_file in "$SKILL_DIR"/*.md; do
        [ -f "$skill_file" ] || continue
        fname="$(basename "$skill_file")"
        expected="$(head -1 "$BASELINE_FILE" | tr -d '[:space:]')"
        if [ -z "$expected" ]; then
            echo "FAIL: no baseline hash found in $BASELINE_FILE"
            ERRORS=$((ERRORS + 1))
            continue
        fi
        actual="$(sha256sum "$skill_file" | awk '{print $1}')"
        if [ "$actual" != "$expected" ]; then
            echo "FAIL: $fname checksum changed (expected $expected, got $actual)"
            ERRORS=$((ERRORS + 1))
        else
            echo "  OK: $fname matches baseline $expected"
        fi
    done
fi

# --- Gate 2: Venue conditionals scan ------------------------------------------

echo "=== Gate 2: Venue conditional scan ==="
# Look for code-level conditionals that switch on venue/remote/local.
# These are distinct from ordinary English uses of the word "local".
VENUE_PATTERNS=(
    'if.*venue'
    'case.*venue'
    'VENUE'
    'is_remote'
    'is_local'
    'run_venue'
    'build_venue'
    'remote.*only\|only.*remote'
)
for pattern in "${VENUE_PATTERNS[@]}"; do
    matches="$(grep -n "$pattern" "$SKILL_DIR"/*.md 2>/dev/null || true)"
    if [ -n "$matches" ]; then
        echo "FAIL: venue conditional pattern '$pattern' found:"
        echo "$matches"
        ERRORS=$((ERRORS + 1))
    else
        echo "  OK: no '$pattern' matches"
    fi
done

# --- Gate 3: Local regression suite -------------------------------------------

echo "=== Gate 3: Local af-build regression suite ==="
cd "$REPO_ROOT"
if uv run --no-sync python -m pytest agent_factory/tests/ -q --tb=short 2>&1; then
    echo "  OK: agent_factory tests pass"
else
    echo "FAIL: agent_factory tests have regressions"
    ERRORS=$((ERRORS + 1))
fi

# --- Summary ------------------------------------------------------------------

echo ""
if [ "$ERRORS" -eq 0 ]; then
    echo "R44 local-venue guard: ALL GATES PASS"
    exit 0
else
    echo "R44 local-venue guard: $ERRORS GATE(S) FAILED"
    exit 1
fi
