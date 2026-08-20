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
#     AF_MODEL_BACKEND=deepseek|sonnet|grok   (env var, per-run, wins)
#       else contents of ~/.af-backend   (the existing `af-backend` command still works)
#       else deepseek
#
#     grok is the native Grok CLI on an OAuth session token (~/.grok/auth.json).
#     It UNSETS XAI_API_KEY so the run cannot silently spend API credits. A
#     missing auth.json or a probe that reports apiKeySource=user is FATAL.
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
#   AF_WATCH_STOP_TTL_S    ignore a stop sentinel older than this (default 86400)
#   AF_ALLOW_SHARED_DB=1   allow two live loops to declare the same Postgres port
#   AF_ROUND_QUIET_WARN_S  log a STALL WARNING after this much round silence (600)
#                          (default: <parent-of-worktree>/af-watch-stop-<project>@<worktree>;
#                           the legacy basename-only path is still honoured, deprecated)
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
#   3  billing/credit failure (API credits exhausted -- a 402; top up and relaunch)
#   4  three consecutive fruitless rounds   5  disk floor   6  Praxis unreachable
#   7  STRAGGLERS: the run left unmerged worker branches and/or leftover worktrees behind. Nothing is
#      ever deleted to reach a green invariant, so the work named in the log is intact -- land it
#      (`--resolve-orphans`) and relaunch.
#   8  QUOTA BLOCKED: a headless session hit the Claude subscription's session/usage limit and was
#      stranded on the interactive /rate-limit-options menu. Distinct from 3 (that is API credits):
#      the remedy is to wait for the subscription window to reset, or switch the plan/backend.
#   AF_MODEL_BACKEND       sonnet | deepseek | grok (see v3)
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
    sonnet|deepseek|grok) ;;
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
    # The model is a PER-RUN choice, not a property of the loop. It was hardcoded to sonnet in
    # three places (launch, probe, and the box's settings.json), so a project that needed a
    # stronger model had no way to say so and no way to discover that it could not -- the loop
    # simply ran, on whatever the hardcode said, and the run looked normal.
    #
    # Default stays sonnet so every existing caller is unchanged. Override per run:
    #     AF_CLAUDE_MODEL=opus agent_factory/scripts/af-ticket-loop.sh ...
    CLAUDE_LAUNCH="unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_MODEL ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN; claude --model ${AF_CLAUDE_MODEL:-sonnet} --dangerously-skip-permissions"

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
    probe="$(cd /tmp && bash -c "unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_MODEL ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN; export CLAUDE_CONFIG_DIR='$probe_cfg'; timeout 120 claude --model ${AF_CLAUDE_MODEL:-sonnet} -p 'Reply with exactly: PONG'" 2>&1 || true)"
    if printf '%s' "$probe" | grep -qiE 'please run /login|invalid api key|authentication_error|oauth.*invalid|401'; then
      echo "[backend] FATAL: sonnet credential present but REJECTED by Anthropic:" >&2
      printf '%s\n' "$probe" | head -3 | sed 's/^/[backend]   /' >&2
      echo "[backend]   fix, once, as ec2-user:  claude setup-token   # then write into $OAUTH_TOKEN_FILE as: export CLAUDE_CODE_OAUTH_TOKEN=..." >&2
      echo "[backend]   or:                      claude   ->  /login" >&2
      return 1
    fi
    printf '%s' "$probe" | grep -q 'PONG' || echo "[backend] WARNING: sonnet auth probe returned no PONG and no auth error (network/transient?) — continuing" >&2
  elif [ "$BACKEND" = "grok" ]; then
    # Native Grok CLI on the xAI *subscription* (OAuth session token). XAI_API_KEY is
    # unset, not merely out-ranked: if the env key is present Grok spends API credits
    # while the session still looks healthy. Same exclusivity rule as the DeepSeek
    # branch. grok login --device-auth writes ~/.grok/auth.json; that file is the
    # subscription. A probe that reports apiKeySource=user is the API-key path and
    # is FATAL — that is the wrong bill.
    GROK_BIN="${GROK_BIN:-$HOME/.grok/bin/grok}"
    GROK_AUTH="$HOME/.grok/auth.json"
    AF_GROK_MODEL="${AF_GROK_MODEL:-grok-4.6}"
    BACKEND_NOTE="xAI Grok subscription (OAuth), model=${AF_GROK_MODEL} — spends xAI subscription, NOT API credits"
    if [ ! -x "$GROK_BIN" ]; then
      echo "[backend] FATAL: grok requested but $GROK_BIN is missing or not executable." >&2
      echo "[backend]   fix: curl -fsSL https://x.ai/cli/install.sh | bash" >&2
      return 1
    fi
    if [ ! -s "$GROK_AUTH" ]; then
      echo "[backend] FATAL: grok requested but $GROK_AUTH is missing or empty." >&2
      echo "[backend]   fix, once, as ec2-user:  grok login --device-auth" >&2
      return 1
    fi
    CLAUDE_LAUNCH="unset XAI_API_KEY GROK_CODE_XAI_API_KEY ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_MODEL CLAUDE_CODE_OAUTH_TOKEN; export PATH=\"\$HOME/.grok/bin:\$HOME/.local/bin:\$PATH\"; ${GROK_BIN} --model ${AF_GROK_MODEL} --always-approve"
    local probe
    probe="$(cd /tmp && unset XAI_API_KEY GROK_CODE_XAI_API_KEY ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL CLAUDE_CODE_OAUTH_TOKEN && timeout 120 "$GROK_BIN" --model "$AF_GROK_MODEL" --always-approve -p 'Reply with exactly: PONG' --output-format json 2>&1 || true)"
    if printf '%s' "$probe" | grep -qiE '"apiKeySource"[[:space:]]*:[[:space:]]*"user"'; then
      echo "[backend] FATAL: grok probe billed as apiKeySource=user (XAI_API_KEY / API credits)." >&2
      echo "[backend]   unset XAI_API_KEY and GROK_CODE_XAI_API_KEY, confirm $GROK_AUTH is an OAuth login, rerun --check." >&2
      return 1
    fi
    if printf '%s' "$probe" | grep -qiE 'authentication failed|please (run |sign in)|not authenticated|login required|invalid api key'; then
      echo "[backend] FATAL: grok credential present but REJECTED:" >&2
      printf '%s\n' "$probe" | head -5 | sed 's/^/[backend]   /' >&2
      echo "[backend]   fix, once, as ec2-user:  grok login --device-auth" >&2
      return 1
    fi
    printf '%s' "$probe" | grep -q 'PONG' || echo "[backend] WARNING: grok auth probe returned no PONG and no auth error (network/transient?) — continuing" >&2
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
# --- BEGIN watch stop sentinel ---
# AF_WATCH stop sentinel. DEFECT 2 (measured on the devbox 2026-08-19): it used to be keyed on the
# WORKTREE BASENAME alone, which has two consequences. Two projects sharing one worktree could not
# be stopped independently — stopping one stopped both. And a sentinel nobody deleted lived
# forever: THREE stale ones were found on the box dating from Aug 4 and Aug 13 (sotos, football-cv,
# taolu-coach), each a latent two-week-old booby trap that silently kills the NEXT watch run at its
# first poll, with no error and no log.
#
# So the sentinel is keyed on PROJECT@WORKTREE, and any sentinel older than AF_WATCH_STOP_TTL_S
# (default 24h) is treated as residue: ignored, and the ignore is LOGGED rather than inferred. The
# old basename-only path is still honoured — a loop already in flight was told to use it — with a
# deprecation line naming the replacement.
AF_WATCH_STOP_TTL_S="${AF_WATCH_STOP_TTL_S:-86400}"
WATCH_STOP="${AF_WATCH_STOP:-$(dirname "$WT")/af-watch-stop-$PROJECT@$(basename "$WT")}"
WATCH_STOP_LEGACY="$(dirname "$WT")/af-watch-stop-$(basename "$WT")"
# An explicit AF_WATCH_STOP is the operator naming ONE path; do not also honour the legacy one.
[ -n "${AF_WATCH_STOP:-}" ] && WATCH_STOP_LEGACY=""
WATCH_STOP_HIT=""

# `say` is defined further down and logs to $LOG; these run both before and after it exists.
af_watch_stop_say(){ if declare -F say >/dev/null 2>&1; then say "$*"; else echo "$*"; fi; }

af_watch_stop_fresh(){   # <path> -> 0 if it exists AND is younger than the TTL
  local path="${1:-}" mtime age
  [ -n "$path" ] && [ -f "$path" ] || return 1
  mtime="$(date -r "$path" +%s 2>/dev/null || stat -c %Y "$path" 2>/dev/null || echo 0)"
  age=$(( $(date +%s) - mtime ))
  if [ "${mtime:-0}" -gt 0 ] && [ "$age" -gt "$AF_WATCH_STOP_TTL_S" ]; then
    if [ "${AF_WATCH_STOP_STALE_SAID:-}" != "$path" ]; then
      AF_WATCH_STOP_STALE_SAID="$path"
      af_watch_stop_say "IGNORING STALE watch stop file $path — it is $((age/3600))h old, past the ${AF_WATCH_STOP_TTL_S}s TTL, so it is residue from an earlier run and not a stop for this one. Delete it, or raise AF_WATCH_STOP_TTL_S, if you actually meant it."
    fi
    return 1
  fi
  return 0
}

af_watch_stopped(){   # 0 when this run should stop; sets WATCH_STOP_HIT to the path that stopped it
  if af_watch_stop_fresh "$WATCH_STOP"; then WATCH_STOP_HIT="$WATCH_STOP"; return 0; fi
  if af_watch_stop_fresh "${WATCH_STOP_LEGACY:-}"; then
    WATCH_STOP_HIT="$WATCH_STOP_LEGACY"
    af_watch_stop_say "DEPRECATED watch stop path ${WATCH_STOP_LEGACY} — it is keyed on the worktree basename alone, so it stops EVERY project sharing this worktree. Use $WATCH_STOP instead."
    return 0
  fi
  return 1
}
# --- END watch stop sentinel ---

# --- BEGIN round stall heartbeat ---
# DEFECT 3 (measured on the devbox 2026-08-19): the round wait logs "round #N progress: K/M
# finished" and then says nothing at all until something changes, so a round whose workers are
# ASLEEP at 0.0% CPU is indistinguishable from one doing work — one sat with `make check-fast` in
# state Sl for 10+ minutes with no log line. This does NOT kill anything (the deadline, the grace
# extension and the pane-stillness guard already own that decision); it only makes the silence
# visible and greppable.
#
# DEFECT 3b (measured live 2026-08-19, the fix for the fix): keying the warning on ticket state
# ALONE cries wolf on every non-trivial round. Two loops that were provably working — af-hudl-cv-
# download showed "1 command · 2 subagents still running" and af-sports_analysis showed "Subagents
# 4 / Poll ticket states every 60s for 5 more… 14m24s" — both got "STALL WARNING round #1" at the
# 10min and 20min marks. The worker's own /af-build prompt TELLS it to poll ticket states every
# 60s, so a correct worker produces long stretches with no finished-count change BY DESIGN. A
# detector that fires on healthy rounds trains the operator to ignore it, which is worse than no
# detector.
#
# So the predicate now needs ALL the liveness signals quiet before it says STALL:
#   (a) finished/open ticket counts unchanged   (the original signal — kept)
#   (b) the session's process tree has accumulated no CPU time since the last poll
#   (c) the tmux pane capture is byte-identical to the last poll, with the spinner/elapsed/token
#       chrome stripped first (that line changes every poll by animation, not by output)
# Any signal that MOVES downgrades the line to a PROGRESS line, which must never say STALL. A
# signal that cannot be SAMPLED (no session, tmux gone, ps unreadable) is UNKNOWN and is reported
# as UNKNOWN — never silently counted as quiet, because failing closed into false alarms is the
# bug being fixed here.
AF_ROUND_QUIET_WARN_S="${AF_ROUND_QUIET_WARN_S:-${AF_STALL_WARN_S:-600}}"

# Cumulative CPU-seconds of a session's process tree, or "" when it cannot be sampled.
# Reuses the driver's own session_cpu when it is defined (it is, further down this script);
# the guard keeps the block independently executable.
af_hb_cpu_sample(){   # $1 = tmux session
  local s="${1:-}" v
  [ -n "$s" ] || return 0
  declare -F session_cpu >/dev/null 2>&1 || return 0
  v="$(session_cpu "$s" 2>/dev/null || true)"
  case "$v" in ''|*[!0-9]*) return 0 ;; esac
  printf '%s' "$v"
}

# Hash of the session's pane with the animated chrome removed, or "" when it cannot be sampled.
# Stripped: the spinner glyphs, the "(NmNNs" / "· NNs" elapsed counters and the token meter — all
# of which change on every single poll whether or not the worker emitted a byte of real output.
af_hb_pane_sample(){   # $1 = tmux session
  local s="${1:-}" pane
  [ -n "$s" ] || return 0
  command -v tmux >/dev/null 2>&1 || return 0
  tmux has-session -t "$s" >/dev/null 2>&1 || return 0
  pane="$(tmux capture-pane -t "$s" -p 2>/dev/null || true)"
  [ -n "$pane" ] || return 0
  printf '%s' "$pane" \
    | sed -E 's/[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✢✳✶✻✽*·]+/ /g; s/[0-9]+m[0-9]+s/ /g; s/(^|[[:space:]])[⇣⇡↓↑]?[0-9][0-9.]*[kKmMs]([[:space:]]|$)/ /g; s/(^|[[:space:]])[⇣⇡↓↑]?[0-9][0-9.]*[kKmMs]([[:space:]]|$)/ /g; s/[0-9.]+[kKmM]? tokens/ /g; s/[[:space:]]+/ /g' \
    | { if declare -F hash_text >/dev/null 2>&1; then hash_text; \
        elif command -v md5sum >/dev/null 2>&1; then md5sum | cut -d' ' -f1; \
        else md5 -q; fi; }
}

