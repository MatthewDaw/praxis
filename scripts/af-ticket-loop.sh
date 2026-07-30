#!/usr/bin/env bash
# One fresh `claude` session per BATCH of parallelizable tickets, ALWAYS — not just on stall.
# (v1–v3 below describe the per-TICKET session this grew out of; v4 is why it is now per-batch.)
#
# af-build's inline (non-Workflow) fallback path keeps every ticket in ONE growing
# CLI session, and that session's context has twice now hit 100% mid-build (once on
# a 4-ticket tail, once on a single ticket) — each time losing progress that had to
# be manually salvaged, a lease released, and the session restarted by hand. Rather
# than detect and react to exhaustion, this driver prevents it structurally: it
# never lets a session accumulate more than one ticket's worth of context. (v4
# keeps that property while widening a session to one parallel BATCH, because the
# batch's per-ticket work happens in worker subagents, not in the driving session.)
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
# v4: one fresh session per BATCH, not per ticket, made 2026-07-30.
#
# The per-ticket session was a context-exhaustion fix, but it also serialized a build that does not
# need to be serial: af-build already knows how to fan the dependency-ready frontier out across
# parallel isolated worktree workers, and the driver's prompt was the only thing forbidding it
# ("build EXACTLY ONE ticket ... do NOT continue to a second"). A 20-ticket plan whose DAG is 6 wide
# therefore ran as 20 sequential hour-capped sessions instead of ~4 rounds.
#
# So each round now computes the dependency-ready FRONTIER itself and hands af-build the whole batch
# as an explicit id scope, capped at AF_BATCH_MAX (default 15). Because the batch ids ARE the run
# scope, af-build's own completeness gate releases the session exactly when the batch is done — the
# skill's fan-out loop re-queries the frontier filtered to that scope, finds it empty, and stops
# without reaching for the next wave. The context-hygiene property that motivated v1 is preserved:
# a session still dies at a bounded amount of work, and the parallel tickets' context lives in
# per-worker subagents rather than accumulating in the driving session.
#
# Consequences the wait loop had to absorb:
#  - "finished_count went up by one" is no longer round-completion, it is round PROGRESS. Breaking on
#    it would kill a 15-ticket round the moment its first worker landed. Completion is now "every id
#    in the batch is finished or blocked", and each finish instead RESETS the stall counter and
#    extends the deadline, so a healthy long round is never reaped for being long.
#  - The per-round timeout scales with batch size instead of a flat 1h.
#  - Worktrees are swept after every round. af-build is supposed to reap its own, but a run that died
#    mid-round leaves them behind, and 29 stranded worktrees once filled a 98GB volume. Merged ones
#    are removed; unmerged ones are reported loudly and left alone unless AF_REAP_UNMERGED=1, because
#    deleting an unmerged worktree destroys work rather than reclaiming scratch.
#
# Batch width is ALSO capped by the Workflow tool's own concurrency limit, min(16, cores-2), so a
# small box runs a 15-wide batch a few workers at a time. That is a throughput ceiling, not a
# correctness problem. Disk is the real constraint: each worktree is a full checkout plus, if the
# project bootstraps per-worktree deps, a full dependency tree.
#
# v5: post-merge verification of each round, made 2026-07-30.
#
# Batching exposed a gap that serial building hid. Every validation a ticket runs happens inside its
# own isolated worktree, against a tree where its change is the only one present — so with a batch of
# five, five green claims are made about five trees that no longer exist, and NOTHING has executed the
# merged result. Dependency-independent is not semantically independent: two tickets can be green
# alone and broken together, and a merge that resolves without a textual conflict proves nothing about
# behavior. Serially this was survivable because the next ticket immediately re-ran the whole-repo
# gates on the integrated tree; a batch has no such incidental check.
#
# So each round that lands anything is followed by a fresh verification session over the merged tree:
# whole-repo gates, then independent adversarial lenses — integration conflict, per-ticket acceptance
# re-run against the MERGED tree, and test-integrity — and tickets whose work does not survive
# integration are regressed in Praxis so the next round rebuilds them. It borrows the shape of an
# ultracode workflow without being one; the Workflow tool is not reachable from a shell driver, so the
# parallel lenses are subagents inside that session. It builds nothing and pushes nothing.
#
# The verdict returns via a sentinel JSON file outside the repo, parsed with a JSON parser rather than
# grep — a grep for "fail" matches the notes prose and would report a passing round as failed. A
# missing verdict is reported as UNVERIFIED, never silently treated as a pass. AF_VERIFY_ROUND=0
# disables the stage.
#
# Usage: af-ticket-loop.sh <project> <worktree> <pg_port> <redis_port> [max_tickets]
#        af-ticket-loop.sh --check
#        AF_BATCH_MAX=6 af-ticket-loop.sh ...   # narrower rounds on a small box
#        AF_VERIFY_ROUND=0 af-ticket-loop.sh ...   # skip post-merge verification
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
# Largest frontier handed to a single session. 15 is the user-facing cap; the Workflow tool caps
# actual concurrency at min(16, cores-2) underneath it, and disk caps it below that on a small box.
BATCH_MAX="${AF_BATCH_MAX:-15}"
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

