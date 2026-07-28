#!/usr/bin/env bash
# One fresh `claude` session per ticket, ALWAYS — not just on stall.
#
# af-build's inline (non-Workflow) fallback path keeps every ticket in ONE growing
# CLI session, and that session's context has twice now hit 100% mid-build (once on
# a 4-ticket tail, once on a single ticket) — each time losing progress that had to
# be manually salvaged, a lease released, and the session restarted by hand. Rather
# than detect and react to exhaustion, this driver prevents it structurally: it
# never lets a session accumulate more than one ticket's worth of context.
#
# Driven from an EC2 devbox (see .claude/commands/af-build-remote-jobs.md), one
# instance per project, each in its own tmux session named `af-<worktree-basename>`.
#
# v2: two reliability fixes made 2026-07-28 after two real incidents:
#  - The old fixed `sleep 40` before sending the ticket prompt raced Claude's own
#    startup twice tonight: when the CLI took longer than 40s to reach its REPL,
#    the ticket instructions (which contain literal parentheses) landed on a bare
#    bash prompt instead and blew up with a syntax error, leaving the session dead
#    at a shell prompt that the old stall-detector couldn't distinguish from "still
#    working" (a non-empty pane). Replaced with a poll for Claude's own ready
#    marker, capped with a generous fallback so a slow-starting box still works.
#  - The old stall-detector only caught three cases: ticket finished, pane totally
#    empty, or specific context/auth error strings. A session that hangs mid-tool-call
#    (network stall, waiting on a container that never responds, etc.) shows a
#    perfectly normal-looking non-empty pane and was invisible to all three checks —
#    one ticket burned the FULL 1-hour timeout doing nothing after its pane stopped
#    updating at the 8-minute mark. Added a content-unchanged-for-N-polls stall
#    check as a fourth exit condition, so a genuinely frozen session gets caught in
#    minutes instead of an hour.
#
# v3: model-backend selection moved OUT of the machine-wide shell environment and INTO
# this driver, made 2026-07-28. Backend used to be chosen by ~/.claude-backend.sh, sourced
# from ~/.bashrc off a ~/.af-backend marker file, which meant three bad properties:
#  - It was a MACHINE-WIDE mutation with no natural undo. Flipping the fleet to sonnet for
#    one run left every future shell on the box (and every other project's loop) on sonnet
#    until someone remembered to flip it back. "Remembered to flip it back" is not a
#    control surface; it is a way to discover next month that a paid subscription drained
#    into a background job nobody was watching.
#  - It was INVISIBLE from the thing it controlled. The only way to know which backend a
#    running loop had picked up was to attach to its TUI and squint, because the choice was
#    made in a shell rc file that had already exited by the time the loop logged anything.
#  - Its half-states were SILENT and expensive-in-the-wrong-direction. A leftover
#    ANTHROPIC_BASE_URL pointing at DeepSeek does not error when you ask for "sonnet"; it
#    cheerfully routes every request to DeepSeek and returns plausible output, so a run you
#    believed was Sonnet 5 was actually deepseek-v4-pro and nothing anywhere said so.
#    The mirror-image failure (leftover subscription token, no base URL) bills real money.
# So the backend is now resolved ONCE, here, and applied PER LAUNCHED SESSION as an explicit
# env preamble on the `claude` command line — which runs AFTER the tmux shell has sourced
# ~/.bashrc, and therefore overrides whatever the machine-wide file did rather than hoping
# it agrees. Each branch sets its own variables AND unsets the other branch's, so there is
# no reachable half-configured state. Resolution order and the deliberately-cheap default:
#
#     AF_MODEL_BACKEND=deepseek|sonnet   (env var, per-run, wins)
#       else contents of ~/.af-backend   (the existing `af-backend` command still works)
#       else deepseek
#
# Anything unrecognized — empty, typo'd, "Sonnet", "sonet" — logs a warning and falls back
# to DEEPSEEK, never to the subscription. A typo must cost nothing; only an exact, spelled
# request may spend money.
#
# Verify BEFORE committing to a multi-hour run:
#
#     ./af-ticket-loop.sh --check                    # prints resolved backend + preflight
#     AF_MODEL_BACKEND=sonnet ./af-ticket-loop.sh --check
#
# and the resolved backend is also logged as the first line of every real run, so any log
# tail answers "what was this actually billing?" without attaching to a TUI.
#
# Usage: af-ticket-loop.sh <project> <worktree> <pg_port> <redis_port> [max_tickets]
#        af-ticket-loop.sh --check
set -euo pipefail