af_round_heartbeat(){   # <round> <progress-signature> <outstanding ids>
  local rnd="${1:-?}" sig="${2:-}" ids="${3:-}" nowt sess cpu pane quiet_for
  local cpu_state pane_state cpu_quiet=0 pane_quiet=0
  nowt="$(date +%s)"
  sess="${AF_HB_SESSION:-${SESSION:-}}"
  cpu="$(af_hb_cpu_sample "$sess")"
  pane="$(af_hb_pane_sample "$sess")"
  if [ "$sig" != "${AF_HB_SIG:-__unset__}" ]; then
    AF_HB_SIG="$sig"; AF_HB_SINCE="$nowt"; AF_HB_LAST="$nowt"
    AF_HB_CPU="$cpu"; AF_HB_PANE="$pane"
    return 0
  fi
  : "${AF_HB_SINCE:=$nowt}"; : "${AF_HB_LAST:=$nowt}"
  if [ $(( nowt - AF_HB_LAST )) -lt "$AF_ROUND_QUIET_WARN_S" ]; then return 0; fi
  quiet_for=$(( (nowt - AF_HB_SINCE) / 60 ))

  # (b) CPU. Cumulative, so only a RISE is evidence of life.
  if [ -z "$cpu" ]; then
    cpu_state="worker cpu UNKNOWN (no session/pid to sample)"
  elif [ -z "${AF_HB_CPU:-}" ]; then
    cpu_state="worker cpu UNKNOWN (no prior sample to compare)"
  elif [ "$cpu" -gt "${AF_HB_CPU:-0}" ] 2>/dev/null; then
    cpu_state="cpu +$(( cpu - AF_HB_CPU ))s"
  else
    cpu_state="worker cpu flat (+0s)"; cpu_quiet=1
  fi

  # (c) Pane bytes, chrome stripped.
  if [ -z "$pane" ]; then
    pane_state="pane UNKNOWN (tmux session unreadable)"
  elif [ -z "${AF_HB_PANE:-}" ]; then
    pane_state="pane UNKNOWN (no prior capture to compare)"
  elif [ "$pane" != "${AF_HB_PANE:-}" ]; then
    pane_state="pane changed"
  else
    pane_state="pane byte-identical"; pane_quiet=1
  fi

  AF_HB_LAST="$nowt"; AF_HB_CPU="$cpu"; AF_HB_PANE="$pane"

  if [ "$cpu_quiet" = 1 ] && [ "$pane_quiet" = 1 ]; then
    af_watch_stop_say "STALL WARNING round #$rnd — every liveness signal quiet for ${quiet_for}min: ticket state unchanged, ${cpu_state}, ${pane_state}. Still outstanding: ${ids:-<unknown>}. Not killing the round; look at it."
  else
    local verdict="worker is active"
    case "${cpu_state}${pane_state}" in *UNKNOWN*) verdict="worker liveness not fully sampled" ;; esac
    af_watch_stop_say "round #$rnd still working — ${quiet_for}min quiet on ticket state, but ${verdict} (${cpu_state}, ${pane_state}). Still outstanding: ${ids:-<unknown>}."
  fi
}
# --- END round stall heartbeat ---
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
# WHEN THIS RUN STARTED. The dividing line between work THIS run is accountable for and residue an
# EARLIER run left behind, used by reap_branches to tell a ticket this run broke from a ticket a
# different run finished days ago. Captured once, before anything can move, and never reset.
#
# The gap it closes (farming_analysis, 2026-08-09): a forgotten loop left worker branches lying
# around; a freshly relaunched loop's orphan sweep found them, saw that the tickets they named read
# `finished` with commits not on the integration ref, and REGRESSED a ticket a different run had
# genuinely completed days earlier — then halted the round over it. The branch was never this run's
# to judge: its tip commit predates the run's own start.
AF_START_EPOCH="${AF_START_EPOCH:-$(date +%s)}"
# State lives beside the worktrees, not inside them: a log or a verdict sentinel written into a repo
# gets swept into a wip commit and read back as ticket output.
AF_STATE_DIR="${AF_STATE_DIR:-$(dirname "$WT")}"
LOG="${AF_LOG:-$AF_STATE_DIR/af-ticket-loop.log}"
if [ -n "${AF_PYTHON:-}" ]; then PY="$AF_PYTHON"
elif [ -x "$AF_REPO/.venv/bin/python" ]; then PY="$AF_REPO/.venv/bin/python"
else PY="$(command -v python3)"; fi

# ------------------------------------------------------ argv/settings agreement + worktree mutex --
#
# Two silent failures, one root cause: NOTHING tied this run's argv to the worktree it runs in.
#
#  1. The PROJECT this driver builds comes from argv, but the completeness-gate hook and every
#     worker session read FACTORY_PROJECT out of <worktree>/.claude/settings.local.json. When those
#     disagree the loop keeps building `$PROJECT` while its hooks resolve `prd-<other>` — and the
#     gate does not shout, it goes INERT, which is the worst way for a gate to fail.
#  2. Nothing stopped two loops sharing one worktree. Observed 2026-08-19: a second loop was started
#     on /workspace/sports_analysis for `mvpvu-data-collection` and repointed the settings file;
#     the pre-existing `mvpvu-foundation` loop was still running in that same tree and had its hooks
#     silently redirected to the wrong project for the rest of its life.
#
# So: FAIL LOUDLY on disagreement (never auto-patch the file — a silent rewrite of a shared config
# is exactly what poisoned the second loop), and take a per-worktree lock so the second loop cannot
# start at all. Deliberately the FIRST thing that happens after argv is parsed, before any Praxis
# call, any disk preflight, and any settings sourcing.
#
# --- BEGIN worktree guard ---
AF_SETTINGS="$WT/.claude/settings.local.json"

af_guard_die(){ echo "FATAL: $*" >&2; exit 2; }

[ -f "$AF_SETTINGS" ] || af_guard_die "no settings file at $AF_SETTINGS — this loop cannot confirm that the worktree agrees the project is '$PROJECT'. Refusing to start."

AF_SETTINGS_PROJECT="$("$PY" - "$AF_SETTINGS" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    print(json.load(open(sys.argv[1]))["env"]["FACTORY_PROJECT"])
except Exception:
    pass
PYEOF
)"

[ -n "$AF_SETTINGS_PROJECT" ] || af_guard_die "$AF_SETTINGS has no env.FACTORY_PROJECT — the completeness gate and every worker session read that key, so with it absent they resolve nothing and the gate goes inert rather than loud. Set it to '$PROJECT' yourself and relaunch; this loop will not write it for you."

if [ "$AF_SETTINGS_PROJECT" != "$PROJECT" ]; then
  af_guard_die "PROJECT MISMATCH — argv says the project is '$PROJECT', but $AF_SETTINGS declares env.FACTORY_PROJECT='$AF_SETTINGS_PROJECT'. Hooks and workers would resolve prd-$AF_SETTINGS_PROJECT while this loop builds $PROJECT, and the completeness gate would go inert rather than loud. Refusing to start, and refusing to rewrite that file — it may be shared with another live loop, which is how this bug happened in the first place."
fi

# Per-WORKTREE mutex, keyed on the path rather than the project: two loops in one tree collide over
# git state and settings.local.json no matter which projects they name. O_EXCL via noclobber is the
# atomic part; the pid line is what makes a crashed run's lock reclaimable without a human, since a
# lock that needs manual cleanup is the same class of problem as the bug above.
AF_LOCK="$WT/.af-loop.lock"
AF_LOCK_HELD=0

af_release_worktree_lock(){
  if [ "${AF_LOCK_HELD:-0}" = "1" ] && [ -n "${AF_LOCK:-}" ]; then
    AF_LOCK_HELD=0
    rm -f "$AF_LOCK" 2>/dev/null || true
  fi
}

af_take_worktree_lock(){
  local holder_pid holder_project
  if ( set -o noclobber; printf '%s %s\n' "$$" "$PROJECT" > "$AF_LOCK" ) 2>/dev/null; then
    AF_LOCK_HELD=1; return 0
  fi
  holder_pid="$(awk 'NR==1{print $1}' "$AF_LOCK" 2>/dev/null || true)"
  holder_project="$(awk 'NR==1{print $2}' "$AF_LOCK" 2>/dev/null || true)"
  if [ -n "$holder_pid" ] && kill -0 "$holder_pid" 2>/dev/null; then
    af_guard_die "ANOTHER LOOP IS ALREADY RUNNING IN THIS WORKTREE — $AF_LOCK is held by pid $holder_pid (project '${holder_project:-unknown}'). Two loops in one worktree fight over git state and .claude/settings.local.json, which is how a live run gets its FACTORY_PROJECT changed underneath it. Stop that run first, or use a separate worktree."
  fi
  # Stale: the recorded pid is dead (or the file is unreadable garbage). Reclaim it automatically.
  echo "reclaiming stale lock $AF_LOCK (pid ${holder_pid:-?} is not alive)" >&2
  rm -f "$AF_LOCK"
  if ( set -o noclobber; printf '%s %s\n' "$$" "$PROJECT" > "$AF_LOCK" ) 2>/dev/null; then
    AF_LOCK_HELD=1; return 0
  fi
  af_guard_die "could not take $AF_LOCK after reclaiming it — another loop won the race. Retry once it exits."
}

af_take_worktree_lock
# Early trap so an exit BEFORE the full af_cleanup_on_exit trap is installed still frees the lock.
# That trap replaces this one and calls af_release_worktree_lock itself, so every exit path — drain,
# halt, SIGINT/SIGTERM, `tmux kill-session` — releases.
trap af_release_worktree_lock EXIT
trap 'af_release_worktree_lock; exit 143' INT TERM
echo "worktree lock $AF_LOCK held by pid $$ for project $PROJECT" >&2
# --- END worktree guard ---

# --- BEGIN db port guard ---
# DEFECT 1, and the most expensive of the three. Measured on the devbox 2026-08-19 with BOTH loops
# LIVE: /workspace/hudl-cv-download (hudl-cv-download) and /workspace/sports_analysis
# (mvpvu-data-collection) BOTH declared Postgres on port 5438; /workspace/beauty-api-buildout and
# /workspace/bestie both on 5434. Each worktree names its own database in
# <worktree>/.claude/settings.local.json under env, and the KEY VARIES by project (PRAXIS_DB_URL,
# POSTGRES_URL, DATABASE_URL, SPORTS_ANALYSIS_DB_URL, SCRAPER_DATABASE_URL, ...) — which is exactly
# why nobody noticed. Two concurrent loops writing one Postgres corrupt each other's state, and
# nothing anywhere warned.
#
# So, now that this worktree's lock is held: look at every OTHER worktree holding a LIVE
# .af-loop.lock and refuse to start if it declares the same port. Deliberately does NOT auto-pick a
# free port — which database a project owns is a human decision, and silently moving one is the
# same class of mistake as silently rewriting settings.local.json. AF_ALLOW_SHARED_DB=1 opts out,
# loudly. Failure exits through af_guard_die, so the early trap above releases the lock.
AF_LOCK_SCAN_ROOT="${AF_LOCK_SCAN_ROOT:-$(dirname "$WT")}"

af_db_port_of(){   # <settings.local.json> -> the Postgres port it declares, or empty
  [ -f "${1:-}" ] || return 0
  "$PY" - "$1" <<'PYEOF' 2>/dev/null || true
import json, sys
from urllib.parse import urlparse

try:
    env = json.load(open(sys.argv[1])).get("env") or {}
except Exception:
    raise SystemExit(0)
# The key varies per project, so match on the VALUE being a postgres URL rather than on a fixed
# list of names — a list is how the next project's spelling gets missed.
for key, value in sorted(env.items()):
    if not isinstance(value, str) or not value.split("://", 1)[0].startswith("postgres"):
        continue
    try:
        port = urlparse(value).port
    except Exception:
        continue
    if port:
        print(port)
        break
PYEOF
}

