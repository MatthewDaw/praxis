#!/usr/bin/env bash
# Drive a QUEUE of agent-supervised ML campaigns with no human in the loop.
#
# WHY THIS EXISTS, AND WHY THE TWO SIBLINGS ARE NOT ENOUGH.
#   af-ml-campaign-loop.sh drives ONE COMPOSING campaign, where the project's own dispatch script
#     picks the next arm. It cannot drive a campaign whose dispatch is a template like
#     `--arm ARM --tag TAG`, because choosing the arm is judgement -- an agent's job, not a shell's.
#   af-ml-campaign-queue.sh chains those composing loops, and refuses any campaign without one.
#   af-ml-supervise-keepalive.sh keeps ONE agent session working, but somebody still had to launch
#     that session, and when its campaign closed the machine went idle until a human noticed.
# So the gap this fills is the whole lifecycle: LAUNCH the agent, KEEP it working, TEAR IT DOWN at a
# terminal state, and START THE NEXT CAMPAIGN. Measured on detection 2026-08-20: eight arms, a
# person nudged after every one, and nothing would have moved to contact_point afterwards.
#
# THE AGENT IS THE POINT, NOT AN IMPLEMENTATION DETAIL. Across the three live campaigns there are
# about nine implemented arms against 152 backlog ideas. A shell cannot author the other 143. This
# queue exists to keep an ARM-AUTHORING agent fed, which is why it launches grok with the
# /af-ml-supervise skill rather than calling a dispatch script.
#
# WHAT IT REFUSES, and why refusing is the feature: a campaign whose preflight says NOT_READY is
# skipped with its reason rather than started, because starting a campaign whose corpus is missing
# burns an hour of subscription to rediscover a fact the preflight already had. C1 court-marking is
# refused outright -- it is not ready to supervise and must never be queued.
#
# Usage:
#   af-ml-agent-queue.sh [--config FILE] [--only NAME] [--praxis DIR] [--repo DIR]
#                        [--continue-past-incomplete] [--max-nudges N] [--poll N] [--dry-run]
#
# Config lines: name|space-file|model-id|stages          ('#' comments, blank lines ignored)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRAXIS="${PRAXIS:-$(cd "$HERE/../.." && pwd)}"
REPO="${AF_REPO:-/workspace/sports_analysis}"
CONFIG="$HERE/af-ml-agent-queue.conf"
KEEPALIVE="$HERE/af-ml-supervise-keepalive.sh"
PREFLIGHT="$HERE/af-ml-campaign-preflight.sh"
GROK_BIN="${GROK_BIN:-$HOME/.grok/bin/grok}"
AF_ML_BACKEND="${AF_ML_BACKEND:-grok}"
CODEX_BIN="${CODEX_BIN:-$(command -v codex 2>/dev/null || echo "$HOME/.nvm/versions/node/v22.23.1/bin/codex")}"
AF_CODEX_MODEL="${AF_CODEX_MODEL:-gpt-5.6-sol}"
AF_GROK_MODEL="${AF_GROK_MODEL:-grok-4.6}"
AF_OMP_NUM_THREADS="${AF_OMP_NUM_THREADS:-7}"
ONLY=""; CONTINUE=0; DRY=0; MAX_NUDGES=40; POLL=60

while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2;;
    --only) ONLY="$2"; shift 2;;
    --praxis) PRAXIS="$2"; shift 2;;
    --repo) REPO="$2"; shift 2;;
    --continue-past-incomplete) CONTINUE=1; shift;;
    --max-nudges) MAX_NUDGES="$2"; shift 2;;
    --poll) POLL="$2"; shift 2;;
    --dry-run) DRY=1; shift;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done

say() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [agent-queue] $*"; }

[ -r "$CONFIG" ] || { echo "no config at $CONFIG" >&2; exit 2; }
[ -x "$KEEPALIVE" ] || { echo "keepalive not executable at $KEEPALIVE" >&2; exit 2; }

# C1 is refused for the whole table before anything launches, not skipped mid-run: discovering a
# forbidden campaign an hour in is worse than refusing the table up front.
#
# TODO(P-2, CODEX-ML-CAMPAIGN-FOUNDATION-AUDIT-PLAN.md #7.1): model-16659b6b8e45 duplicates the
# `refused` map in the versioned sports CampaignSpec manifest at
# sports_analysis/src/sports_analysis/experimentation/portfolio/preflight_manifest.json, which
# knowledge/ml_registry/preflight.py already reads generically. This check stays a literal string
# match until af-ml-agent-queue.sh itself is retired under P-3 -- keep both in sync by hand.
while IFS='|' read -r name space model stages; do
  case "${name// /}" in ''|\#*) continue;; esac
  if [ "${model// /}" = "model-16659b6b8e45" ] || printf '%s' "$name" | grep -qi court; then
    echo "REFUSED: $name is C1 court-marking, which is NOT READY to supervise and must never be queued." >&2
    exit 2
  fi
