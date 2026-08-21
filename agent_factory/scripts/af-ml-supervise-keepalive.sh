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
#   QUOTA     -- the backend refused: a weekly/usage limit or an auth failure. NOT a stall, and
#                nudging it is pointless -- no number of retries buys credit. Detected on the FIRST
#                poll rather than after a stall budget, because the operator needs to know the
#                difference between "the agent is stuck" and "the account is out".
#   STUCK     -- across --stall-nudges consecutive nudges the agent produced NO ledger row and
#                touched NO source file (see --watch-dir). Nudging harder will not fix a campaign
#                with nothing left to run, and a keepalive that spins on a dead campaign burns a
#                subscription while looking busy. Authoring counts as progress -- see below.
#
# Usage:
#   af-ml-supervise-keepalive.sh --session ml-supervise-detection \
#       --ledger /workspace/sports_analysis/ml/detection/results.tsv \
#       --space-file /workspace/sports_analysis/ml/detection/registry/space.json \
#       --model-id <model-id-from-sports-CampaignSpec-manifest> \
#       --stages representation,architecture,augmentation,training,tuning,capacity \
#       [--praxis /workspace/praxis] [--poll 60] [--idle-polls 3] \
#       [--max-nudges 50] [--stall-nudges 6] [--message-file FILE]
#       [--watch-dir DIR]   # source tree whose writes count as progress; without it, only the
#                           # ledger does, which mistakes arm-authoring for a stall
set -uo pipefail

PRAXIS="${PRAXIS:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SESSION=""; LEDGER=""; SPACE=""; MODEL=""; STAGES=""; MSG_FILE=""; WATCH_DIR=""
RELAUNCH_FILE=""; CONTEXT_PCT=75; GROK_BIN="${GROK_BIN:-$HOME/.grok/bin/grok}"
# BACKEND selects the pane vocabulary used by every check below. grok and codex paint DIFFERENT
# status bars, and a check written for one silently never matches on the other -- which is the
# dangerous direction: a busy-test that never matches reports a WORKING agent as idle, forever.
AF_ML_BACKEND="${AF_ML_BACKEND:-grok}"
CODEX_BIN="${CODEX_BIN:-$(command -v codex 2>/dev/null || echo "$HOME/.nvm/versions/node/v22.23.1/bin/codex")}"
AF_GROK_MODEL="${AF_GROK_MODEL:-grok-4.6}"; CWD=""
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
    --relaunch-prompt-file) RELAUNCH_FILE="$2"; shift 2;;
    --relaunch-cwd) CWD="$2"; shift 2;;
    --context-pct) CONTEXT_PCT="$2"; shift 2;;
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

# A QUOTA WALL LOOKS EXACTLY LIKE A HEALTHY SESSION to every other check here: the tmux session is
# alive, the context is intact, the status bar still renders, and grok keeps its busy markers while
# parked on a "Buy more credits" dialog. Measured 2026-08-20: work stopped at 14:51 and the loop
# went on nudging until 15:08 -- and a human reading ledger COUNT rather than the pane reported the
# run as healthy ten minutes into the outage. Row counts are historical; they say what HAS happened,
# never what IS happening. So this is checked first and exits immediately.
session_blocked_on_quota() {
  local pane
  pane="$(tmux capture-pane -p -t "$SESSION" 2>/dev/null)" || return 1
  printf '%s' "$pane" | grep -qiE "weekly limit|buy more credits|upgrade tier|usage limit|out of credits|rate limit exceeded|authentication failed|not authenticated" && return 0
  if [ "$AF_ML_BACKEND" = "codex" ]; then
    # AUTHORITATIVE and offline: codex can be running, painting a normal status bar, and still be
    # unable to think because the ChatGPT session lapsed. Verified 2026-08-20: prints
    # "Logged in using ChatGPT" and exits 0 when healthy.
    "$CODEX_BIN" login status >/dev/null 2>&1 || return 0
    # UNVERIFIED against a real codex rate-limit pane -- no exhausted codex session was available
    # when this was written, so these strings are inferred, not measured. The login check above is
    # the load-bearing one; treat a miss here as expected until someone confirms the real text.
    printf '%s' "$pane" | grep -qiE "hit your usage limit|try again later|quota exceeded" && return 0
  fi
  return 1
}