# ---------------------------------------------------------------- backend selection ----
DEEPSEEK_KEY_FILE="$HOME/.deepseek_key"
OAUTH_TOKEN_FILE="$HOME/.claude/oauth-token.sh"
CREDENTIALS_FILE="$HOME/.claude/.credentials.json"

resolve_backend(){   # -> BACKEND, CLAUDE_LAUNCH, BACKEND_NOTE; nonzero if preflight fails
  local requested
  requested="${AF_MODEL_BACKEND:-}"
  [ -n "$requested" ] || [ ! -r "$HOME/.af-backend" ] || requested="$(tr -d ' \n\r' < "$HOME/.af-backend")"
  [ -n "$requested" ] || requested="deepseek"

  BACKEND="$requested"
  case "$BACKEND" in
    sonnet|deepseek) ;;
    *) echo "[backend] WARNING: unrecognized backend '$BACKEND' — falling back to deepseek (never to a paid subscription)" >&2
       BACKEND="deepseek" ;;
  esac

  if [ "$BACKEND" = "sonnet" ]; then
    # Subscription mode. ANTHROPIC_BASE_URL and ANTHROPIC_AUTH_TOKEN must be UNSET, not
    # merely overridden — a surviving DeepSeek base URL silently reroutes "sonnet" to
    # DeepSeek with no error at all, which is the exact confusion this whole block exists
    # to make impossible. ANTHROPIC_API_KEY is unset too: if it were set the CLI would bill
    # pay-as-you-go API credits instead of the subscription, which is a different bill.
    BACKEND_NOTE="Anthropic subscription (Claude Max), model=sonnet — spends Claude quota, NOT API credits"
    if [ ! -r "$OAUTH_TOKEN_FILE" ] && [ ! -r "$CREDENTIALS_FILE" ]; then
      echo "[backend] FATAL: sonnet requested but no subscription credential on this box." >&2
      echo "[backend]   expected $OAUTH_TOKEN_FILE (long-lived token) or $CREDENTIALS_FILE (interactive login)." >&2
      echo "[backend]   fix, once, as ec2-user:  claude setup-token   # then paste into $OAUTH_TOKEN_FILE as: export CLAUDE_CODE_OAUTH_TOKEN=..." >&2
      echo "[backend]   or:                      claude   ->  /login" >&2
      return 1
    fi
    CLAUDE_LAUNCH="unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_MODEL ANTHROPIC_API_KEY; [ -r \$HOME/.claude/oauth-token.sh ] && . \$HOME/.claude/oauth-token.sh; claude --model sonnet --dangerously-skip-permissions"

    # The credential FILE existing proves nothing — a long-lived setup-token can be revoked
    # or expire while the file sits there looking healthy, and the failure surfaces as every
    # ticket dying at the REPL with "please run /login". Spend one throwaway prompt proving
    # the credential is live before committing hours to it. Only an explicit auth rejection
    # is fatal; a network blip or timeout is a warning, so a flaky moment can't block a
    # run whose credential is actually fine.
    local probe
    probe="$(cd /tmp && bash -c "unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_MODEL ANTHROPIC_API_KEY; [ -r \$HOME/.claude/oauth-token.sh ] && . \$HOME/.claude/oauth-token.sh; timeout 120 claude --model sonnet -p 'Reply with exactly: PONG'" 2>&1 || true)"
    if printf '%s' "$probe" | grep -qiE 'please run /login|invalid api key|authentication_error|oauth.*invalid|401'; then
      echo "[backend] FATAL: sonnet credential present but REJECTED by Anthropic:" >&2
      printf '%s\n' "$probe" | head -3 | sed 's/^/[backend]   /' >&2
      echo "[backend]   fix, once, as ec2-user:  claude setup-token   # then write into $OAUTH_TOKEN_FILE as: export CLAUDE_CODE_OAUTH_TOKEN=..." >&2
      echo "[backend]   or:                      claude   ->  /login" >&2
      return 1
    fi
    printf '%s' "$probe" | grep -q 'PONG' || echo "[backend] WARNING: sonnet auth probe returned no PONG and no auth error (network/transient?) — continuing" >&2
  else
    # DeepSeek mode. The subscription token is unset rather than out-ranked: the CLI has
    # been observed preferring a cached credential over an env token, so exclusivity is
    # enforced instead of assumed. The key is read from the file BY THE REMOTE SHELL at
    # launch time (note the escaped $(...)) so the secret never appears in this script's
    # log, in the tmux pane, or in the shell history of the session it starts.
    BACKEND_NOTE="DeepSeek direct, model=${AF_DEEPSEEK_MODEL:-deepseek-v4-pro} — spends DeepSeek prepaid balance, zero Claude quota"
    if [ ! -r "$DEEPSEEK_KEY_FILE" ] || [ ! -s "$DEEPSEEK_KEY_FILE" ]; then
      echo "[backend] FATAL: deepseek requested but $DEEPSEEK_KEY_FILE is missing or empty." >&2
      echo "[backend]   fix: printf %s 'sk-...' > $DEEPSEEK_KEY_FILE && chmod 600 $DEEPSEEK_KEY_FILE" >&2
      return 1
    fi
    CLAUDE_LAUNCH="unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY; export ANTHROPIC_BASE_URL=${AF_DEEPSEEK_BASE_URL:-https://api.deepseek.com/anthropic}; export ANTHROPIC_MODEL=${AF_DEEPSEEK_MODEL:-deepseek-v4-pro}; export ANTHROPIC_AUTH_TOKEN=\"\$(tr -d ' \\n\\r' < \$HOME/.deepseek_key)\"; claude --dangerously-skip-permissions"
  fi
  return 0
}

