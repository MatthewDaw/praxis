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
# as an explicit id scope, capped at AF_BATCH_MAX (default 16). Because the batch ids ARE the run
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
#    mid-round leaves them behind, and 29 stranded worktrees once filled a 98GB volume. The tree is
#    scratch and is purged unconditionally; the BRANCH is the artifact and survives the purge.
#  - Branches are then reaped in turn, because a surviving branch is only an artifact until its work
#    lands. See reap_branches: a run that leaves residue destroys the signal it needs to report a
#    genuine orphan.
#
# The round is fanned out with parallel Agent SUBAGENTS, one per ticket in a single message, NOT with
# the Workflow tool. That is deliberate and it is the whole reason a batch of N actually runs N-wide:
# Workflow derives its concurrency from CPU count, so on a small box it throttles hard. Measured, not
# assumed — a 5-ticket round dispatched through Workflow reported "2/5 agents done" with exactly two
# worktrees showing file activity while the other three waited. The Agent tool carries no such
# core-derived cap and supports the same per-agent worktree isolation, so the prompt below spells out
# both halves: do not use Workflow, and put every worker in ONE message so they are concurrent.
#
# Do NOT delete that instruction as redundant. It is the only thing standing between this loop and a
# core-derived throttle: an agent that reaches for Workflow as the "obvious" parallel primitive gets
# silently narrowed to a couple of concurrent workers, and the round then looks merely slow rather
# than broken. The specific number is deliberately NOT hardcoded here — it is computed per box at run
# time (WORKFLOW_CAP, below) so this shared multi-project driver never asserts one machine's
# arithmetic as a universal fact.
#
# Disk is then the real constraint: each worktree is a full checkout plus, if the project bootstraps
# per-worktree deps, a full dependency tree.
#
# The default width is 8, not the 15 first written here. Workers are I/O-bound on the model API, so
# width does NOT need a core apiece — but it is not free either: every worker that reaches its
# end-of-ticket whole-repo gate runs a real test suite, and enough of those landing together turn a
# 4-core box into a queue. 8 keeps a wide frontier moving without betting the round on that pileup.
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
# v6: SHIPPED WITH THE PLUGIN, made 2026-07-30.
#
# This driver used to live at <repo>/scripts and each project got at it however it could — which in
# practice meant copies. One project ran for weeks under its own supervisor against a months-old copy
# while the canonical script two directories away had been rewritten twice; nothing anywhere said the
# two had diverged, because a copy is indistinguishable from the original until you diff it. Every
# project pointing at ONE file is the only version of this that stays true.
#
# So it now ships inside the agent_factory plugin and resolves its own dependencies from its own path
# rather than assuming a checkout at /workspace/praxis: the hooks come from AF_PLUGIN_DIR, the
# interpreter from the factory repo's venv, and logs/verdicts from the directory holding the target
# worktree. Nothing in it is project-specific any more, so af-build-remote picks it up automatically
# for every project instead of each one needing its own launcher.
#
# Usage: af-ticket-loop.sh <project> <worktree> <pg_port> <redis_port> [max_tickets]
#        af-ticket-loop.sh --check
#
# Knobs, all optional:
# Modes:
#   af-ticket-loop.sh --resolve-orphans <project> <worktree>
#                          land every stranded worker branch and stop. Same sweep + resolver the
#                          round flow runs; use it to clear an existing backlog without waiting
#                          for a round. Every round ALSO sweeps, so a backlog cannot re-form.
#
#   AF_WATCH=1             do not exit when the ticket set drains or stalls -- wait and re-query, so
#                          tickets authored AFTER the run started are picked up without a relaunch.
#                          This is what makes an unattended build loop self-sustaining; without it
#                          every project needs its own external supervisor, which is how three
#                          separate restart bugs got written outside this repo.
#   AF_WATCH_POLL_S=300    how often watch mode re-queries (default 300)
#   AF_WATCH_STOP=<path>   stop sentinel; `touch` it to end a watching run cleanly
#                          (default: <parent-of-worktree>/af-watch-stop-<worktree>)
#   AF_BATCH_MAX=32        round width (default 16). NOT narrowed by CPU underneath -- the round
#                          fans out with Agent subagents, which carry no core-derived cap. DISK is
#                          the real ceiling: each worker is a full checkout (+ deps if bootstrapped).
#   AF_VERIFY_ROUND=0      skip post-merge verification (default on, multi-ticket rounds only)
#   AF_VERIFY_TIMEOUT_S    bound that verification (default 2700)
#   AF_MIN_FREE_GB=25      raise the disk floor a round must clear (default 15)
#   AF_KEEP_BRANCHES=1     report worker branches instead of reaping them (debugging a bad round)
#   AF_HUMAN_BRANCHES      space-separated globs of branches a HUMAN owns, ADDED to the built-in
#                          main/master/develop/trunk/release-*/hotfix-* defaults. Branch ownership is
#                          default-inclusive -- anything not exempt here and not merged is treated as
#                          factory work owed a merge -- so this is the one place to register a
#                          long-lived hand-made branch that must not trip the straggler invariant.
#
# Exit codes:
#   0  clean drain, or a dependency stall the operator must unblock
#   1  preflight failure (bad args, no worktree, model backend, universal lane)
#   3  billing/credit failure
#   4  three consecutive fruitless rounds   5  disk floor   6  Praxis unreachable
#   7  STRAGGLERS: the run left unmerged worker branches and/or leftover worktrees behind. Nothing is
#      ever deleted to reach a green invariant, so the work named in the log is intact -- land it
#      (`--resolve-orphans`) and relaunch.
#   AF_MODEL_BACKEND       sonnet | deepseek (see v3)
#   AF_PLUGIN_DIR / AF_REPO / AF_PYTHON / AF_STATE_DIR / AF_LOG   for an unusual layout
set -euo pipefail

# ---------------------------------------------------------------- backend selection ----
DEEPSEEK_KEY_FILE="$HOME/.deepseek_key"
OAUTH_TOKEN_FILE="$HOME/.claude/oauth-token.sh"
CREDENTIALS_FILE="$HOME/.claude/.credentials.json"

# Reads claudeAiOauth.subscriptionType out of the credentials file: "max", "pro", or
# empty when there is no readable file (macOS keeps auth in the Keychain, so absence is
# normal and must not be treated as failure). Never prints the token.
_credential_subscription_type(){
  [ -r "$CREDENTIALS_FILE" ] || { echo ""; return 0; }
  python3 - "$CREDENTIALS_FILE" <<'PYEOF' 2>/dev/null || echo ""
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print(""); raise SystemExit(0)
print((d.get("claudeAiOauth") or {}).get("subscriptionType", ""))
PYEOF
}

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
    # READ the credential rather than asserting a bill. The old line was a
    # constant: it printed "Claude Max ... NOT API credits" no matter which
    # credential the session actually picked up, and was confidently wrong for
    # every run on this box. A billing claim nobody verifies is worse than none.
    _sub="$(_credential_subscription_type)"
    case "$_sub" in
      max|pro)  BACKEND_NOTE="Anthropic subscription (Claude ${_sub}), model=sonnet — spends Claude quota, NOT API credits" ;;
      "")       BACKEND_NOTE="model=sonnet — credential type UNKNOWN (no readable .credentials.json); billing NOT verified" ;;
      *)        BACKEND_NOTE="model=sonnet — credential reports '${_sub}', NOT a Max/Pro subscription; this may bill API credits" ;;
    esac
    # A credential FILE is one way to hold a subscription, not the only way: on macOS the CLI keeps
    # its auth in the Keychain, so a perfectly working laptop has neither file. Refusing to start
    # there would be a false negative on the machine that most obviously CAN run a build, so absence
    # is only a warning. The live probe below is the real gate -- it asks Anthropic instead of
    # inspecting the filesystem, and it is authoritative either way.
    if [ ! -r "$OAUTH_TOKEN_FILE" ] && [ ! -r "$CREDENTIALS_FILE" ]; then
      echo "[backend] note: no credential file ($OAUTH_TOKEN_FILE / $CREDENTIALS_FILE)." >&2
      echo "[backend]   normal on macOS, where the CLI uses the Keychain. Probing the credential for real." >&2
      echo "[backend]   if the probe rejects: claude -> /login   (or, headless: claude setup-token)" >&2
    fi
    # CLAUDE_CODE_OAUTH_TOKEN is unset, NOT sourced. ~/.claude/oauth-token.sh exports
    # that variable, and an env token OVERRIDES ~/.claude/.credentials.json -- so
    # sourcing it silently decides the bill. Measured on the devbox: identical launch
    # WITH the file sourced banners "Claude API", WITHOUT it banners "Claude Max",
    # while .credentials.json held subscriptionType=max the whole time. The loop was
    # therefore spending API credits while printing that it was not.
    # The credentials file is the subscription; let the CLI read it.
    CLAUDE_LAUNCH="unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_MODEL ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN; claude --model sonnet --dangerously-skip-permissions"

    # The credential FILE existing proves nothing — a long-lived setup-token can be revoked
    # or expire while the file sits there looking healthy, and the failure surfaces as every
    # ticket dying at the REPL with "please run /login". Spend one throwaway prompt proving
    # the credential is live before committing hours to it. Only an explicit auth rejection
    # is fatal; a network blip or timeout is a warning, so a flaky moment can't block a
    # run whose credential is actually fine.
    # The probe must test THE SAME credential the sessions will use and reach the
    # model the same way, or it is theatre. Two corrections:
    #
    #  1. It used to source oauth-token.sh, which exports CLAUDE_CODE_OAUTH_TOKEN
    #     and OVERRIDES ~/.claude/.credentials.json. The launch line no longer does
    #     that (it unsets the variable so the subscription credential wins), so a
    #     probe that still sourced it was validating a different identity than the
    #     one about to run — on this box, an API/org token instead of Claude Max.
    #
    #  2. Installed plugins mute headless `claude -p`: it exits 0 having printed
    #     NOTHING. That is why this probe warned "no PONG" on every single run
    #     while the credential was perfectly healthy, and a warning that fires
    #     always is a warning nobody reads. Point the probe at a config dir holding
    #     only the credential, exactly as the graded judge does (see
    #     evals/plan_repro/claude_cli.py). The credential is symlinked, never
    #     copied, so no secret is duplicated and a re-login is picked up.
    local probe probe_cfg
    probe_cfg="${TMPDIR:-/tmp}/af-probe-claude-config"
    mkdir -p "$probe_cfg" 2>/dev/null || true
    if [ -r "$CREDENTIALS_FILE" ]; then
      ln -sf "$CREDENTIALS_FILE" "$probe_cfg/.credentials.json" 2>/dev/null || true
    fi
    probe="$(cd /tmp && bash -c "unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_MODEL ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN; export CLAUDE_CONFIG_DIR='$probe_cfg'; timeout 120 claude --model sonnet -p 'Reply with exactly: PONG'" 2>&1 || true)"
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

# ------------------------------------------------------------------- where everything lives ----
# The driver SHIPS WITH THE PLUGIN and locates everything from its own path, so every project on
# every box runs the same code. It used to be a loose file at <repo>/scripts that each project
# copied, and copies drift: one project spent an entire evening running a months-old v3 copy under
# its own supervisor while the canonical script three directories away had been rewritten twice.
# A copy that cannot be told apart from the original is not a deployment strategy.
#
# Resolution, all overridable for an unusual layout:
#   AF_PLUGIN_DIR  the agent_factory package        (default: the parent of this script's dir)
#   AF_REPO        the factory checkout holding it  (default: the parent of AF_PLUGIN_DIR)
#   AF_PYTHON      interpreter with the hooks' deps (default: AF_REPO/.venv/bin/python, else python3)
#   AF_STATE_DIR   logs + verdict sentinels         (default: the parent of the target worktree)
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
AF_PLUGIN_DIR="${AF_PLUGIN_DIR:-$(dirname "$(dirname "$SELF")")}"
AF_REPO="${AF_REPO:-$(dirname "$AF_PLUGIN_DIR")}"

# --resolve-orphans is a MODE flag, not the project: shift it off so the positional parsing below
# stays identical for both entry points (otherwise PROJECT would literally be "--resolve-orphans").
AF_MODE=""
if [ "${1:-}" = "--resolve-orphans" ]; then AF_MODE="resolve-orphans"; shift; fi
PROJECT="$1"; WT="$2"; PG="${3:-}"; REDIS="${4:-}"; MAX="${5:-999}"
# Largest frontier handed to a single session, and the ONLY parallelism cap this driver enforces.
#
# It is NOT further narrowed underneath: the round fans out with Agent subagents, which carry no
# core-derived cap, so a batch of N really does run N-wide. (An earlier version of this comment
# claimed the Workflow tool capped concurrency beneath this number — that was wrong, because this
# loop never calls Workflow. The false claim made small rounds look inevitable when they were only
# a default.)
#
# DISK is the real ceiling, not CPU: each worker gets a full checkout plus, where the project
# bootstraps per-worktree deps, a full dependency tree. Measure a round's footprint on the target
# volume and set AF_BATCH_MAX from that; the AF_MIN_FREE_GB floor aborts a round that would not fit,
# but it cannot un-spend disk already consumed mid-round.
BATCH_MAX="${AF_BATCH_MAX:-16}"

# What the Workflow tool WOULD narrow us to on this specific machine, computed rather than assumed.
# Only used to explain, in the dispatch prompt, why the round must not go through Workflow — nothing
# in this driver is limited by it.
_cores="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
WORKFLOW_CAP=$(( _cores - 2 )); [ "$WORKFLOW_CAP" -gt 16 ] && WORKFLOW_CAP=16
[ "$WORKFLOW_CAP" -lt 1 ] && WORKFLOW_CAP=1
SESSION="af-$(basename "$WT")"   # per-worktree, so concurrent projects never collide on tmux session name
# AF_WATCH stop sentinel — per-worktree, so stopping one project never stops another.
WATCH_STOP="${AF_WATCH_STOP:-$(dirname "$WT")/af-watch-stop-$(basename "$WT")}"
# What this run integrates INTO -- whatever the project worktree has checked out. Workers must be
# told it explicitly, because they do not start on it: `isolation: worktree` creates each worker's
# tree from the repo's default branch (`refs/remotes/origin/HEAD`, i.e. origin/main), NOT from the
# checked-out branch. Verified 2026-07-31 from the branch reflog: "Created from origin/main".
#
# On a long-running integration branch that gap is the whole ballgame. proposed-side-buildout's
# `consolidate/all-work` had run 351 commits past origin/main, so every worker authored its change
# against files the integration branch did not have; the work then would not apply back onto it, not
# even cherry-picked alone, and four green rounds landed exactly nothing. The gap reopens on its own,
# too -- it was back to 21 commits within one round of being reconciled, because every ticket this
# factory lands widens it again.
#
# Resolve a BRANCH NAME when there is one, and fall back to the raw SHA when there is not. Asking for
# `rev-parse --abbrev-ref HEAD` alone is a trap on a detached HEAD: it does not fail, it SUCCEEDS and
# returns the literal string "HEAD", so the `|| echo HEAD` fallback never fires and the worker's
# first command becomes `git merge --ff-only HEAD` -- merging HEAD into itself, a silent no-op that
# skips the rebase entirely while reporting success. Both build worktrees on this box are detached
# (sotos-build, appeal_engine-build), so the ref-name form would have protected neither. `symbolic-ref`
# is the right probe because it genuinely fails when detached; the SHA it falls back to resolves fine
# from inside a worker's worktree, since they share one object store.
INTEGRATION_REF="$(git -C "$WT" symbolic-ref --quiet --short HEAD 2>/dev/null || git -C "$WT" rev-parse HEAD 2>/dev/null || echo HEAD)"
# Last id sets integrate_round successfully read out of Praxis, space-padded. reap_branches uses them
# to tell a ticket of ours from a foreign tracker's, and the EXIT trap reuses whatever the last round
# managed to read — an empty set only narrows what the reap is willing to touch, never widens it.
AF_KNOWN_IDS=" "
AF_FINISHED_IDS=" "
# State lives beside the worktrees, not inside them: a log or a verdict sentinel written into a repo
# gets swept into a wip commit and read back as ticket output.
AF_STATE_DIR="${AF_STATE_DIR:-$(dirname "$WT")}"
LOG="${AF_LOG:-$AF_STATE_DIR/af-ticket-loop.log}"
if [ -n "${AF_PYTHON:-}" ]; then PY="$AF_PYTHON"
elif [ -x "$AF_REPO/.venv/bin/python" ]; then PY="$AF_REPO/.venv/bin/python"
else PY="$(command -v python3)"; fi
# Exported, so every embedded heredoc below imports the hooks without hardcoding a path of its own.
export PYTHONPATH="$AF_PLUGIN_DIR/hooks:$AF_PLUGIN_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
# Exported for the same reason PYTHONPATH is, one level out: the per-ticket WORKERS are claude
# sessions that type `python3` in their own Bash calls, and that resolves against THEIR PATH, not
# against the interpreter this driver so carefully chose. On the box where the sotos run lost its
# quality gate, /usr/bin/python3 was 3.9, `import tomllib` raised ModuleNotFoundError, and the
# universal `minimalism-dry` lane silently evaluated to zero checks on every ticket. These are the
# exact names `_ticket_state._sidecar_pythons()` already reads (PRAXIS_HOOK_PYTHON first, AF_PYTHON
# second), so a hook that lands in a too-old interpreter can still recover the lane out-of-process
# instead of pretending it is empty. Both are set, not one: PRAXIS_HOOK_PYTHON is the hook-side
# name, AF_PYTHON is this driver's own override knob and must agree with what it resolved.
export PRAXIS_HOOK_PYTHON="$PY"
export AF_PYTHON="$PY"

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