ready_batch(){  # -> space-separated ids of the dependency-ready frontier, capped at $2
  $PY - "$PROJECT" "${1:-15}" <<'PYEOF' 2>/dev/null
import sys
sys.path[:0]=['/workspace/praxis/agent_factory/hooks','/workspace/praxis/agent_factory/src']
import _praxis, _ticket_state as ts
p, cap = sys.argv[1], int(sys.argv[2])
# ready_tickets must see the WHOLE requirement set, not just the incomplete slice: it derives the
# "still unfinished" id set from what it is given, so a blocked or in_progress prerequisite that was
# filtered out first would read as satisfied and a dependent ticket would be dispatched too early.
facts = _praxis.facts_by(category='requirement', space=p, snapshot=f'prd-{p}')
out = []
for t in ts.ready_tickets(facts):
    m = t.get('meta') or {}
    rid = m.get('requirement_id') or t.get('id')
    if rid:
        out.append(str(rid))
    if len(out) >= cap:
        break
print(' '.join(out))
PYEOF
}

batch_open(){  # args: ids... -> how many are still incomplete|in_progress (blocked counts as done)
  $PY - "$PROJECT" "$@" <<'PYEOF' 2>/dev/null
import sys
sys.path[:0]=['/workspace/praxis/agent_factory/hooks']
import _praxis
p, want = sys.argv[1], set(sys.argv[2:])
n = 0
for f in _praxis.facts_by(category='requirement', space=p, snapshot=f'prd-{p}'):
    m = f.get('meta') or {}
    ids = {str(f.get('id') or ''), str(m.get('requirement_id') or '')} - {''}
    if (ids & want) and m.get('build_state') in ('incomplete', 'in_progress'):
        n += 1
print(n)
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
      "wip: preserve in-flight output before per-batch session restart (af-ticket-loop)"
    say "committed WIP: $(git log --oneline -1)"
  fi
}

# Sweep the round's worktrees. af-build is told to reap its own, but a session that died mid-round
# leaves them behind and they accumulate across rounds — one observed run stranded 29, each holding a
# full dependency tree, and filled the volume. A worktree whose branch is already an ancestor of the
# integration branch is pure scratch and is removed; one that is NOT merged still holds unintegrated
# commits, so it is reported rather than deleted. Removing a worktree keeps its branch either way.
sweep_worktrees(){
  cd "$WT"
  local kept=0
  while read -r path; do
    [ -n "$path" ] || continue
    [ "$path" = "$WT" ] && continue
    local head; head=$(git -C "$path" rev-parse HEAD 2>/dev/null || echo "")
    if [ -n "$head" ] && git merge-base --is-ancestor "$head" HEAD 2>/dev/null; then
      git worktree remove --force "$path" 2>/dev/null && say "reaped merged worktree $path"
    elif [ "${AF_REAP_UNMERGED:-0}" = "1" ]; then
      git worktree remove --force "$path" 2>/dev/null && say "reaped UNMERGED worktree $path (AF_REAP_UNMERGED=1 — its branch survives)"
    else
      kept=$((kept+1))
      say "WARNING: worktree $path holds UNMERGED commits — left in place. Integrate it, or re-run with AF_REAP_UNMERGED=1 to drop the worktree and keep the branch."
    fi
  done < <(git worktree list --porcelain | awk '/^worktree /{print $2}')
  git worktree prune 2>/dev/null || true
  [ "$kept" -gt 0 ] && say "$kept unmerged worktree(s) still on disk — watch free space"
  return 0
}