if [ "${1:-}" = "--check" ]; then
  marker='<absent>'; [ -r "$HOME/.af-backend" ] && marker="$(tr -d ' \n\r' < "$HOME/.af-backend")"
  echo "requested   : AF_MODEL_BACKEND=${AF_MODEL_BACKEND:-<unset>}  ~/.af-backend=$marker"
  if resolve_backend; then
    echo "resolved    : $BACKEND"
    echo "billing     : $BACKEND_NOTE"
    echo "launch cmd  : $CLAUDE_LAUNCH"
    echo "preflight   : OK"
    exit 0
  fi
  echo "resolved    : ${BACKEND:-?}"
  echo "preflight   : FAILED (see above) — a real run would refuse to start"
  exit 1
fi

PROJECT="$1"; WT="$2"; PG="$3"; REDIS="$4"; MAX="${5:-999}"
SESSION="af-$(basename "$WT")"   # per-worktree, so concurrent projects never collide on tmux session name
LOG="/workspace/af-ticket-loop.log"
PY=/workspace/praxis/.venv/bin/python
export PYTHONPATH=/workspace/praxis/agent_factory/hooks:/workspace/praxis/agent_factory/src

# How many consecutive 30s polls the pane may go completely unchanged before we
# treat the session as frozen rather than quietly working (10 * 30s = 5 minutes).
# A session that's genuinely still working almost always has SOME live-updating
# text (a growing "Thinking for Ns..." timer, streamed tool output) inside any
# 5-minute window; total pixel-for-pixel stillness that long is a strong signal
# nothing is happening, not just a quiet stretch.
# Must stay ABOVE the longest tool timeout the agent itself uses, or this reaps healthy work.
# Nothing is written to the session transcript while a Bash tool call is in flight, so a
# legitimate `npx vitest run` with a 600000ms (10min) timeout is indistinguishable from a
# hang — and af-build really does issue 10-minute test commands (observed 2026-07-28 on
# sotos). 5min (the first attempt) and 10min (the second) both sat at or under that ceiling.
# 15min clears it while still catching a real hang 4x faster than the 1h timeout.
#
# The hangs this catches are genuine and confirmed, not detector noise: one sotos session
# wrote its last transcript entry at 04:51:15 and was still silent when reaped at 05:01:28 —
# 10min13s with no tool call and no token, stalled waiting on a model response that never
# arrived (the upstream API has no client-side timeout here).
STALL_POLLS=30

# How long (2s polls) to wait for Claude's own REPL to actually be ready before
# sending the ticket prompt, instead of a blind fixed sleep. "bypass permissions
# on" is the footer Claude's TUI shows once its input box is live and ready to
# receive text — present in every ready-state pane capture observed on this box.
READY_POLL_MAX=40   # 40 * 2s = 80s cap

# Source the project's pinned Praxis identity so every _praxis call authenticates —
# without this, auth fails closed, stderr is swallowed by claimable()'s redirect,
# and `set -e` kills the whole driver on its very first call with NO log output at
# all (exactly what happened the first time this ran).
eval "$(python3 -c "
import json
d = json.load(open('$WT/.claude/settings.local.json'))['env']
for k in ('PRAXIS_ORG', 'PRAXIS_API_KEY', 'PRAXIS_API_BASE_URL'):
    print('export %s=\"%s\"' % (k, d[k]))
