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
# Usage: af-ticket-loop.sh <project> <worktree> <pg_port> <redis_port> [max_tickets]
set -euo pipefail

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
STALL_POLLS=10

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
  tmux send-keys -t "$SESSION" "cd $WT && claude --dangerously-skip-permissions" Enter

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
    if echo "$pane" | grep -qiE "please run /login|401|expired"; then say "auth error, ending wait"; break; fi
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
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  pkill -f "[c]laude --dangerously-skip-permissions" 2>/dev/null || true
  sleep 3
  say "session closed; restarting fresh for the next ticket"
done
say "af-ticket-loop finished for $PROJECT"