# ------------------------------------------------------------------ post-merge round verification --
#
# Every validation a batch ran was run INSIDE an isolated per-ticket worktree, against a tree where
# that ticket's change was the only one present. Nothing has ever executed the MERGED result. That is
# a real gap, not a theoretical one: dependency-independent is not semantically independent — two
# tickets can each be green alone and broken together, one can silently revert the other's edit to a
# shared file, and a merge that resolves without a textual conflict proves nothing about behavior.
#
# So after each round is merged and swept, a FRESH session verifies the integrated tree. It borrows
# the shape of an ultracode workflow — several independent lenses, adversarial rather than
# confirmatory — but it is a plain session, not the Workflow tool, which is unavailable to a shell
# driver. It builds nothing: its only writes are to Praxis, regressing tickets whose work does not
# survive integration so the next round rebuilds them. That self-heals instead of shipping a green
# claim over a broken merge.
#
# The verdict comes back through a sentinel file rather than pane scraping, and that file lives
# OUTSIDE the repo so it can never be swept into a wip commit or mistaken for ticket output.
#
# AF_VERIFY_ROUND=0 disables it. Skipped when a round finished zero tickets — nothing was integrated —
# and skipped for a single-ticket round, which merges exactly the tree its worker already validated.
VERDICT="/workspace/af-round-verdict-$PROJECT.json"

verify_round(){   # $1 = round number, $2.. = the round's ticket ids
  local rnd="$1"; shift
  local ids_csv; ids_csv=$(printf '%s,' "$@"); ids_csv=${ids_csv%,}
  local vsession="$SESSION-verify"
  rm -f "$VERDICT"

  tmux kill-session -t "$vsession" 2>/dev/null || true
  tmux new-session -d -s "$vsession" -c "$WT"
  tmux send-keys -t "$vsession" "cd $WT && $CLAUDE_LAUNCH" Enter
  local vready=0
  for _ in $(seq 1 "$READY_POLL_MAX"); do
    sleep 2
    pane=$(tmux capture-pane -t "$vsession" -p 2>/dev/null || echo "")
    if echo "$pane" | grep -qE "bypass permissions on"; then vready=1; break; fi
  done
  [ "$vready" = "0" ] && say "WARNING: verify REPL not confirmed ready, sending anyway"

  # No parentheses in this prompt, deliberately — if the REPL is not actually up the text lands on a
  # bash prompt, where a stray paren is a syntax error that kills the pane.
  tmux send-keys -t "$vsession" "Post-merge verification of build round $rnd for project $PROJECT. Tickets just merged: $ids_csv. Each was built and validated ALONE in its own worktree, so the merged tree you are looking at has never been verified as a whole. Do NOT build features, do NOT claim tickets, do NOT start new work, do NOT push. Verify the integrated result only. Step 1: run the repo's whole-repo gates on the current merged tree — full test suite, build, repo-wide typecheck and lint. Step 2: dispatch INDEPENDENT parallel review subagents over the combined diff of this round, one per lens, each told to actively look for a failure rather than confirm success. Lens A integration conflict: did two of these tickets edit the same module, config, migration, schema, or shared type in ways that are individually fine and jointly wrong, or did one silently revert another. Lens B acceptance survival: for EACH ticket id above, re-run its own acceptance test against the MERGED tree and confirm it still passes here, not just in its worktree. Lens C test integrity: did any ticket reach green by deleting, skipping, xfailing, narrowing assertions on, or excluding from config a test that used to run — treat that as a failure, not a pass. Step 3: for every ticket whose work does NOT survive integration, regress it in Praxis so the next round rebuilds it: record_outcome with success False, then release with state incomplete, on the prd-$PROJECT snapshot, and say plainly why. Do not attempt to fix it yourself. Step 4: write your verdict as JSON to $VERDICT with exactly these keys: verdict which is pass or fail, gates_green true or false, regressed which is an array of ticket ids you regressed, and notes which is one short string. Write that file LAST, after everything else is done, and then STOP."
  sleep 3; tmux send-keys -t "$vsession" Enter
  say "round #$rnd: post-merge verification dispatched over $ids_csv"

  local vwaited=0 vstall=0 vhash="" vlast=""
  while [ "$vwaited" -lt "${AF_VERIFY_TIMEOUT_S:-2700}" ]; do
    sleep 30; vwaited=$((vwaited+30))
    [ -f "$VERDICT" ] && break
    pane=$(tmux capture-pane -t "$vsession" -p 2>/dev/null || echo "")
    if ! echo "$pane" | grep -qE "."; then say "verify session gone before writing a verdict"; break; fi
    if echo "$pane" | grep -qiE "insufficient balance|402|quota exceeded|payment required|credit balance is too low"; then
      say "BILLING FAILURE during verification — halting"; tmux kill-session -t "$vsession" 2>/dev/null || true; exit 3
    fi
    vhash=$(printf '%s' "$pane" | md5sum | cut -d' ' -f1)
    if [ "$vhash" = "$vlast" ]; then
      vstall=$((vstall+1))
      [ "$vstall" -ge "$STALL_POLLS" ] && { say "verify session frozen for $((STALL_POLLS*30/60))min — giving up on this round's verification"; break; }
    else
      vstall=0; vlast="$vhash"
    fi
  done

  local pane_pid; pane_pid=$(tmux list-panes -t "$vsession" -F '#{pane_pid}' 2>/dev/null | head -1 || true)
  tmux kill-session -t "$vsession" 2>/dev/null || true
  [ -n "${pane_pid:-}" ] && pkill -P "$pane_pid" 2>/dev/null || true

  if [ ! -f "$VERDICT" ]; then
    # Absence of a verdict is NOT a pass. The round's tickets stay finished — this stage regresses
    # nothing on its own — but say so loudly, because an unverified merge is exactly the state the
    # stage exists to eliminate.
    say "WARNING: round #$rnd produced NO verification verdict — the merged tree is UNVERIFIED. Treat its green claim as unproven."
    return 0
  fi
  # Read the sentinel with a parser, not grep: a bare grep for the word fail matches the notes prose
  # and would report a passing round as failed.
  local summary
  summary=$(python3 - "$VERDICT" <<'PYEOF' 2>/dev/null || echo "verdict=UNREADABLE"
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print(f"verdict=UNPARSEABLE ({e})"); raise SystemExit
reg = d.get("regressed") or []
print("verdict=%s gates_green=%s regressed=%d%s :: %s" % (
    d.get("verdict"), d.get("gates_green"), len(reg),
    (" [" + ",".join(str(r) for r in reg) + "]") if reg else "",
    str(d.get("notes", ""))[:200]))
PYEOF
)
  say "round #$rnd verification: $summary"
  rm -f "$VERDICT"
  return 0
}