done < "$CONFIG"

# One agent at a time. Two campaigns share the box's CPU and each holds registry locks; running
# them concurrently makes both slower and their throughput numbers incomparable.
LOCK="${AF_QUEUE_LOCK:-/tmp/af-ml-agent-queue.lock}"
if ! mkdir "$LOCK" 2>/dev/null; then
  holder="$(cat "$LOCK/pid" 2>/dev/null || echo '?')"
  if [ "$holder" != "?" ] && kill -0 "$holder" 2>/dev/null; then
    echo "another af-ml-agent-queue holds $LOCK (pid $holder)" >&2; exit 7
  fi
  say "clearing stale lock $LOCK (pid $holder is gone)"; rm -rf "$LOCK"; mkdir "$LOCK" || exit 7
fi
echo "$$" > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

launch_agent() { # $1=session $2=cwd $3=prompt
  local ready_pat
  if [ "$AF_ML_BACKEND" = "codex" ]; then
    # OPENAI_API_KEY is unset with the rest ON PURPOSE. codex prefers an API key when one is
    # present, which silently spends usage-based credits while the pane still looks like a normal
    # subscription session -- the bill is the only place the difference shows up.
    tmux new-session -d -s "$1" -c "$2" \
      "unset OPENAI_API_KEY XAI_API_KEY GROK_CODE_XAI_API_KEY ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_MODEL CLAUDE_CODE_OAUTH_TOKEN; export OMP_NUM_THREADS=$AF_OMP_NUM_THREADS; $CODEX_BIN --model $AF_CODEX_MODEL --dangerously-bypass-approvals-and-sandbox 2>&1 | tee -a /workspace/$1.log"
    # Verified 2026-08-20: this placeholder is painted once codex accepts input. It is NOT usable
    # as an idle signal afterwards -- it stays on screen mid-turn (see session_idle in the
    # keepalive) -- but for FIRST-PAINT it is the right marker.
    ready_pat="Ask Codex to do anything"
  else
    tmux new-session -d -s "$1" -c "$2" \
      "unset XAI_API_KEY GROK_CODE_XAI_API_KEY ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_MODEL CLAUDE_CODE_OAUTH_TOKEN; export PATH=\"\$HOME/.grok/bin:\$PATH\"; $GROK_BIN --model $AF_GROK_MODEL --always-approve 2>&1 | tee -a /workspace/$1.log"
    ready_pat="always-approve"
  fi
  # both paint a first-run splash before accepting input; sending into that loses the message.
  for _ in $(seq 1 30); do
    sleep 2
    tmux capture-pane -p -t "$1" 2>/dev/null | grep -q "$ready_pat" && break
  done
  sleep 3
  tmux send-keys -t "$1" "$3"
  sleep 3
  # Enter goes SEPARATELY: grok's bracketed paste swallows one sent in the same call, leaving the
  # prompt populated but unsent -- which looks exactly like a working agent that never starts.
  tmux send-keys -t "$1" Enter
}