# --- portability: this driver runs on the Linux devbox AND on a developer's Mac (an iOS project
# cannot close an xcodebuild gate on Linux, so "run it where the toolchain is" is a normal case, not
# an exception). Two GNU-isms had to go; everything else is POSIX enough to be shared.
free_gb_of(){   # -> integer GB available on the filesystem holding $1
  local path="$1" out
  # GNU coreutils first, then BSD/macOS `df -g`, whose Avail is the 4th column.
  out=$(df -BG --output=avail "$path" 2>/dev/null | tail -1 | tr -dc '0-9')
  [ -n "$out" ] || out=$(df -g "$path" 2>/dev/null | awk 'NR==2 {print $4}' | tr -dc '0-9')
  echo "${out:-0}"
}

hash_text(){    # -> stable digest of stdin; md5sum is coreutils, md5 is BSD
  if command -v md5sum >/dev/null 2>&1; then md5sum | cut -d' ' -f1
  else md5 -q; fi
}

# Every pid in a tmux session's process tree: the pane process and all its descendants
# (claude -> bash -> pytest). Printed one per line; empty when the session is gone.
session_pids(){  # $1 = tmux session name
  local ppid gen next k depth=0
  ppid=$(tmux list-panes -t "$1" -F '#{pane_pid}' 2>/dev/null | head -1 || true)
  [ -z "${ppid:-}" ] && return 0
  printf '%s\n' "$ppid"; gen="$ppid"
  while [ -n "$gen" ] && [ "$depth" -lt 8 ]; do
    next=""
    for k in $gen; do
      local c; c=$(pgrep -P "$k" 2>/dev/null || true)
      [ -n "$c" ] && { printf '%s\n' $c; next="$next $c"; }
    done
    gen="$next"; depth=$((depth+1))
  done
}

# Total CPU-seconds consumed so far by a session's whole process tree.
session_cpu(){  # $1 = tmux session name
  session_pids "$1" | while read -r p; do [ -n "$p" ] && ps -o time= -p "$p" 2>/dev/null; done \
    | awk -F: '{ s=$NF+0; if (NF>1) s+=$(NF-1)*60; if (NF>2) s+=$(NF-2)*3600; t+=s } END { printf "%d", t+0 }'
}

# Is real work still running underneath a session whose pane has gone quiet?
# The pane-stillness heuristic cannot see a single long tool call: a test suite prints
# nothing for its whole run, so a healthy verification looks identical to a hung one.
# Ask the OS instead — but compare CPU across a short interval rather than reading it
# once, because `ps` TIME is CUMULATIVE: a session that worked earlier and is now wedged
# at a prompt still shows a large total, which would suppress the stall detector forever.
# A rising total means work is happening NOW. Returns 0 (busy) / 1 (idle); never fails.
# Recent writes under the project worktree. A worker that is BLOCKED ON I/O --
# polling a log while a detached OCR/build process grinds -- burns no CPU in its
# own process tree, so the CPU test below calls it idle while it is plainly
# working. Observed 2026-08-05: a COV-1B worker sat at "Polling real_final.log
# for OCR" for 48min having emitted 358k tokens, was reported not-busy, and the
# round was reaped as frozen. File mtime measures PROGRESS directly instead of
# inferring it from CPU, which is the property actually wanted here.
worktree_recently_written(){   # $1 = worktree path, $2 = minutes
  local wt="${1:-}" mins="${2:-10}"
  [ -n "$wt" ] && [ -d "$wt" ] || return 1
  # -mmin -N is cheap and stops at the first hit. Skip .git plumbing, whose
  # mtimes move for reasons unrelated to a worker making progress.
  find "$wt" -path "$wt/.git" -prune -o -type f -mmin "-$mins" -print 2>/dev/null \
    | head -1 | grep -q . && return 0
  return 1
}

verify_children_busy(){  # $1 = tmux session name
  local before after
  # Signal A: the session's own process tree is burning CPU.
  before=$(session_cpu "$1")
  if [ -n "${before:-}" ]; then
    sleep 3
    after=$(session_cpu "$1")
    [ -n "${after:-}" ] && [ "$after" -gt "$before" ] 2>/dev/null && return 0
  fi
  # Signal B: something under the worktree was written recently. Either signal is
  # sufficient -- both are positive evidence of life, and a quiet pane is not.
  worktree_recently_written "${WT:-}" "${AF_BUSY_WRITE_MINS:-10}" && return 0
  return 1
}

# Resolve the backend BEFORE any ticket work, and refuse to start a multi-hour run on a
# half-configured one. Failing here costs seconds; failing three tickets in costs an hour
# and a lease that has to be released by hand.
resolve_backend || { say "FATAL: model backend preflight failed for '${BACKEND:-?}' — refusing to start"; exit 1; }
say "backend=$BACKEND ($BACKEND_NOTE)"

# ---------------------------------------------------------------------------
# Package caches MUST sit on the worktree's own filesystem.
#
# Every round creates one git worktree per ticket, and each needs a usable
# environment. uv and pnpm materialize those by HARDLINK from their cache, so the
# Nth environment costs ~0 bytes — but a hardlink cannot cross a filesystem
# boundary, and when the cache lives on a different mount they SILENTLY become
# full copies. Nothing errors; the run just gets slower and fatter until the disk
# floor stops it.
#
# af-build's skill has documented this as something the operator must export.
# Measured on a box where nobody had: worktrees were 2.9G each (a 668M venv copied
# per ticket), the batch had to be capped at 4 to fit, and the 30G root filled
# while 52G sat unused on the worktree volume. Pointing the cache at the worktree
# filesystem took the same worktrees to 42M-1.1G.
#
# So the driver sets it rather than hoping. If the operator set one explicitly on a
# DIFFERENT filesystem, say so loudly instead of degrading in silence.
# AF_CACHE_ROOT overrides the location.
fs_of(){ df -P "$1" 2>/dev/null | awk 'NR==2{print $1}'; }
_wt_fs="$(fs_of "$WT")"
AF_CACHE_ROOT="${AF_CACHE_ROOT:-$(dirname "$WT")/.af-pkg-cache}"
mkdir -p "$AF_CACHE_ROOT" 2>/dev/null || true
for _spec in "UV_CACHE_DIR:uv" "PIP_CACHE_DIR:pip" "npm_config_cache:npm"; do
  _var="${_spec%%:*}"; _sub="${_spec##*:}"
  eval "_cur=\${$_var:-}"
  if [ -z "$_cur" ]; then
    mkdir -p "$AF_CACHE_ROOT/$_sub" 2>/dev/null || true
    export "$_var=$AF_CACHE_ROOT/$_sub"
  elif [ -n "$_wt_fs" ] && [ "$(fs_of "$_cur")" != "$_wt_fs" ]; then
    say "WARNING: $_var=$_cur is on a different filesystem than the worktree $WT."
    say "         uv/pnpm hardlinks degrade to full COPIES across that boundary, so every"
    say "         per-ticket environment costs its full size instead of ~0. Point it at"
    say "         $AF_CACHE_ROOT/$_sub, or unset it and let this driver choose."
  fi
done
say "pkg caches under $AF_CACHE_ROOT (worktree fs ${_wt_fs:-unknown}) — hardlink-eligible"

# Same contract as the backend preflight, for the interpreter: prove the universal quality lane can
# actually LOAD before spending hours on tickets it is supposed to gate.
#
# This exists because it once failed silently end to end. seeded_checks.py does `import tomllib`
# (stdlib, 3.11+); the build box's python3 was 3.9; `_universal_checks()` caught the
# ModuleNotFoundError with a bare `except Exception: return []`; and the mandatory minimalism-dry
# gate vanished from EVERY ticket. Three tickets reached FINISHED ungated and nothing anywhere said
# a word. The hook side no longer fails open, but a run whose interpreter cannot load the lane still
# has no business starting -- it would just fail loudly on every ticket instead of once, here.
#
# All three legs are checked because they fail differently: tomllib is the version leg, the package
# import is the PYTHONPATH/layout leg, and a NON-EMPTY result is the seeded_checks.toml leg (a
# missing or mis-parsed toml imports fine and yields zero checks, which is indistinguishable from
# the outage this whole comment is about).
preflight_universal_lane(){
  local out rc
  out="$("$PY" - <<'PYEOF' 2>&1
import sys
try:
    import tomllib  # noqa: F401  -- the 3.11+ leg; this is the import that broke the sotos run
except Exception as exc:
    print("cannot import tomllib (needs Python >= 3.11): %s: %s" % (type(exc).__name__, exc))
    sys.exit(1)
try:
    from agent_factory.seeded_checks import universal_seeded_checks
except Exception as exc:
    print("cannot import agent_factory.seeded_checks: %s: %s" % (type(exc).__name__, exc))
    sys.exit(1)
try:
    checks = universal_seeded_checks()
except Exception as exc:
    print("universal_seeded_checks() raised: %s: %s" % (type(exc).__name__, exc))
    sys.exit(1)
if not checks:
    print("universal_seeded_checks() returned an EMPTY list -- the universal lane would gate nothing")
    sys.exit(1)
print("%d universal check(s): %s" % (len(checks), ",".join(c.check_id for c in checks)))
PYEOF
)" && rc=0 || rc=$?
  if [ "${rc:-0}" -ne 0 ]; then
    say "FATAL: universal seeded-check preflight failed — refusing to start"
    say "  interpreter : $PY  ($("$PY" -V 2>&1 || echo 'version unknown'))"
    say "  failure     : ${out:-<no output>}"
    say "  why fatal   : without this lane every ticket builds with NO universal quality gate, and"
    say "                that is exactly how a whole run once finished tickets ungated in silence."
    say "  remediation : point AF_PYTHON (or PRAXIS_HOOK_PYTHON) at a Python >= 3.11 that can import"
    say "                agent_factory.seeded_checks — e.g. AF_PYTHON=$AF_REPO/.venv/bin/python — and"
    say "                confirm $AF_PLUGIN_DIR/seeded_checks.toml exists and parses."
    return 1
  fi
  say "universal lane OK via $PY — $out"
  return 0
}
preflight_universal_lane || exit 1

# Run a Praxis query so a transient backend failure CANNOT kill the driver.
#
# Every query below is invoked as `var=$(query)` and swallows its own stderr with 2>/dev/null.
# Under `set -euo pipefail` that combination is lethal and completely silent: one non-zero exit
# from the python — an API 5xx, a DNS blip, a connection reset — makes the assignment fail, `set -e`
# terminates the whole script, and the redirected stderr means NOTHING is written to the log. The
# run just stops mid-round, looking from the outside exactly like a healthy loop that went quiet.
# That is how the appeal_engine run died on 2026-07-31: last line "round #3 progress: 3/4 finished"
# at 03:44, no error, no signal, no OOM, driver gone, its tmux session left orphaned for hours.
#
# The `${var:-default}` fallbacks at each call site were written to survive exactly this, but they
# are unreachable dead code as long as `set -e` fires on the assignment first. Routing every query
# through here is what makes them live: the query runs inside an `if`, where `set -e` is suspended,
# so a failure returns a status the caller can actually see and decide about.
#
# Transient by assumption, so retry with backoff before giving up; a hard failure returns 1, and
# each call site declares what that means for it.
#
# Success is judged by EXIT STATUS alone, never by whether stdout is empty. `ready_batch` prints
# nothing for a genuine dependency stall, and that is a real answer the loop must be free to act on
# — treating empty-but-successful as a failure would retry a true stall forever instead of halting
# loudly, trading a silent death for a silent spin.
praxis_q(){  # args: query-fn [args...] -> its stdout; 1 if it never succeeded
  local out="" i
  for i in 1 2 3 4 5; do
    if out=$("$@" 2>/dev/null); then printf '%s' "$out"; return 0; fi
    # Linear backoff, ~2.5min total across 5 attempts. Overridable so the regression test can drive
    # the retry path without sleeping through it.
    sleep $((i * ${AF_QUERY_BACKOFF_S:-10}))
  done
  return 1
}

# Consecutive-outage bookkeeping for the passes that genuinely cannot proceed without Praxis.
# Riding out a blip is right; spinning forever against a backend that is actually down is not, so a
# long enough streak halts loudly — the same contract as a dependency stall, and the opposite of the
# silent death this whole mechanism replaces.
outages=0
outage(){  # args: what-failed -> waits, or halts the run once the streak is too long
  outages=$((outages + 1))
  if [ "$outages" -ge "${AF_MAX_OUTAGES:-10}" ]; then
    say "HALTING — Praxis unreachable for $outages consecutive passes (last: $1). Nothing can be claimed, dispatched or verified until it is back; check the backend, then relaunch."
    exit 6
  fi
  say "Praxis unreachable ($1) — outage $outages/${AF_MAX_OUTAGES:-10}, waiting 60s before retrying this pass"
  sleep 60
}

claimable(){  # -> count of incomplete|in_progress for PROJECT
  $PY - "$PROJECT" <<'PYEOF' 2>/dev/null
import sys
import _praxis
p=sys.argv[1]
f=_praxis.facts_by(category='requirement', space=p, snapshot=f'prd-{p}')
print(sum(1 for x in f if ((x.get('meta') or {}).get('build_state')) in ('incomplete','in_progress')))
PYEOF
}