# BUSY IS NOT ONE STRING, and getting this wrong nudges a working agent.
# "Esc:cancel" appears while a TURN is in flight, but grok also runs tool calls with the turn
# already returned, showing "N command(s) still running" instead -- and it QUEUES anything typed
# while that is true. Measured on detection 2026-08-20: the Esc:cancel-only test read a working
# agent as idle and piled up FOUR queued nudges, each of which would land later as a redundant
# instruction in a context already at 237K/500K. A queued nudge is worse than a missed one: it is
# not a no-op, it is future noise the agent must read and reconcile.
# So: any of these means DO NOT SEND.
session_idle() {
  local pane
  pane="$(tmux capture-pane -p -t "$SESSION" 2>/dev/null)" || return 1
  if [ "$AF_ML_BACKEND" = "codex" ]; then
    # CODEX INVERTS THE GROK RULE. grok shows its prompt only when idle, so "prompt visible" is a
    # usable ready signal there. codex paints "Ask Codex to do anything" as a PLACEHOLDER that is
    # present mid-turn too -- captured 2026-08-20 from hudl-explode-codex with "Working (1m 00s"
    # on the line directly above it. Porting grok's ready-pattern across would therefore match on
    # every poll and nudge a working agent continuously.
    # The turn marker is "esc to interrupt", inside codex's "Working (...)" line.
    # "background terminal running" is counted BUSY deliberately: codex may sit at an accepting
    # prompt while a background job runs, but the cost asymmetry from the grok incident applies --
    # a queued nudge is future noise the agent must reconcile, a missed one is a no-op.
    printf '%s' "$pane" | grep -qiE "esc to interrupt|background terminal running|Working \(" && return 1
    return 0
  fi
  printf '%s' "$pane" | grep -qE "Esc:cancel|still running|queued|Thinking|Responding" && return 1
  return 0
}

# CONTEXT EXHAUSTION IS A SILENT FAILURE, and it is the one this loop could not previously see.
# grok prints "340K / 500K" in its status bar. As that fills the agent does not crash -- it degrades,
# and a degrading agent still reads as BUSY to every check above, so the keepalive would happily
# nurse a session that had stopped being able to think. Measured on detection 2026-08-20: 237K at
# 10:20, 340K by 10:55 -- roughly 100K per half hour, i.e. under two hours of useful life.
#
# The remedy is a RESTART, not a nudge, and it is cheap here for a specific reason: the campaign's
# state is DURABLE ON DISK. The ledger, the registry space and every registered idea survive the
# session; /af-ml-supervise reloads all of it. What a restart discards is only the conversation,
# which past the threshold is a liability rather than an asset.
#
# It only ever cycles an IDLE session. Killing a session mid-dispatch would lose a running arm and
# leave its idea claimed, which is exactly the two-runs-racing damage the lease exists to prevent.
context_used_pct() {
  local pane used total
  pane="$(tmux capture-pane -p -t "$SESSION" 2>/dev/null)" || { echo 0; return; }
  if [ "$AF_ML_BACKEND" = "codex" ]; then
    # codex does not print a token readout, so there is NOTHING to scrape and cycling cannot fire.
    # Returning 0 here is a real capability gap, not a safe default: the silent-degradation failure
    # this function exists to catch is UNPROTECTED under codex. Said out loud once at startup
    # rather than left to look like a healthy 0%.
    echo 0; return
  fi
  # e.g. "340K / 500K"
  read -r used total <<<"$(printf '%s' "$pane" | grep -oE '[0-9]+K */ *[0-9]+K' | tail -1 | tr -d 'K' | tr '/' ' ')"
  [ -n "${used:-}" ] && [ -n "${total:-}" ] && [ "$total" -gt 0 ] || { echo 0; return; }
  echo $(( used * 100 / total ))
}