n=0
round=0
while :; do
  left=$(claimable); left=${left:-999}
  say "$PROJECT claimable=$left"
  [ "$left" = "0" ] && { say "DONE — nothing claimable"; break; }
  [ "$n" -ge "$MAX" ] && { say "hit max_tickets=$MAX, stopping"; break; }

  release_inprogress >/dev/null

  # Compute the frontier AFTER releasing dead leases, so a ticket stranded in_progress by a crashed
  # session is eligible for this round instead of sitting out until someone notices.
  budget=$((MAX - n)); [ "$budget" -lt "$BATCH_MAX" ] && cap="$budget" || cap="$BATCH_MAX"
  batch=$(ready_batch "$cap")
  if [ -z "$batch" ]; then
    # Claimable work exists but nothing is dependency-ready: a cycle, or a chain rooted on a blocked
    # ticket. Restarting sessions cannot fix that, so halt loudly instead of spinning.
    say "DEPENDENCY STALL — $left claimable but nothing ready; every remaining ticket waits on an unfinished or blocked prerequisite. Fix the depends_on chain or unblock the root, then relaunch."
    break
  fi
  set -- $batch
  size=$#
  ids_csv=$(printf '%s,' "$@"); ids_csv=${ids_csv%,}
  round=$((round+1)); n=$((n+size))
  say "round #$round: dispatching $size ticket(s) in parallel — $ids_csv"

  before=$(finished_count); before=${before:-0}

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

  # The batch ids ARE the run scope, which is what makes one round per session self-limiting: af-build
  # stamps its run marker on exactly these tickets, so its completeness gate releases the session when
  # they are done and its fan-out loop finds an empty in-scope frontier rather than starting a wave the
  # next session is meant to own. Parentheses are avoided in this string on purpose — if the REPL is not
  # actually ready, the text lands on a bash prompt, and a stray paren there is a syntax error that
  # leaves the session dead at a shell prompt.
  tmux send-keys -t "$SESSION" "/af-build $PROJECT $ids_csv — build EXACTLY these $size tickets and nothing else. They are dependency-independent, so fan them out in ONE parallel round via the ultracode Workflow, one isolated git worktree per ticket. When every ticket in the batch is finished or blocked: merge each ticket branch into the already-checked-out branch, remove ALL worktrees the round created, then STOP and report. Do NOT claim, read, or start any ticket outside that id list even if more remain — a fresh session picks up the next batch. Work ONLY on the already-checked-out branch, do NOT push. Postgres localhost:$PG$( [ -n "${REDIS:-}" ] && [ "$REDIS" != "none" ] && echo ", Redis localhost:$REDIS" )."
  sleep 3; tmux send-keys -t "$SESSION" Enter
  say "submitted round #$round with $size ticket(s), waiting for the batch to finish or stall"

  # Wait for: every batch ticket to leave the open set (success), context exhaustion, auth error,
  # the session to die, OR the pane going completely unchanged for STALL_POLLS polls in a row (a
  # frozen session — see v2 note above).
  #
  # A per-ticket finish is PROGRESS, not completion: breaking on the first one would kill a 15-ticket
  # round the moment its fastest worker landed, orphaning fourteen live workers and their worktrees.
  # So each finish instead resets the stall counter and buys more wall clock, and the round ends only
  # when the batch's open count hits zero. The deadline scales with batch size for the same reason.
  deadline=$((3600 + (size - 1) * 1200))
  # Pane stillness is a WEAKER hang signal on a parallel round than it was on a solo ticket: the
  # driving session spends the round awaiting its Workflow rather than emitting tool output, and a
  # quiet stretch between two workers landing is normal. Widen the window when more than one ticket
  # is in flight — a genuinely dead round is still caught by the scaled deadline, and unlike v2 there
  # is now an independent liveness signal in Praxis, the batch's open count.
  stall_polls=$STALL_POLLS
  [ "$size" -gt 1 ] && stall_polls=$((STALL_POLLS * 2))
  waited=0
  same_count=0
  last_hash=""
  done_seen=$before
  while [ "$waited" -lt "$deadline" ]; do
    sleep 30; waited=$((waited+30))
    now=$(finished_count); now=${now:-$done_seen}
    if [ "$now" -gt "$done_seen" ]; then
      say "round #$round progress: $((now - before))/$size finished"
      done_seen=$now
      same_count=0                       # real progress is not a stall, whatever the pane looks like
      waited=$((waited > 900 ? waited - 900 : 0))
    fi
    open=$(batch_open "$@"); open=${open:-1}
    if [ "$open" = "0" ]; then say "round #$round complete — all $size ticket(s) finished or blocked"; break; fi
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
      if [ "$same_count" -ge "$stall_polls" ]; then
        say "pane unchanged for $((stall_polls*30/60))min — treating as frozen/stalled, ending wait"
        break
      fi
    else
      same_count=0
      last_hash="$pane_hash"
    fi
  done
  [ "$waited" -ge "$deadline" ] && say "round #$round timed out after $((deadline/60))min with $(batch_open "$@") ticket(s) still open"

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
  # Sweep AFTER the session is dead, never while it is live: removing a worktree out from under a
  # running worker deletes the tree its evals are executing against.
  sweep_worktrees

  # Circuit breaker. A round that finishes NOTHING will, on the next pass, release the same leases
  # and dispatch the same frontier — so a persistent failure that isn't one of the specific panes
  # matched above (a broken repo, a project whose env never comes up, a model that rejects every
  # prompt) loops indefinitely at full cost. The DeepSeek-balance incident burned ~6 hours across 46
  # such cycles before it was spotted, and the billing grep added afterwards only covers that one
  # cause. Three fruitless rounds is the general version of that guard.
  after=$(finished_count); after=${after:-$before}
  if [ "$after" -gt "$before" ]; then
    fruitless=0
    # Verify the MERGE, not the tickets — they were each proven alone, in a worktree that no longer
    # exists. Runs only when something actually landed, and only after the build session is dead so
    # the two never race on the same tree. It may regress tickets, which is why it runs BEFORE the
    # next frontier is computed: a ticket it sends back reappears in the very next batch.
    # Only for a genuinely parallel round. A 1-ticket batch is the old serial case: its worker branched
    # from this HEAD, validated there, and merged straight back, so there is no cross-ticket
    # interaction for this stage to find and a whole extra session would buy nothing.
    if [ "${AF_VERIFY_ROUND:-1}" = "1" ] && [ "$size" -gt 1 ]; then
      verify_round "$round" "$@"
    elif [ "$size" -le 1 ]; then
      say "round #$round: single-ticket batch — skipping post-merge verification, nothing merged alongside it"
    fi
  else
    fruitless=$((${fruitless:-0} + 1))
    say "round #$round finished ZERO tickets ($fruitless in a row)"
    if [ "$fruitless" -ge 3 ]; then
      say "HALTING — 3 consecutive rounds finished nothing. Something is failing that a restart cannot fix; attach to the pane or read the log before relaunching."
      exit 4
    fi
  fi
  say "session closed; restarting fresh for the next batch"
done
say "af-ticket-loop finished for $PROJECT"
