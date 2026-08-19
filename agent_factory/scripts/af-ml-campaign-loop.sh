#!/usr/bin/env bash
# Drive a COMPOSING ml_registry campaign to completion without a human between stages.
#
# WHY THIS EXISTS. A composing campaign cannot use `supervise-campaign --dispatch-script`: the arm
# to run next depends on verdicts that do not exist when the script is written. So the project
# drives its own loop -- and every project's loop runs one batch of arms and EXITS. Nothing
# relaunched it, so a human sat in the stage transitions.
#
# Measured on the first real campaign: it completed a partial architecture search and stopped.
# Augmentation, training, tuning and capacity were never reached, and no train-to-convergence
# existed at all. Nothing errored. Each invocation exited 0 having done exactly what it was asked,
# and what it was asked was one stage's worth of arms. AN EMPTY QUEUE IS NOT A FINISHED CAMPAIGN,
# and that is the distinction this loop enforces via `campaign-complete`.
#
# It stops for exactly three reasons, and they are different:
#   COMPLETE  -- campaign-complete exits 0.
#   BLOCKED   -- a diagnosis the loop cannot fix by running more arms (a budget that will truncate
#                every retry, a stage nobody authored). Re-running would burn compute reproducing
#                the same failure, so it stops and says which.
#   STALLED   -- an iteration produced no new trial. Without this a misconfigured dispatch spins
#                forever at zero cost per iteration and looks busy.
#
# Usage:
#   AF_DISPATCH="uv run python -m stroke_lab.campaign --max-arms 8 ..." \
#   af-ml-campaign-loop.sh --space-file <s>.json --model-id <id> \
#       --stages representation,architecture,augmentation,training,tuning,capacity \
#       [--praxis /workspace/praxis] [--max-iterations 40]
set -uo pipefail

PRAXIS="${PRAXIS:-/workspace/praxis}"
MAX_ITER=40
STAGES=""; SPACE=""; MODEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --space-file) SPACE="$2"; shift 2;;
    --model-id) MODEL="$2"; shift 2;;
    --stages) STAGES="$2"; shift 2;;
    --praxis) PRAXIS="$2"; shift 2;;
    --max-iterations) MAX_ITER="$2"; shift 2;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done
: "${AF_DISPATCH:?set AF_DISPATCH to the command that runs ONE batch of arms}"
[ -n "$SPACE" ] && [ -n "$MODEL" ] && [ -n "$STAGES" ] || {
  echo "--space-file, --model-id and --stages are all required" >&2; exit 2; }

registry () { (cd "$PRAXIS" && uv run python -m knowledge.ml_registry.cli "$@"); }
trials () { registry campaign-status --space-file "$SPACE" --model-id "$MODEL" --json 2>/dev/null \
              | python3 -c 'import json,sys; print(json.load(sys.stdin)["trials_total"])' 2>/dev/null || echo 0; }

echo "[progress] campaign-loop starting: stages=$STAGES max_iterations=$MAX_ITER"
for i in $(seq 1 "$MAX_ITER"); do
  if registry campaign-complete --space-file "$SPACE" --model-id "$MODEL" --stages "$STAGES" >/tmp/af-complete.txt 2>&1; then
    echo "[progress] campaign-loop COMPLETE after $((i-1)) iteration(s)"; cat /tmp/af-complete.txt; exit 0
  fi

  # A blocking diagnosis means running more arms cannot help. Stop rather than burn compute
  # reproducing the same failure -- which is what an unguarded retry loop does.
  if registry campaign-status --space-file "$SPACE" --model-id "$MODEL" --json 2>/dev/null \
       | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as exc:                 # a parse failure is NOT a blocking diagnosis
    print("  could not read campaign-status: %s" % exc, file=sys.stderr)
    sys.exit(0)                          # let the loop proceed; campaign-complete still gates it
bad = [x for x in d.get("diagnoses", []) if x.get("severity") == "blocking"]
for x in bad:
    print("  %s: %s" % (x.get("kind"), x.get("detail")))
sys.exit(1 if bad else 0)'; then :; else
    echo "[progress] campaign-loop BLOCKED at iteration $i -- running more arms cannot fix this"
    exit 3
  fi

  before="$(trials)"
  echo "[progress] campaign-loop iteration $i: $(head -1 /tmp/af-complete.txt)"
  eval "$AF_DISPATCH"
  after="$(trials)"

  if [ "$before" = "$after" ]; then
    echo "[progress] campaign-loop STALLED at iteration $i: dispatch produced no new trial "
    echo "           (trials still $after). Nothing will change by repeating it."
    exit 4
  fi
done
echo "[progress] campaign-loop hit --max-iterations $MAX_ITER without completing"
exit 5