cycle_session() {
  local pct="$1"
  if [ -z "$RELAUNCH_FILE" ] || [ ! -r "$RELAUNCH_FILE" ]; then
    say "CONTEXT ${pct}% but no --relaunch-prompt-file given, so this loop cannot restart the agent."
    say "CONTEXT the session will keep degrading. Pass --relaunch-prompt-file to enable cycling."
    return 1
  fi
  say "CONTEXT ${pct}% of window used -- cycling the session. Campaign state is on disk; only the"
  say "CONTEXT conversation is discarded, and past this threshold it is costing more than it carries."
  tmux kill-session -t "$SESSION" 2>/dev/null
  sleep 3
  tmux new-session -d -s "$SESSION" -c "${CWD:-$PWD}" \
    "unset XAI_API_KEY GROK_CODE_XAI_API_KEY ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_MODEL CLAUDE_CODE_OAUTH_TOKEN; export PATH=\"\$HOME/.grok/bin:\$PATH\"; $GROK_BIN --model $AF_GROK_MODEL --always-approve 2>&1 | tee -a /workspace/$SESSION.log"
  local i
  for i in $(seq 1 30); do
    sleep 2
    tmux capture-pane -p -t "$SESSION" 2>/dev/null | grep -q "always-approve" && break
  done
  sleep 3
  tmux send-keys -t "$SESSION" "$(cat "$RELAUNCH_FILE")"
  sleep 3
  send_submit
  say "CONTEXT cycled; fresh session re-entered /af-ml-supervise and reloaded state from the registry."
  return 0
}

# Enter QUEUES while grok is mid-command and only SUBMITS at an empty prompt; Ctrl-Enter sends now.
# Sending the wrong one leaves the message sitting in the box looking delivered -- which cost real
# time today when a steer sat unsent while the campaign ran arms it had been told to skip.
send_submit() {
  tmux send-keys -t "$SESSION" Enter
  sleep 2
  if tmux capture-pane -p -t "$SESSION" 2>/dev/null | grep -qE "Pasted:|Enter:queue|Enter:send now"; then
    tmux send-keys -t "$SESSION" C-Enter
  fi
}

nudges=0; idle_run=0; stall=0; last_progress="$(progress_token)"
say "watching $SESSION; ledger $LEDGER at $(ledger_rows) rows; poll ${POLL}s, idle after $IDLE_POLLS polls; progress = ledger rows OR source writes under ${WATCH_DIR:-<none>}"

say "backend=$AF_ML_BACKEND session=$SESSION"
if [ "$AF_ML_BACKEND" = "codex" ]; then
  # Stated plainly because the failure is invisible: under codex there is no token readout to
  # scrape, so context_used_pct always returns 0 and cycle_session can never fire. A session that
  # degrades from a full context will keep reading BUSY and this loop will keep nursing it.
  say "WARNING: context cycling is DISABLED under codex (no token readout to scrape)."
  say "WARNING: long campaigns need a manual /new or an external turn budget."
fi
while :; do
  sleep "$POLL"

  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    say "GONE: tmux session $SESSION no longer exists after $nudges nudge(s)."; exit 3
  fi

  if session_blocked_on_quota; then
    say "QUOTA: the backend refused -- weekly/usage limit or auth. This is NOT a stall; nudging buys"
    say "QUOTA: nothing. Campaign state is durable on disk (ledger, registry space, ideas), so this"
    say "QUOTA: is safe to resume once credit is restored. Stopping after $nudges nudge(s)."
    exit 8
  fi

  if campaign_complete; then
    say "COMPLETE: campaign-complete exits 0 after $nudges nudge(s). Nothing left to drive."; exit 0
  fi

  pct="$(context_used_pct)"
  if [ "${pct:-0}" -ge "$CONTEXT_PCT" ] && session_idle; then
    cycle_session "$pct" && { nudges=0; idle_run=0; stall=0; last_progress="$(progress_token)"; continue; }
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
  send_submit
done