ready_batch(){  # -> space-separated ids of the dependency-ready frontier, capped at $2
  $PY - "$PROJECT" "${1:-15}" <<'PYEOF' 2>/dev/null
import sys
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

finding_guard(){  # args: round-no ids... -> regresses any ticket that answered a finding with nothing
  # A ticket carrying an OPEN post-merge verification finding may not close by changing nothing.
  #
  # The finding is the only thing that can see a defect living BETWEEN tickets, and it is written as
  # prose on the ticket while the completion gate reads only pinned checks. Prose loses: one ticket
  # was regressed with a report naming the defect, the evidence and the fix, and closed again TWICE
  # with its file untouched, because every pinned check stayed green against tests that hand-built
  # the very shape the finding said was wrong.
  #
  # Deliberately NOT "an open finding blocks completion": verification runs only AFTER a ticket
  # finishes and merges, so blocking the finish would mean it could never reach the verification
  # that clears it. Any genuine attempt satisfies this guard; only doing nothing does not.
  $PY - "$PROJECT" "$1" "${AF_ROUND_BASE:-HEAD}" "${@:2}" <<'PYEOF' 2>/dev/null
import subprocess, sys
import _praxis, _ticket_state as ts
proj, rnd, base = sys.argv[1], sys.argv[2], sys.argv[3]
want = set(sys.argv[4:])
kw = dict(space=proj, snapshot=f"prd-{proj}")
n = 0
for f in _praxis.facts_by(category="requirement", **kw) or []:
    m = f.get("meta") or {}
    rid = str(m.get("requirement_id") or f.get("id"))
    if rid not in want or m.get("build_state") != "finished":
        continue
    try:
        out = subprocess.run(["git", "log", "--oneline", f"{base}..HEAD", f"--grep={rid}"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        out = "unknown"          # cannot prove absence -> do not accuse
    if out:
        continue
    why = ts.finding_unanswered_without_change(m, 0)
    if not why:
        continue
    _praxis.regress_requirements(proj, [f["id"]], {f["id"]: {
        "audit_disposition": f"ROUND #{rnd}: {why}",
    }}, **kw)
    sys.stderr.write(f"finding-guard regressed {rid}\n")
    n += 1
print(n)
PYEOF
}

stall_roots(){  # -> one line per blocking root: "<id> <state> blocks N: <dependents>"
  # A stall names its ROOT. The message used to say "unblock the root" without saying which — so a
  # human (or an agent) had to walk 30 tickets' depends_on edges by hand to find it. Observed: a
  # bucket-creation ticket blocked on a check only its DEPENDENT could satisfy, and a 30-ticket plan
  # went to zero ready with nothing naming the one ticket holding it.
  $PY - "$PROJECT" <<'PYEOF' 2>/dev/null
import sys
import _praxis
p = sys.argv[1]
facts = _praxis.facts_by(category='requirement', space=p, snapshot=f'prd-{p}') or []
state, deps = {}, {}
for f in facts:
    m = f.get('meta') or {}
    rid = str(m.get('requirement_id') or f.get('id') or '')
    if not rid:
        continue
    state[rid] = m.get('build_state')
    deps[rid] = [str(d) for d in (m.get('depends_on') or [])]
# A root is anything not finished that nothing unfinished precedes -- i.e. it is itself waiting on
# nobody. Those are the only tickets a human can act on; everything else is downstream of them.
unfinished = {r for r, s in state.items() if s != 'finished'}
roots = [r for r in unfinished if not (set(deps.get(r, ())) & unfinished)]
def blocked_behind(root):
    seen, frontier = set(), [root]
    while frontier:
        cur = frontier.pop()
        for r in unfinished:
            if cur in deps.get(r, ()) and r not in seen:
                seen.add(r); frontier.append(r)
    return sorted(seen)
for r in sorted(roots):
    behind = blocked_behind(r)
    print(f"{r} [{state.get(r)}] blocks {len(behind)}: {', '.join(behind[:8])}{' ...' if len(behind) > 8 else ''}")
PYEOF
}

batch_open(){  # args: ids... -> how many are still incomplete|in_progress (blocked counts as done)
  $PY - "$PROJECT" "$@" <<'PYEOF' 2>/dev/null
import sys
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
import _praxis
p=sys.argv[1]
f=_praxis.facts_by(category='requirement', space=p, snapshot=f'prd-{p}')
print(sum(1 for x in f if ((x.get('meta') or {}).get('build_state'))=='finished'))
PYEOF
}

known_ids(){  # -> space-separated requirement_ids for PROJECT, in ANY build_state
  # The set of ids this project is entitled to have an opinion about. Integration uses it to tell
  # "a ticket of ours that isn't done" (a real veto) from "an id belonging to somebody else's
  # tracker" (not our business) -- see integrate_round for why conflating the two stalls a build.
  $PY - "$PROJECT" <<'PYEOF' 2>/dev/null
import sys
import _praxis
p=sys.argv[1]
out=[]
for f in _praxis.facts_by(category='requirement', space=p, snapshot=f'prd-{p}'):
    m=f.get('meta') or {}
    rid=m.get('requirement_id') or f.get('id')
    if rid: out.append(str(rid))
print(' '.join(out))
PYEOF
}

finished_ids(){  # -> space-separated requirement_ids currently in build_state=finished
  $PY - "$PROJECT" <<'PYEOF' 2>/dev/null
import sys
import _praxis
p=sys.argv[1]
out=[]
for f in _praxis.facts_by(category='requirement', space=p, snapshot=f'prd-{p}'):
    m=f.get('meta') or {}
    if m.get('build_state')=='finished':
        rid=m.get('requirement_id') or f.get('id')
        if rid: out.append(str(rid))
print(' '.join(out))
PYEOF
}

regress_ticket(){  # args: requirement_id branch -> send a lying "finished" ticket back to incomplete
  # A ticket reads finished and its commits are sitting on a branch the integration ref has never
  # seen and never will. The ticket is wrong, not the branch: it was marked done and its work did not
  # land. Returning it to incomplete is what makes the next round rebuild it -- the same mechanism the
  # post-merge verifier uses when integration rejects a ticket.
  $PY - "$PROJECT" "$1" "$2" <<'PYEOF' 2>/dev/null
import sys
import _praxis
p, rid, br = sys.argv[1], sys.argv[2], sys.argv[3]
kw = dict(space=p, snapshot=f'prd-{p}')
for f in _praxis.facts_by(category='requirement', **kw):
    m = f.get('meta') or {}
    if str(m.get('requirement_id') or f.get('id')) != rid:
        continue
    _praxis.regress_requirements(p, [f['id']], {f['id']: {
        'claim_owner': None, 'claim_at': None,
        'claim_heartbeat_at': None, 'claim_lease_ttl': None,
        'audit_disposition': f'regressed by af-ticket-loop: read finished, but its commits never '
                             f'reached the integration branch -- they exist only on {br}, and no '
                             f'replacement for this ticket landed either. Rebuild against the '
                             f'integrated tree.'}}, **kw)
    print('regressed', rid)
    break
PYEOF
}

release_inprogress(){  # release any live lease before a fresh session claims (post-crash safety)
  # This used to BRACKET its writes in stamp_planning/clear_planning: a blessed plan refuses
  # candidate edits (the S12 bless guard), so the sweep re-armed the planning marker to get its
  # patch_meta through -- i.e. it UNBLESSED and re-blessed the plan as a side effect of BUILDING,
  # and left the plan unblessed outright if the loop died mid-sweep. Regressing a ticket is build
  # state, so it now goes through the sanctioned, unguarded /requirements/regress endpoint, which
  # needs no marker at all. Do not reintroduce the bracket.
  $PY - "$PROJECT" <<'PYEOF' 2>/dev/null
import sys
import _praxis
p=sys.argv[1]; kw=dict(space=p, snapshot=f'prd-{p}')
NOTE=('lease released by af-ticket-loop: prior session ended (context cap or crash), '
      'returning to incomplete for a fresh session.')
stranded={}
for f in _praxis.facts_by(category='requirement', **kw):
    m=f.get('meta') or {}
    if m.get('build_state')=='in_progress':
        # Null the lease keys alongside the regress so nothing dangles, and record WHY -- the next
        # worker's briefing reads audit_disposition back.
        stranded[f['id']]={'claim_owner':None,'claim_at':None,'claim_heartbeat_at':None,
                           'claim_lease_ttl':None,'audit_disposition':NOTE}
        print('released', m.get('requirement_id'))
if stranded:
    _praxis.regress_requirements(p, list(stranded), detail=stranded, **kw)
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

# Merge the round's work into the integration branch. THE DRIVER OWNS THIS, not the session.
#
# The round prompt asks the session to merge before it stops, and round after round it did not: four
# tickets finished, every commit stayed on its own worktree-agent-* branch, and the post-merge
# verifier -- correctly refusing to fabricate a merge -- reported "there is no merged tree to verify".
# The sweep then logged twelve unmerged worktrees. Asking a session that is about to end its turn to
# perform the one irreversible integration step is asking for exactly that: the work exists, is
# green, and is invisible on the branch anyone would look at.
#
# Merging is mechanical, so the driver does it: deterministic, outlives the session, and observable
# in the log. A conflict is NOT resolved here -- two dependency-independent tickets that touched one
# file need a builder's judgment, so the merge is aborted and the ticket regressed to be rebuilt
# against the now-integrated tree.
# Requirement ids named by a set of commits, filtered to the ones THIS PROJECT owns.
#
# Workers end a commit subject with the ticket id -- "feat(af-clean): ... (R8)" -- and that suffix is
# the only link from a branch back to a ticket. It is also a convention the whole world shares, so the
# raw scan is filtered against the ids Praxis says we own before any of it is believed; see the long
# note in integrate_round for the run this filter exists to prevent.
# $1 = a git log range/rev, $2 = the known-id set, space-padded on both sides.
af_owned_ids(){
  local raw i out=""
  # The id must merely CONTAIN a digit, not END in one. The previous pattern
  # required a trailing [0-9][0-9]*, so any ticket whose id ends in a letter was
  # unmatchable no matter how correctly the commit was written: (DATA-1) and
  # (OBS-27) matched, (COV-1B) never did. That silently stranded 42 files and
  # ~1800 insertions of finished work on 2026-08-06, and it would have stranded
  # them again on a re-run, because the commit subject was not the problem.
  #
  # Still deliberately narrow: anchored to a TRAILING (...) so a conventional-commit
  # scope cannot be mistaken for an id, and every candidate is intersected with
  # this project's known id set below, so another tracker's (JIRA-42) is ignored.
  raw=$(git log --format=%s "$1" 2>/dev/null \
        | sed -n 's/.*(\([A-Za-z][A-Za-z0-9_-]*\))[[:space:]]*$/\1/p' \
        | grep '[0-9]' | sort -u)
  for i in $raw; do
    case "$2" in *" $i "*) out="${out:+$out }$i" ;; esac
  done
  printf '%s' "$out"
}

integrate_round(){
  cd "$WT"
  local merged=0 conflicted=0 skipped=0 br ahead path ids i ok fin known
  # WHICH branch is safe to merge is decided per branch, by its TICKET's state -- not by the round's.
  # A branch name carries no ticket id, but the worker's own commit subjects do: they end in the
  # requirement id, e.g. "feat(af-clean): ... (R8)". So read the ids out of the branch's commits and
  # merge only when every id it claims is FINISHED in Praxis. That admits a finished ticket's work
  # even when a sibling in the same round died, and still refuses the half-built tree of a worker
  # killed mid-BUILD -- possibly between confirm-red and confirm-green -- which would otherwise
  # launder unverified edits under cover of a green round. An unintegrated branch is recoverable; a
  # laundered one is not.
  #
  # The id scan is scoped to THIS PROJECT's requirements, and that scoping is load-bearing. The
  # "(ID)" suffix is a convention the whole world shares, not a factory signature: a host repo's own
  # history is full of "(BES-115)"-style tracker ids, and `HEAD..$br` contains that history whenever
  # the integration branch has drifted behind the base workers branch from. Treating a foreign id as
  # a ticket asks Praxis about a requirement it has never heard of, gets "not finished" by default,
  # and vetoes a branch whose real ticket is finished and sitting at its tip. Observed 2026-07-31 on
  # proposed-side-buildout: `consolidate/all-work` sat 351 commits behind upstream, so every worker
  # branch dragged in ~26 BES-* ids, every merge was skipped as "unproven", and four green rounds
  # landed exactly nothing while Praxis went on reporting the tickets finished.
  #
  # So: an id we do not own is not evidence of anything and is ignored. An id we DO own and that is
  # not finished still vetoes the branch -- that is the property this check exists for, unchanged.
  if ! fin=$(praxis_q finished_ids) || ! known=$(praxis_q known_ids); then
    say "SKIPPING INTEGRATION this round — Praxis unreachable, so no branch's provenance can be established. Branches stay put and integrate next round; nothing is lost."
    return 0
  fi
  fin=" $fin "
  known=" $known "
  # Cached for the EXIT trap, which reaps branches on paths where Praxis is unreachable or where
  # asking it would mean a 2.5-minute retry storm inside a signal handler. A stale-but-real id set is
  # what lets the trap still recognise a `build/<TICKET>` branch as ours rather than as a human's.
  AF_KNOWN_IDS="$known"
  AF_FINISHED_IDS="$fin"
  # Commit anything loose first: `git merge` refuses to run on a dirty tree, and refusing to
  # integrate a whole round because one stray file is uncommitted is the wrong trade.
  commit_wip
  while read -r path; do
    [ -n "$path" ] || continue
    case "$path" in */.claude/worktrees/*) ;; *) continue ;; esac
    br=$(git -C "$path" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
    [ -n "$br" ] && [ "$br" != "HEAD" ] || continue
    ahead=$(git rev-list --count "HEAD..$br" 2>/dev/null || echo 0)
    [ "${ahead:-0}" -gt 0 ] || continue
    # Keep only ids this project actually owns; everything else is another tracker's history.
    ids=$(af_owned_ids "HEAD..$br" "$known")
    if [ -z "$ids" ]; then
      # LOUD on purpose. This is finished work being left behind, not a routine
      # skip: the branch holds commits nobody will merge, and because workers
      # branch from origin/main the next round rebuilds all of it from scratch.
      # Observed 2026-08-06: 42 files / ~1800 insertions stranded because the
      # subjects read `feat(cov1b):` instead of ending in `(COV-1B)`. Naming the
      # actual subjects turns a five-second fix into a five-second fix, instead
      # of an hour of silent rework.
      skipped=$((skipped+1))
      say "WARNING: STRANDING $br ($ahead commit(s)) — no commit subject ends in a $PROJECT ticket id, so provenance cannot be established and this work will NOT be merged"
      say "  subjects seen:"
      git log --format='    %h %s' "HEAD..$br" 2>/dev/null | head -5 | tee -a "$LOG"
      say "  fix: the id must be TRAILING and exact, e.g. 'feat(scope): what it did (${known%% *})' — a conventional-commit scope does not count"
      continue
    fi
    ok=1
    for i in $ids; do
      case "$fin" in *" $i "*) ;; *) ok=0; say "skipping $br — ticket $i is not finished, branch may hold partial work" ;; esac
    done
    [ "$ok" = "1" ] || { skipped=$((skipped+1)); continue; }
    if git merge --no-edit "$br" >/dev/null 2>&1; then
      merged=$((merged+1)); say "integrated $br ($ahead commit(s))"
    else
      # A conflict is NOT grounds to give up on the branch. Aborting and moving on leaves a ticket
      # reading finished with its work stranded on a branch nothing will ever merge — the exact
      # false-green this loop exists to prevent. It happened three times in one run (REM-20, REM-28,
      # HIP-14), and every one had to be resolved by hand afterwards. The abort here only restores a
      # clean tree so the REMAINING branches in this round can still be tried; the branch is handed to
      # resolve_conflicts() below, which is the stage that actually lands it.
      git merge --abort 2>/dev/null || true
      conflicted=$((conflicted+1))
      printf '%s\t%s\n' "$br" "$ids" >> "$CONFLICTS"
      say "CONFLICT integrating $br — deferred to the conflict resolver (ticket(s):$ids )"
    fi
  done < <(git worktree list --porcelain | awk '/^worktree /{print $2}')
  [ "$merged" -gt 0 ] && say "round integrated: $merged branch(es) merged into $(git rev-parse --abbrev-ref HEAD)"
  [ "$conflicted" -gt 0 ] && say "$conflicted branch(es) conflicted — handing them to the conflict resolver"
  [ "$skipped" -gt 0 ] && say "$skipped branch(es) skipped as unproven — their commits stay on their branches"
  return 0
}

# Queue EVERY unmerged worker branch for resolution, not just this round's conflicts.
#
# integrate_round only ever sees the current round's worktrees, and reap_branches regresses a lying
# ticket but never merges one. So a branch stranded by an EARLIER round — one that conflicted before
# the resolver existed, or whose session died mid-merge — is invisible to both and simply accumulates.
# One project reached ELEVEN, three of them with tickets still reading finished, and none of it was
# noticed until someone counted branches by hand. Sweeping here makes that state unreachable: every
# round lands every outstanding branch, so "orphan" is a condition that cannot survive a single round.
queue_orphan_branches(){
  cd "$WT" || return 0
  unset AF_WT_BRANCH_CACHE   # each pass re-reads the worktree set the ownership rule is derived from
  local br ids queued=0 already
  while read -r br; do
    [ -n "$br" ] || continue
    af_is_owed_merge "$br" || continue
    git merge-base --is-ancestor "$br" HEAD 2>/dev/null && continue        # already landed
    already=$(cut -f1 "$CONFLICTS" 2>/dev/null | grep -xF "$br" || true)
    [ -n "$already" ] && continue                                          # this round already queued it
    ids=$(af_branch_ids "HEAD..$br")
    printf '%s\t%s\n' "$br" "$ids" >> "$CONFLICTS"
    queued=$((queued+1))
    say "orphan branch from an earlier round queued for landing: $br (ticket(s):$ids )"
  done < <(git for-each-ref --format='%(refname:short)' refs/heads/)
  [ "$queued" -gt 0 ] && say "$queued orphan branch(es) swept into this round's resolution"
  return 0
}

# ------------------------------------------------------------------------- conflict resolution --
#
# Integration conflicts used to end the branch's story: abort, warn, "needs a rebuild against the
# merged tree" — and nothing ever performed that rebuild, so the ticket stayed finished and its work
# stayed on a branch. Three tickets in one run reached that state, and resolving them by hand showed
# why a script could not: each needed JUDGEMENT, not mechanics. REM-20 conflicted because REM-30 had
# deleted a guard file it stamped and rewritten another; the correct merge kept REM-30's structure,
# left the deleted file deleted, and applied REM-20's stamp to the guards that now existed — a
# mechanical resolution would have resurrected a deliberately-removed file and restored a doc comment
# that no longer described the code.
#
# So the resolver is an agent, dispatched exactly like verify_round: it has the diff, both sides, the
# repo's gates, and the ability to reason about intent. What it may NOT do is fake a resolution —
# taking one side wholesale to make the merge succeed is worse than not merging, because it silently
# discards work while reporting success. When it genuinely cannot resolve, it regresses the ticket
# with a report, which is the honest outcome and re-queues the work.
resolve_conflicts(){   # $1 = round number
  local rnd="$1" n
  [ -s "$CONFLICTS" ] || return 0
  n=$(wc -l < "$CONFLICTS" | tr -d ' ')
  say "conflict resolver: $n branch(es) to land for round #$rnd"

  local rsession="af-resolve-$(basename "$WT")"
  tmux kill-session -t "$rsession" 2>/dev/null || true
  tmux new-session -d -s "$rsession" -c "$WT"
  # LAUNCH THE AGENT. Omitting this line leaves a bare login shell in the pane, and every later
  # send-keys types the prompt at a bash prompt instead of into Claude: bash dies on the first
  # parenthesis, the resolver reports itself dispatched, and NOT ONE branch is merged. That is
  # exactly how a project reached 11 orphans while the log claimed the resolver ran each round.
  tmux send-keys -t "$rsession" "cd $WT && $CLAUDE_LAUNCH" Enter
  local rready=0 i
  for i in $(seq 1 60); do
    sleep 2
    pane=$(tmux capture-pane -p -t "$rsession" 2>/dev/null || true)
    if echo "$pane" | grep -qE "bypass permissions on"; then rready=1; break; fi
  done
  # A pane that never signals ready has NO AGENT in it. Sending the prompt anyway is strictly worse
  # than not sending it: it silently does nothing while logging as though resolution was attempted,
  # and the branches stay stranded with no signal that anyone must intervene. Fail loudly instead and
  # leave $CONFLICTS intact so the next round — or a rerun — still sees every branch as owed.
  if [ "$rready" != "1" ]; then
    say "FATAL: conflict resolver pane never signalled ready — no agent in the session, NOT sending the prompt"
    say "FATAL: $n branch(es) remain unmerged and still queued in $CONFLICTS; re-run --resolve-orphans once the model backend is healthy"
    tmux kill-session -t "$rsession" 2>/dev/null || true
    return 1
  fi
  sleep 3; tmux send-keys -t "$rsession" Enter

  tmux send-keys -t "$rsession" "You are resolving MERGE CONFLICTS for build round $rnd of project $PROJECT, in the checkout at $WT which is already on the integration branch. Each line of $CONFLICTS is a TAB-separated branch name and the ticket id(s) whose work it carries. These branches were built and passed their own gates; they conflict only because sibling work landed first. Do NOT build features, do NOT claim tickets, do NOT push. THE ONE ABSOLUTE RULE: EVERY branch listed MUST end up merged. Leaving a branch unmerged is not an available outcome — a branch nobody merges strands a ticket that reads finished with its work nowhere, and that is the failure this stage exists to eliminate. You always finish with a merge commit for every branch. For EACH branch in order: run git merge --no-ff <branch>, and resolve every conflicted file by UNDERSTANDING BOTH SIDES rather than picking one. Conflicts here are almost always semantic: a file one side deleted and the other edited, a helper one side moved and the other extended, a registry both sides appended to. Keep the intent of BOTH changes wherever you honestly can. If one side DELETED a file the other modified, the deletion almost always wins and the other side change must be re-applied to whatever replaced it — read the deleting commit message to find what superseded it, and never resurrect a deliberately deleted file. When a specific hunk genuinely CANNOT preserve both intents — the two changes are contradictory, or choosing needs a product decision you cannot make — do NOT stall and do NOT abandon the branch. Resolve that hunk by taking the INTEGRATION side (the tree as it already is, which is proven), finish the merge, and record precisely what you dropped: which ticket owned it, which file and behaviour was lost, and what a rebuild has to re-establish. Dropping intent is acceptable ONLY when it is recorded — the ticket is then rebuilt from the current tree, which is the honest repair. Silently taking one side to make a merge succeed is the one thing you must never do. After each branch, PROVE the merged tree: run the repo build and typecheck, and the tests covering the files you touched. If a merge you just made breaks the tree and you cannot fix it, still keep the merge but record the whole branch as dropped-intent so its ticket is rebuilt. Commit each merge with a message naming the branch, the ticket id(s), what conflicted, what you kept from each side, and anything you dropped. When every branch is merged, write JSON to $RESOLVED with exactly these keys: merged which is an array of EVERY branch name you merged (this must list every branch in $CONFLICTS — there is no other outcome), and dropped_intent which is an array of objects each with branch, tickets, and reason stating concretely what was lost and what a rebuild must re-establish (empty array if you preserved everything). Write that file LAST and then STOP. You are running HEADLESS with no human attached: never ask a clarifying question. Work ONLY inside $WT, on the already-checked-out branch, and do NOT push."
  sleep 3; tmux send-keys -t "$rsession" Enter

  local waited=0 rstall=0 rlast="" pane rhash
  while [ "$waited" -lt "${AF_RESOLVE_TIMEOUT_S:-2400}" ]; do
    sleep 30; waited=$((waited+30))
    [ -f "$RESOLVED" ] && break
    if ! tmux has-session -t "$rsession" 2>/dev/null; then say "conflict resolver: session gone before writing a result"; break; fi
    pane=$(tmux capture-pane -p -t "$rsession" 2>/dev/null || true)
    rhash=$(printf '%s' "$pane" | hash_text)
    if [ "$rhash" = "$rlast" ]; then
      rstall=$((rstall+1))
      if [ "$rstall" -ge "$STALL_POLLS" ] && verify_children_busy "$rsession"; then rstall=0; fi
      [ "$rstall" -ge "$STALL_POLLS" ] && { say "conflict resolver frozen — giving up on this round's conflicts"; break; }
    else
      rstall=0; rlast="$rhash"
    fi
  done
  tmux kill-session -t "$rsession" 2>/dev/null || true

  if [ ! -f "$RESOLVED" ]; then
    say "WARNING: conflict resolver produced NO result — $n branch(es) remain unmerged and their tickets still read finished. They will be caught by post-merge verification, which regresses them."
    : > "$CONFLICTS"; return 0
  fi
  # Anything the resolver could not land is regressed HERE, with its reason, rather than left to rot:
  # a finished ticket whose work is not on the branch is a lie, and the honest repair is to re-queue it.
  $PY - "$PROJECT" "$rnd" "$RESOLVED" "$WT" "$CONFLICTS" <<'PYEOF' 2>&1 | while IFS= read -r l; do say "$l"; done
import json, subprocess, sys
import _praxis, _ticket_state as ts
proj, rnd, path, wt, conflicts = sys.argv[1:6]
kw = dict(space=proj, snapshot=f"prd-{proj}")

def git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True, cwd=wt)

expected = {}
for line in open(conflicts):
    if line.strip():
        br, _, ids = line.partition("\t")
        expected[br.strip()] = ids.split()

try:
    d = json.load(open(path))
except Exception as e:
    print(f"conflict resolver: unreadable result ({e}) — falling back to the enforcement sweep")
    d = {}

merged = set(d.get("merged") or [])
dropped = {str(u.get("branch")): u for u in (d.get("dropped_intent") or [])}

# ENFORCEMENT. The agent is INSTRUCTED to merge every branch, but a guarantee that depends on an
# agent following instructions is not a guarantee. So verify against git itself: for every branch we
# handed it, is that branch now an ancestor of HEAD? Anything that is not gets merged here, taking the
# integration side for conflicting hunks, and its ticket regressed — so the invariant holds even if the
# resolver died, timed out, or ignored the rule.
for br, tickets in expected.items():
    if git("rev-parse", "--verify", br).returncode != 0:
        print(f"conflict resolver: {br} no longer exists — nothing to land")
        continue
    if git("merge-base", "--is-ancestor", br, "HEAD").returncode == 0:
        print(f"conflict resolver: LANDED {br}")
        continue
    print(f"conflict resolver: {br} was NOT landed by the resolver — forcing it in (integration side wins)")
    git("merge", "--abort")
    r = git("merge", "--no-ff", "--no-commit", br)
    if r.returncode != 0:
        # take the integration side for every conflicted path, then commit: the merge lands either way
        for fp in git("diff", "--name-only", "--diff-filter=U").stdout.split():
            git("checkout", "--ours", "--", fp); git("add", "--", fp)
    git("commit", "--no-edit", "-m",
        f"merge: {br} (round #{rnd}) — forced by the conflict-resolution enforcement sweep; "
        f"integration side kept for conflicting hunks, ticket(s) {' '.join(tickets)} regressed to rebuild")
    if git("merge-base", "--is-ancestor", br, "HEAD").returncode == 0:
        print(f"conflict resolver: FORCE-LANDED {br}")
        dropped.setdefault(br, {"branch": br, "tickets": tickets,
                                "reason": "the resolver did not land this branch; the enforcement sweep merged it "
                                          "taking the integration side for every conflicting hunk, so this ticket's "
                                          "change is NOT present and must be rebuilt"})
    else:
        print(f"conflict resolver: CRITICAL — could not force-land {br}; regressing its ticket(s) anyway")
        dropped.setdefault(br, {"branch": br, "tickets": tickets,
                                "reason": "branch could not be merged even by the enforcement sweep"})

# Any branch whose intent was dropped -> its ticket goes back in the queue with the reason attached.
by_rid = {}
for f in _praxis.facts_by(category="requirement", **kw) or []:
    m = f.get("meta") or {}
    by_rid[str(m.get("requirement_id") or f.get("id"))] = f
for br, u in dropped.items():
    reason = str(u.get("reason") or "intent dropped during conflict resolution")
    sha = git("rev-parse", br).stdout.strip()[:12]
    for rid in (u.get("tickets") or expected.get(br) or []):
        f = by_rid.get(str(rid))
        if not f:
            continue
        new_finding = {"round": rnd, "source": "conflict-resolution", "branch": br,
                      "merged_but_intent_dropped": True, "abandoned_sha": sha,
                      "reason": "branch merged, but this ticket's change was not preserved",
                      "evidence": reason,
                      "required_fix": "re-establish the behaviour against the current integrated tree; "
                                      "do NOT re-merge the branch, it is already an ancestor of HEAD"}
        # R16/E3: accumulate onto this ticket's existing regression_detail — a concurrent finding
        # (post-merge verification, ingestion) must never be clobbered by this one.
        accumulated = ts.accumulate_regression_detail((f.get("meta") or {}).get("regression_detail"), new_finding)
        _praxis.regress_requirements(proj, [f["id"]], {f["id"]: {
            "claim_owner": None, "claim_at": None,
            "claim_heartbeat_at": None, "claim_lease_ttl": None,
            "audit_disposition": (f"REGRESSED by conflict resolution of round #{rnd}: branch {br} was merged, but this "
                                  f"ticket's intent did not survive. WHAT WAS LOST: {reason} THE REBUILD MUST: re-establish "
                                  f"that behaviour against the CURRENT integrated tree."),
            "regression_detail": accumulated}},
            **kw)
        print(f"conflict resolver: regressed {rid} — merged, but its intent was dropped")

# Final invariant, checked against git and not against anyone's report.
left = [b for b in expected if git("rev-parse", "--verify", b).returncode == 0
        and git("merge-base", "--is-ancestor", b, "HEAD").returncode != 0]
print(f"conflict resolver: INVARIANT {'HOLDS' if not left else 'VIOLATED'} — unmerged branches remaining: {len(left)}"
      + (f" {left}" if left else ""))
PYEOF
  rm -f "$RESOLVED"; : > "$CONFLICTS"
  return 0
}

# Sweep the round's worktrees. af-build is told to reap its own, but a session that died mid-round
# leaves them behind and they accumulate across rounds — one observed run stranded 29, each holding a
# full dependency tree, and filled the volume. A worktree whose branch is already an ancestor of the
# integration branch is pure scratch and is removed; one that is NOT merged still holds unintegrated
# commits, so it is reported rather than deleted. Removing a worktree keeps its branch either way.
# Scratch roots this loop may reap, enumerated rather than assumed. Trees have been created in BOTH
# layouts: <repo>/.claude/worktrees/<id> (Agent `isolation: worktree`) and <repo>_worktrees/<TICKET>
# (the older per-ticket layout). The sweep used to match only the first, so six trees under
# /workspace/appeal_engine_worktrees survived every sweep this driver ever ran and had to be removed
# by hand. Anything outside these roots — the main checkout, a sibling project — is never touched.
af_scratch_roots(){
  printf '%s\n' "$WT/.claude/worktrees" "${WT}_worktrees"
}

# A third layout, SIBLING to the checkout rather than under a root: `<WT>-wt-<TICKET>`. The sotos run
# of 2026-08-03 left /workspace/sotos-wt-HIP23 and /workspace/sotos-wt-HIP27 behind, and neither was
# under either root above, so every sweep this driver ran walked straight past them. Matched as a
# glob, not a root, because there is no directory to enumerate.
af_scratch_globs(){
  printf '%s\n' "${WT}-wt-*" "${WT}-worktree-*" "${WT}_wt_*"
}

af_is_scratch(){   # $1 = candidate path
  local p="$1" root g
  while read -r root; do
    case "$p" in "$root"/*) return 0 ;; esac
  done < <(af_scratch_roots)
  while read -r g; do
    case "$p" in $g) return 0 ;; esac
  done < <(af_scratch_globs)
  return 1
}

# LOCKED worktrees. `git worktree remove` and `git worktree prune` both REFUSE a locked tree, so a
# sweep that does not unlock first silently no-ops on exactly the trees most likely to be stale --
# two of the four leftovers from the sotos run were locked, and every sweep "succeeded" without
# touching them. Unlock, then remove; `--force --force` is the fallback for a git old enough or a
# lock stubborn enough that `unlock` did not take. A failure after all three is REPORTED LOUDLY, never
# swallowed: the whole point is that a sweep which cannot do its job says so.
af_force_remove_worktree(){   # $1 = path
  git worktree unlock "$1" >/dev/null 2>&1 || true
  git worktree remove --force "$1" >/dev/null 2>&1 && return 0
  git worktree remove --force --force "$1" >/dev/null 2>&1 && return 0
  return 1
}

sweep_worktrees(){
  cd "$WT" || return 0
  local kept=0
  unset AF_WT_BRANCH_CACHE   # this function mutates the worktree set the ownership rule reads
  while read -r path; do
    [ -n "$path" ] || continue
    [ "$path" = "$WT" ] && continue
    # ONLY agent scratch trees are removable. `git worktree list` reports every worktree of the
    # repo, which includes the MAIN CHECKOUT and every sibling project worktree — and a build branch
    # is normally ahead of main, so `is-ancestor main HEAD` is TRUE and the merged-check below would
    # have happily deleted the factory checkout that all three loops execute from. Observed on the
    # first real sweep, which reported "/workspace/praxis holds UNMERGED commits" — it was one
    # ancestry check away from removing the repo root. Scratch trees live under one of the roots
    # af_scratch_roots names; nothing else is this function's business.
    af_is_scratch "$path" || continue
    # Never yank a tree out from under a live worker: its evals are executing against these files.
    if [ -n "$(ls /proc/*/cwd 2>/dev/null | head -1)" ] && \
       readlink /proc/*/cwd 2>/dev/null | grep -qF "$path"; then
      say "worktree $path is IN USE by a live process — skipping"
      continue
    fi
    local head; head=$(git -C "$path" rev-parse HEAD 2>/dev/null || echo "")
    # PURGE unconditionally. Removing a worktree KEEPS its branch, so an unintegrated commit is not
    # lost by this -- it stays reachable on worktree-agent-* and is merged by integrate_round once
    # its ticket is finished. Leaving the trees instead is what put 29 of them on one box and filled
    # a 98GB volume, and what left 14 lying around here holding 10+ commits each. The tree is
    # scratch; the branch is the artifact.
    if [ -n "$head" ] && git merge-base --is-ancestor "$head" HEAD 2>/dev/null; then
      af_force_remove_worktree "$path" \
        && say "purged integrated worktree $path" \
        || say "WARNING: could not purge integrated worktree $path — it may be locked by a process that outlived its run; remove it by hand"
    else
      kept=$((kept+1))
      # Name the branch, read from the worktree BEFORE it is removed. Resolving it from a commit sha
      # afterwards yields nothing, which is what made every one of these lines read "remain on
      # branch " with a blank -- the exact pointer someone needs to find the work later.
      local wbr; wbr=$(git -C "$path" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
      af_force_remove_worktree "$path" \
        && say "purged worktree $path — its commits remain on branch ${wbr:-<detached ${head:0:8}>}" \
        || say "WARNING: could not purge $path (branch ${wbr:-<detached ${head:0:8}>}) — remove it by hand; the terminal straggler invariant will keep failing until it is gone"
    fi
  done < <(git worktree list --porcelain | awk '/^worktree /{print $2}')
  git worktree prune 2>/dev/null || true

  # ORPHANED DIRECTORIES. A tree whose registration is already gone is invisible to
  # `git worktree list`, and `git worktree prune` drops the stale registration WITHOUT deleting the
  # directory it pointed at — so a registry-driven sweep can never reach it, however often it runs.
  # Observed 2026-08-02: .claude/worktrees held seven directories and exactly one live registration;
  # the other six were unreachable by any sweep and had to be rm -rf'd by hand. Reap by directory
  # too, not only by registry.
  # Membership is tested against the REGISTRY, not by asking git about the directory. `git -C <dir>
  # rev-parse --git-dir` walks UPWARD and finds the enclosing repo, so it succeeds for every plain
  # directory inside the checkout — the first version of this check used it and skipped 100% of
  # orphans as "still live". Verified by test before shipping.
  local root d registered
  registered=$(git worktree list --porcelain | awk '/^worktree /{print $2}')
  while read -r root; do
    [ -d "$root" ] || continue
    for d in "$root"/*; do
      [ -d "$d" ] || continue
      printf '%s\n' "$registered" | grep -qxF "$d" && continue
      if readlink /proc/*/cwd 2>/dev/null | grep -qF "$d"; then
        say "orphan dir $d is IN USE by a live process — skipping"
        continue
      fi
      rm -rf "$d" && say "removed orphaned worktree dir $d"
    done
    rmdir "$root" 2>/dev/null || true
  done < <(af_scratch_roots)
  # Same treatment for the sibling `<WT>-wt-<TICKET>` layout, which has no root directory to walk.
  local g
  while read -r g; do
    for d in $g; do
      [ -d "$d" ] || continue
      printf '%s\n' "$registered" | grep -qxF "$d" && continue
      if readlink /proc/*/cwd 2>/dev/null | grep -qF "$d"; then
        say "orphan dir $d is IN USE by a live process — skipping"
        continue
      fi
      rm -rf "$d" && say "removed orphaned worktree dir $d"
    done
  done < <(af_scratch_globs)

  unset AF_WT_BRANCH_CACHE
  [ "$kept" -gt 0 ] && say "$kept worktree(s) purged before integration — their branches survive and merge once their tickets finish"
  return 0
}

# ------------------------------------------------------------------------------ branch reaping --
#
# A finished ticket owns no branches. A completed run leaves ZERO worker branches -- not fewer, zero.
#
# Worktree removal deliberately KEEPS the branch, and that is the safety property that makes purging a
# scratch tree lossless: the tree is scratch, the branch is the artifact. But nothing ever revisited
# the branch afterwards. One branch is created per worker, per ticket, per round, and lived forever.
# Measured on /workspace/appeal_engine, three runs of an 11-ticket project: 38 branches, of which 34
# were fully merged into main -- pure residue -- and 4 "unmerged", none of which held work worth
# keeping. One was already upstream by patch-id, two were attempts the post-merge verifier REJECTED
# and rebuilt (main carries the corrected versions; one differed only by the five lint-baseline lines
# that made the rebuild pass), and one was a stale baseline re-anchor superseded months of commits ago.
#
# The cost is not disk. "Unmerged branch" is supposed to be a loud, rare signal meaning THIS WORK NEVER
# LANDED, LOOK AT IT -- and it was buried under dozens of identical worktree-agent-* names that meant
# nothing. A cleanup pass that leaves residue does not merely waste space, it destroys the signal the
# same system depends on to report its real failures.
#
# Four dispositions, decided per branch, and only the first two delete anything:
#   REAPED      no commit of its own is missing from the integration ref (ancestry, or patch-equivalent
#               upstream after a rebuild/cherry-pick). Nothing to recover; deleting it loses nothing.
#   SUPERSEDED  it holds unique commits, but a LATER commit for the same ticket did land. The verifier
#               rejected this attempt and a rebuild replaced it, so it is dead by construction.
#   FAILURE     it holds unique commits for a ticket Praxis calls `finished`, and NOTHING for that
#               ticket ever landed. The ticket is lying. Keep the branch, regress the ticket, fail the
#               round loudly. This exact case happened and was caught only by luck: round #2's verifier
#               noticed "LADDER-1 (5a3ddd6) was never merged into main (only on branch
#               worktree-agent-a2bece339fa2dbd7d)". Branch bookkeeping did not, and would have left it
#               sitting there forever looking like ordinary residue.
#   SURVIVOR    unique commits whose ticket is NOT finished, or whose provenance cannot be established.
#               Work still in flight. Reported by name, never deleted.
#
# Equivalence is tested with `git cherry`, not ancestry. Plain ancestry misses a rebuilt or
# cherry-picked equivalent, which is exactly what made three of those four branches look unmerged.

# ------------------------------------------------------------------ WHO OWNS A BRANCH --
#
# TWO questions, deliberately answered by two different rules, because they carry opposite risks:
#
#   "is this branch OWED A MERGE?"   -> af_is_owed_merge. DEFAULT-INCLUSIVE. Getting it wrong means a
#                                       ticket reads finished with its work nowhere. Being over-broad
#                                       costs a log line; being under-broad costs the whole guarantee.
#   "may this driver DELETE it?"     -> af_is_factory_named. DELIBERATELY NARROW. Getting it wrong
#                                       destroys someone's work. Nothing here ever deletes a branch
#                                       carrying commits that are not already upstream.
#
# This split exists because the single old test was an ALLOWLIST OF NAMES -- worktree-agent-*,
# worktree-wf_*, build/<known-id> -- used for BOTH questions. On 2026-08-03 a sotos run finished all
# 260 tickets, logged `claimable=0` and `drained -- nothing claimable`, and left behind two branches
# named `af-build/HIP-23` and `af-build/HIP-27` holding commits that were on no integration branch,
# plus two LOCKED `.claude/worktrees/agent-<hex>` trees. Nothing in this script mints `af-build/*`; a
# worker/skill chose that name. The allowlist did not match it, so queue_orphan_branches skipped it,
# reap_branches skipped it, and the round reported success. Two tickets read `finished` with their
# work nowhere -- precisely the lie every function in this section exists to make impossible.
#
# A guarantee that depends on guessing every name a worker might invent is not a guarantee, and its
# failure mode is SILENCE. So ownership is now derived from FACTS:
#
#   FACT 1  a branch checked out in a worktree of this repo is factory work by definition. Name
#           irrelevant: `git worktree list --porcelain` is authoritative.
#   FACT 2  every other local branch is owed a merge UNLESS a human explicitly owns it. The exemption
#           list is short, explicit, and extendable only on purpose (AF_HUMAN_BRANCHES).
#
# A NEW name cannot be missed, because no name is required to match anything.

# The ONLY way out of "owed a merge". Kept narrow on purpose: the integration branch itself, the
# handful of universally-human trunk/release refs, and whatever a human deliberately registers in
# AF_HUMAN_BRANCHES (space-separated glob patterns, ADDED to the defaults rather than replacing them,
# so nobody can accidentally exempt everything by setting it).
af_is_human_branch(){   # $1 = branch name
  local b="$1" p head_br
  local defaults='main master develop trunk release/* releases/* hotfix/*'
  head_br=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || echo "")
  [ -n "$head_br" ] && [ "$b" = "$head_br" ] && return 0
  [ -n "${INTEGRATION_REF:-}" ] && [ "$b" = "$INTEGRATION_REF" ] && return 0
  for p in $defaults ${AF_HUMAN_BRANCHES:-}; do
    case "$b" in $p) return 0 ;; esac
  done
  return 1
}

# FACT 1. Checked out in some worktree OTHER than the integration checkout => factory work, whatever
# it is called. This is the name-independent half of the rule and it is why an invented prefix cannot
# hide: the worker had to check the branch out somewhere to commit on it.
af_is_worktree_branch(){   # $1 = branch name
  local b
  # Cached per pass: this is consulted once per local branch by three callers, and a repo with a few
  # hundred branches would otherwise fork `git worktree list` a thousand times a round. Every mutator
  # of the worktree set (sweep_worktrees) clears it, and a STALE cache errs toward "owned", which is
  # the safe direction for a default-inclusive rule.
  if [ -z "${AF_WT_BRANCH_CACHE+x}" ]; then
    AF_WT_BRANCH_CACHE=$(git worktree list --porcelain 2>/dev/null \
      | awk -v self="${WT:-}" '/^worktree /{p=$2} /^branch /{sub("refs/heads/","",$2); if (p != self) print $2}')
  fi
  while read -r b; do
    [ -n "$b" ] || continue
    [ "$b" = "$1" ] && return 0
  done <<< "$AF_WT_BRANCH_CACHE"
  return 1
}

# Default-inclusive. Used by queue_orphan_branches, reap_branches and the terminal invariant.
af_is_owed_merge(){   # $1 = branch name
  af_is_worktree_branch "$1" && return 0
  af_is_human_branch "$1" && return 1
  return 0
}

# Narrow, and the ONLY gate on deletion. `worktree-agent-*`/`worktree-wf_*` are minted by the
# Agent/Workflow `isolation: worktree` machinery and carry no human meaning. Beyond those, a branch is
# ours when its LAST path segment is a requirement id Praxis says THIS PROJECT owns -- which is a
# fact, not a name guess, and covers `build/HIP-23`, `af-build/HIP-23` and any future prefix alike,
# while never matching a human's `build/login-redesign` or a sibling project's ticket. An empty
# AF_KNOWN_IDS therefore makes this answer NO (nothing is deleted), never yes -- and, crucially, it no
# longer makes af_is_owed_merge answer no, which is the second silent-miss path the old code had.
af_is_factory_named(){   # $1 = branch name
  case "$1" in worktree-agent-*|worktree-wf_*) return 0 ;; esac
  case "${AF_KNOWN_IDS:-}" in *" ${1##*/} "*) return 0 ;; esac
  return 1
}

# Ticket ids named by a branch's commits. Filtered to ids we own when Praxis has told us what we own;
# unfiltered when it has not, because "we could not ask" must never read as "no ids, skip it". The
# hardcoded `(REM|SSW|HIP|...)` prefix alternation this replaces was a third allowlist, and a project
# whose ids used a new prefix would have gone unqueued in exactly the same silent way.
af_branch_ids(){   # $1 = git range
  case "${AF_KNOWN_IDS:-}" in
    ''|' ')
      git log --format=%s "$1" 2>/dev/null \
        | sed -n 's/.*(\([A-Za-z][A-Za-z0-9_-]*[0-9][0-9]*\))[[:space:]]*$/\1/p' | sort -u | tr '\n' ' '
      ;;
    *) af_owned_ids "$1" "$AF_KNOWN_IDS" ;;
  esac
}

