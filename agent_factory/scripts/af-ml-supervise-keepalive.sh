#!/usr/bin/env bash
# Keep an af-ml-supervise session working instead of idling between arms.
#
# WHY THIS EXISTS. A grok session running /af-ml-supervise ends its TURN after each arm -- it
# adjudicates, reports, and returns to the prompt. Nothing is wrong and nothing has crashed; the
# agent is simply waiting for a human that an unattended run does not have. Measured on the
# detection campaign 2026-08-20: eight arms, and a person had to nudge it after every one. A
# campaign that needs a keystroke per arm is not running unattended, it is being hand-cranked.
#
# It is deliberately NOT af-ml-campaign-loop.sh. That loop drives a COMPOSING campaign, where the
# project's own dispatch script picks the next arm. Detection's dispatch is a template
# (`--arm ARM --tag TAG`) because choosing the arm needs judgement -- which is the agent's whole
# job. So this nudges the agent rather than replacing it.
#
# THE IDLE TEST IS THE LOAD-BEARING PART. grok's status bar shows "Esc:cancel" while a turn is in
# flight and drops it at the prompt. Polling that is what distinguishes "thinking for four minutes"
# from "finished and waiting", and getting it wrong in the optimistic direction means interrupting
# a live turn -- which loses the agent's in-flight reasoning. So idle must be observed on
# CONSECUTIVE polls before anything is sent, and a turn that takes an hour is left alone.
#
# It stops for reasons that are different from each other, and says which:
#   COMPLETE  -- campaign-complete exits 0. The real end.
#   BUDGET    -- --max-nudges reached. The loop refuses to run forever unattended.
#   GONE      -- the tmux session died. Nothing to nudge.
#   STUCK     -- across --stall-nudges consecutive nudges the agent produced NO ledger row and
#                touched NO source file (see --watch-dir). Nudging harder will not fix a campaign
#                with nothing left to run, and a keepalive that spins on a dead campaign burns a
#                subscription while looking busy. Authoring counts as progress -- see below.
#
# Usage:
#   af-ml-supervise-keepalive.sh --session ml-supervise-detection \
#       --ledger /workspace/sports_analysis/ml/detection/results.tsv \
#       --space-file /workspace/sports_analysis/ml/detection/registry/space.json \
#       --model-id model-533010b57800 \
#       --stages representation,architecture,augmentation,training,tuning,capacity \
#       [--praxis /workspace/praxis] [--poll 60] [--idle-polls 3] \
#       [--max-nudges 50] [--stall-nudges 6] [--message-file FILE]
#       [--watch-dir DIR]   # source tree whose writes count as progress; without it, only the
#                           # ledger does, which mistakes arm-authoring for a stall
set -uo pipefail

PRAXIS="${PRAXIS:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SESSION=""; LEDGER=""; SPACE=""; MODEL=""; STAGES=""; MSG_FILE=""; WATCH_DIR=""
# STALL_NUDGES is 6, not 3: authoring an arm legitimately spans several turns and several nudges.
POLL=60; IDLE_POLLS=3; MAX_NUDGES=50; STALL_NUDGES=6

while [ $# -gt 0 ]; do
  case "$1" in
    --session) SESSION="$2"; shift 2;;
    --ledger) LEDGER="$2"; shift 2;;
    --space-file) SPACE="$2"; shift 2;;
    --model-id) MODEL="$2"; shift 2;;
    --stages) STAGES="$2"; shift 2;;
    --praxis) PRAXIS="$2"; shift 2;;
    --poll) POLL="$2"; shift 2;;
    --idle-polls) IDLE_POLLS="$2"; shift 2;;
    --max-nudges) MAX_NUDGES="$2"; shift 2;;
    --stall-nudges) STALL_NUDGES="$2"; shift 2;;
    --message-file) MSG_FILE="$2"; shift 2;;
    --watch-dir) WATCH_DIR="$2"; shift 2;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done
[ -n "$SESSION" ] && [ -n "$LEDGER" ] && [ -n "$SPACE" ] && [ -n "$MODEL" ] && [ -n "$STAGES" ] || {
  echo "--session, --ledger, --space-file, --model-id and --stages are all required" >&2; exit 2; }

say() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [keepalive] $*"; }

# The default nudge says as little as possible. The agent already holds the campaign contract from
# /af-ml-supervise; repeating it every arm would crowd the context that the campaign's own history
# needs, and a keepalive that re-argues strategy every sixty seconds starts steering the science.
DEFAULT_MSG="Continue the campaign. Pick up where you left off: claim the next idea, dispatch ONE arm, adjudicate it against the ledger, and resolve the verdict. If every implemented arm is exhausted, author the next one from the backlog rather than stopping. If the campaign is genuinely finished or blocked in a way more arms cannot fix, say so explicitly and stop -- that is a real answer and I will read it."