AF_DB_PORT="$(af_db_port_of "$AF_SETTINGS")"
if [ -n "$AF_DB_PORT" ]; then
  for _lock in "$AF_LOCK_SCAN_ROOT"/*/.af-loop.lock; do
    [ -f "$_lock" ] || continue
    _other_wt="$(dirname "$_lock")"
    [ "$_other_wt" = "$WT" ] && continue
    _other_pid="$(awk 'NR==1{print $1}' "$_lock" 2>/dev/null || true)"
    _other_project="$(awk 'NR==1{print $2}' "$_lock" 2>/dev/null || true)"
    # A lock whose pid is dead is residue, not a live loop — same reclaim rule as the mutex above.
    { [ -n "$_other_pid" ] && kill -0 "$_other_pid" 2>/dev/null; } || continue
    # Third field when the holder wrote one; otherwise read its worktree, which is what a loop
    # already in flight (lock line "<pid> <project>") requires.
    _other_port="$(awk 'NR==1{print $3}' "$_lock" 2>/dev/null || true)"
    [ -n "$_other_port" ] || _other_port="$(af_db_port_of "$_other_wt/.claude/settings.local.json")"
    [ "$_other_port" = "$AF_DB_PORT" ] || continue
    if [ "${AF_ALLOW_SHARED_DB:-0}" = "1" ]; then
      echo "WARNING: AF_ALLOW_SHARED_DB=1 OVERRIDE — starting anyway although $WT (project '$PROJECT') and $_other_wt (project '${_other_project:-unknown}', live pid $_other_pid) BOTH declare Postgres on port $AF_DB_PORT. Two loops writing one database corrupt each other's state; you have asserted that this sharing is intentional." >&2
      continue
    fi
    af_guard_die "SHARED DATABASE PORT — $WT (project '$PROJECT') and $_other_wt (project '${_other_project:-unknown}', held by LIVE pid $_other_pid) both declare Postgres on port $AF_DB_PORT. Two concurrent loops writing one database corrupt each other's state, silently. Give this project its own port in $AF_SETTINGS and relaunch, or set AF_ALLOW_SHARED_DB=1 if the sharing is deliberate. This loop will not pick a port for you and will not rewrite that file."
  done
fi
# Record the port on our own lock line so the next loop can see it without re-reading our settings.
# Appended as a THIRD field only: a loop already in flight wrote "<pid> <project>", and the readers
# above parse both shapes.
if [ -n "$AF_DB_PORT" ] && [ "${AF_LOCK_HELD:-0}" = "1" ]; then
  printf '%s %s %s\n' "$$" "$PROJECT" "$AF_DB_PORT" > "$AF_LOCK" 2>/dev/null || true
fi
# --- END db port guard ---
# Exported, so every embedded heredoc below imports the hooks without hardcoding a path of its own.
#
# $AF_PLUGIN_DIR itself leads, and it is not decoration. `hooks/` is a plain DIRECTORY, not an
# installed package, so `from hooks import _praxis` only resolves when the directory CONTAINING
# hooks/ is on the path — and the two entries that follow put the hook MODULES on the path while
# leaving their parent off it. The 2026-08-07 build shipped a whole subsystem behind exactly that
# asymmetry: 1207 unit tests green (pytest sets `pythonpath = ["src", ".", "hooks"]`, which does
# include the parent) while this script's only call into it died on
# `ModuleNotFoundError: No module named 'hooks'` and was swallowed by a `|| true`.
# `agent_factory._hooks` now repairs the path from its own file location so the import no longer
# DEPENDS on this line — this is the belt to that module's braces, not a substitute for it.
export PYTHONPATH="$AF_PLUGIN_DIR:$AF_PLUGIN_DIR/hooks:$AF_PLUGIN_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
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

# THE BOX WORKTREE REGISTRY (D6), populated HERE because this is the only process that knows the
# answer. `agent_factory.widening.resolve_sibling_worktree` needs, for a project, the checkout whose
# HEAD is that project's current INTEGRATION state — the healthy reference half of a widening proof.
# It reads that from $BOX_WORKTREE_REGISTRY and treats "absent" as "sibling unavailable", so with
# nothing populating the variable `attempt_widen` PARKS unconditionally: it can never widen, only
# emit parking flags forever. Nothing in the repo defined it, and nothing in the repo COULD — the
# mapping is a property of this box's on-disk layout, not of the source tree, which is exactly why
# the module sources it from the environment rather than hardcoding a path.
#
# The authoritative entry is our own: $PROJECT's integration state is $WT, by construction — this
# driver merges every finished ticket into it. That is also the entry that actually gets used, since
# `attempt_widen` defaults `sibling_project` to the project being widened. Sibling projects are
# added best-effort by scanning the state dir for other checkouts (both layouts this box has ever
# used: a bare `<project>` directory and the `<project>-build` convention), and a wrong guess there
# costs a park, not a bad widen — the proof still has to FAIL on the bad artifact and PASS on that
# reference before anything widens.
#
# AF_BOX_WORKTREE_REGISTRY overrides the scan wholesale for a box whose layout this cannot infer.
if [ -n "${AF_BOX_WORKTREE_REGISTRY:-}" ]; then
  export BOX_WORKTREE_REGISTRY="$AF_BOX_WORKTREE_REGISTRY"
else
  BOX_WORKTREE_REGISTRY="$(
    "$PY" - "$PROJECT" "$WT" "$AF_STATE_DIR" <<'PYEOF' 2>/dev/null || echo ''
import json, os, sys
project, wt, state_dir = sys.argv[1], sys.argv[2], sys.argv[3]
reg = {}
try:
    for name in sorted(os.listdir(state_dir)):
        path = os.path.join(state_dir, name)
        if not os.path.exists(os.path.join(path, ".git")):
            continue
        # `<project>-build` / `<project>_build` are this box's build-checkout conventions; a bare
        # directory name is taken as the project name itself.
        for suffix in ("-build", "_build"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        reg.setdefault(name, path)
except OSError:
    pass
# Ours LAST and unconditional: a scan guess must never shadow the one entry we actually know.
reg[project] = wt
print(json.dumps(reg, sort_keys=True))
PYEOF
  )"
  export BOX_WORKTREE_REGISTRY
fi

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

# Launch the configured agent into a detached tmux session. For grok, the prompt is
# the TUI's initial argv so we do not have to type a 4KB string into a not-quite-ready
# input box (the failure mode the Claude send-keys path exists to detect). For sonnet
# and deepseek the CLI still starts empty and the caller send-keys the prompt after
# pane_is_ready.
af_launch_agent(){   # $1=tmux session  $2=optional initial prompt (grok only)
  local sess="$1" prompt="${2:-}" pf
  tmux kill-session -t "$sess" 2>/dev/null || true
  tmux new-session -d -s "$sess" -c "$WT"
  if [ "$BACKEND" = grok ] && [ -n "$prompt" ]; then
    pf="${AF_STATE_DIR}/af-${sess}.prompt"
    printf '%s' "$prompt" > "$pf"
    tmux send-keys -t "$sess" "cd $WT && set -a && . '${AF_STATE_DIR}/af-agent.env' && set +a && $CLAUDE_LAUNCH -- \"\$(cat '$pf')\"" Enter
  else
    tmux send-keys -t "$sess" "cd $WT && set -a && . '${AF_STATE_DIR}/af-agent.env' && set +a && $CLAUDE_LAUNCH" Enter
  fi
}

af_wait_ready(){   # $1=tmux session  $2=optional poll cap (default READY_POLL_MAX)
  local sess="$1" max="${2:-$READY_POLL_MAX}" i pane
  for i in $(seq 1 "$max"); do
    sleep 2
    pane=$(tmux capture-pane -t "$sess" -p 2>/dev/null || echo "")
    if echo "$pane" | grep -qE "bypass permissions on"; then return 0; fi
    if [ "$BACKEND" = grok ]; then
      # The first-run directory-trust splash also prints "Grok Build 1.0.5".
      # Treating that banner as ready made unattended grok rounds sit 10–30min
      # with the prompt never started (hudl-cv-download 2026-08-19).
      if echo "$pane" | grep -qiE "Do you trust the contents|Yes, proceed"; then
        tmux send-keys -t "$sess" "y" Enter
        continue
      fi
      if echo "$pane" | grep -qiE "always-approve|Always approve|to interrupt|Waiting for response|Subagents"; then
        return 0
      fi
    fi
  done
  return 1
}

# Source the project's pinned Praxis identity so every _praxis call authenticates —
# without this, auth fails closed, stderr is swallowed by claimable()'s redirect,
# and `set -e` kills the whole driver on its very first call with NO log output at
# all (exactly what happened the first time this ran).
eval "$(python3 -c "
import json, shlex
d = json.load(open('$WT/.claude/settings.local.json'))['env']
# Claude injects this whole env map into the session. Grok does not, so the
# loop exports every key — Praxis identity AND FACTORY_PROJECT — into this
# shell and into the sourced file the tmux launch line reads.
lines = []
for k, v in d.items():
    lines.append('export %s=%s' % (k, shlex.quote(str(v))))
open('$AF_STATE_DIR/af-agent.env', 'w').write('\\n'.join(lines) + '\\n')
print('\\n'.join(lines))
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

# CONSTITUTIONAL INVARIANT: a headless verify/build session must NEVER silently block on an
# interactive prompt. When the Claude Max subscription hits its session/usage limit the CLI does not
# error out -- it strands on the interactive `/rate-limit-options` menu ("1. Stop and wait for limit
# to reset / 2. Switch to usage credits / 3. Switch to Team plan"), which nothing headless can
# answer. Undetected, the pane just sits until the full STALL_POLLS window burns and the round is
# logged UNVERIFIED, and every following round walks its workers into the identical wall (observed
# 2026-08-10: round #3 UNVERIFIED at the 15-min verify stall, rounds #4/#5 then finished ZERO,
# detected only by the generic pane-unchanged watchdog). These strings are the menu's OWN text and
# do not occur in ordinary tool output, so -- unlike a bare "rate limit" -- matching them is a
# reliable, low-false-positive signal of quota exhaustion, DISTINCT from a generic quiet-pane stall
# and from an API-credit 402 (which the billing grep owns). Reads the captured pane on stdin.
rate_limited(){ grep -qiE "hit your (session|usage) limit|/rate-limit-options|stop and wait for limit to reset|switch to usage credits"; }

# Exit code 8 -- quota/session-limit blocked. Deliberately NOT folded into 3 (API-credit/billing
# 402): the operator action is different (wait for the subscription window to reset, or switch the
# plan/backend -- not "top up credits"), and the straggler-exit reasoning applies verbatim: a
# distinct terminal state gets a distinct code so a human or monitor can read it.
AF_EXIT_QUOTA_BLOCKED=8

# React the INSTANT the menu is seen -- never burn the stall window on it -- and HALT the whole run
# rather than dispatch further rounds into the same wall. $1 = where we are, $2 = session to tear
# down. Never returns.
halt_quota_blocked(){
  local where="$1" sess="${2:-}"
  say "QUOTA BLOCKED at $where — the model backend hit its Claude subscription session/usage limit and the"
  say "  headless session is stranded on the interactive /rate-limit-options menu, which nothing here can answer."
  say "  backend was: ${BACKEND:-?} (${BACKEND_NOTE:-?})"
  say "  This is NOT a generic stall and NOT an API-credit 402 — the subscription's session quota is exhausted."
  say "  HALTING the whole run (exit $AF_EXIT_QUOTA_BLOCKED) instead of launching more rounds that will hit the"
  say "  same wall. Remedy: wait for the quota to reset (the menu named the reset time), or switch the plan/backend, then relaunch."
  [ -n "$sess" ] && tmux kill-session -t "$sess" 2>/dev/null || true
  exit "$AF_EXIT_QUOTA_BLOCKED"
}

# Resolve the backend BEFORE any ticket work, and refuse to start a multi-hour run on a
# half-configured one. Failing here costs seconds; failing three tickets in costs an hour
# and a lease that has to be released by hand.
resolve_backend || { say "FATAL: model backend preflight failed for '${BACKEND:-?}' — refusing to start"; exit 1; }
say "backend=$BACKEND ($BACKEND_NOTE)"
# PREFLIGHT NOTE for a subscription backend: a Claude Max/Pro subscription meters a session/usage
# QUOTA, not API credits, so a long unattended run can exhaust it mid-flight and strand a headless
# session on the interactive /rate-limit-options menu. The loop now DETECTS that and halts loudly
# (exit $AF_EXIT_QUOTA_BLOCKED) rather than burning stall windows round after round -- but the run
# still stops, so for a long unattended build prefer an API-credit backend or watch for the halt.
case "$BACKEND" in
  sonnet) say "NOTE: backend is a Claude subscription (session quota, not API credits) — a long unattended run can hit the session limit; the loop will halt with exit $AF_EXIT_QUOTA_BLOCKED if it does, but consider API credits for very long runs." ;;
  grok) say "NOTE: backend is an xAI Grok subscription (OAuth session token, not XAI_API_KEY) — API credits are unset on every launch." ;;
esac

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

plan_bless_state(){  # -> blessed | armed | never-blessed
  $PY - "$PROJECT" <<'PYEOF' 2>/dev/null
import sys
import _praxis
p = sys.argv[1]
# force=True: this driver's whole job is to notice when the plan's state CHANGES (an intake session
# arming it mid-run, or a bless finally landing), so it must never read _praxis' short-lived cache.
print(_praxis.plan_bless_state(p, f'prd-{p}', force=True))
PYEOF
}

# THE BLESS GATE. A plan that is ARMED (an intake session holds the planning marker) or has NEVER
# BEEN BLESSED is not a build target: its tickets are still being written, merged, split and
# deleted underneath anyone reading them.
#
# The incident (farming_analysis, 2026-08-09): a forgotten af-ticket-loop was still running while a
# human was mid-intake on the very plan it was pointed at. It claimed tickets out of an unblessed
# plan, built them, and left branches that then poisoned the NEXT run's orphan sweep. Two halves fix
# it: `_praxis.claim_requirement` refuses the claim outright so no caller can be launched around it,
# and this — the loud, early half that stops the run before it burns a round finding out.
#
# Returns 0 when dispatch may proceed. Non-watch mode never returns non-zero: an unblessed plan is
# not a condition that resolves by trying again, so it EXITS. Watch mode is exactly the case where
# it does resolve by trying again (the human is finishing intake right now), so it waits.
require_blessed_plan(){   # -> 0 = blessed, dispatch may proceed; 1 = caller must not dispatch yet
  local st why
  if ! st=$(praxis_q plan_bless_state); then outage "plan_bless_state"; return 1; fi
  [ "$st" = blessed ] && return 0
  case "$st" in
    armed) why="an intake session holds the planning marker — the plan is ARMED for editing" ;;
    *)     why="intake never finished — this plan has never been blessed" ;;
  esac
  say "preflight: FAILED — plan not blessed: prd-$PROJECT is '$st'; $why. Nothing may be claimed from it."
  if [ "${AF_WATCH:-0}" = "1" ]; then
    if [ "${watch_bless_at:-}" != "$st" ]; then
      say "WAITING for the bless (AF_WATCH=1) — re-checking every ${AF_WATCH_POLL_S:-300}s; no ticket is claimed or dispatched until prd-$PROJECT is blessed. Stop with: touch $WATCH_STOP"
      watch_bless_at="$st"
    fi
    af_watch_stopped && { say "watch stop file present ($WATCH_STOP_HIT) — exiting"; exit 0; }
    sleep "${AF_WATCH_POLL_S:-300}"
    return 1
  fi
  say "Finish intake (af-intake-plan) so prd-$PROJECT is blessed, then relaunch — or run with AF_WATCH=1 so this loop waits for the bless instead of exiting."
  exit 1
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
ref = (p, f'prd-{p}')
out = []
for t in ts.ready_tickets(facts):
    m = t.get('meta') or {}
    rid = m.get('requirement_id') or t.get('id')
    if not rid:
        continue
    # A ticket parked on a manual sign-off is not dispatchable work: its automated obligations are
    # already met, so a worker can only rebuild what exists and fail the same human gate again —
    # one wasted full build per round, forever, until the sign-off lands (observed: R62, rounds
    # #6-#7 of 2026-08-10). The moment a human records the manual pass the strict gate goes green,
    # parked_on_manual flips false, and the very next frontier poll dispatches it to finish.
    try:
        if ts.parked_on_manual(t, ref):
            continue
    except Exception:
        pass   # unanswerable "parked?" -> dispatch as before; a wasted rebuild beats a stuck run
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
  #
  # BUG E — but "regress on no change" cannot loop unbounded. Live incident: a finding's defect had
  # already been fixed by an EARLIER round's commit, so the rebuild correctly produced zero commits,
  # this guard regressed it for exactly that, and the pair ping-ponged every ~9 minutes forever. The
  # finding carried check_id=None, so the check_id-keyed auto-suspend could never fire and nothing
  # else could break the loop. So we bound it: a per-(ticket, finding) streak counter, and after
  # AF_FINDING_REGRESS_MAX zero-commit regressions of the SAME still-open finding we STOP regressing
  # and write a LOUD escalation line to $FINDING_ESCALATION for the caller to say — a human must look,
  # because the finding is stale or was already resolved by a prior commit. An answering commit (or the
  # finding closing) resets the streak, so a ticket that is genuinely churning is unaffected.
  : > "$FINDING_ESCALATION" 2>/dev/null || true
  $PY - "$PROJECT" "$1" "${AF_ROUND_BASE:-HEAD}" "$FINDING_STREAK" "$FINDING_ESCALATION" "$AF_FINDING_REGRESS_MAX" "${@:2}" <<'PYEOF' 2>/dev/null
import hashlib, json, subprocess, sys
import _praxis, _ticket_state as ts
proj, rnd, base = sys.argv[1], sys.argv[2], sys.argv[3]
streak_path, esc_path, kmax = sys.argv[4], sys.argv[5], int(sys.argv[6] or 2)
want = set(sys.argv[7:])
kw = dict(space=proj, snapshot=f"prd-{proj}")

# Per-(ticket, finding-reason) count of consecutive zero-commit regressions, persisted across rounds
# AND across process restarts (the regress loop outlives any one loop process). A malformed/absent
# file is an empty streak, never a crash — losing the count only costs one extra regress cycle.
try:
    streak = json.load(open(streak_path))
    if not isinstance(streak, dict):
        streak = {}
except Exception:
    streak = {}

def _forget(rid):  # an answered/closed finding resets this ticket's streak(s)
    for k in [k for k in streak if k.startswith(rid + "\x00")]:
        streak.pop(k, None)

escalations, n = [], 0
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
        _forget(rid)             # answered by a commit -> not a no-change close
        continue
    why = ts.finding_unanswered_without_change(m, 0)
    of = ts.open_finding(m)
    if not why or of is None:
        _forget(rid)             # no open finding stands -> nothing to regress, clear any stale streak
        continue
    # Identity of THIS finding is its recorded symptom text; a new/different finding starts a new
    # streak. check_id is captured only to name it in the escalation (it is frequently None here, which
    # is precisely the case the check_id-keyed suspend cannot catch).
    reason = str(of.get("reason") or "").strip()
    cid = of.get("check_id")
    key = rid + "\x00" + hashlib.sha1(reason.encode("utf-8", "replace")).hexdigest()[:16]
    prior = int(streak.get(key, 0))
    if prior >= kmax:
        # Already regressed kmax times for this exact finding with zero answering commits, and it is
        # STILL open. Regressing again just re-dispatches the same ticket forever. Escalate, do not
        # regress. Keep climbing the counter so the escalation re-fires each poll rather than going
        # silent (a stuck finding a human has not yet cleared must stay visible).
        streak[key] = prior + 1
        escalations.append((rid, prior, cid, reason))
        continue
    _praxis.regress_requirements(proj, [f["id"]], {f["id"]: {
        "audit_disposition": f"ROUND #{rnd}: {why}",
    }}, **kw)
    streak[key] = prior + 1
    sys.stderr.write(f"finding-guard regressed {rid} (streak {streak[key]}/{kmax})\n")
    n += 1

try:
    json.dump(streak, open(streak_path, "w"))
except Exception as e:
    sys.stderr.write(f"finding-guard: could not persist streak ({e})\n")
try:
    with open(esc_path, "w") as fh:
        for rid, cnt, cid, reason in escalations:
            fh.write(f"{rid}\t{cnt}\t{cid or ''}\t{reason[:300]}\n")
except Exception as e:
    sys.stderr.write(f"finding-guard: could not write escalations ({e})\n")
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
import _praxis, _ticket_state as ts
p = sys.argv[1]
facts = _praxis.facts_by(category='requirement', space=p, snapshot=f'prd-{p}') or []
state, deps, parked = {}, {}, set()
for f in facts:
    m = f.get('meta') or {}
    rid = str(m.get('requirement_id') or f.get('id') or '')
    if not rid:
        continue
    state[rid] = m.get('build_state')
    deps[rid] = [str(d) for d in (m.get('depends_on') or [])]
    try:
        if ts.parked_on_manual(f, (p, f'prd-{p}')):
            parked.add(rid)
    except Exception:
        pass
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
    tag = " PARKED awaiting manual sign-off" if r in parked else ""
    print(f"{r} [{state.get(r)}]{tag} blocks {len(behind)}: {', '.join(behind[:8])}{' ...' if len(behind) > 8 else ''}")
PYEOF
}

batch_open(){  # args: ids... -> how many are still incomplete|in_progress (blocked counts as done)
  # PARKED-ON-MANUAL also counts as done. A ticket whose every automated obligation is met and
  # which now waits only on a human sign-off cannot be closed by any amount of wall clock — the
  # worker may never self-certify a manual requirement — so counting it open makes the round
  # unfinishable by construction and the scaled deadline becomes the only exit. Observed
  # 2026-08-10: R62 (manual-verify gate) held round #6 open to the full 100min timeout with its
  # work done and merged. Parked tickets are reported once so the sign-off need is never silent.
  $PY - "$PROJECT" "$@" <<'PYEOF' 2>/dev/null
import sys
import _praxis, _ticket_state as ts
p, want = sys.argv[1], set(sys.argv[2:])
ref = (p, f'prd-{p}')
n, parked = 0, []
for f in _praxis.facts_by(category='requirement', space=p, snapshot=f'prd-{p}'):
    m = f.get('meta') or {}
    ids = {str(f.get('id') or ''), str(m.get('requirement_id') or '')} - {''}
    if (ids & want) and m.get('build_state') in ('incomplete', 'in_progress'):
        try:
            if ts.parked_on_manual(f, ref):
                parked.append(str(m.get('requirement_id') or f.get('id')))
                continue
        except Exception:
            pass   # an unanswerable "parked?" stays open — never silently closes a round
        n += 1
print(n)
if parked:
    sys.stderr.write("parked awaiting manual sign-off: " + " ".join(sorted(parked)) + "\n")
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
  #
  # Its stdout is TEE'd into the run log because the caller discards it: the "nothing matched" line
  # below is the whole point of that branch, and a message only the caller never reads is no message.
  $PY - "$PROJECT" "$1" "$2" <<'PYEOF' 2>/dev/null | tee -a "$LOG"
import sys
import _praxis
p, rid, br = sys.argv[1], sys.argv[2], sys.argv[3]
kw = dict(space=p, snapshot=f'prd-{p}')
# NOT FINDING THE TICKET IS SUCCESS, and says so. A branch can name a requirement id that no active
# fact carries any more -- a ticket deleted at intake, renamed, or belonging to another project
# whose ids overlap. There is nothing to regress, so the regress SUCCEEDED at everything it could do;
# reporting failure instead turned "that ticket is gone" into a failed round with no explanation.
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
else:
    print(f'no active fact for {rid} — skipping regress')
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
  scrub_test_results
  if [ -n "$(git status --porcelain)" ]; then
    git add -A
    git -c user.name="af-build" -c user.email="af-build@praxis.local" commit -q -m \
      "wip: preserve in-flight output before per-batch session restart (af-ticket-loop)"
    say "committed WIP: $(git log --oneline -1)"
  fi
}

# BUG B-3 — the loop must NEVER commit Playwright test-results/ artifacts (trace.zip,
# error-context.md). commit_wip's `git add -A` sweeps EVERYTHING loose in $WT, and a worker that ran
# Playwright leaves a test-results/ tree behind; those artifacts have been committed into project
# repos. Belt and braces, run against the current worktree ($PWD, set by commit_wip's `cd "$WT"`):
# exclude the path LOCALLY so `git add -A` never stages it, and drop any copy an earlier run already
# tracked. .git/info/exclude, not .gitignore, keeps this out of the project's committed history — it
# is the loop's own hygiene, not a policy edit the project's tree should carry.
scrub_test_results(){
  local xf=".git/info/exclude"
  if [ -d .git ] && [ -d .git/info ]; then
    grep -qxF 'test-results/' "$xf" 2>/dev/null || echo 'test-results/' >> "$xf" 2>/dev/null || true
  fi
  # Un-track any test-results already committed by an earlier run (leaves the files on disk; the
  # exclude above then keeps them unstaged). --ignore-unmatch so "none tracked" is not an error.
  git rm -r --cached --ignore-unmatch --quiet -- 'test-results' >/dev/null 2>&1 || true
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
  # Grok isolation=worktree checks out under $WT/.grok/worktrees and
  # ~/.grok/worktrees/, not only $WT/.claude/worktrees/. Filtering to the
  # Claude path made a 16-ticket grok round finish in Praxis with zero
  # commits on main (hudl-cv-download 2026-08-19): the driver logged
  # "0 unmerged branches" and post-merge verification regressed all 16.
  while read -r path; do
    [ -n "$path" ] || continue
    [ "$path" = "$WT" ] && continue
    [ "$path" = "${WT%/}" ] && continue
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

# Grok spawn_subagent isolation=worktree often materializes a SEPARATE clone under
# ~/.grok/worktrees/workspace-<checkout>/subagent-*, not a `git worktree add` of $WT.
# Those commits never appear in this repo's worktree list, so integrate_round and
# queue_orphan_branches cannot see them. Fetch each unique HEAD into a local
# worktree-agent-* ref so the existing merge/orphan path can land it.
# Observed 2026-08-19 on hudl-cv-download: 16 tickets finished in those clones,
# this repo had zero worker branches, post-merge verification regressed all 16.
salvage_external_grok_clones(){
  cd "$WT" || return 0
  local root d sha short br ahead
  root="${HOME}/.grok/worktrees/workspace-$(basename "$WT")"
  [ -d "$root" ] || return 0
  for d in "$root"/subagent-*; do
    [ -d "$d" ] || continue
    sha=$(git -C "$d" rev-parse HEAD 2>/dev/null || echo "")
    [ -n "$sha" ] || continue
    if git merge-base --is-ancestor "$sha" HEAD 2>/dev/null; then
      continue
    fi
    short=$(basename "$d" | tr -c 'A-Za-z0-9._-' '-' | cut -c1-40)
    br="worktree-agent-salvage-${short}"
    if git fetch --no-tags "$d" "HEAD:refs/heads/$br" 2>/dev/null; then
      ahead=$(git rev-list --count "HEAD..$br" 2>/dev/null || echo 0)
      [ "${ahead:-0}" -gt 0 ] && say "salvaged $ahead commit(s) from $d onto $br"
    else
      say "WARNING: could not fetch grok clone $d — its commits stay outside this repo"
    fi
  done
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
  local rprompt="You are resolving MERGE CONFLICTS for build round $rnd of project $PROJECT, in the checkout at $WT which is already on the integration branch. Each line of $CONFLICTS is a TAB-separated branch name and the ticket id(s) whose work it carries. These branches were built and passed their own gates; they conflict only because sibling work landed first. Do NOT build features, do NOT claim tickets, do NOT push. THE ONE ABSOLUTE RULE: EVERY branch listed MUST end up merged. Leaving a branch unmerged is not an available outcome — a branch nobody merges strands a ticket that reads finished with its work nowhere, and that is the failure this stage exists to eliminate. You always finish with a merge commit for every branch. For EACH branch in order: run git merge --no-ff <branch>, and resolve every conflicted file by UNDERSTANDING BOTH SIDES rather than picking one. Conflicts here are almost always semantic: a file one side deleted and the other edited, a helper one side moved and the other extended, a registry both sides appended to. Keep the intent of BOTH changes wherever you honestly can. If one side DELETED a file the other modified, the deletion almost always wins and the other side change must be re-applied to whatever replaced it — read the deleting commit message to find what superseded it, and never resurrect a deliberately deleted file. When a specific hunk genuinely CANNOT preserve both intents — the two changes are contradictory, or choosing needs a product decision you cannot make — do NOT stall and do NOT abandon the branch. Resolve that hunk by taking the INTEGRATION side (the tree as it already is, which is proven), finish the merge, and record precisely what you dropped: which ticket owned it, which file and behaviour was lost, and what a rebuild has to re-establish. Dropping intent is acceptable ONLY when it is recorded — the ticket is then rebuilt from the current tree, which is the honest repair. Silently taking one side to make a merge succeed is the one thing you must never do. After each branch, PROVE the merged tree: run the repo build and typecheck, and the tests covering the files you touched. If a merge you just made breaks the tree and you cannot fix it, still keep the merge but record the whole branch as dropped-intent so its ticket is rebuilt. Commit each merge with a message naming the branch, the ticket id(s), what conflicted, what you kept from each side, and anything you dropped. When every branch is merged, write JSON to $RESOLVED with exactly these keys: merged which is an array of EVERY branch name you merged (this must list every branch in $CONFLICTS — there is no other outcome), and dropped_intent which is an array of objects each with branch, tickets, and reason stating concretely what was lost and what a rebuild must re-establish (empty array if you preserved everything). Write that file LAST and then STOP. You are running HEADLESS with no human attached: never ask a clarifying question. Work ONLY inside $WT, on the already-checked-out branch, and do NOT push."
  # LAUNCH THE AGENT. Omitting this leaves a bare login shell, and later send-keys type
  # the prompt at bash: bash dies on the first parenthesis and NOT ONE branch is merged.
  af_launch_agent "$rsession" "$rprompt"
  if ! af_wait_ready "$rsession" 60; then
    say "FATAL: conflict resolver pane never signalled ready — no agent in the session, NOT sending the prompt"
    say "FATAL: $n branch(es) remain unmerged and still queued in $CONFLICTS; re-run --resolve-orphans once the model backend is healthy"
    tmux kill-session -t "$rsession" 2>/dev/null || true
    return 1
  fi
  if [ "$BACKEND" != grok ]; then
    sleep 3; tmux send-keys -t "$rsession" Enter
    tmux send-keys -t "$rsession" "$rprompt"
    sleep 3; tmux send-keys -t "$rsession" Enter
  fi

  local waited=0 rstall=0 rlast="" pane rhash
  while [ "$waited" -lt "${AF_RESOLVE_TIMEOUT_S:-2400}" ]; do
    sleep 30; waited=$((waited+30))
    [ -f "$RESOLVED" ] && break
    if ! tmux has-session -t "$rsession" 2>/dev/null; then say "conflict resolver: session gone before writing a result"; break; fi
    pane=$(tmux capture-pane -p -t "$rsession" 2>/dev/null || true)
    # The conflict resolver is a headless session too: halt on the subscription rate-limit menu
    # rather than let it sit out the stall window (same invariant as the verify/build waits).
    if echo "$pane" | rate_limited; then halt_quota_blocked "conflict resolver round #$rnd" "$rsession"; fi
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
from agent_factory import ingestion_api
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
        # R5 — "regression without ingestion is not a legal state". A merger-driven regression is
        # NEVER a bare `regress_requirements` any more: it goes through the ingestion API, which
        # classifies the failure, lands the lesson, and regresses in the SAME motion. This site used
        # to call `_praxis.regress_requirements` directly, so every conflict-resolution regression
        # threw away the one thing the loop actually learned from it.
        #
        # Deliberately NOT wrapped in try/except: `regress_with_ingestion` documents that it catches
        # nothing, so a Praxis outage propagates and halts this pass loudly — the same way the bare
        # regress it replaces always did. Anything quieter would let a run keep going while its
        # regressions silently evaporated.
        lesson = (f"conflict resolution of round #{rnd} merged branch {br} but ticket {rid}'s change "
                  f"did not survive it. {reason}")
        ingestion_api.regress_with_ingestion(
            proj, [f["id"]], lesson,
            source=f"af-ticket-loop/conflict-resolution/{proj}",
            channel="machine", commit_sha=sha,
        )
        # R16/E3: accumulate onto this ticket's existing regression_detail — a concurrent finding
        # must never be clobbered by this one. Re-READ first: the ingestion call above just wrote its
        # OWN entry onto this ticket, and accumulating onto the pre-ingestion copy captured in `f`
        # would drop it.
        current = _praxis.get_fact(f["id"], **kw) or f
        accumulated = ts.accumulate_regression_detail((current.get("meta") or {}).get("regression_detail"), new_finding)
        _praxis.regress_requirements(proj, [f["id"]], {f["id"]: {
            "claim_owner": None, "claim_at": None,
            "claim_heartbeat_at": None, "claim_lease_ttl": None,
            "audit_disposition": (f"REGRESSED by conflict resolution of round #{rnd}: branch {br} was merged, but this "
                                  f"ticket's intent did not survive. WHAT WAS LOST: {reason} THE REBUILD MUST: re-establish "
                                  f"that behaviour against the CURRENT integrated tree."),
            "regression_detail": accumulated}},
            **kw)
        print(f"conflict resolver: regressed {rid} WITH INGESTION — merged, but its intent was dropped")

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
# The repo's MAIN worktree, or empty. `git worktree list` always reports it first.
#
# It matters because the build checkout is not always the main one: on this box $WT is
# /workspace/af-praxis, a LINKED worktree whose `.git` is a file pointing into
# /workspace/praxis/.git/worktrees/af-praxis. Two consequences broke both halves of the straggler
# machinery on 2026-08-07, and both are fixed by knowing this path:
#   1. `isolation: worktree` mints every agent tree under the MAIN worktree
#      (/workspace/praxis/.claude/worktrees/agent-*), never under $WT. A scratch-root list anchored to
#      $WT alone therefore matched NONE of them, so every round they were REPORTED by the broad
#      reporting path and skipped by the narrow sweep -- a violation no sweep could ever clear.
#   2. The main checkout shows up in `git worktree list` on its own branch, so FACT 1 below called
#      `main` owed-a-merge and reported /workspace/praxis ITSELF as a leftover worktree. That is the
#      checkout this driver executes from; it can never be swept, so the terminal invariant was
#      guaranteed to fail the run at drain however well the build went.
af_main_worktree(){
  git worktree list --porcelain 2>/dev/null | awk '/^worktree /{print $2; exit}'
}

af_scratch_roots(){
  local main; main=$(af_main_worktree)
  printf '%s\n' "$WT/.claude/worktrees" "$WT/.grok/worktrees" "${WT}_worktrees"
  # Agent trees live under whichever worktree the harness considered the project root. When $WT is a
  # linked worktree that is the MAIN one, not $WT -- so name it too, or the sweep walks past every
  # tree the round actually created. Still anchored to a real worktree of THIS repo: nothing outside
  # the repo becomes sweepable by this.
  [ -n "$main" ] && [ "$main" != "$WT" ] && printf '%s\n' "$main/.claude/worktrees" "$main/.grok/worktrees"
  return 0
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
  # The MAIN worktree is excluded alongside $WT. FACT 1 reads "checked out in some OTHER worktree =>
  # factory work", which holds for scratch trees but not for the repo's own canonical checkout: when
  # $WT is a linked worktree, the main checkout is a sibling entry sitting on `main`, and counting it
  # made `main` owed-a-merge -- so the base branch itself, and the checkout holding it, were reported
  # as stragglers nothing could clear (see af_main_worktree).
  if [ -z "${AF_WT_BRANCH_CACHE+x}" ]; then
    AF_WT_BRANCH_CACHE=$(git worktree list --porcelain 2>/dev/null \
      | awk -v self="${WT:-}" -v main="$(af_main_worktree)" \
          '/^worktree /{p=$2} /^branch /{sub("refs/heads/","",$2); if (p != self && p != main) print $2}')
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

# FOREIGN ERA: was this branch's newest commit written BEFORE this run started?
#
# A branch whose tip predates AF_START_EPOCH cannot be output of this run — no worker this run
# spawned could have authored it. It is residue from a previous (possibly forgotten) loop, and the
# ticket it names was finished by that other run, on its own terms, quite possibly days ago. This
# run has no standing to call that ticket a liar.
#
# Committer date, not author date: a rebase rewrites the committer date, and what is being asked is
# "did this ref get written during my run", not "when was the change originally conceived".
# A branch with no readable tip (deleted mid-sweep, corrupt ref) answers NO — the conservative
# answer, since it leaves the existing regress path in charge rather than silently archiving.
af_branch_is_foreign_era(){   # $1 = branch name
  local ct
  ct=$(git log -1 --format=%ct "$1" 2>/dev/null) || return 1
  [ -n "$ct" ] || return 1
  [ "$ct" -lt "${AF_START_EPOCH:-0}" ]
}

# A branch name flattened into something `git tag` accepts and a human can still read.
af_sanitize_branch(){   # $1 = branch name
  printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '-'
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
  local br live uniq ids i sup reaped=0 failed=0 survivors="" reason status head_br tag
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
          # FOREIGN-ERA FIRST. Everything below this point accuses the ticket of lying, and that
          # accusation is only this run's to make about work this run did. A branch whose tip
          # predates AF_START_EPOCH belongs to an earlier run; regressing its ticket destroys a
          # completion somebody else earned and — because the case below also fails the round —
          # halts a healthy run over ancient residue.
          if [ "$status" != foreign ] && af_branch_is_foreign_era "$br"; then status=foreign; fi
          if [ "$status" = foreign ]; then
            reason="${reason:+$reason; }$i finished before this run started"
            continue
          fi
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
          case "$status" in failure|foreign) ;; *) status=survivor ;; esac
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
      foreign)
        # Not this run's residue and not this run's verdict. The commits are preserved as a TAG
        # before the branch goes, so nothing is lost and nothing is regressed; the branch itself is
        # removed so the next sweep does not rediscover it and ask the same question forever.
        # Deliberately does NOT increment `failed`: an earlier run's leftovers must not fail a round.
        tag="archive/foreign-$(af_sanitize_branch "$br")"
        if git tag -f "$tag" "$br" >/dev/null 2>&1 && git branch -D "$br" >/dev/null 2>&1; then
          reaped=$((reaped+1))
          say "foreign-era branch $br ($reason; tip predates this run's start) archived as tag $tag, not regressed"
        else
          survivors="${survivors:+$survivors }$br"
          say "WARNING: could not archive foreign-era branch $br — leaving it alone; it is NOT this run's to regress"
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
  local main_wt; main_wt=$(af_main_worktree)
  while read -r p; do
    [ -n "$p" ] || continue
    [ "$p" = "$WT" ] && continue
    # The repo's own canonical checkout is not a leftover, it is where this driver lives. Skipping it
    # loses no coverage: the branch loop above already reports any branch holding unmerged commits,
    # whichever tree has it checked out.
    [ -n "$main_wt" ] && [ "$p" = "$main_wt" ] && continue
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
# BUG D — a dependency stall whose ROOT is a `blocked` ticket or one parked on manual sign-off cannot
# be cleared by any amount of watching: it needs a human. After AF_HUMAN_STALL_MAX_POLLS such polls
# under AF_WATCH the loop STOPS watching and exits with this distinct code, so an operator (or an
# outer supervisor) can tell "stuck, needs me" from a clean drain (exit 0). A transient stall — root
# still in_progress/incomplete, i.e. normal progress — keeps watching quietly as before.
AF_EXIT_HUMAN_STALL=9
AF_HUMAN_STALL_MAX_POLLS="${AF_HUMAN_STALL_MAX_POLLS:-3}"

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
  af_surface_flags
  af_end_of_run_summary
  # LAST, and unconditional: this replaces the guard's early trap, so if it does not release the
  # per-worktree lock nothing does, and a dead run bricks the tree for the next one.
  af_release_worktree_lock
  return "$rc"
}

# BUG B-5 — the loop NEVER pushes, so a clean drain and a halt look identical to an operator unless
# they are told the run left work unshipped. On EVERY exit (drain, halt, outage, kill) emit one line
# stating how many commits the integration branch is AHEAD of origin (unpushed) and how many rounds
# this run went UNVERIFIED, so the operator knows they must verify the merged tree and push. Rides the
# EXIT trap for the same reason af_surface_flags does: it must fire on the halted and killed exits too,
# not just the happy path. Once-only (INT/TERM then EXIT both fire the handler).
AF_ROUNDS_UNVERIFIED=0
AF_SUMMARY_SAID=0
af_end_of_run_summary(){
  [ "$AF_SUMMARY_SAID" = "1" ] && return 0
  AF_SUMMARY_SAID=1
  local ahead="?" up=""
  if [ -n "${WT:-}" ] && [ -e "${WT}/.git" ]; then
    # Prefer the integration branch's own upstream; fall back to origin/<branch>, then origin/main.
    up=$(git -C "$WT" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || echo "")
    if [ -z "$up" ]; then
      local br; br=$(git -C "$WT" symbolic-ref --quiet --short HEAD 2>/dev/null || echo "")
      if [ -n "$br" ] && git -C "$WT" rev-parse --verify -q "origin/$br" >/dev/null 2>&1; then up="origin/$br"
      elif git -C "$WT" rev-parse --verify -q origin/main >/dev/null 2>&1; then up="origin/main"
      fi
    fi
    if [ -n "$up" ]; then
      ahead=$(git -C "$WT" rev-list --count "$up..HEAD" 2>/dev/null || echo "?")
    else
      # No origin/upstream to compare against: report the whole reachable history as unpushed.
      ahead=$(git -C "$WT" rev-list --count HEAD 2>/dev/null || echo "?")
    fi
  fi
  say "END-OF-RUN — this loop NEVER pushes. $WT is $ahead commit(s) AHEAD of origin (unpushed)${up:+ vs $up}, and ${AF_ROUNDS_UNVERIFIED} round(s) this run went UNVERIFIED. You must verify the merged tree and push before this work is real."
}

# R24 — flags are PUSH, not pull: a suspension, a parking, an undraftable check, a check-defeat must
# not wait for an operator to go looking for it. Three things were wrong with how this ran:
#
#   1. It died on import. `python -m agent_factory.af_retro` under this script's own PYTHONPATH hit
#      `ModuleNotFoundError: No module named 'hooks'`, because PYTHONPATH carried the hook MODULES
#      and not the directory containing them. `agent_factory._hooks` fixes that from the module side
#      and the PYTHONPATH export above now carries $AF_PLUGIN_DIR as well.
#   2. `|| true` swallowed the failure whole. That is why #1 could survive a full build: the one
#      call into the subsystem failed every time and printed nothing. A read failure still must not
#      fail a completed run, so the exit status is still discarded — but the OUTPUT is captured and
#      a failure is now said out loud, which is the difference between "cannot" and "silent".
#   3. It ran on ONE line at the very end of the script, AFTER `af_assert_no_stragglers "exit"` —
#      which exits 7 from inside itself when the invariant is violated. So the guarantee that flags
#      always surface held only on the happy path, and was absent on precisely the failed, halted
#      and killed exits where an operator most needs to see them. Moving it onto the EXIT trap makes
#      it unconditional: drain, circuit breaker, Praxis outage, straggler exit 7, `tmux kill-session`
#      all route through af_cleanup_on_exit.
#
# `timeout`-bounded because this runs inside a signal handler and reads Praxis, which may be exactly
# the thing that is down; and once-only, because INT/TERM fire the handler and then EXIT fires it
# again.
AF_FLAGS_SURFACED=0
af_surface_flags(){
  # `if`, not `[ ... ] && return 0`: this script runs under `set -e`, where a bare test that
  # evaluates FALSE is a failing simple command and takes the whole script with it.
  if [ "$AF_FLAGS_SURFACED" = "1" ]; then return 0; fi
  AF_FLAGS_SURFACED=1
  local runner="" out rc
  if command -v timeout >/dev/null 2>&1; then runner="timeout ${AF_FLAGS_TIMEOUT_S:-120}"; fi
  out=$($runner "$PY" -m agent_factory.af_retro --flags "$PROJECT" 2>&1); rc=$?
  if [ "$rc" != "0" ]; then
    say "WARNING: pending-flag surfacing FAILED with status $rc — any suspension, parking, undraftable check or check-defeat from this run is UNREPORTED. Run: PYTHONPATH=$PYTHONPATH $PY -m agent_factory.af_retro --flags $PROJECT"
    printf '%s\n' "$out" | while IFS= read -r l; do [ -n "$l" ] && say "  flags: $l"; done
    return 0
  fi
  printf '%s\n' "$out" | while IFS= read -r l; do [ -n "$l" ] && say "$l"; done
  return 0
}
trap af_cleanup_on_exit EXIT INT TERM

# ------------------------------------------------------------------------------- loop-end hooks --
#
# Two maintenance sweeps that were specified to run off the af-build loop-end hook and had NO caller
# at all, so neither had ever executed in production:
#
#   ingestion_api.reprove_quiet_checks    KD7 — a GATING check that has gone quiet past the re-prove
#                                         cadence re-runs against its retained bad artifact. Still
#                                         failing keeps it gating; an artifact that no longer
#                                         reproduces demotes it to REPORT_ONLY with a reason. Without
#                                         this, a check proven once gates forever on the strength of
#                                         a proof nobody ever revisited.
#   failure_taxonomy.sweep_near_duplicate_classes
#                                         R20/FL15 — merges near-duplicate failure classes, crediting
#                                         the survivor with the loser's recurrence count. Recurrence
#                                         count is what drives widening and universal promotion, so a
#                                         corpus that keeps splitting one failure across near-dup
#                                         classes never reaches either threshold.
#
# OFF THE CRITICAL PATH, and that is a hard requirement, not a preference: reprove_quiet_checks runs
# real proof commands in disposable worktrees and can take minutes. So this backgrounds the whole
# thing, bounds it with `timeout` where the box has one, sends everything to the log, and is never
# waited on and never gates anything. The worst case for a sweep that hangs or dies is a log line.
#
# Called at every ROUND boundary rather than once at the very end. Both sweeps carry their own
# throttling — reprove_quiet_checks skips anything inside its cadence window, and the near-dup sweep
# is idempotent once classes are merged — so per-round costs nothing and means the sweeps still run
# on a run that is killed, halted by the circuit breaker, or exits before it ever drains.
AF_LOOPEND_TIMEOUT_S="${AF_LOOPEND_TIMEOUT_S:-600}"
af_loop_end_hooks(){
  local why="$1" runner=""
  # `if`, not `&&`/`[ ]`: under `set -e` a test that evaluates false is a failing command, and this
  # is called from the middle of the round loop where that would abort the whole run.
  if [ "${AF_LOOP_END_HOOKS:-1}" != "1" ]; then return 0; fi
  if command -v timeout >/dev/null 2>&1; then runner="timeout ${AF_LOOPEND_TIMEOUT_S}"; fi
  {
    $runner "$PY" - "$PROJECT" "$WT" "$why" <<'PYEOF'
import sys
from agent_factory import failure_taxonomy, ingestion_api
proj, wt, why = sys.argv[1], sys.argv[2], sys.argv[3]
# Each sweep is independently guarded: neither is allowed to hide the other's result, and neither is
# allowed to matter enough to be worth propagating out of a detached background job.
try:
    outcomes = ingestion_api.reprove_quiet_checks(proj, healthy_repo_path=wt)
    kept = sum(1 for o in outcomes if o.get("result") == "kept-gating")
    demoted = [o for o in outcomes if o.get("result") == "demoted"]
    print(f"[loop-end {why}] re-prove: {len(outcomes)} quiet check(s) examined, "
          f"{kept} kept gating, {len(demoted)} demoted to report-only")
    for o in demoted:
        print(f"[loop-end {why}] re-prove DEMOTED {o.get('check_id')}: {o.get('reason')}")
except Exception as exc:  # noqa: BLE001 - a maintenance sweep never fails a build
    print(f"[loop-end {why}] re-prove sweep failed: {exc!r}")
try:
    merges = failure_taxonomy.sweep_near_duplicate_classes()
    print(f"[loop-end {why}] near-dup sweep: {len(merges)} class merge(s)")
    for m in merges:
        print(f"[loop-end {why}] merged class {m['loser_id']} into {m['survivor_id']} "
              f"at similarity {m['score']:.2f}; survivor recurrence now {m['credited_recurrence']}")
except Exception as exc:  # noqa: BLE001 - same
    print(f"[loop-end {why}] near-dup sweep failed: {exc!r}")
PYEOF
  } >>"$LOG" 2>&1 &
  say "loop-end hooks dispatched off-critical-path at $why — re-prove cadence + near-dup class sweep; results land in $LOG"
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
VERDICT="$AF_STATE_DIR/af-round-verdict-$PROJECT.json"
# Conflict-resolution handoff: integrate_round appends "<branch>\t<ticket ids>" here, and
# resolve_conflicts drains it. Per-project, so concurrent loops never read each other's.
CONFLICTS="$AF_STATE_DIR/af-round-conflicts-$PROJECT.tsv"
RESOLVED="$AF_STATE_DIR/af-round-resolved-$PROJECT.json"
# R17 handoff: the OPEN findings this round's tickets still owe an answer to, written by
# verify_round for the verifier to re-check. A FILE and not a prompt substitution on purpose — a
# finding's recorded symptom is arbitrary agent prose and the prompt is sent inside a double-quoted
# shell string, where a stray backtick or $ in that prose would be expanded by bash before tmux ever
# saw it.
FINDINGS="$AF_STATE_DIR/af-round-findings-$PROJECT.json"
# BUG E — bound the zero-commit finding-regress streak. finding_guard persists a per-(ticket,finding)
# regress count in FINDING_STREAK; once a ticket has been regressed AF_FINDING_REGRESS_MAX times for
# the SAME still-open finding without a single answering commit, it STOPS regressing and appends a
# LOUD escalation to FINDING_ESCALATION instead. This breaks the infinite regress observed live: a
# finding whose defect an earlier round's commit already fixed (so there is no new commit to "answer"
# it) and whose check_id is None (so the check_id-keyed auto-suspend can never fire) was re-dispatched
# every ~9 minutes forever. Persistent across process restarts on purpose — the regress loop outlives
# any single loop process; keys are per (ticket, finding-reason) so a genuinely new finding starts fresh.
FINDING_STREAK="$AF_STATE_DIR/af-finding-regress-streak-$PROJECT.json"
FINDING_ESCALATION="$AF_STATE_DIR/af-finding-regress-escalation-$PROJECT.tsv"
AF_FINDING_REGRESS_MAX="${AF_FINDING_REGRESS_MAX:-2}"
# The regression pass writes one line here for every ticket whose "it produced no commit" regression
# was SUPPRESSED because the ticket is parked on a human sign-off (see the MANUAL SIGN-OFF block in
# the regress heredoc). A file, not stderr, because this is the one thing in that pass a human must
# actually see: `say`ing it puts the pending sign-off on the console, not just in the log tail.
PARKED_REPORT="$AF_STATE_DIR/af-round-parked-$PROJECT.txt"

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
  rm -f "$VERDICT" "$PARKED_REPORT"

  # R17 — hand the verifier the findings it must RE-EVALUATE, not just the ticket ids. Without this
  # the resolution pass below has nothing but the check's exit code to go on, and "the check passed"
  # is exactly the evidence R17 refuses to accept as proof the symptom is gone.
  $PY - "$PROJECT" "$@" > "$FINDINGS" 2>>"$LOG" <<'PYEOF' || printf '[]' > "$FINDINGS"
import json, sys
import _praxis, _ticket_state as ts
proj = sys.argv[1]
want = set(sys.argv[2:])
kw = dict(space=proj, snapshot=f"prd-{proj}")
out = []
for f in _praxis.facts_by(category="requirement", **kw) or []:
    m = f.get("meta") or {}
    rid = str(m.get("requirement_id") or f.get("id"))
    if rid not in want:
        continue
    for d in ts.open_findings(m):
        out.append({"id": rid, "check_id": str(d.get("check_id") or ""),
                    "symptom": str(d.get("reason") or "")[:600],
                    "evidence": str(d.get("evidence") or "")[:600]})
print(json.dumps(out, indent=2))
PYEOF
  [ -s "$FINDINGS" ] || printf '[]' > "$FINDINGS"

  local vprompt="Post-merge verification of build round $rnd for project $PROJECT. Tickets just merged: $ids_csv. Each was built and validated ALONE in its own worktree, so the merged tree you are looking at has never been verified as a whole. Do NOT build features, do NOT claim tickets, do NOT start new work, do NOT push. Verify the integrated result only. Step 1: run the repo's whole-repo gates on the current merged tree — full test suite, build, repo-wide typecheck and lint. THIS IS THE ONLY PLACE THOSE GATES RUN: the workers were told to skip them so that N of them would not run N concurrent suites, so you are the authoritative repo-wide gate for this round and nothing else has proven the merged tree compiles or passes. If any gate is red, identify which ticket's change caused it and regress that ticket per step 3; if you genuinely cannot attribute it, regress the whole batch rather than passing a red tree. Step 2: dispatch INDEPENDENT parallel review subagents over the combined diff of this round, one per lens, each told to actively look for a failure rather than confirm success. If this round merged only ONE ticket, the cross-ticket lens is trivially satisfied and you may skip lens A, but step 1's gates and lenses B and C still run in full — they are the only repo-wide check this ticket gets. Lens A integration conflict: did two of these tickets edit the same module, config, migration, schema, or shared type in ways that are individually fine and jointly wrong, or did one silently revert another. Lens B acceptance survival: for EACH ticket id above, re-run its own acceptance test against the MERGED tree and confirm it still passes here, not just in its worktree. Lens C test integrity: did any ticket reach green by deleting, skipping, xfailing, narrowing assertions on, or excluding from config a test that used to run — treat that as a failure, not a pass. Step 3: NAME every ticket whose work does NOT survive integration. A ticket whose meta.verify reads manual is the one class where absence of a commit is NOT evidence of anything: its acceptance is a human sign-off over something rendered or observed, so it produces no commit BY DESIGN and its work being missing from src, tests and docs is the expected shape, not a defect. Judge such a ticket only on code it actually landed, and never name it for having produced no merged commit — that regression is suppressed by the loop and reported as parked awaiting sign-off, so naming it wastes the round. Do NOT write that regression to Praxis yourself and do not try to fix the ticket — the loop that dispatched you performs the regression from your verdict, using a write path it already owns. Your job is the judgement, not the write. This split is deliberate: when verifiers were asked to do their own Praxis write, nine consecutive rounds reported zero regressions while their own notes named the failing tickets, so every one of those tickets stayed marked finished on work that had failed integration. Step 3b: read the file $FINDINGS. It is a JSON array of the OPEN findings these tickets still owe an answer to, each with id, check_id, symptom and evidence. If the array is empty, skip this step entirely. Otherwise, for EACH entry do TWO INDEPENDENT things and never let either one decide the other. First, run the check named by check_id against the merged tree and record whether it passes; if check_id is empty there is no check to run and check_passed is false. Second, and SEPARATELY, re-evaluate the recorded symptom itself against the merged tree — read the code, run the specific reproduction the evidence describes — and record whether that symptom is STILL PRESENT. A check that exits zero is NOT evidence the symptom is gone: a check can be defeated by a change that satisfies the command while leaving the defect exactly where it was, and the case where your two answers DISAGREE is the single most valuable thing you can report here, so report both honestly rather than making them agree. Step 4: write your verdict as JSON to $VERDICT with exactly these keys: verdict which is pass or fail, gates_green true or false, notes which is one short string, regressed which is an array of OBJECTS — one per ticket that must be regressed, each with four string fields: id the ticket id, reason what actually failed stated concretely, evidence the exact failing test name, gate, file and error text or the precise merge symptom, and fix what the rebuild must do differently — plus an OPTIONAL fifth string field check, a single command that FAILS on this broken merged tree and would PASS once the fix lands. Supply check only when you actually ran that command and watched it fail; a guess is worse than omitting it. It must be one plain command with no shell operators, no pipes, no redirection and no absolute paths, starting with one of pytest, python, python3, npm, npx, make, ruff, mypy, eslint, playwright or grep — anything else is rejected and the check is dropped, though your regression still lands. And findings_recheck which is an array of objects carrying step 3b's answers, each with id the ticket id, check_id copied from $FINDINGS, check_passed true or false, and symptom_present true or false; write an empty array if step 3b was skipped. An empty array asserts every ticket survived integration. Write these for the NEXT WORKER, not for a log: it will claim the ticket cold with no memory of this round, so a bare id or a vague \"tests failed\" wastes an entire rebuild while it re-derives what you already know. Name the failing test, quote the error, and say what the fix has to address. Good: {\"id\":\"REM-10\",\"reason\":\"its new default-prefix-attribution controller is unregistered in RESTRICTED_RECORD_MANIFEST and the permission_pages seed\",\"evidence\":\"chat14-restricted-record-manifest.test.ts and chat16-chart-access-record.test.ts both fail on the merged tree with 'scope not registered'\",\"fix\":\"register the new controller/scope in RESTRICTED_RECORD_MANIFEST and add its permission_pages seed row, then re-run both suites against the merged tree\"}. Bad: {\"id\":\"REM-10\",\"reason\":\"failed\",\"evidence\":\"\",\"fix\":\"fix it\"}. Write that file LAST, after everything else is done, and then STOP. You are running HEADLESS with no human attached: never ask a clarifying question or present a numbered choice, because nothing can answer it and the session will sit until it is reaped. Decide, or record the blocker and stop. If you cannot verify at all, that is itself a verdict: write the JSON with verdict fail and notes saying why, rather than asking what to do."
  af_launch_agent "$vsession" "$vprompt"
  if ! af_wait_ready "$vsession"; then
    say "WARNING: verify REPL not confirmed ready, sending anyway"
  fi
  if [ "$BACKEND" != grok ]; then
    tmux send-keys -t "$vsession" "$vprompt"
    sleep 3; tmux send-keys -t "$vsession" Enter
  fi
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
    if echo "$pane" | grep -qiE "insufficient balance|quota exceeded|payment required|credit balance is too low|insufficient_quota|billing_(error|hard_limit)|billing (error|failure)|(http[ /]?|status[ :]?)402|402 payment required"; then
      say "BILLING FAILURE during verification — halting"; tmux kill-session -t "$vsession" 2>/dev/null || true; exit 3
    fi
    # Quota/session-limit on a subscription backend: caught BEFORE the stall accounting below so we
    # react the instant the interactive menu appears instead of wasting the full 15-min verify stall
    # window on a prompt no headless session can answer (see rate_limited/halt_quota_blocked).
    if echo "$pane" | rate_limited; then halt_quota_blocked "verify round #$rnd" "$vsession"; fi
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
    AF_ROUNDS_UNVERIFIED=$((AF_ROUNDS_UNVERIFIED + 1))   # BUG B-5 — feeds the end-of-run summary
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
import json, re, sys
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

# THE UNDER-REPORT CASE: the verdict's authoritative `regressed` field is EMPTY (so the loop will
# regress nothing and the round reads green) while the verifier's OWN notes assert a ticket should be
# regressed. Observed live: a verdict with regressed=[] whose notes read
# "Should-regress REM-29,REM-28,REM-27" -- the judgement named the failures and the field that
# carries them to the write path silently dropped them, so a round passed on work that had failed
# integration. This is the same failure class the verifier/loop split was built to stop (nine rounds
# reported zero regressions while their notes named the failing tickets), one level up: a finding is
# not answered by a NOTE about it any more than by a zero-commit close. Fires ONLY when `regressed`
# is empty -- a non-empty field already carries its tickets to the loop's regression pass -- so the
# consequence is exactly the rest of this gate's: the round is downgraded to UNVERIFIED (never a
# pass), tickets keep whatever state they earned, and nothing is auto-regressed on prose alone.
_UNDERREPORT_RE = re.compile(
    r"should[\s-]*(?:be\s+)?regress|must\s+be\s+regress|need(?:s|ed)?\s+(?:to\s+be\s+)?regress"
    r"|regress(?:ed|es|ing)?\s+[A-Za-z][A-Za-z0-9]*-\d+", re.I)
if not reg and _UNDERREPORT_RE.search(str(d.get("notes") or "")):
    incoherent.append("regressed is empty but the notes assert a ticket should be regressed "
                      "(%r) -- a finding is not answered by a note about it" % (str(d.get("notes"))[:160],))

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
      AF_ROUNDS_UNVERIFIED=$((AF_ROUNDS_UNVERIFIED + 1))   # BUG B-5 — feeds the end-of-run summary
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
  #
  # STDERR GOES TO THE LOG, not /dev/null. Only stdout is the count, so the per-ticket "regressed
  # <id> :: <reason>" lines and any ingestion failure below were being written to a stream this
  # command threw away — which is how "the merger does not ingest" could have run for a full build
  # without leaving a trace. Redirecting to $LOG keeps stdout clean AND keeps the narration.
  local regressed_n
  regressed_n=$($PY - "$PROJECT" "$rnd" "$VERDICT" "$WT" "$PARKED_REPORT" <<'PYEOF' 2>>"$LOG" || echo 0
import json, subprocess, sys
import _praxis, _ticket_state as ts
from agent_factory import failure_taxonomy, ingestion_api, widening

proj, rnd, path = sys.argv[1], sys.argv[2], sys.argv[3]
wt = sys.argv[4] if len(sys.argv) > 4 else "."
parked_path = sys.argv[5] if len(sys.argv) > 5 else ""
head_sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                          cwd=wt).stdout.strip() or None
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

def _named_by_a_commit(rid):
    """True iff SOME commit in the merged history names this ticket by the trailing-(ID) convention
    integration merges on (see af_owned_ids). Whole history, not this round's base, on purpose: the
    question here is "does any code attributable to this ticket exist at all", and answering it over
    the whole history is the conservative direction — more matches means the regression proceeds.

    An UNPROVABLE answer (no git, not a repo, timeout) returns True for the same reason: absence of
    work may only ever be inferred from positive evidence, never from a failed probe."""
    try:
        r = subprocess.run(["git", "log", "--format=%s", "--fixed-strings", f"--grep=({rid})"],
                           capture_output=True, text=True, timeout=30, cwd=wt)
    except Exception:
        return True
    return r.returncode != 0 or bool((r.stdout or "").strip())

n = 0
parked = []     # tickets whose "no commit" regression was suppressed — reported, never silent
ingested = []   # one entry per ticket actually ingested+regressed; feeds the widening pass below
for rid, detail in entries:
    f = by_rid.get(rid)
    if not f:
        sys.stderr.write(f"regress: no ticket {rid} in prd-{proj}\n"); continue

    # ------------------------------------------------ MANUAL SIGN-OFF IS NOT A MISSING COMMIT ----
    # A verify="manual" ticket produces no commit BY DESIGN — its completion is a human sign-off,
    # not code — so "it never produced a merged commit" is a property of the ticket class, not a
    # defect. Regressing it for that re-dispatches a ticket no worker can ever advance: dispatched ->
    # cannot produce commits -> regressed -> re-dispatched, forever. Observed on mvpvu-foundation
    # round #1: `verdict=fail gates_green=True regressed=1` naming R21 — meta.verify="manual", its
    # acceptance a human re-labelling pass over rendered images — with "R21's worktree branch never
    # produced a merged commit in this round ... whatever R21 was supposed to build is not present
    # anywhere in src/, tests/, docs/". All three lenses "confirmed" it independently, because all
    # three were applying a commits-must-exist invariant that does not hold for this ticket class.
    #
    # NARROW ON PURPOSE — this is not "manual tickets are exempt from verification". BOTH must hold:
    #   * the ticket is PARKED on a manual sign-off (ts.parked_on_manual: every automated obligation
    #     is covered and green and only the human/external-sourced pass is missing), and
    #   * NO commit anywhere names it, so there is no diff of its to fault and any integration
    #     failure attributed to it is a misattribution by construction.
    # A manual ticket that DID land code is regressed on its merits like any other; a ticket with
    # unmet AUTOMATED obligations is not parked, so its zero-commit regression still lands.
    #
    # Suppressing the regression is NOT passing the ticket, which is the opposite error: completion
    # still runs through all_validations_passed, whose manual clause no worker-sourced pass can ever
    # satisfy. The ticket stays exactly where it was — parked — and says so through $PARKED_REPORT.
    # It clears the moment a human records the sign-off (the same exit the frontier already uses).
    if not _named_by_a_commit(rid):
        try:
            is_parked = ts.parked_on_manual(f, (proj, f"prd-{proj}"))
        except Exception as exc:   # unanswerable "parked?" -> regress as reported, never swallow it
            is_parked = False
            sys.stderr.write(f"parked-on-manual check failed for {rid} ({exc!r}) — "
                             f"regressing as the verifier reported\n")
        if is_parked:
            note = (f"{rid}: PARKED awaiting manual sign-off — NOT regressed. Round #{rnd}'s "
                    f"verification faulted it for producing no merged commit, but a verify=manual "
                    f"ticket produces none by design: every automated obligation of {rid} is "
                    f"covered and green and only the human sign-off is outstanding. It cannot "
                    f"self-certify and is not finished — record the manual pass to release it.")
            parked.append(note)
            sys.stderr.write(note + "\n")
            continue

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

    # R5 — "regression without ingestion is not a legal state". THE HEADLINE DEFECT this replaces:
    # the merger regressed straight through `_praxis.regress_requirements`, so a ticket that failed
    # integration was re-queued and the failure was learned NOWHERE. `regress_with_ingestion` is the
    # merger's single legal entry point: it classifies the failure, lands the lesson, drafts and
    # (given a run) proves a check, binds it NARROWLY to exactly these ticket ids, and regresses —
    # one motion, no way to do half of it.
    #
    # NOT wrapped in a bare try/except: the function documents that it catches nothing, so a Praxis
    # outage propagates, the heredoc dies, and the `|| echo 0` above reports zero regressions — the
    # identical failure shape the bare regress had. Only RunBodyRejected is caught, and only to fall
    # back to a lesson-only ingestion: a verifier that suggested a malformed check command must cost
    # us the CHECK, never the regression.
    lesson = " ".join(p for p in (
        f"post-merge verification of round #{rnd} found ticket {rid}'s work did not survive "
        f"integration into the merged tree.",
        f"WHAT FAILED: {reason}" if reason else "",
        f"EVIDENCE: {evidence}" if evidence else "",
    ) if p)
    drafted_run = str(detail.get("check") or detail.get("run") or "").strip() or None
    ingest_kw = dict(source=f"af-ticket-loop/post-merge-verification/{proj}",
                     channel="machine", commit_sha=head_sha)
    try:
        result = ingestion_api.regress_with_ingestion(proj, [f["id"]], lesson,
                                                      drafted_run=drafted_run, **ingest_kw)
    except ingestion_api.RunBodyRejected as exc:
        sys.stderr.write(f"ingestion: verifier's check body for {rid} rejected ({exc}) — "
                         f"ingesting the lesson alone; the regression still lands\n")
        result = ingestion_api.regress_with_ingestion(proj, [f["id"]], lesson, **ingest_kw)
    ingested.append({"rid": rid, "ticket_cid": f["id"], "lesson": lesson,
                     "evidence": evidence, "check_id": result.get("check_id")})

    # R16/E3: accumulate onto this ticket's existing regression_detail — a concurrent finding
    # (conflict resolution, ingestion) must never be clobbered by this one. Re-READ first: the
    # ingestion above just wrote its own entry, and `f` is the pre-ingestion copy.
    current = _praxis.get_fact(f["id"], **kw) or f
    accumulated = ts.accumulate_regression_detail((current.get("meta") or {}).get("regression_detail"), new_finding)
    _praxis.regress_requirements(proj, [f["id"]], {f["id"]: {
        "claim_owner": None, "claim_at": None,
        "claim_heartbeat_at": None, "claim_lease_ttl": None,
        "audit_disposition": summary,
        "regression_detail": accumulated,
    }}, **kw)
    n += 1
    sys.stderr.write(f"regressed {rid} WITH INGESTION (lesson={result.get('lesson_id')} "
                     f"check={result.get('check_id')}) :: {(reason or 'no reason given')[:160]}\n")

# ---------------------------------------------------------------- FL14/R14: recurrence -> widening
#
# Runs AFTER every regression above has landed, and inside its own try/except, ON PURPOSE: widening
# is an optimisation and a regression is the invariant. Nothing in this block may be allowed to lose
# a regression that already succeeded — which is also why `n` is printed from the finally clause.
#
# THIS is where a recurrence is detected. `failure_taxonomy.assign_class` is the R3 dedup entry
# point: a lesson matching an existing class attaches its evidence and INCREMENTS that class's
# recurrence count instead of minting a duplicate. A count above 1 means this exact failure class has
# now been seen in a scope it was not bound to, which is R14's widening trigger — and the widen only
# actually happens if a FRESH class-specific proof FAILS on the new scope's pinned bad artifact and
# PASSES on the healthy sibling resolved through $BOX_WORKTREE_REGISTRY (exported by this script).
# Generic breakage fails or passes BOTH sides and never widens.
try:
    for item in ingested:
        assignment = failure_taxonomy.assign_class(
            item["lesson"], evidence=item["evidence"] or item["lesson"],
            source=f"af-ticket-loop/{proj}",
            meta={"project": proj, "ticket_id": item["rid"], "check_id": item["check_id"]},
        )
        recurrences = int(assignment.get("recurrence_count") or 1)
        sys.stderr.write(f"taxonomy: {item['rid']} -> class {assignment['class_id']} "
                         f"({assignment['action']}, recurrence #{recurrences})\n")
        if recurrences < 2:
            continue
        class_id = assignment["class_id"]
        # The check to widen is the one already bound to this failure class. A class recurring with
        # no check behind it has nothing to widen — the ingestion above will have drafted one only
        # when a run was supplied.
        for check in ingestion_api.read_checks(proj):
            cmeta = check.get("meta") or {}
            if str(cmeta.get("failure_class_id") or "") != str(class_id):
                continue
            run, artifact_id = cmeta.get("run"), cmeta.get("artifact_id")
            if not run or not artifact_id:
                sys.stderr.write(f"widen: check {check.get('id')} has no run/pinned artifact — "
                                 f"nothing to prove a widen with\n")
                continue
            verdict = widening.attempt_widen(
                check["id"], proj, item["rid"], class_id=class_id,
                bad_artifact_meta=(ingestion_api.read_artifact(artifact_id).get("meta") or {}),
                run=run,
            )
            sys.stderr.write(f"widen: check {check['id']} scope {item['rid']} -> "
                             f"{verdict.get('status')} ({verdict.get('reason') or ''})\n")
            if verdict.get("status") != "widened":
                continue
            # R14/D8 — the same class proven across >= MIN_DISTINCT_PROJECTS_FOR_PROMOTION distinct
            # projects is promoted org-wide. The distinct set is read off the class's own evidence
            # log, whose `source` every driver stamps as "af-ticket-loop/<project>" (above), so this
            # counts projects that really recurred rather than a single project's say-so.
            projects = set()
            for cls in ingestion_api.read_classes():
                if str(cls.get("id")) != str(class_id):
                    continue
                for ev in (cls.get("meta") or {}).get("evidence") or []:
                    src = str(ev.get("source") or "")
                    if src.startswith("af-ticket-loop/"):
                        projects.add(src.split("/", 1)[1])
            try:
                promotion = ingestion_api.promote_universal(
                    check.get("text") or item["lesson"], run,
                    recurring_projects=sorted(projects),
                    source=f"af-ticket-loop/{proj}",
                )
                sys.stderr.write(f"promote-universal: {promotion.get('status')} "
                                 f"{promotion.get('check_id') or promotion.get('reason')} "
                                 f"across {sorted(projects)}\n")
            except ingestion_api.UniversalPromotionCollision as exc:
                # Documented as a LOUD collision report, never a silent duplicate write. Loud here
                # means the log — it is not a reason to fail a round whose work already landed.
                sys.stderr.write(f"promote-universal COLLISION: {exc}\n")
except Exception as exc:  # noqa: BLE001 - see the block comment: never lose a landed regression
    sys.stderr.write(f"widening/promotion pass failed after {n} regression(s) landed: {exc!r}\n")

# Hand the suppressed-regression report to the driver so it can `say` it. Failing to write it is
# narrated and never fatal: a lost report costs visibility, dying here would cost the regressions.
if parked and parked_path:
    try:
        with open(parked_path, "w") as fh:
            fh.write("\n".join(parked) + "\n")
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"could not write the parked report ({exc!r}); it is in this log above\n")

print(n)
PYEOF
)
  case "$regressed_n" in ''|*[!0-9]*) regressed_n=0 ;; esac
  if [ "$regressed_n" -gt 0 ]; then
    say "round #$rnd: $regressed_n ticket(s) regressed with a failure report attached — the next round rebuilds them"
  fi
  # A suppressed manual-sign-off regression is REPORTED, not silent: the ticket is not finished and
  # not rebuildable, and the only thing that can move it is the human this line is addressed to.
  if [ -s "$PARKED_REPORT" ]; then
    while IFS= read -r pline; do
      [ -n "$pline" ] || continue
      say "round #$rnd: $pline"
    done < "$PARKED_REPORT"
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
  #
  # BUT NOT BY `ts.resolve_finding(m)`, which is what this used to call. That stamps EVERY open
  # finding on the ticket with no scoping and no re-evaluation, so a rerun that passed check A
  # silently answered check B's finding too, and "the check exited zero" was accepted as proof the
  # SYMPTOM was gone. R17 forbids both, and `agent_factory.resolution.resolve_or_defeat` is the
  # replacement: it is called once per (finding, check) pair, stamps only the findings naming THAT
  # check, and takes the symptom re-evaluation as a SEPARATE input from the check's exit code. When
  # the two disagree — check green, symptom still there — that is a CHECK-DEFEAT: the rebuilt
  # state's artifact is pinned, the defeat is classified into the failure taxonomy, and the defeated
  # check is demoted GATING -> REPORT_ONLY and flagged, instead of being trusted forever.
  #
  # Where the two inputs come from: the verifier's own `findings_recheck` array (step 3b of its
  # prompt), which reports check_passed and symptom_present INDEPENDENTLY per finding. A ticket the
  # verifier did not report on falls back to the pre-existing behaviour — it still reads finished
  # after the regress pass, so this round examined the merged tree and did not fault it — because
  # the alternative, refusing to resolve without an explicit recheck, resurrects the 17-round
  # finding ping-pong that stamping was added to break.
  local cleared_n
  cleared_n=$($PY - "$PROJECT" "$rnd" "$VERDICT" "$WT" "$@" <<'PYEOF' 2>>"$LOG" || echo 0
import json, subprocess, sys
import _praxis, _ticket_state as ts
from agent_factory import ingestion_api, resolution
proj, rnd, vpath, wt = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
want = set(sys.argv[5:])
kw = dict(space=proj, snapshot=f"prd-{proj}")
head_sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                          cwd=wt).stdout.strip() or "HEAD"

# {ticket_id: {check_id: (check_passed, symptom_present)}} from the verifier's step-3b report.
recheck = {}
try:
    for item in (json.load(open(vpath)).get("findings_recheck") or []):
        if isinstance(item, dict) and item.get("id"):
            recheck.setdefault(str(item["id"]), {})[str(item.get("check_id") or "")] = (
                bool(item.get("check_passed")), bool(item.get("symptom_present")))
except Exception as e:
    sys.stderr.write(f"findings_recheck unreadable ({e}) — falling back to survival-implies-resolved\n")

n = 0
for f in _praxis.facts_by(category="requirement", **kw) or []:
    m = f.get("meta") or {}
    rid = str(m.get("requirement_id") or f.get("id"))
    if rid not in want or m.get(ts.M_BUILD_STATE) != "finished":
        continue
    open_now = ts.open_findings(m)
    if not open_now:
        continue
    # R17's scoping unit is the CHECK, so the ticket's open findings are handled one check at a
    # time. Findings this loop authored itself (conflict resolution, post-merge verification) carry
    # no check_id and group under "".
    resolved_any = False
    for check_id in sorted({str(d.get("check_id") or "") for d in open_now}):
        reported = recheck.get(rid, {}).get(check_id)
        if reported is None:
            check_passed, symptom_present = True, False
            basis = "the ticket survived the merged tree and this round did not fault it"
        else:
            check_passed, symptom_present = reported
            basis = (f"the verifier re-ran check {check_id or '<none>'} (passed={check_passed}) and "
                     f"separately re-evaluated the recorded symptom (present={symptom_present})")
        if symptom_present and not check_id:
            # A persisting symptom with no check behind it cannot be a check-defeat — there is no
            # check to demote. Report it as still-failing so the finding simply stays OPEN.
            check_passed = False
        outcome = resolution.resolve_or_defeat(
            m, check_id, check_passed=check_passed, symptom_present=symptom_present,
            project=proj, ticket_id=rid, commit_sha=head_sha, repo_path=wt,
            resolved_by=f"post-merge verification of round #{rnd}: {basis}",
        )
        # Feed the returned list back so the NEXT check's pass sees this one's stamps (R17/E3 —
        # resolving one check's findings must never erase a sibling's).
        m[ts.M_REGRESSION_DETAIL] = outcome["regression_detail"]
        resolved_any = resolved_any or outcome["status"] == "resolved"
        sys.stderr.write(f"finding {rid} check={check_id or '<none>'} -> {outcome['status']} "
                         f"({outcome.get('reason') or basis})\n")

        # R6/R10/R20a — THE FIRST-REAL-CATCH UPGRADE, wired at the one place in the running system
        # where a first real catch is actually observable.
        #
        # Every /af-learn (DF4) human insert arrives GATING with proof_status="unproven", and every
        # machine draft arrives REPORT_ONLY; both are provisional pending exactly one event: a REAL,
        # non-drafting execution of the check that PASSES after it has caught something. Until then
        # an af-learn check gates the build while flagged unproven, forever. `upgrade_on_first_pass`
        # is what clears that, and it had ZERO production callers — so the flag never cleared and
        # R20a was dead code with a green unit test.
        #
        # This is the only point in the loop where a NAMED, ALREADY-EXISTING check is re-run for
        # real against a tree and its outcome reported back: step 3b of the verify prompt hands back
        # `check_passed` per finding, and the finding only exists because that check failed a build
        # earlier — i.e. it already caught. Passing here closes the catch→fix→pass cycle.
        #
        # Three guards, each load-bearing:
        #   `reported is not None`  — the fallback branch above ASSUMES check_passed=True from mere
        #                             survival. That is not an execution and must never prove
        #                             anything; only a verifier that actually ran the command counts.
        #   `check_id`              — a finding this loop authored itself carries no check.
        #   status == "resolved"    — on a check-defeat (command green, symptom still present)
        #                             resolve_or_defeat DEMOTES the check. Promoting the same check
        #                             to "proven" in the same breath would be a straight
        #                             contradiction, so the defeat wins.
        # Wrapped, and only after the stamping: an upgrade is bookkeeping, the resolution is the
        # invariant, and a Praxis hiccup here may not cost a finding that was already answered.
        if check_id and reported is not None and outcome["status"] == "resolved":
            try:
                upgraded = ingestion_api.upgrade_on_first_pass(check_id, proj, True)
                umeta = (upgraded or {}).get("meta") or {}
                sys.stderr.write(f"first-real-pass: check {check_id} -> "
                                 f"proof_status={umeta.get('proof_status')} "
                                 f"state={umeta.get('enforcement_state')} (ticket {rid})\n")
            except Exception as exc:  # noqa: BLE001 - never cost a landed resolution
                sys.stderr.write(f"first-real-pass upgrade of check {check_id} failed: {exc!r}\n")
    try:
        _praxis.write_build_state(f.get("cid") or f["id"],
                                  {ts.M_REGRESSION_DETAIL: ts.regression_details(m)}, **kw)
        n += 1 if resolved_any else 0
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
  # BUG E — any ticket that hit the regress cap is NOT re-dispatched; it is escalated ONCE per poll,
  # loudly, because the loop can no longer make progress on it and a human must intervene.
  if [ -s "$FINDING_ESCALATION" ]; then
    say "!! ESCALATION — the loop has STOPPED regressing ticket(s) it has already regressed ${AF_FINDING_REGRESS_MAX}x for the SAME open verification finding with ZERO answering commits, and the finding is STILL open. This is NOT a build failure to retry: the finding is stale, or was already resolved by an earlier commit that named a sibling ticket (its check_id is often None, so nothing else can break the loop). A HUMAN must inspect and dismiss the finding or fix it by hand:"
    while IFS=$'\t' read -r erid ecnt ecid ereason; do
      [ -n "$erid" ] || continue
      say "     $erid regressed ${ecnt}x, check_id=${ecid:-<none>} :: ${ereason}"
    done < "$FINDING_ESCALATION"
  fi

  rm -f "$VERDICT" "$FINDINGS"
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

# BLESS PREFLIGHT — before a single ticket is looked at, let alone claimed. Non-watch mode exits
# here; watch mode loops inside require_blessed_plan until the plan is blessed (or the stop file
# appears), so a loop launched a minute too early simply waits for intake to finish.
until require_blessed_plan; do :; done

n=0
round=0
while :; do
  # RE-CHECKED EVERY ROUND, not just at startup: a plan can be re-armed mid-run (an amendment, a
  # correction), and from that moment its tickets are moving again. A preflight-only check would
  # have passed at 03:00 and gone on dispatching against a plan opened for editing at 04:00.
  require_blessed_plan || continue
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
      af_watch_stopped && { say "watch stop file present ($WATCH_STOP_HIT) — exiting"; break; }
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
    roots=""; needs_human=0
    if roots=$(stall_roots 2>/dev/null) && [ -n "$roots" ]; then
      say "STALL ROOT(S) — act on these; everything else is downstream:"
      printf '%s\n' "$roots" | while IFS= read -r line; do [ -n "$line" ] && say "    $line"; done
      # BUG D — distinguish a TRANSIENT stall (root still in_progress/incomplete → normal progress,
      # keep watching quietly) from one whose ROOT is `blocked` or parked on manual sign-off, which no
      # amount of polling can clear: only a human can. stall_roots tags the latter with `[blocked]` or
      # `PARKED awaiting manual sign-off`. Live incident: a `T23 [blocked] blocks 4` stall re-logged
      # the identical line every 5 min for ~10 HOURS under AF_WATCH with no escalation.
      case "$roots" in
        *"[blocked]"*|*"PARKED awaiting manual sign-off"*) needs_human=1 ;;
      esac
    else
      say "  (could not resolve the stall root — walk depends_on by hand)"
    fi
    if [ "$needs_human" = "1" ]; then
      say "!! ESCALATION — the stall ROOT is a BLOCKED or manual-sign-off ticket; watching cannot clear it, a HUMAN must unblock or sign off the root above. This is NOT waiting on normal progress."
    fi
    if [ "${AF_WATCH:-0}" = "1" ]; then
      # BUG D — a human-needed stall is not watched forever. Count consecutive human-needed polls and,
      # once they reach AF_HUMAN_STALL_MAX_POLLS, exit LOUDLY with a distinct code instead of logging
      # the same line for hours. A transient root resets the counter — real progress is still coming.
      if [ "$needs_human" = "1" ]; then
        human_stall_polls=$((${human_stall_polls:-0} + 1))
        if [ "$human_stall_polls" -ge "$AF_HUMAN_STALL_MAX_POLLS" ]; then
          say "HALTING (exit $AF_EXIT_HUMAN_STALL) — the dependency stall has needed a human for $human_stall_polls consecutive poll(s) and nothing here can clear it. Unblock/sign off the root ticket above, then relaunch."
          exit "$AF_EXIT_HUMAN_STALL"
        fi
      else
        human_stall_polls=0
      fi
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
      af_watch_stopped && { say "watch stop file present ($WATCH_STOP_HIT) — exiting"; break; }
      sleep "${AF_WATCH_POLL_S:-300}"
      continue
    fi
    say "Then relaunch, or run with AF_WATCH=1 so the loop waits for the fix instead of exiting."
    break
  fi
  watch_stall_at=""
  human_stall_polls=0   # BUG D — the stall cleared; a later blocked-root stall starts its count fresh
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

  # The commit this round starts FROM. finding_guard asks "did this ticket answer its finding with
  # any commit?" via `git log $AF_ROUND_BASE..HEAD --grep=<id>`, and that variable was READ (line
  # ~919, `${AF_ROUND_BASE:-HEAD}`) but never ASSIGNED anywhere in this script — so the range was
  # always HEAD..HEAD, which is empty by construction. Every finished ticket carrying an open
  # finding therefore looked like it had produced nothing and was regressed for it, round after
  # round, until the AF_FINDING_REGRESS_MAX streak cap escalated. That is a second zero-commit
  # false-positive engine on top of BUG E's, and it fires even when the ticket's commits are sitting
  # right there in the round's own merge.
  #
  # Captured BEFORE dispatch so the range covers exactly this round's work. Falls back to HEAD (the
  # previous, always-empty behaviour) only if rev-parse fails, so a broken git can never make the
  # guard MORE aggressive than it was.
  AF_ROUND_BASE="$(git -C "$WT" rev-parse HEAD 2>/dev/null || echo HEAD)"
  export AF_ROUND_BASE

  say "round #$round: dispatching $size ticket(s) in parallel — $ids_csv"

  # The batch ids ARE the run scope, which is what makes one round per session self-limiting: af-build
  # stamps its run marker on exactly these tickets, so its completeness gate releases the session when
  # they are done and its fan-out loop finds an empty in-scope frontier rather than starting a wave the
  # next session is meant to own. Parentheses are avoided in this string on purpose — if the REPL is not
  # actually ready, the text lands on a bash prompt, and a stray paren there is a syntax error that
  # leaves the session dead at a shell prompt.
  round_prompt="/af-build $PROJECT $ids_csv — build EXACTLY these $size tickets and nothing else. They are dependency-independent, so build them ALL AT ONCE. Do NOT use the Workflow tool for this: its concurrency is derived from CPU count and on THIS machine resolves to $WORKFLOW_CAP concurrent agent(s), which would throttle a $size-wide round for no reason. Instead spawn ONE worktree-isolated subagent per ticket (Claude Agent, or Grok spawn_subagent with isolation=worktree), ALL of them in a SINGLE message so they actually run concurrently, each with isolation set to worktree so their edits never collide. Give each subagent the af-build per-ticket worker contract VERBATIM, scoped to its own single ticket id, so every worker stays eval-first and lease-safe. CRITICAL — REBASE FIRST, before reading a single file or writing a line of code. A worktree is created from the repo default branch, NOT from the branch this run integrates into, so every worker starts on the wrong base. The FIRST command each worker runs in its own worktree is: git merge --ff-only $INTEGRATION_REF — and if that is refused because the worktree has diverged, git rebase $INTEGRATION_REF instead. A worker that skips this authors its change against files the integration branch does not have, and its work will not apply back onto it even cherry-picked alone; that failure is silent, because the ticket still goes green in its own tree and only the round's merge discovers the work cannot land. If BOTH commands fail, do NOT build: record the blocker on the ticket and stop, because anything built on that base is unmergeable by construction. ONE deliberate amendment to that contract, and state it to every worker, precisely — this narrows WHICH tests run, it does not remove the ticket's gate. Each worker STILL runs, and still has to pass, every test related to the code it is changing: its red-to-green acceptance eval, every one of its pinned validations, the existing test files covering the modules it edited, the tests of any caller or dependent of what it changed, and typecheck plus lint SCOPED to the touched paths. A worker whose own related tests are red is NOT finished and must not release the ticket — that gate is unchanged and non-negotiable. What a worker skips is ONLY the repo-wide sweep: the full test suite across the whole repository, and repo-wide build, typecheck, or lint over paths it never touched. The repo-wide gates are run ONCE, on the MERGED tree, by this round's post-merge verification. The reason is measured: $size workers each running the full suite at end-of-ticket puts $size concurrent suites on a box with a handful of cores, and a suite that takes two minutes alone took twenty, with a worker burning 26 minutes and 259k tokens without producing a commit. Deferring the SWEEP is not removing a gate: each ticket is still gated on its own related tests, and the repo-wide sweep still runs before the round is done — once, on the tree that actually matters. All $size must be in flight together — a round that runs them a couple at a time is a bug, not a safe choice. CRITICAL — do NOT end your turn while any ticket in that list is still unfinished. Waiting on agent-completion notifications is NOT enough: a turn that ends with workers in flight gets those workers STOPPED, and the round scores zero even though real work was happening. So HOLD the turn open by polling instead: run a shell sleep of 60 seconds, then re-query the build_state of every batch ticket from Praxis, and repeat that sleep-and-query cycle for as long as any of them is still incomplete or in_progress. Only after every batch ticket reads finished or blocked may you merge, reap, and report. When every ticket is finished or blocked: merge each ticket branch into the already-checked-out branch, remove ALL worktrees the round created, then STOP and report. You are running HEADLESS with no human attached: never ask a clarifying question or present a numbered choice, because nothing can answer it and the session will sit until it is reaped. Decide, or record the blocker and stop. Do NOT claim, read, or start any ticket outside that id list even if more remain — a fresh session picks up the next batch. Work ONLY on the already-checked-out branch, do NOT push. Every worker edits ONLY the files inside its OWN assigned worktree — the factory checkout that holds this driver and the af-build hooks is TOOLING, not the project, and editing it is out of bounds even when a ticket is about that code. Two reasons it matters: those hook files are imported at runtime by every project's loop on this machine, so a half-finished edit there can break builds that have nothing to do with this ticket; and edits made outside a worktree sit on no branch, so the round's merge step cannot see them and they are silently dropped when the round ends. For ANY factory or Praxis python invocation, run $PY rather than a bare python3 — this run preflighted that interpreter and a bare python3 may resolve to an older one whose missing tomllib makes the universal quality checks load as an empty list. Pass that same path down to every subagent you spawn.$SERVICES."
  # The env preamble runs in the tmux shell AFTER ~/.bashrc has already sourced the
  # machine-wide backend file, so it deliberately overrides that file rather than
  # trusting it to agree with $AF_MODEL_BACKEND. Grok gets the prompt as argv.
  af_launch_agent "$SESSION" "$round_prompt"
  # Cold starts on a loaded box exceed the old 80s cap (measured: round #1 of taolu-coach), so
  # poll 3x longer — and if the pane NEVER signals ready there is NO AGENT in it, so sending is
  # strictly worse than failing loudly. The tickets are unclaimed, so the next pass retries them.
  if ! af_wait_ready "$SESSION" $((READY_POLL_MAX * 3)); then
    say "FATAL: round #$round pane never signalled ready after $((READY_POLL_MAX*6))s — no agent in the session, NOT sending the prompt; killing the session so the next pass retries these tickets"
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    continue
  fi
  submitted=0
  if [ "$BACKEND" = grok ]; then
    submitted=1
  else
    # Submit, then CONFIRM the prompt landed. The wedge this catches is measured, not hypothetical:
    # a prompt sent into a not-quite-ready TUI is swallowed whole, the pane sits at an idle input box
    # showing the 'Try "..."' placeholder, and the round waits 30+ minutes for the stall net.
    sleep 2; tmux send-keys -t "$SESSION" Enter; sleep 2
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
  fi
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
  # GRACE: the deadline is the backstop that ends a wedged round, but fired blind it also
  # guillotines a healthy endgame — R38 (2026-08-10) was 4 graded passes in with 2 stylistic
  # defects left and a worker actively writing when the 100min cap killed round #6; the rebuild
  # cost a full fresh-context round. So at expiry, ask the OS the same question the stall guard
  # asks (verify_children_busy): if real work is provably happening, extend in AF_ROUND_GRACE_S
  # steps up to AF_ROUND_GRACE_MAX_S total. A wedged round shows no busy children and dies on
  # schedule; only demonstrable work buys time, and the budget keeps "busy" from meaning forever.
  grace_step=${AF_ROUND_GRACE_S:-900}
  grace_max=${AF_ROUND_GRACE_MAX_S:-2700}
  grace_spent=0
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
    if [ "$open" = "0" ]; then say "round #$round complete — all $size ticket(s) finished, blocked, or parked on manual sign-off"; break; fi
    # Neither the finished count NOR the batch's open count moved? Say so out loud, on an interval,
    # so a round that is asleep reads differently from a round that is working.
    af_round_heartbeat "$round" "$now/$open" "$ids_csv"
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
    if echo "$pane" | grep -qiE "insufficient balance|quota exceeded|payment required|credit balance is too low|insufficient_quota|billing_(error|hard_limit)|billing (error|failure)|(http[ /]?|status[ :]?)402|402 payment required"; then
      say "BILLING FAILURE (out of credits/quota) — halting the whole loop; top up and relaunch"
      commit_wip
      tmux kill-session -t "$SESSION" 2>/dev/null || true
      exit 3
    fi
    # Subscription session/usage limit: the CLI strands on the interactive /rate-limit-options menu,
    # which no headless worker can answer. Caught BEFORE the stall accounting below so it does not
    # burn a fresh STALL_POLLS window per ticket forever (the exact cascade that finished ZERO
    # tickets in rounds #4/#5 on 2026-08-10). Commit WIP first, then halt the whole run.
    if echo "$pane" | rate_limited; then
      commit_wip
      halt_quota_blocked "build round #$round" "$SESSION"
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
    if [ "$waited" -ge "$deadline" ] && [ "$grace_spent" -lt "$grace_max" ] && verify_children_busy "$SESSION"; then
      deadline=$((deadline + grace_step)); grace_spent=$((grace_spent + grace_step))
      say "round #$round deadline reached but a child process is live — extending by $((grace_step/60))min (grace used $((grace_spent/60))/$((grace_max/60))min)"
    fi
  done
  [ "$waited" -ge "$deadline" ] && say "round #$round timed out after $((waited/60))min with $(batch_open "$@") ticket(s) still open"

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
  salvage_external_grok_clones
  integrate_round
  # REQUIRED, not optional: sweep in any branch stranded by an earlier round so the resolver lands it
  # too. Without this the resolver only ever fixes the round that created the conflict, and anything
  # older stays orphaned forever — which is precisely how 11 accumulated.
  queue_orphan_branches
  # Every ticket id the resolver is about to land this round — conflicted batch branches plus swept
  # orphans. Captured BEFORE resolve_conflicts drains the queue, because the verification gate below
  # must treat these as integrated work even on a round that finishes zero tickets: an orphan branch's
  # ticket is usually ALREADY finished (by the run that built it), so landing it never moves
  # finished_count, and gating verification on finished_count alone let a round merge eight orphan
  # branches, leave the full suite red on the integrated tree, and regress nothing (2026-08-09) —
  # the dispatcher then had no claimable work and the circuit breaker halted a fixable run.
  landed_ids=$( { cut -f2- "$CONFLICTS" 2>/dev/null | tr '\t ' '\n' | sort -u | tr '\n' ' '; } || true)
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

  # Loop-end hooks. Backgrounded, bounded, never waited on — see af_loop_end_hooks.
  af_loop_end_hooks "round #$round"

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
    AF_ROUNDS_UNVERIFIED=$((AF_ROUNDS_UNVERIFIED + 1))   # BUG B-5 — feeds the end-of-run summary
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
      # shellcheck disable=SC2046  # deliberate word-split: ids are single tokens
      verify_round "$round" $(printf '%s\n' "$@" $landed_ids | sort -u | tr '\n' ' ')
    fi
  else
    fruitless=$((${fruitless:-0} + 1))
    say "round #$round finished ZERO tickets ($fruitless in a row)"
    # A zero-finish round can still have LANDED work — the orphan sweep merges branches for tickets
    # finished by an earlier run, which never moves finished_count. That merge is exactly as
    # unverified as any other, and skipping this stage on it is how a red integrated tree ends up
    # with no ticket regressed to fix it. Verify whenever the resolver landed anything.
    if [ -n "${landed_ids// /}" ] && [ "${AF_VERIFY_ROUND:-1}" = "1" ]; then
      # shellcheck disable=SC2046
      verify_round "$round" $landed_ids
    fi
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
#
# The R24 flag surfacing used to live on the NEXT line, after this assertion. It is now in
# af_cleanup_on_exit, on the EXIT trap — see af_surface_flags for why that placement was the whole
# bug: a straggler here exits 7 from inside the assertion and the next line never ran.
af_assert_no_stragglers "exit"
say "af-ticket-loop finished for $PROJECT"