reap_branches(){
  cd "$WT" || return 0
  unset AF_WT_BRANCH_CACHE
  local br live uniq ids i sup reaped=0 failed=0 survivors="" reason status head_br
  # Compare against HEAD, never against the INTEGRATION_REF captured at startup. On a detached
  # checkout -- which is what both build worktrees on this box are -- INTEGRATION_REF is a fixed sha
  # that does NOT move as integrate_round merges into HEAD, so every branch this round just landed
  # would still read as unique work, and a finished ticket's branch would be reported as a hard
  # failure. HEAD is what integrate_round merged into, so HEAD is what "did it land" means here.
  head_br=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || echo "")
  # A branch checked out anywhere -- this checkout, a live worker's tree, a sibling project sharing the
  # ref store -- is in flight. git refuses to delete it anyway; skipping first keeps the log honest.
  live=$(git worktree list --porcelain | awk '/^branch /{sub("refs/heads/","",$2); print $2}')
  while read -r br; do
    [ -n "$br" ] || continue
    [ -n "$head_br" ] && [ "$br" = "$head_br" ] && continue
    af_is_owed_merge "$br" || continue
    printf '%s\n' "$live" | grep -qxF "$br" && continue
    # `+` = a commit with no equivalent upstream; `-` = already there under a different sha.
    uniq=$(git cherry HEAD "$br" 2>/dev/null | sed -n 's/^+ //p')
    if [ -z "$uniq" ]; then
      # Fully upstream, so nothing can be lost either way -- but DELETION is still gated on the narrow
      # test. The iteration above is default-inclusive so that no straggler can hide behind a novel
      # name; that breadth must not turn into a licence to delete a human's already-merged `fix/*`.
      # Not ours to delete and nothing is owed: leave it, silently, as before.
      af_is_factory_named "$br" || continue
      # Let git enforce the safe case rather than the caller's belief about it. `-d` refuses anything
      # it does not consider merged, so it is tried FIRST and its refusal is meaningful. It refuses a
      # patch-equivalent branch too (ancestry is all it knows), and that is the one case where `git
      # cherry` is the better authority -- so `-D` is reached only after cherry has already proven
      # every commit is upstream, and it says so in the log.
      if [ "${AF_KEEP_BRANCHES:-0}" = "1" ]; then
        survivors="${survivors:+$survivors }$br"
      elif git branch -d "$br" >/dev/null 2>&1; then
        reaped=$((reaped+1))
      elif git branch -D "$br" >/dev/null 2>&1; then
        reaped=$((reaped+1))
        say "reaped $br — not an ancestor of ${head_br:-HEAD}, but every commit of it is already upstream by patch-id"
      else
        survivors="${survivors:+$survivors }$br"
        say "WARNING: could not delete $br"
      fi
      continue
    fi
    # Unique commits. Whose, and did that ticket land some other way?
    ids=$(af_owned_ids "HEAD..$br" "${AF_KNOWN_IDS:- }")
    status=superseded; reason=""
    if [ -z "$ids" ]; then
      status=survivor; reason="its commits name no $PROJECT ticket, so its provenance cannot be established"
    fi
    for i in $ids; do
      # A commit naming this ticket that is on the integration ref and NOT on this branch is a landed
      # replacement -- the rebuild that superseded this attempt.
      sup=$(git log -E --grep="\($i\)[[:space:]]*\$" --format='%h %s' -n 1 HEAD --not "$br" 2>/dev/null || true)
      if [ -n "$sup" ]; then
        reason="${reason:+$reason; }$i superseded by ${sup%% *}"
        continue
      fi
      case "${AF_FINISHED_IDS:- }" in
        *" $i "*)
          status=failure
          say "ROUND FAILED — ticket $i reads finished in Praxis, but its work is NOT on ${head_br:-HEAD} and no replacement for it landed. The commits exist only on branch $br:"
          git log --format='  %h %s' "HEAD..$br" 2>/dev/null | while read -r l; do say "$l"; done
          if praxis_q regress_ticket "$i" "$br" >/dev/null; then
            say "regressed $i to incomplete — the next round rebuilds it. Branch $br is KEPT."
          else
            say "WARNING: could not regress $i in Praxis — it will keep reading finished while its work sits unmerged on $br. Fix by hand."
          fi
          ;;
        *)
          [ "$status" = failure ] || status=survivor
          reason="${reason:+$reason; }$i is not finished — work still in flight"
          ;;
      esac
    done
    case "$status" in
      superseded)
        # Same asymmetry as above: this is the one reap that drops commits which are upstream in NO
        # form, so it happens only for a branch the narrow test proves is factory-minted. Anything
        # else is kept and reported, however superseded it looks.
        if [ "${AF_KEEP_BRANCHES:-0}" = "1" ] || ! af_is_factory_named "$br"; then
          survivors="${survivors:+$survivors }$br"
        else
          # Named before deletion, with the tip sha, because this is the one reap that drops commits
          # that are not upstream in any form. The sha keeps them recoverable until gc.
          say "reaped $br (tip $(git rev-parse --short "$br" 2>/dev/null)) — a superseded attempt: $reason"
          git branch -D "$br" >/dev/null 2>&1 && reaped=$((reaped+1)) || say "WARNING: could not delete $br"
        fi
        ;;
      failure)  failed=$((failed+1)); survivors="${survivors:+$survivors }$br" ;;
      survivor) survivors="${survivors:+$survivors }$br"; say "keeping $br — $reason" ;;
    esac
  done < <(git for-each-ref --format='%(refname:short)' refs/heads/)

  # The report line. An empty survivor list is the NORMAL case and has to be visibly normal, or a real
  # orphan goes on looking exactly like the residue this whole function exists to delete.
  set -- $survivors
  say "$reaped branches reaped, $# unmerged branches remain:${survivors:+ $survivors}"
  [ "$failed" -gt 0 ] && { say "$failed ticket(s) were marked finished with their work unmerged — see above. This round FAILED its branch-integrity check."; return 1; }
  return 0
}

