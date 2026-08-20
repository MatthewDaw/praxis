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
#
# AF_HEARTBEAT_INTERVAL_S (default 300) is how often this loop renews the idea claims held by
# the arm it is currently running -- see the heartbeat block below for why it has to.
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

# WHY THIS LOOP HEARTBEATS. An idea claim has a lease (lifecycle.DEFAULT_IDEA_CLAIM_LEASE_TTL_S,
# 900s) and supervisor._reclaim_dead_trial reads an EXPIRED lease as proof the worker died: it
# supersedes that idea's in-flight trial and registers a new one. lifecycle.heartbeat_idea_claim
# exists but nothing called it automatically, so the lease only ever meant "this arm finished
# inside 15 minutes" -- and an arm that merely takes longer than that was indistinguishable from
# a dead one. Association arms run ~50s and never noticed; detection and contact arms, and any
# training arm at all, exceed 900s routinely, and the reclaim then puts TWO runs on the same idea
# racing each other -- the exact failure the one-trial-in-flight rule exists to prevent.
#
# The dispatch wrapper is the right place to fix it because it is the only thing in the system
# that KNOWS the arm is alive: the heartbeat runs only between start_heartbeat and
# stop_heartbeat, i.e. only while $AF_DISPATCH is actually running, so it can never keep a dead
# worker's claim live -- the moment the command returns (or the loop exits, or is killed) the
# lease starts expiring again and _reclaim_dead_trial goes back to being correct.
MAIN_PID="$BASHPID"
HEARTBEAT_INTERVAL_S="${AF_HEARTBEAT_INTERVAL_S:-300}"
HEARTBEAT_PID=""

# Every claimed idea of THIS model, as "<idea-id><TAB><claim-owner>". Read straight off the space
# file rather than through the CLI: this is a read, it must not take the flock the claim writes
# serialize on, and a torn read is simply skipped -- a missed beat costs one interval, and the
# next one lands well inside the lease.
claimed_ideas () {
  (cd "$PRAXIS" && python3 -c '
import json, sys
try:
    facts = json.load(open(sys.argv[1]))["facts"]
except Exception:
    sys.exit(0)
for f in facts:
    m = f.get("meta", {})
    if f.get("category") == "idea" and m.get("model_id") == sys.argv[2] and m.get("status") == "claimed":
        print("%s\t%s" % (f.get("id", ""), m.get("claim_owner", "")))
' "$SPACE" "$MODEL" 2>/dev/null)
}

heartbeat_until_killed () {
  while :; do
    sleep "$HEARTBEAT_INTERVAL_S"
    # A killed parent must not leave this beating: an orphan heartbeat renews the claim of a
    # worker nobody is running any more, which is the one thing worse than no heartbeat at all.
    kill -0 "$MAIN_PID" 2>/dev/null || exit 0
    claimed_ideas | while IFS=$'\t' read -r idea owner; do
      [ -n "$idea" ] && [ -n "$owner" ] || continue
      registry heartbeat-idea-claim --space-file "$SPACE" --idea-id "$idea" --owner "$owner" \
        >/dev/null 2>&1 || true
    done
  done
}

start_heartbeat () { heartbeat_until_killed & HEARTBEAT_PID=$!; }

stop_heartbeat () {
  # Guarded on BASHPID because every command substitution in this script runs in a subshell that
  # inherits $HEARTBEAT_PID: without this, a subshell firing the EXIT trap would kill the live
  # heartbeat of a dispatch still running in the parent.
  [ "$BASHPID" = "$MAIN_PID" ] || return 0
  [ -n "$HEARTBEAT_PID" ] || return 0
  kill "$HEARTBEAT_PID" 2>/dev/null
  wait "$HEARTBEAT_PID" 2>/dev/null
  HEARTBEAT_PID=""
}

trap stop_heartbeat EXIT
trap 'stop_heartbeat; exit 130' INT
trap 'stop_heartbeat; exit 143' TERM

# A campaign whose win_condition is the bare "beats baseline by noise_floor" string closes WON on
# its FIRST adopted trial, every other declared stage untried -- and only bootstrap ever checked
# for it, so a model registered through register-model (or register-model-with-baseline, or
# either CLI verb) carries it unexamined. Reproduced: register-model with that string succeeded
# and supervise-campaign closed the campaign after ONE dispatch on a +1.2-floor adoption. That is
# a BLOCKED diagnosis in this loop's sense -- running more arms cannot fix it, and an unattended
# loop is exactly where a one-trial "win" goes unnoticed -- so it is refused here, before trial
# one, while declaring what winning means still costs one line.
if ! (cd "$PRAXIS" && uv run python -c '
import sys
from pathlib import Path
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.supervisor import check_win_on_adoption_declared
from knowledge.ml_registry.write_path import RegistrySpace
model = RegistrySpace.load(Path(sys.argv[1])).get(sys.argv[2])
if model is None:
    sys.exit(0)   # a missing model is campaign-status/campaign-complete business, not this gate
try:
    check_win_on_adoption_declared(model)
except RegistryValidationError as refusal:
    print("  win_condition: %s" % refusal)
    sys.exit(1)
' "$SPACE" "$MODEL"); then
  echo "[progress] campaign-loop BLOCKED before iteration 1 -- running more arms cannot fix this"
  exit 3
fi

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
  start_heartbeat
  eval "$AF_DISPATCH"
  stop_heartbeat
  after="$(trials)"

  if [ "$before" = "$after" ]; then
    # "No new trial" has two very different causes and reporting them alike wastes an operator's
    # time. Either the dispatch is broken, or there is simply NO ELIGIBLE WORK LEFT -- and
    # campaign-complete already knows which, so ask it rather than guessing.
    #
    # Measured: a campaign exhausted its runnable backlog and the loop reported STALLED, which
    # reads as a broken dispatch. The real answer -- two stages had no arms authored, and nothing
    # had been trained to convergence -- was one command away and went unsaid for hours.
    echo "[progress] campaign-loop produced no new trial at iteration $i. Why:"
    registry campaign-complete --space-file "$SPACE" --model-id "$MODEL" --stages "$STAGES" \
      2>&1 | sed "s/^/           /"
    echo "[progress] campaign-loop STOPPING: nothing further can run without authoring work above."
    exit 4
  fi
done
echo "[progress] campaign-loop hit --max-iterations $MAX_ITER without completing"
exit 5