nudge_text() { if [ -n "$MSG_FILE" ] && [ -r "$MSG_FILE" ]; then cat "$MSG_FILE"; else printf '%s' "$DEFAULT_MSG"; fi; }

ledger_rows() { [ -r "$LEDGER" ] && wc -l < "$LEDGER" | tr -d ' ' || echo 0; }

# PROGRESS IS NOT THE SAME AS A LEDGER ROW, and conflating them declared a working agent dead.
# Measured on detection 2026-08-20: the agent spent ~6 minutes authoring a new arm -- writing
# det_lab/families.py and det_lab/rink_mask.py and pulling model weights -- across several turns,
# returning to the prompt between each. The ledger could not grow until that arm RAN, so a
# ledger-only stall test counted three nudges and gave up. The arm it was writing then scored
# 0.6123 against a 0.6076 baseline: the first arm in the campaign to beat the incumbent, produced
# while the watchdog was calling it stuck. Authoring IS the work, so the newest source mtime counts
# as progress alongside the ledger.
newest_source_mtime() {
  [ -n "$WATCH_DIR" ] && [ -d "$WATCH_DIR" ] || { echo 0; return; }
  find "$WATCH_DIR" -type f \( -name '*.py' -o -name '*.json' -o -name '*.tsv' \) \
       -newermt '-1 day' -printf '%T@\n' 2>/dev/null | sort -rn | head -1 | cut -d. -f1 || echo 0
}

progress_token() { echo "$(ledger_rows):$(newest_source_mtime)"; }

campaign_complete() {
  (cd "$PRAXIS" && PRAXIS_DB_DISABLED=1 uv run python -m knowledge.ml_registry.cli \
      campaign-complete --space-file "$SPACE" --model-id "$MODEL" --stages "$STAGES" >/dev/null 2>&1)
}

# grok keeps "Esc:cancel" in its status bar for the duration of a turn. Absent means the turn ended.
session_idle() {
  local pane
  pane="$(tmux capture-pane -p -t "$SESSION" 2>/dev/null)" || return 1
  printf '%s' "$pane" | grep -q "Esc:cancel" && return 1
  return 0
}

nudges=0; idle_run=0; stall=0; last_progress="$(progress_token)"
say "watching $SESSION; ledger $LEDGER at $(ledger_rows) rows; poll ${POLL}s, idle after $IDLE_POLLS polls; progress = ledger rows OR source writes under ${WATCH_DIR:-<none>}"

while :; do
  sleep "$POLL"

  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    say "GONE: tmux session $SESSION no longer exists after $nudges nudge(s)."; exit 3
  fi

  if campaign_complete; then
    say "COMPLETE: campaign-complete exits 0 after $nudges nudge(s). Nothing left to drive."; exit 0
  fi

  if ! session_idle; then idle_run=0; continue; fi

  idle_run=$((idle_run + 1))
  # Require the idle state on CONSECUTIVE polls: a single sample can catch the gap between a tool
  # result and the next thought, and interrupting there discards the turn's reasoning.
  [ "$idle_run" -ge "$IDLE_POLLS" ] || continue
  idle_run=0

  if [ "$nudges" -ge "$MAX_NUDGES" ]; then
    say "BUDGET: $MAX_NUDGES nudges spent without the campaign closing. Stopping rather than running unattended forever."; exit 4
  fi

  rows="$(ledger_rows)"
  progress="$(progress_token)"
  if [ "$progress" = "$last_progress" ]; then
    stall=$((stall + 1))
    if [ "$stall" -ge "$STALL_NUDGES" ]; then
      say "STUCK: $STALL_NUDGES consecutive nudges with NO ledger row and NO source file touched."
      say "STUCK: not even authoring. Idling for a reason nudging cannot fix -- read the pane."
      exit 5
    fi
  else
    stall=0
  fi
  last_progress="$progress"

  nudges=$((nudges + 1))
  say "idle for $((IDLE_POLLS * POLL))s at $rows ledger rows -- nudge $nudges/$MAX_NUDGES (stall $stall/$STALL_NUDGES)"
  # send-keys the text, then Enter SEPARATELY: grok's bracketed paste swallows a trailing Enter sent
  # in the same call, which leaves the message sitting unsent in the prompt looking like it worked.
  tmux send-keys -t "$SESSION" "$(nudge_text)"
  sleep 3
  tmux send-keys -t "$SESSION" Enter
done