# -------------------------------------------------------------- the terminal straggler invariant --
#
# Everything above is best-effort machinery. THIS is the thing that makes "no stragglers" a property
# of the run rather than a hope, because it is checked against git immediately before the loop is
# allowed to say the word "drained".
#
# The gap it closes: on 2026-08-03 a sotos run logged `claimable=0` and `drained -- nothing claimable`
# while two branches held unmerged commits for tickets Praxis called finished, and two LOCKED
# worktrees sat at the pre-build commit. Every individual stage had "succeeded". There was a
# HOLDS/VIOLATED assertion, but it lived inside the conflict resolver and only ever looked at the
# branches that resolver had been handed -- so a branch nothing queued was invisible to it too. An
# invariant that is only consulted by the code path that already knew about the problem is not an
# invariant.
#
# Two facts are asserted, both read from git, neither from any stage's report:
#   1. ZERO branches that are owed a merge and hold commits upstream carries in no form.
#   2. ZERO leftover worktrees -- any scratch tree at all, plus any other worktree of this repo
#      sitting on a branch that is owed a merge.
# Note (2) reports trees OUTSIDE the sweepable roots as well. Sweeping stays narrow (only delete what
# is provably scratch) while reporting stays broad, so an unfamiliar layout produces a loud failure
# instead of a silent miss -- the sotos `<WT>-wt-<ID>` trees were exactly that shape.
af_stragglers(){   # prints one line per straggler; EMPTY output means clean
  cd "$WT" 2>/dev/null || return 0
  local br p wt_br
  unset AF_WT_BRANCH_CACHE   # read the world fresh: this is the assertion, not a fast path
  while read -r br; do
    [ -n "$br" ] || continue
    af_is_owed_merge "$br" || continue
    git merge-base --is-ancestor "$br" HEAD 2>/dev/null && continue
    # `git cherry`, same authority reap_branches uses: a rebuilt or cherry-picked equivalent has
    # landed and is not a straggler, however its sha reads.
    [ -n "$(git cherry HEAD "$br" 2>/dev/null | sed -n 's/^+ //p')" ] || continue
    printf 'unmerged branch %s (%s commit(s) not on %s)\n' "$br" \
      "$(git rev-list --count "HEAD..$br" 2>/dev/null || echo '?')" \
      "$(git symbolic-ref --quiet --short HEAD 2>/dev/null || git rev-parse --short HEAD 2>/dev/null)"
  done < <(git for-each-ref --format='%(refname:short)' refs/heads/)
  while read -r p; do
    [ -n "$p" ] || continue
    [ "$p" = "$WT" ] && continue
    wt_br=$(git -C "$p" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
    if af_is_scratch "$p"; then
      printf 'leftover worktree %s (branch %s)\n' "$p" "${wt_br:-<detached>}"
      continue
    fi
    [ -n "$wt_br" ] && [ "$wt_br" != HEAD ] && af_is_owed_merge "$wt_br" \
      && printf 'leftover worktree %s (branch %s)\n' "$p" "$wt_br"
  done < <(git worktree list --porcelain | awk '/^worktree /{print $2}')
  return 0
}

# Exit code 7 -- a NEW code, deliberately not reused. The existing semantics are 0 = clean drain or
# dependency stall, 1 = preflight, 3 = billing, 4/5/6 = unrecoverable-but-understood halts, and every
# one of those is a state an operator or supervisor already knows how to read. Folding a straggler
# into any of them would either make it look like a clean drain (the exact lie being fixed) or
# misattribute it to a cause that is not what happened. 7 means one thing: THE RUN LEFT WORK BEHIND.
AF_EXIT_STRAGGLERS=7

# $1 = where we are, $2 = 1 to make it fatal (terminal paths), 0 to log-and-continue (round tails,
# where an unmerged branch for a ticket still in flight is legitimate and the next round lands it).
# In BOTH modes a straggler forces a real resolution attempt first -- reporting is not the remedy.
af_assert_no_stragglers(){
  local where="$1" fatal="${2:-1}" s l
  s=$(af_stragglers)
  if [ -n "$s" ]; then
    say "STRAGGLERS PRESENT at $where — forcing a resolution pass before anything reports success:"
    printf '%s\n' "$s" | while read -r l; do say "  $l"; done
    : > "$CONFLICTS"
    queue_orphan_branches || true
    resolve_conflicts "straggler-sweep@$where" || true
    sweep_worktrees || true
    AF_QUERY_BACKOFF_S=0 reap_branches || true
    s=$(af_stragglers)
  fi
  if [ -z "$s" ]; then
    say "straggler invariant HOLDS at $where — zero unmerged worker branches, zero leftover worktrees"
    return 0
  fi
  say "STRAGGLER INVARIANT VIOLATED at $where — resolution ran and did NOT clear these:"
  printf '%s\n' "$s" | while read -r l; do say "  $l"; done
  if [ "$fatal" != "1" ]; then
    say "not terminal yet — the next round retries. If they are still here at drain the run FAILS."
    return 1
  fi
  say "This run is NOT clean and will not be reported as drained. Nothing was deleted: every branch above still holds its commits. Land them by hand, or rerun: af-ticket-loop.sh --resolve-orphans $PROJECT $WT"
  exit "${AF_EXIT_STRAGGLERS:-7}"
}

# Cleanup is an INVARIANT, not a happy-path step.
#
# sweep_worktrees used to run in exactly two places: the disk preflight, and after integrate_round
# inside the loop. Every OTHER way this script can end therefore leaked the round's trees — the
# DEPENDENCY STALL break, the Praxis-outage exit 6, the fruitless-round exit 4, exit 3, exit 5, and
# any operator `tmux kill-session`. On one box that left 8 trees holding 10GB, on a volume the same
# script halts the build to protect. A trap makes the sweep unconditional: whatever happens, the
# scratch trees go, and their branches (the actual artifacts) survive.
#
# Branch reaping rides the same trap for the same reason. It has to run AFTER the sweep, because git
# refuses to delete a branch that is still checked out in a worktree -- reaping first would report
# every one of the round's branches as undeletable. The reap needs no Praxis for its main job: whether
# a branch's commits are already upstream is a pure git question, and the cached id sets are only used
# to classify what is left over.
af_cleanup_on_exit(){
  local rc=$?
  set +e
  if [ -n "${WT:-}" ] && [ -e "${WT}/.git" ]; then
    sweep_worktrees || true
    # No backoff inside a signal handler. reap_branches may need to regress a lying ticket, and
    # praxis_q's linear backoff would sit here for ~2.5 minutes per attempt after an operator's
    # `tmux kill-session` — in the only case it can happen, a Praxis that is down and cannot accept
    # the write anyway. Still retries, just without sleeping through a shutdown.
    AF_QUERY_BACKOFF_S=0 reap_branches || true
  fi
  return "$rc"
}
trap af_cleanup_on_exit EXIT INT TERM

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
VERDICT="$AF_STATE_DIR/af-round-verdict-$PROJECT.json"
# Conflict-resolution handoff: integrate_round appends "<branch>\t<ticket ids>" here, and
# resolve_conflicts drains it. Per-project, so concurrent loops never read each other's.
CONFLICTS="$AF_STATE_DIR/af-round-conflicts-$PROJECT.tsv"
RESOLVED="$AF_STATE_DIR/af-round-resolved-$PROJECT.json"

# Told to the workers so they hit the right services. A project with neither -- an iOS app, say --
# gets no clause at all rather than the literal "Postgres localhost:none", which reads as a real
# host that is simply down and sends a worker debugging a connection that was never meant to exist.
SERVICES=""
[ -n "${PG:-}" ] && [ "$PG" != "none" ] && SERVICES=" Postgres localhost:$PG"
[ -n "${REDIS:-}" ] && [ "$REDIS" != "none" ] && SERVICES="$SERVICES${SERVICES:+,} Redis localhost:$REDIS"

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
  tmux send-keys -t "$vsession" "Post-merge verification of build round $rnd for project $PROJECT. Tickets just merged: $ids_csv. Each was built and validated ALONE in its own worktree, so the merged tree you are looking at has never been verified as a whole. Do NOT build features, do NOT claim tickets, do NOT start new work, do NOT push. Verify the integrated result only. Step 1: run the repo's whole-repo gates on the current merged tree — full test suite, build, repo-wide typecheck and lint. THIS IS THE ONLY PLACE THOSE GATES RUN: the workers were told to skip them so that N of them would not run N concurrent suites, so you are the authoritative repo-wide gate for this round and nothing else has proven the merged tree compiles or passes. If any gate is red, identify which ticket's change caused it and regress that ticket per step 3; if you genuinely cannot attribute it, regress the whole batch rather than passing a red tree. Step 2: dispatch INDEPENDENT parallel review subagents over the combined diff of this round, one per lens, each told to actively look for a failure rather than confirm success. If this round merged only ONE ticket, the cross-ticket lens is trivially satisfied and you may skip lens A, but step 1's gates and lenses B and C still run in full — they are the only repo-wide check this ticket gets. Lens A integration conflict: did two of these tickets edit the same module, config, migration, schema, or shared type in ways that are individually fine and jointly wrong, or did one silently revert another. Lens B acceptance survival: for EACH ticket id above, re-run its own acceptance test against the MERGED tree and confirm it still passes here, not just in its worktree. Lens C test integrity: did any ticket reach green by deleting, skipping, xfailing, narrowing assertions on, or excluding from config a test that used to run — treat that as a failure, not a pass. Step 3: NAME every ticket whose work does NOT survive integration. Do NOT write that regression to Praxis yourself and do not try to fix the ticket — the loop that dispatched you performs the regression from your verdict, using a write path it already owns. Your job is the judgement, not the write. This split is deliberate: when verifiers were asked to do their own Praxis write, nine consecutive rounds reported zero regressions while their own notes named the failing tickets, so every one of those tickets stayed marked finished on work that had failed integration. Step 4: write your verdict as JSON to $VERDICT with exactly these keys: verdict which is pass or fail, gates_green true or false, notes which is one short string, and regressed which is an array of OBJECTS — one per ticket that must be regressed, each with four string fields: id the ticket id, reason what actually failed stated concretely, evidence the exact failing test name, gate, file and error text or the precise merge symptom, and fix what the rebuild must do differently. An empty array asserts every ticket survived integration. Write these for the NEXT WORKER, not for a log: it will claim the ticket cold with no memory of this round, so a bare id or a vague "tests failed" wastes an entire rebuild while it re-derives what you already know. Name the failing test, quote the error, and say what the fix has to address. Good: {"id":"REM-10","reason":"its new default-prefix-attribution controller is unregistered in RESTRICTED_RECORD_MANIFEST and the permission_pages seed","evidence":"chat14-restricted-record-manifest.test.ts and chat16-chart-access-record.test.ts both fail on the merged tree with 'scope not registered'","fix":"register the new controller/scope in RESTRICTED_RECORD_MANIFEST and add its permission_pages seed row, then re-run both suites against the merged tree"}. Bad: {"id":"REM-10","reason":"failed","evidence":"","fix":"fix it"}. Write that file LAST, after everything else is done, and then STOP. You are running HEADLESS with no human attached: never ask a clarifying question or present a numbered choice, because nothing can answer it and the session will sit until it is reaped. Decide, or record the blocker and stop. If you cannot verify at all, that is itself a verdict: write the JSON with verdict fail and notes saying why, rather than asking what to do."
  sleep 3; tmux send-keys -t "$vsession" Enter
  say "round #$rnd: post-merge verification dispatched over $ids_csv"

  local vwaited=0 vstall=0 vhash="" vlast="" vlastpane=""
  while [ "$vwaited" -lt "${AF_VERIFY_TIMEOUT_S:-2700}" ]; do
    sleep 30; vwaited=$((vwaited+30))
    [ -f "$VERDICT" ] && break
    pane=$(tmux capture-pane -t "$vsession" -p 2>/dev/null || echo "")
    # Keep the last non-empty pane. When a verify session dies without a verdict the session is
    # already gone by the time anyone looks, so without this the log says only "gone" and the reason
    # is unrecoverable — which is exactly what happened on the stage's first real run.
    echo "$pane" | grep -qE "." && vlastpane="$pane"
    # Same rule as the build wait: a blank frame is a redraw, not a death.
    if ! tmux has-session -t "$vsession" 2>/dev/null; then say "verify session gone before writing a verdict"; break; fi
    if echo "$pane" | grep -qiE "insufficient balance|402|quota exceeded|payment required|credit balance is too low"; then
      say "BILLING FAILURE during verification — halting"; tmux kill-session -t "$vsession" 2>/dev/null || true; exit 3
    fi
    vhash=$(printf '%s' "$pane" | hash_text)
    if [ "$vhash" = "$vlast" ]; then
      vstall=$((vstall+1))
      # A still pane is NOT proof of a frozen session here. Verification's whole job is to run the
      # repo-wide suite ONCE on the merged tree, and that is a single tool call that prints nothing
      # while it runs — on a real project it lasted 21-28min against a 15min stall threshold, so the
      # detector reaped healthy work every round and the loop logged UNVERIFIED forever (observed:
      # "verify session frozen for 15min" at 28min in, with pytest still running). STALL_POLLS' own
      # comment already states the invariant this violates: it must stay ABOVE the longest tool
      # timeout the agent uses. So before calling it frozen, ask the OS whether real work is still
      # happening underneath the quiet pane.
      if [ "$vstall" -ge "$STALL_POLLS" ] && verify_children_busy "$vsession"; then
        say "verify pane still for $((vstall*30/60))min but a child process is live (long suite) — not frozen, still waiting"
        vstall=0
      fi
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
    if [ -n "$vlastpane" ]; then
      say "--- last verify pane before it died (why the verdict is missing) ---"
      printf '%s\n' "$vlastpane" | tail -25 | sed 's/^/    /' | tee -a "$LOG"
      say "--- end verify pane ---"
    fi
    return 0
  fi
  # Read the sentinel with a parser, not grep: a bare grep for the word fail matches the notes prose
  # and would report a passing round as failed.
  #
  # COHERENCE GATE. The three fields can disagree, and until this existed the loop printed the
  # disagreement and carried on as though the round had passed. Observed live: a round logged
  # `verdict=pass gates_green=False regressed=0`, which asserts simultaneously that the merged tree's
  # repo-wide gates were RED and that every ticket survived integration and nothing needs rebuilding.
  # The verify prompt already forbids exactly that state -- "if you genuinely cannot attribute it,
  # regress the whole batch rather than passing a red tree" -- but a prompt is not an enforcement
  # point, and self-reported verdicts are precisely where self-judgement leaks back in.
  #
  # Deliberately NOT auto-regressing the batch on incoherence. `gates_green=false` is also what a
  # verifier reports when a repo simply has no lint/typecheck tooling configured, so regressing on it
  # would trade a false pass for a false failure and rebuild healthy tickets forever. Instead an
  # incoherent verdict is downgraded to UNVERIFIED -- the same treatment as a MISSING verdict a few
  # lines above, which this file already refuses to call a pass. Tickets keep whatever state they
  # earned; the round's green claim does not get to stand.
  local summary
  summary=$(python3 - "$VERDICT" <<'PYEOF' 2>/dev/null || echo "verdict=UNREADABLE"
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print(f"verdict=UNPARSEABLE ({e})"); raise SystemExit
reg = d.get("regressed") or []
verdict = str(d.get("verdict") or "").strip().lower()
gates = d.get("gates_green")

incoherent = []
if verdict not in ("pass", "fail"):
    incoherent.append("verdict=%r is neither pass nor fail" % (d.get("verdict"),))
if gates is None:
    incoherent.append("gates_green is missing")
elif gates is False and verdict == "pass":
    incoherent.append("gates_green=false asserts the merged tree's repo-wide gates are RED, "
                      "while verdict=pass asserts the round is good")
if verdict == "fail" and not reg:
    incoherent.append("verdict=fail names no ticket to regress, so the failure would rebuild nothing")

prefix = ""
if incoherent:
    prefix = "INCOHERENT (treated as UNVERIFIED, not a pass) -- " + "; ".join(incoherent) + " :: "
print("%sverdict=%s gates_green=%s regressed=%d%s :: %s" % (
    prefix, d.get("verdict"), d.get("gates_green"), len(reg),
    (" [" + ",".join(str(r) for r in reg) + "]") if reg else "",
    str(d.get("notes", ""))[:200]))
PYEOF
)
  say "round #$rnd verification: $summary"
  case "$summary" in
    INCOHERENT*|verdict=UNREADABLE*|verdict=UNPARSEABLE*)
      say "WARNING: round #$rnd's verdict does not hold together, so the merged tree is UNVERIFIED. Its green claim is unproven; any ticket it named is still regressed below, but its silence about the others proves nothing."
      ;;
  esac

  # THE LOOP performs the regression, not the verifier agent.
  #
  # Step 3 of the verify prompt used to tell the agent to write the regression to Praxis itself
  # (record_outcome + release). It never once succeeded: across a full run, NINE consecutive
  # verdicts reported regressed=0, including one whose own notes read
  # "Should-regress REM-29,REM-28,REM-27" — the agent decided correctly and the write did not land,
  # so REM-10, REM-20, REM-27, REM-28, REM-29 and REM-30 all sat at build_state=finished while
  # their work had failed integration or was never merged at all. A gate that detects and cannot
  # record is advisory, and reads exactly like a passing round.
  #
  # The write path is _praxis.patch_meta -- the same one regress_ticket()/reap_branches already use
  # successfully, so it is known-good. Done in ONE python pass rather than a shell for-loop because
  # each regression now carries a multi-line failure report, and round-tripping that through shell
  # word-splitting would mangle exactly the detail this exists to preserve.
  #
  # WHAT THE NEXT WORKER GETS. A bare id tells it that a ticket failed, not what to fix, and the
  # verdict's `notes` is one truncated string for the whole round. So the verifier now reports a
  # per-ticket object, and every field lands on the ticket:
  #   meta.regression_detail  the structured report (what failed / evidence / what the rebuild must do)
  #   meta.audit_disposition  a human-readable summary, which is what surfaces in listings
  # Both are read straight off the ticket by whoever claims it next, so the rebuild starts from the
  # actual failure instead of re-deriving it.
  local regressed_n
  regressed_n=$($PY - "$PROJECT" "$rnd" "$VERDICT" <<'PYEOF' 2>/dev/null || echo 0
import json, sys
import _praxis, _ticket_state as ts

proj, rnd, path = sys.argv[1], sys.argv[2], sys.argv[3]
kw = dict(space=proj, snapshot=f"prd-{proj}")
try:
    d = json.load(open(path))
except Exception:
    print(0); raise SystemExit

# Accept three shapes so no verdict is ever silently dropped on a schema change:
#   "regressed": ["R1", ...]                                    (legacy / bare ids)
#   "regressed": [{"id": "R1", "reason": ..., "evidence": ..., "fix": ...}, ...]
#   "should_regress": [...]                                     (either of the above)
entries, seen = [], set()
for item in (d.get("regressed") or []) + (d.get("should_regress") or []):
    if isinstance(item, dict):
        rid = str(item.get("id") or item.get("ticket") or "").strip()
        detail = {k: v for k, v in item.items() if k not in ("id", "ticket")}
    else:
        rid, detail = str(item).strip(), {}
    if rid and rid not in seen:
        seen.add(rid); entries.append((rid, detail))

if not entries:
    print(0); raise SystemExit

by_rid = {}
for f in _praxis.facts_by(category="requirement", **kw) or []:
    m = f.get("meta") or {}
    by_rid[str(m.get("requirement_id") or f.get("id"))] = f

n = 0
for rid, detail in entries:
    f = by_rid.get(rid)
    if not f:
        sys.stderr.write(f"regress: no ticket {rid} in prd-{proj}\n"); continue
    reason   = str(detail.get("reason") or detail.get("why") or d.get("notes") or "").strip()
    evidence = str(detail.get("evidence") or detail.get("failing") or "").strip()
    fix      = str(detail.get("fix") or detail.get("required") or "").strip()
    parts = [f"REGRESSED by post-merge verification of round #{rnd}: this ticket read finished, "
             f"but its work did not survive integration into the merged tree."]
    if reason:   parts.append(f"WHAT FAILED: {reason}")
    if evidence: parts.append(f"EVIDENCE: {evidence}")
    if fix:      parts.append(f"THE REBUILD MUST: {fix}")
    parts.append("Rebuild against the CURRENT integrated tree, not the tree this ticket was "
                 "originally written against — the failure is an integration failure, so the "
                 "original worktree's green result does not carry over.")
    summary = " ".join(parts)
    new_finding = {"round": rnd, "source": "post-merge-verification",
                  "reason": reason, "evidence": evidence, "required_fix": fix}
    # R16/E3: accumulate onto this ticket's existing regression_detail — a concurrent finding
    # (conflict resolution, ingestion) must never be clobbered by this one.
    accumulated = ts.accumulate_regression_detail((f.get("meta") or {}).get("regression_detail"), new_finding)
    _praxis.regress_requirements(proj, [f["id"]], {f["id"]: {
        "claim_owner": None, "claim_at": None,
        "claim_heartbeat_at": None, "claim_lease_ttl": None,
        "audit_disposition": summary,
        "regression_detail": accumulated,
    }}, **kw)
    n += 1
    sys.stderr.write(f"regressed {rid} :: {(reason or 'no reason given')[:160]}\n")
print(n)
PYEOF
)
  case "$regressed_n" in ''|*[!0-9]*) regressed_n=0 ;; esac
  if [ "$regressed_n" -gt 0 ]; then
    say "round #$rnd: $regressed_n ticket(s) regressed with a failure report attached — the next round rebuilds them"
  fi

  # THE OTHER HALF OF THE FINDING CONTRACT. open_finding()'s docstring has always said a finding
  # is answered "when a later verification round confirms the ticket survived (which stamps
  # resolved)" — but nothing ever stamped it: resolve_finding() had a unit test and NO production
  # caller. The measured cost of that dead wire: a ticket whose finding was fixed by a SIBLING's
  # merge finishes its rebuild with zero commits (correctly — there is nothing left to change),
  # finding_guard regresses it for exactly that, and the pair ping-pongs forever. T8+T1 rode that
  # loop for 17 consecutive rounds; T10/T20, sports R2 and farming R26 for 6-8 each. So: any round
  # ticket that STILL reads finished after the regress pass above — i.e. this verification round
  # examined the merged tree and did not fault it — gets its open finding stamped resolved.
  local cleared_n
  cleared_n=$($PY - "$PROJECT" "$rnd" "$@" <<'PYEOF' 2>/dev/null || echo 0
import sys
import _praxis, _ticket_state as ts
proj, rnd = sys.argv[1], sys.argv[2]
want = set(sys.argv[3:])
kw = dict(space=proj, snapshot=f"prd-{proj}")
n = 0
for f in _praxis.facts_by(category="requirement", **kw) or []:
    m = f.get("meta") or {}
    rid = str(m.get("requirement_id") or f.get("id"))
    if rid not in want or m.get("build_state") != "finished":
        continue
    if not ts.open_findings(m):
        continue
    resolved_by = f"post-merge verification of round #{rnd}: the ticket survived the merged tree, so the finding is answered"
    accumulated = ts.resolve_finding(m, resolved_by=resolved_by)
    try:
        _praxis.write_build_state(f.get("cid") or f["id"], {"regression_detail": accumulated}, **kw)
        n += 1
        sys.stderr.write(f"finding resolved for {rid} by round #{rnd} verification\n")
    except Exception as e:
        sys.stderr.write(f"finding-resolve write failed for {rid}: {e}\n")
print(n)
PYEOF
)
  case "$cleared_n" in ''|*[!0-9]*) cleared_n=0 ;; esac
  if [ "$cleared_n" -gt 0 ]; then
    say "round #$rnd: $cleared_n surviving ticket(s) had their open verification finding stamped resolved"
  fi

  # A ticket may not answer an OPEN verification finding by changing nothing. Runs AFTER the
  # regress pass above so a ticket this round already regressed is not counted twice.
  local fg
  fg=$(finding_guard "$rnd" "$@" 2>/dev/null || echo 0)
  case "$fg" in ''|*[!0-9]*) fg=0 ;; esac
  if [ "$fg" -gt 0 ]; then
    say "round #$rnd: $fg ticket(s) closed an OPEN verification finding with ZERO commits — regressed. A finding is not answered by changing nothing."
  fi

  rm -f "$VERDICT"
  return 0
}