overall=0; ran=0; done_ok=0
while IFS='|' read -r name space model stages; do
  name="${name// /}"; space="${space// /}"; model="${model// /}"; stages="${stages// /}"
  case "$name" in ''|\#*) continue;; esac
  [ -z "$ONLY" ] || [ "$ONLY" = "$name" ] || continue

  say "$name: preflight"
  if [ -x "$PREFLIGHT" ]; then
    # Capture the status on its own line: `if ! out="$(...)"` discards it, and a queue that reports
    # "NOT READY (preflight exit 0)" cannot tell a missing corpus from a missing repo.
    out="$("$PREFLIGHT" --campaign "$name" --repo "$REPO" --praxis "$PRAXIS" 2>&1)"; rc=$?
    if [ "$rc" != 0 ]; then
      # NO_RUNNABLE_ARMS (4) is NOT a reason to skip an agent-driven campaign: authoring the next
      # arm is precisely what the agent is for. A shell loop would stall here; an agent should not.
      if [ "$rc" = "4" ]; then
        say "$name: no implemented arm left -- the agent will author one. Proceeding."
      else
        say "$name: NOT READY (preflight exit $rc) -- skipping without launching."
        printf '%s\n' "$out" | tail -5 | sed 's/^/    /'
        overall=1
        [ "$CONTINUE" = 1 ] || { say "STOPPING at $name. Fix it, or pass --continue-past-incomplete."; break; }
        continue
      fi
    fi
  else
    say "$name: no preflight at $PREFLIGHT -- proceeding blind."
  fi

  session="ml-supervise-$name"
  prompt="/af-ml-supervise ${name} campaign: ${model}, space ${space}, ledger ${REPO}/ml/${name}/results.tsv, praxis ${PRAXIS}, repo ${REPO}. Work the backlog one arm at a time: claim an idea, dispatch ONE arm, adjudicate it against the ledger, resolve the verdict, then continue to the next WITHOUT waiting to be asked. When no implemented arm remains, author the next one from ml/${name}/backlog.jsonl rather than stopping. Never touch the frozen eval. Snapshot with ${REPO}/scripts/backup_campaign_state.sh around dispatches. Do not commit or push. There is NO GPU on this box, so do not start a full fine-tune. If the campaign is finished, or blocked in a way more arms cannot fix, say so explicitly and stop."

  if [ "$DRY" = 1 ]; then
    say "$name: DRY RUN -- would launch tmux '$session' and keepalive it"; ran=$((ran+1)); continue
  fi

  tmux kill-session -t "$session" 2>/dev/null
  # Persist the prompt: the keepalive re-sends it verbatim when it cycles a context-exhausted
  # session, so the restarted agent re-enters the SAME campaign contract rather than a paraphrase.
  prompt_file="/tmp/af-ml-prompt-$name.txt"
  printf '%s' "$prompt" > "$prompt_file"
  say "$name: launching agent session $session ($([ "$AF_ML_BACKEND" = codex ] && echo "$AF_CODEX_MODEL" || echo "$AF_GROK_MODEL"), OMP_NUM_THREADS=$AF_OMP_NUM_THREADS)"
  launch_agent "$session" "$REPO" "$prompt"
  ran=$((ran+1))

  say "$name: keepalive attached; this blocks until the campaign reaches a terminal state"
  "$KEEPALIVE" --session "$session" --ledger "$REPO/ml/$name/results.tsv" \
      --space-file "$space" --model-id "$model" --stages "$stages" \
      --praxis "$PRAXIS" --poll "$POLL" --max-nudges "$MAX_NUDGES" \
      --watch-dir "$REPO/src" \
      --relaunch-prompt-file "$prompt_file" --relaunch-cwd "$REPO" --context-pct 75
  rc=$?

  # The session is torn down whatever happened: a finished campaign's session left alive holds a
  # subscription seat and confuses the next run's session-name check.
  tmux kill-session -t "$session" 2>/dev/null

  # THE GATE. Advancing on the keepalive's return code alone would trust a process that could have
  # exited 0 for a reason unrelated to the campaign -- a race in its own poll, a tmux hiccup, a
  # future edit to its exit codes. So completion is RE-ASKED of the registry itself, which is the
  # only thing that actually knows: campaign-complete exits 0 when every declared stage is populated
  # and closed, no arm awaits a re-run, and the winner is trained to convergence. An empty queue is
  # not a finished campaign, and neither is a keepalive that happened to stop.
  if [ "$rc" = 0 ]; then
    if (cd "$PRAXIS" && PRAXIS_DB_DISABLED=1 uv run python -m knowledge.ml_registry.cli \
          campaign-complete --space-file "$space" --model-id "$model" --stages "$stages" >/dev/null 2>&1); then
      say "$name: COMPLETE -- confirmed by campaign-complete, not merely by the keepalive exiting."
    else
      say "$name: keepalive exited 0 but campaign-complete DISAGREES. Treating as NOT finished."
      say "$name: refusing to advance on a completion the registry will not confirm."
      rc=6
    fi
  fi

  case "$rc" in
    0) done_ok=$((done_ok+1));;
    3) say "$name: session GONE -- the agent died. See /workspace/$session.log."; overall=1;;
    4) say "$name: nudge BUDGET spent without closing. Still open; re-run to continue."; overall=1;;
    5) say "$name: STUCK -- idle across repeated nudges with no new trial. Read the pane before re-running."; overall=1;;
    6) say "$name: UNCONFIRMED -- the registry does not agree the campaign is finished."; overall=1;;
    8) say "$name: QUOTA -- the model backend refused (weekly/usage limit or auth). Nothing here can"
       say "$name: fix that, and the NEXT campaign would hit the same wall, so the queue stops."
       overall=1;;
    *) say "$name: keepalive exited $rc."; overall=1;;
  esac

  if [ "$rc" != 0 ]; then
    # --continue-past-incomplete deliberately does NOT apply to rc=6. The other outcomes are honest
    # stopping points an operator may choose to step over; rc=6 means the registry contradicts a
    # claim of completion, and stepping over a contradiction is how a queue reports a clean sweep
    # having finished nothing. That one is not overridable by a flag.
    if [ "$rc" = 8 ]; then
      say "STOPPING: out of model credit. Every remaining campaign shares that account."
      break
    fi
    if [ "$rc" = 6 ]; then
      say "STOPPING at $name. A disputed completion is never stepped over, with or without --continue-past-incomplete."
      break
    fi
    if [ "$CONTINUE" != 1 ]; then
      say "STOPPING at $name. An unfinished campaign is not a finished one; pass --continue-past-incomplete to move on anyway."
      break
    fi
  fi
done < "$CONFIG"

say "done: $done_ok/$ran campaign(s) COMPLETE, exit=$overall"
exit "$overall"