")"

say(){ echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$LOG"; }

# Resolve the backend BEFORE any ticket work, and refuse to start a multi-hour run on a
# half-configured one. Failing here costs seconds; failing three tickets in costs an hour
# and a lease that has to be released by hand.
resolve_backend || { say "FATAL: model backend preflight failed for '${BACKEND:-?}' — refusing to start"; exit 1; }
say "backend=$BACKEND ($BACKEND_NOTE)"

claimable(){  # -> count of incomplete|in_progress for PROJECT
  $PY - "$PROJECT" <<'PYEOF' 2>/dev/null
import sys
sys.path[:0]=['/workspace/praxis/agent_factory/hooks']
import _praxis
p=sys.argv[1]
f=_praxis.facts_by(category='requirement', space=p, snapshot=f'prd-{p}')
print(sum(1 for x in f if ((x.get('meta') or {}).get('build_state')) in ('incomplete','in_progress')))
PYEOF
}

finished_count(){
  $PY - "$PROJECT" <<'PYEOF' 2>/dev/null
import sys
sys.path[:0]=['/workspace/praxis/agent_factory/hooks']
import _praxis
p=sys.argv[1]
f=_praxis.facts_by(category='requirement', space=p, snapshot=f'prd-{p}')
print(sum(1 for x in f if ((x.get('meta') or {}).get('build_state'))=='finished'))
PYEOF
}

release_inprogress(){  # release any live lease before a fresh session claims (post-crash safety)
  $PY - "$PROJECT" <<'PYEOF' 2>/dev/null
import sys, time
sys.path[:0]=['/workspace/praxis/agent_factory/hooks','/workspace/praxis/agent_factory/src']
import _praxis, _ticket_state as ts
p=sys.argv[1]; kw=dict(space=p, snapshot=f'prd-{p}'); owner='af-ticket-loop'
ts.stamp_planning(p, owner)
for f in _praxis.facts_by(category='requirement', **kw):
    m=f.get('meta') or {}
    if m.get('build_state')=='in_progress':
        _praxis.patch_meta(f['id'], {'build_state':'incomplete','claim_owner':None,'claim_at':None,
            'claim_heartbeat_at':None,'claim_lease_ttl':None,
            'audit_disposition':'lease released by af-ticket-loop: prior session ended (context cap or crash), returning to incomplete for a fresh session.'}, **kw)
        print('released', m.get('requirement_id'))
ts.clear_planning(p, owner)
PYEOF
}

commit_wip(){
  cd "$WT"
  if [ -n "$(git status --porcelain)" ]; then
    git add -A
    git -c user.name="af-build" -c user.email="af-build@praxis.local" commit -q -m \
      "wip: preserve in-flight output before per-ticket session restart (af-ticket-loop)"
    say "committed WIP: $(git log --oneline -1)"
  fi
}

n=0
while :; do
  left=$(claimable); left=${left:-999}
  say "$PROJECT claimable=$left"
  [ "$left" = "0" ] && { say "DONE — nothing claimable"; break; }
  n=$((n+1))
  [ "$n" -gt "$MAX" ] && { say "hit max_tickets=$MAX, stopping"; break; }

  before=$(finished_count); before=${before:-0}
  release_inprogress >/dev/null

  tmux kill-session -t "$SESSION" 2>/dev/null || true
  tmux new-session -d -s "$SESSION" -c "$WT"
  # The env preamble runs in the tmux shell AFTER ~/.bashrc has already sourced the
  # machine-wide backend file, so it deliberately overrides that file rather than
  # trusting it to agree with $AF_MODEL_BACKEND.
  tmux send-keys -t "$SESSION" "cd $WT && $CLAUDE_LAUNCH" Enter

  ready=0
  for _ in $(seq 1 "$READY_POLL_MAX"); do
    sleep 2
    pane=$(tmux capture-pane -t "$SESSION" -p 2>/dev/null || echo "")
    if echo "$pane" | grep -qE "bypass permissions on"; then ready=1; break; fi
  done
  [ "$ready" = "0" ] && say "WARNING: claude REPL not confirmed ready after $((READY_POLL_MAX*2))s, sending anyway"

  tmux send-keys -t "$SESSION" "/af-build $PROJECT — full scope. Build EXACTLY ONE ticket end-to-end (the next dependency-ready one), then STOP and report — do NOT continue to a second ticket in this session, even if more remain. Work ONLY on the already-checked-out branch, do NOT push. Postgres localhost:$PG$( [ -n "${REDIS:-}" ] && [ "$REDIS" != "none" ] && echo ", Redis localhost:$REDIS" )."
  sleep 3; tmux send-keys -t "$SESSION" Enter
  say "submitted ticket #$n, waiting for it to finish or stall"

  # Wait for: finished count to increase (success), context exhaustion, auth error,
  # the session to die, OR the pane going completely unchanged for STALL_POLLS polls
  # in a row (a frozen session — see v2 note above).
  waited=0
  same_count=0
  last_hash=""
  while [ "$waited" -lt 3600 ]; do
    sleep 30; waited=$((waited+30))
    now=$(finished_count); now=${now:-$before}
    if [ "$now" -gt "$before" ]; then say "ticket #$n finished ($before -> $now)"; break; fi
    pane=$(tmux capture-pane -t "$SESSION" -p 2>/dev/null || echo "")
    if ! echo "$pane" | grep -qE "."; then say "session gone, ending wait"; break; fi
    if echo "$pane" | grep -qE "100% context used"; then say "context exhausted mid-ticket, ending wait"; break; fi
    # Deliberately NARROW. This used to also match bare "401" and "expired", which occur
    # constantly in ordinary output (line numbers, token counts, diffs, prose) and killed
    # healthy sessions on sight -- one such false positive is in the 2026-07-28 log with
    # the API verified healthy (HTTP 200) at the same moment.
    if echo "$pane" | grep -qiE "please run /login|invalid api key|authentication_error"; then
      say "auth error, ending wait"; break
    fi
    # A BILLING failure is terminal for the whole run, not a per-ticket blip: every
    # subsequent session 402s the instant it starts, so restarting just burns a fresh
    # STALL_POLLS window per ticket forever. Observed 2026-07-28: the DeepSeek balance
    # ran out at 11:08 and the loop churned 46 stall/restart cycles over ~6 HOURS
    # without completing anything, because the auth check above does not match a 402
    # and a 402'd pane is otherwise indistinguishable from a frozen one. Halt loudly.
    if echo "$pane" | grep -qiE "insufficient balance|402|quota exceeded|billing|payment required|credit balance is too low"; then
      say "BILLING FAILURE (out of credits/quota) — halting the whole loop; top up and relaunch"
      commit_wip
      tmux kill-session -t "$SESSION" 2>/dev/null || true
      exit 3
    fi
    pane_hash=$(printf '%s' "$pane" | md5sum | cut -d' ' -f1)
    if [ "$pane_hash" = "$last_hash" ]; then
      same_count=$((same_count+1))
      if [ "$same_count" -ge "$STALL_POLLS" ]; then
        say "pane unchanged for $((STALL_POLLS*30/60))min — treating as frozen/stalled, ending wait"
        break
      fi
    else
      same_count=0
      last_hash="$pane_hash"
    fi
  done
  [ "$waited" -ge 3600 ] && say "ticket #$n timed out after 1h"

  commit_wip
  # Kill ONLY this session's own claude, never every claude on the box. The old blanket
  # `pkill -f "[c]laude --dangerously-skip-permissions"` matched the OTHER concurrently
  # running project's session too, so with two loops up (sotos + appeal_engine) each
  # restart murdered the other's in-flight ticket; that session then sat at a dead bash
  # prompt, tripped the stall check below, restarted, and killed the first one back --
  # a mutual-kill deadlock that burned 50 minutes and finished zero tickets before it
  # was spotted (2026-07-28). Children of this session's pane are exactly this project's.
  # `|| true` is load-bearing: this script runs `set -euo pipefail`, and when the tmux
  # session is ALREADY gone (the "session gone" path, or an operator killing a hung
  # session by hand) `tmux list-panes` fails, pipefail propagates that through the pipe,
  # the command substitution returns non-zero, and `set -e` silently kills the entire
  # loop. That is exactly how both project loops died on 2026-07-28 — each time right
  # after "committed WIP", never reaching "restarting fresh", leaving the project dead
  # until noticed by hand. An absent pane is a NORMAL state here, not an error.
  pane_pid=$(tmux list-panes -t "$SESSION" -F '#{pane_pid}' 2>/dev/null | head -1 || true)
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  [ -n "${pane_pid:-}" ] && pkill -P "$pane_pid" 2>/dev/null || true
  sleep 3
  say "session closed; restarting fresh for the next ticket"
done
say "af-ticket-loop finished for $PROJECT"