# --resolve-orphans <project> <worktree>: land every stranded worker branch, then stop.
# Same sweep and same resolver the round flow runs — this just lets an operator clear a backlog
# without waiting for a round, and is how an existing pile gets cleaned the first time.
if [ "$AF_MODE" = "resolve-orphans" ]; then
  cd "$WT" || { say "FATAL: no worktree at $WT"; exit 1; }
  : > "$CONFLICTS"
  queue_orphan_branches
  if [ ! -s "$CONFLICTS" ]; then
    # "Nothing queued" is a claim, not a fact. Assert against git before believing it — the queue is
    # exactly what missed af-build/HIP-23.
    af_assert_no_stragglers "--resolve-orphans"
    say "no orphan worker branches — nothing to land"; exit 0
  fi
  resolve_conflicts "orphans"
  sweep_worktrees || true
  af_assert_no_stragglers "--resolve-orphans"
  say "--resolve-orphans: done"
  exit 0
fi

n=0
round=0
while :; do
  if ! left=$(praxis_q claimable); then outage "claimable"; continue; fi
  left=${left:-999}
  say "$PROJECT claimable=$left"
  if [ "$left" = "0" ]; then
    # WATCH MODE: a drained set is not the end of the work, only the end of the work that EXISTED
    # when the run started. Without this the loop exits here, and every ticket authored afterwards is
    # invisible until a human relaunches it — which is precisely the gap that got papered over with a
    # per-project supervisor script living outside this repo. Three bugs in that script (restart-on-
    # any-exit through a billing failure; treating a dependency stall as a clean drain and relaunching
    # 340 times in 8 hours; a grep pattern that matched its own command line) were all re-derivations
    # of state this loop already knows precisely. So the wait belongs HERE, where the distinction is
    # free: a DELIBERATE halt is an `exit`, and only a genuine drain reaches this branch.
    # THE DRAIN GATE. Nothing claimable means nothing is in flight, so ANY branch still owed a merge
    # and ANY leftover worktree is a straggler by definition — there is no future round to land it.
    # This is the assertion whose absence let a sotos run print `drained — nothing claimable` on top
    # of two unmerged af-build/* branches. It is fatal: a run that left work behind must not exit 0.
    if [ "${watch_said_drain:-0}" != "1" ]; then af_assert_no_stragglers "drain"; fi
    if [ "${AF_WATCH:-0}" = "1" ]; then
      [ "${watch_said_drain:-0}" = "1" ] || { say "drained — nothing claimable; WATCHING for new tickets every ${AF_WATCH_POLL_S:-300}s (AF_WATCH=1). Stop with: touch $WATCH_STOP"; watch_said_drain=1; }
      [ -f "$WATCH_STOP" ] && { say "watch stop file present ($WATCH_STOP) — exiting"; break; }
      sleep "${AF_WATCH_POLL_S:-300}"
      continue
    fi
    say "DONE — nothing claimable"; break
  fi
  watch_said_drain=0
  [ "$n" -ge "$MAX" ] && { say "hit max_tickets=$MAX, stopping"; break; }

  # DISK PREFLIGHT — fail CLOSED, exactly like an unreachable Praxis.
  #
  # This guard used to live inside af-build's canonical fan-out workflow, and telling the round to use
  # Agent subagents instead of the Workflow tool left it behind. It has to exist somewhere: a round
  # materializes one full checkout per worker, plus whatever dependency tree the project bootstraps in
  # each, and several loops now run on one box at once. A full volume does not fail loudly — it
  # corrupts writes mid-build and strands the run, which is strictly worse than refusing to start.
  free_gb=$(free_gb_of "$WT")
  if [ "$free_gb" -lt "${AF_MIN_FREE_GB:-15}" ]; then
    # Reclaim first: merged worktrees from earlier rounds are pure scratch, and sweeping them is the
    # cheap fix that keeps a long run alive. Only halt if the disk is still tight afterwards.
    say "disk low: ${free_gb}G free — sweeping worktrees before deciding"
    sweep_worktrees
    free_gb=$(free_gb_of "$WT")
    if [ "$free_gb" -lt "${AF_MIN_FREE_GB:-15}" ]; then
      say "HALTING — only ${free_gb}G free after sweeping, below the ${AF_MIN_FREE_GB:-15}G floor. A round of worktrees plus their dependency trees would exhaust the disk and corrupt the build. Reclaim space, then relaunch."
      exit 5
    fi
    say "swept back to ${free_gb}G free — continuing"
  fi

  # Degraded, not fatal: a lease that goes unreleased costs this round one ticket, whereas dying
  # here costs the whole run — and dying here is what `set -e` did before the `|| say`.
  release_inprogress >/dev/null 2>&1 || say "WARNING: could not release stale leases this pass — a crashed session's ticket may sit out this round"

  # Compute the frontier AFTER releasing dead leases, so a ticket stranded in_progress by a crashed
  # session is eligible for this round instead of sitting out until someone notices.
  budget=$((MAX - n)); [ "$budget" -lt "$BATCH_MAX" ] && cap="$budget" || cap="$BATCH_MAX"
  if ! batch=$(praxis_q ready_batch "$cap"); then outage "ready_batch"; continue; fi
  if [ -z "$batch" ]; then
    # Claimable work exists but nothing is dependency-ready: a cycle, or a chain rooted on a blocked
    # ticket. Restarting sessions cannot fix that, so halt loudly instead of spinning.
    say "DEPENDENCY STALL — $left claimable but nothing ready; every remaining ticket waits on an unfinished or blocked prerequisite."
    # Name the root(s). Best-effort: a failed query must not turn a stall into an outage.
    if roots=$(stall_roots 2>/dev/null) && [ -n "$roots" ]; then
      say "STALL ROOT(S) — act on these; everything else is downstream:"
      printf '%s\n' "$roots" | while IFS= read -r line; do [ -n "$line" ] && say "    $line"; done
    else
      say "  (could not resolve the stall root — walk depends_on by hand)"
    fi
    if [ "${AF_WATCH:-0}" = "1" ]; then
      # Watching a stall is safe HERE and was not safe from outside. An external supervisor could only
      # see exit 0 — identical to a clean drain — so it relaunched the whole loop: fresh session, fresh
      # preflight, fresh model-backend probe, 340 times over 8 hours. In-process the cost is a sleep
      # and one Praxis query, and the moment a human unblocks the root ticket the very next poll picks
      # it up. Re-announce only when the claimable count MOVES, so a stall that nobody has fixed yet
      # stays one line in the log instead of a wall of them.
      if [ "${watch_stall_at:-}" != "$left" ]; then
        say "WATCHING through the stall (AF_WATCH=1) — re-checking every ${AF_WATCH_POLL_S:-300}s; unblocking a root ticket resumes the run with no relaunch. Stop with: touch $WATCH_STOP"
        watch_stall_at="$left"
      fi
      [ -f "$WATCH_STOP" ] && { say "watch stop file present ($WATCH_STOP) — exiting"; break; }
      sleep "${AF_WATCH_POLL_S:-300}"
      continue
    fi
    say "Then relaunch, or run with AF_WATCH=1 so the loop waits for the fix instead of exiting."
    break
  fi
  watch_stall_at=""
  outages=0   # a computed frontier proves Praxis is back; the streak only counts CONSECUTIVE failures
  set -- $batch
  size=$#
  ids_csv=$(printf '%s,' "$@"); ids_csv=${ids_csv%,}

  # Read the baseline BEFORE the round counters move. This is the last query that can still abort the
  # pass cleanly, and retrying a pass that had already claimed a round number would skip numbers in
  # the log and over-count `n` against MAX.
  if ! before=$(praxis_q finished_count); then outage "finished_count baseline"; continue; fi
  before=${before:-0}

  round=$((round+1)); n=$((n+size))
  say "round #$round: dispatching $size ticket(s) in parallel — $ids_csv"

  tmux kill-session -t "$SESSION" 2>/dev/null || true
  tmux new-session -d -s "$SESSION" -c "$WT"
  # The env preamble runs in the tmux shell AFTER ~/.bashrc has already sourced the
  # machine-wide backend file, so it deliberately overrides that file rather than
  # trusting it to agree with $AF_MODEL_BACKEND.
  tmux send-keys -t "$SESSION" "cd $WT && $CLAUDE_LAUNCH" Enter

  # Cold starts on a loaded box exceed the old 80s cap (measured: round #1 of taolu-coach), so
  # poll 3x longer — and if the pane NEVER signals ready there is NO AGENT in it, so sending is
  # strictly worse than failing loudly (same contract as the conflict-resolver path). The tickets
  # are unclaimed, so the next pass simply retries them.
  ready=0
  for _ in $(seq 1 $((READY_POLL_MAX * 3))); do
    sleep 2
    pane=$(tmux capture-pane -t "$SESSION" -p 2>/dev/null || echo "")
    if echo "$pane" | grep -qE "bypass permissions on"; then ready=1; break; fi
  done
  if [ "$ready" != "1" ]; then
    say "FATAL: round #$round pane never signalled ready after $((READY_POLL_MAX*6))s — no agent in the session, NOT sending the prompt; killing the session so the next pass retries these tickets"
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    continue
  fi
  # Dismiss any startup panel (what's-new / trust dialog) before typing, exactly as the
  # conflict-resolver path does — a 4KB prompt typed into a panel is swallowed silently.
  sleep 2; tmux send-keys -t "$SESSION" Enter; sleep 2

  # The batch ids ARE the run scope, which is what makes one round per session self-limiting: af-build
  # stamps its run marker on exactly these tickets, so its completeness gate releases the session when
  # they are done and its fan-out loop finds an empty in-scope frontier rather than starting a wave the
  # next session is meant to own. Parentheses are avoided in this string on purpose — if the REPL is not
  # actually ready, the text lands on a bash prompt, and a stray paren there is a syntax error that
  # leaves the session dead at a shell prompt.
  round_prompt="/af-build $PROJECT $ids_csv — build EXACTLY these $size tickets and nothing else. They are dependency-independent, so build them ALL AT ONCE. Do NOT use the Workflow tool for this: its concurrency is derived from CPU count and on THIS machine resolves to $WORKFLOW_CAP concurrent agent(s), which would throttle a $size-wide round for no reason. Instead spawn ONE Agent subagent per ticket, ALL of them in a SINGLE message so they actually run concurrently, each with isolation set to worktree so their edits never collide. Give each subagent the af-build per-ticket worker contract VERBATIM, scoped to its own single ticket id, so every worker stays eval-first and lease-safe. CRITICAL — REBASE FIRST, before reading a single file or writing a line of code. A worktree is created from the repo default branch, NOT from the branch this run integrates into, so every worker starts on the wrong base. The FIRST command each worker runs in its own worktree is: git merge --ff-only $INTEGRATION_REF — and if that is refused because the worktree has diverged, git rebase $INTEGRATION_REF instead. A worker that skips this authors its change against files the integration branch does not have, and its work will not apply back onto it even cherry-picked alone; that failure is silent, because the ticket still goes green in its own tree and only the round's merge discovers the work cannot land. If BOTH commands fail, do NOT build: record the blocker on the ticket and stop, because anything built on that base is unmergeable by construction. ONE deliberate amendment to that contract, and state it to every worker, precisely — this narrows WHICH tests run, it does not remove the ticket's gate. Each worker STILL runs, and still has to pass, every test related to the code it is changing: its red-to-green acceptance eval, every one of its pinned validations, the existing test files covering the modules it edited, the tests of any caller or dependent of what it changed, and typecheck plus lint SCOPED to the touched paths. A worker whose own related tests are red is NOT finished and must not release the ticket — that gate is unchanged and non-negotiable. What a worker skips is ONLY the repo-wide sweep: the full test suite across the whole repository, and repo-wide build, typecheck, or lint over paths it never touched. The repo-wide gates are run ONCE, on the MERGED tree, by this round's post-merge verification. The reason is measured: $size workers each running the full suite at end-of-ticket puts $size concurrent suites on a box with a handful of cores, and a suite that takes two minutes alone took twenty, with a worker burning 26 minutes and 259k tokens without producing a commit. Deferring the SWEEP is not removing a gate: each ticket is still gated on its own related tests, and the repo-wide sweep still runs before the round is done — once, on the tree that actually matters. All $size must be in flight together — a round that runs them a couple at a time is a bug, not a safe choice. CRITICAL — do NOT end your turn while any ticket in that list is still unfinished. Waiting on agent-completion notifications is NOT enough: a turn that ends with workers in flight gets those workers STOPPED, and the round scores zero even though real work was happening. So HOLD the turn open by polling instead: run a shell sleep of 60 seconds, then re-query the build_state of every batch ticket from Praxis, and repeat that sleep-and-query cycle for as long as any of them is still incomplete or in_progress. Only after every batch ticket reads finished or blocked may you merge, reap, and report. When every ticket is finished or blocked: merge each ticket branch into the already-checked-out branch, remove ALL worktrees the round created, then STOP and report. You are running HEADLESS with no human attached: never ask a clarifying question or present a numbered choice, because nothing can answer it and the session will sit until it is reaped. Decide, or record the blocker and stop. Do NOT claim, read, or start any ticket outside that id list even if more remain — a fresh session picks up the next batch. Work ONLY on the already-checked-out branch, do NOT push. Every worker edits ONLY the files inside its OWN assigned worktree — the factory checkout that holds this driver and the af-build hooks is TOOLING, not the project, and editing it is out of bounds even when a ticket is about that code. Two reasons it matters: those hook files are imported at runtime by every project's loop on this machine, so a half-finished edit there can break builds that have nothing to do with this ticket; and edits made outside a worktree sit on no branch, so the round's merge step cannot see them and they are silently dropped when the round ends. For ANY factory or Praxis python invocation, run $PY rather than a bare python3 — this run preflighted that interpreter and a bare python3 may resolve to an older one whose missing tomllib makes the universal quality checks load as an empty list. Pass that same path down to every subagent you spawn.$SERVICES."
  # Submit, then CONFIRM the prompt landed. The wedge this catches is measured, not hypothetical:
  # a prompt sent into a not-quite-ready TUI is swallowed whole, the pane sits at an idle input box
  # showing the 'Try "..."' placeholder, and the round waits 30+ minutes for the stall net. An idle
  # pane still showing that placeholder (and no "esc to interrupt" activity hint) means the send
  # did NOT take — resend into the now-ready input instead of waiting for the stall detector.
  submitted=0
  for _attempt in 1 2 3; do
    tmux send-keys -t "$SESSION" "$round_prompt"
    sleep 3; tmux send-keys -t "$SESSION" Enter
    landed=0
    for _ in $(seq 1 8); do
      sleep 5
      pane=$(tmux capture-pane -t "$SESSION" -p 2>/dev/null || echo "")
      if echo "$pane" | grep -q "esc to interrupt"; then landed=1; break; fi
      if ! echo "$pane" | grep -q 'Try "'; then landed=1; break; fi
    done
    if [ "$landed" = "1" ]; then submitted=1; break; fi
    say "WARNING: round #$round prompt did not land (attempt $_attempt) — input still idle, resending"
  done
  if [ "$submitted" != "1" ]; then
    say "FATAL: round #$round prompt never landed after 3 attempts — killing session; tickets remain unclaimed for the next pass"
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    continue
  fi
  say "submitted round #$round with $size ticket(s), waiting for the batch to finish or stall"

  # Wait for: every batch ticket to leave the open set (success), context exhaustion, auth error,
  # the session to die, OR the pane going completely unchanged for STALL_POLLS polls in a row (a
  # frozen session — see v2 note above).
  #
  # A per-ticket finish is PROGRESS, not completion: breaking on the first one would kill a 15-ticket
  # round the moment its fastest worker landed, orphaning fourteen live workers and their worktrees.
  # So each finish instead resets the stall counter and buys more wall clock, and the round ends only
  # when the batch's open count hits zero. The deadline scales with batch size for the same reason.
  # AF_ROUND_DEADLINE_S overrides the per-round wall clock. The 3600s default is
  # fine for a code ticket and far too short for one whose acceptance runs a real
  # workload: COV-1B drives PaddleOCR over a corpus, and rounds 1 and 2 of
  # 2026-08-05 were both killed at exactly 60min with the ticket still open and
  # its worker still writing. Killing a round does not just lose time -- the
  # worktree purge discards everything the worker had not committed.
  deadline=$(( ${AF_ROUND_DEADLINE_S:-3600} + (size - 1) * 1200 ))
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
  lastpane=""
  done_seen=$before
  while [ "$waited" -lt "$deadline" ]; do
    sleep 30; waited=$((waited+30))
    # Mid-round, a Praxis blip is NOT worth reacting to: the batch is live in its own session and
    # still working. Fall back to the last known counts so the poll is a no-op, and let the deadline
    # and the pane-stillness check keep watching. This is the single poll that killed the run.
    now=$(praxis_q finished_count) || now=$done_seen
    now=${now:-$done_seen}
    if [ "$now" -gt "$done_seen" ]; then
      say "round #$round progress: $((now - before))/$size finished"
      done_seen=$now
      same_count=0                       # real progress is not a stall, whatever the pane looks like
      waited=$((waited > 900 ? waited - 900 : 0))
    fi
    # Same reasoning: an unanswerable "are they done yet?" means keep waiting, never end the round.
    open=$(praxis_q batch_open "$@") || open=1
    open=${open:-1}
    if [ "$open" = "0" ]; then say "round #$round complete — all $size ticket(s) finished or blocked"; break; fi
    pane=$(tmux capture-pane -t "$SESSION" -p 2>/dev/null || echo "")
    # Retain the last live frame. Three rounds died as a bare "session gone" with the pane already
    # destroyed, so the reason was unrecoverable each time and the same round was retried blind.
    echo "$pane" | grep -qE "." && lastpane="$pane"
    # An EMPTY capture does NOT mean the session died. The TUI clears the screen to redraw -- which is
    # exactly what a worker's completion notification triggers -- so a poll can legitimately catch a
    # blank frame from a perfectly healthy session. Believing that blank frame is what killed round
    # after round: the driver logged "session gone" at 22:39:26 and its teardown killed the session at
    # 22:39:27, while the transcript shows the session still processing a task-notification at that
    # exact second, with both workers alive. Rounds scored zero because the driver murdered them.
    # `tmux has-session` is the authoritative test, so ask it instead of inferring from pixels.
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
      say "session gone (confirmed via has-session), ending wait"
      if [ -n "${lastpane:-}" ]; then
        say "--- last build pane before it died ---"
        printf '%s\n' "$lastpane" | grep -vE '^[[:space:]]*$' | tail -20 | sed 's/^/    /' | tee -a "$LOG"
        say "--- end build pane ---"
      fi
      break
    fi
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
    pane_hash=$(printf '%s' "$pane" | hash_text)
    if [ "$pane_hash" = "$last_hash" ]; then
      same_count=$((same_count+1))
      # A still pane is not a dead worker. The TUI emits nothing while a single long
      # tool call runs -- a full pytest, a docker build, a graded judge call with its
      # own 600s timeout -- so a healthy ticket goes quiet for far longer than the
      # 15min threshold. The verify wait below already learned this the hard way
      # ("verify session frozen for 15min" at 28min in, with pytest still running) and
      # was given this exact guard; the round wait, which is where the actual BUILD
      # happens, never got it. Observed 2026-08-05: DATA-1 ran 29min, was reaped as
      # frozen, and three such rounds in a row tripped the HALT -- the driver killed
      # its own healthy work and then concluded something unfixable was wrong.
      #
      # STALL_POLLS' own comment states the invariant this violates: it must stay
      # ABOVE the longest tool timeout the agent uses. Rather than guess that number,
      # ask the OS whether real work is still happening underneath the quiet pane.
      if [ "$same_count" -ge "$stall_polls" ] && verify_children_busy "$SESSION"; then
        say "build pane still for $((same_count*30/60))min but a child process is live (long tool call) — not frozen, still waiting"
        same_count=0
      fi
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
  # Integrate FIRST, then sweep: the sweep only reaps worktrees whose branch is already an ancestor
  # of HEAD, so merging first is what turns this round's scratch into reclaimable space instead of
  # another dozen "unmerged, left in place" warnings.
  # ORDER IS LOAD-BEARING and every step is mandatory before the next batch starts:
  #   1. INTEGRATE — merge each finished ticket's branch into the integration branch.
  #   2. PURGE     — remove every scratch worktree (branches survive it), reclaiming the disk.
  #   3. REAP      — delete every branch whose work has landed, and NAME whatever is left. Must follow
  #                  the purge: git will not delete a branch that is still checked out somewhere.
  #   4. VERIFY    — check the merged result, below, before this round is considered done.
  # Verification has to see the tree AFTER the merge, or it verifies nothing: the first attempt ran
  # against an unmerged tree and correctly reported "there is no merged tree to verify". And all
  # three must finish before the loop clears context and dispatches the next batch, or a round's
  # work is carried forward unintegrated and unchecked into a session that no longer remembers it.
  : > "$CONFLICTS"
  integrate_round
  # REQUIRED, not optional: sweep in any branch stranded by an earlier round so the resolver lands it
  # too. Without this the resolver only ever fixes the round that created the conflict, and anything
  # older stays orphaned forever — which is precisely how 11 accumulated.
  queue_orphan_branches
  # Land anything that conflicted BEFORE the worktrees are swept and before verification runs: a
  # conflicted branch is unfinished integration, not a finished round, and verifying a tree that is
  # missing a ticket's work verifies the wrong thing.
  resolve_conflicts "$round"
  # Purge AFTER the session is dead, never while it is live: removing a worktree out from under a
  # running worker deletes the tree its evals are executing against.
  sweep_worktrees
  # `|| true` because a branch-integrity failure regresses its ticket and must NOT abort the run under
  # `set -e`: the next round is precisely what rebuilds it. The failure is loud in the log either way.
  reap_branches || true
  # Non-fatal at a round boundary: a branch for a ticket still in flight is legitimate here, and the
  # next round lands it. It still forces a resolution pass and still logs loudly, so a straggler is
  # visible from the round it appears in rather than only at the end of the run.
  af_assert_no_stragglers "round #$round" 0 || true

  # Circuit breaker. A round that finishes NOTHING will, on the next pass, release the same leases
  # and dispatch the same frontier — so a persistent failure that isn't one of the specific panes
  # matched above (a broken repo, a project whose env never comes up, a model that rejects every
  # prompt) loops indefinitely at full cost. The DeepSeek-balance incident burned ~6 hours across 46
  # such cycles before it was spotted, and the billing grep added afterwards only covers that one
  # cause. Three fruitless rounds is the general version of that guard.
  # An unanswerable tally must not feed the circuit breaker in either direction: scoring it fruitless
  # on a blip walks a healthy run toward the 3-strike halt, and scoring it productive hides a real
  # failure. Say so instead, and leave the streak exactly where it was.
  if ! after=$(praxis_q finished_count); then
    say "WARNING: Praxis unreachable after round #$round — cannot tell what landed, so this round counts as neither productive nor fruitless and its merged tree goes UNVERIFIED. Treat any green claim from it as unproven."
    after=""
  fi
  if [ -z "${after:-}" ]; then
    :   # unanswerable — deliberately touch neither `fruitless` nor the verification stage
  elif [ "$after" -gt "$before" ]; then
    fruitless=0
    # Verify the MERGE, not the tickets — they were each proven alone, in a worktree that no longer
    # exists. Runs only when something actually landed, and only after the build session is dead so
    # the two never race on the same tree. It may regress tickets, which is why it runs BEFORE the
    # next frontier is computed: a ticket it sends back reappears in the very next batch.
    # EVERY round that lands work is verified, including a 1-ticket round.
    #
    # This used to skip single-ticket batches, on the reasoning that one worker merges exactly the
    # tree it already validated, whole-repo gates included, so a second session bought nothing. That
    # reasoning died the moment workers stopped running the repo-wide sweep: with the sweep deferred
    # here, skipping this stage on a 1-ticket round means the full suite, repo build, typecheck and
    # lint run NOWHERE for that ticket. The cross-ticket lenses are trivially satisfied when there is
    # only one ticket -- the gates are not, and they are now this stage's job alone.
    if [ "${AF_VERIFY_ROUND:-1}" = "1" ]; then
      verify_round "$round" "$@"
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
# EVERY `break` above lands here — drain, max_tickets, dependency stall, watch-stop. None of them is
# allowed to announce a finished run over work that never landed, so the invariant is asserted once
# more on the way out, where it covers the paths the drain gate does not.
af_assert_no_stragglers "exit"
say "af-ticket-loop finished for $PROJECT"
