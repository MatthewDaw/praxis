#!/usr/bin/env bash
# Run one composing ml_registry campaign after another, unattended, in the order a table declares.
#
# WHY THIS EXISTS. af-ml-campaign-loop.sh drives ONE campaign to a decision and exits. Overnight
# that leaves fifteen hours of a 16-vCPU box idle behind a campaign that finished at 22:40, because
# nothing starts the next one. This composes that loop; it does not change it.
#
# THE DISTINCTION THIS QUEUE EXISTS TO KEEP. af-ml-campaign-loop.sh already refuses to call an empty
# queue of arms a finished campaign. A queue runner that advances on "the loop exited" reintroduces
# exactly that error one level up: three campaigns that each STALLED at iteration 1 would be
# reported at breakfast as three campaigns run. So this ADVANCES ONLY ON COMPLETE -- loop exit 0 --
# and stops on anything else, naming which campaign and why. --continue-past-incomplete opts out,
# and is off by default for that reason.
#
# THE LIMIT AN OPERATOR MUST NOT BE ALLOWED TO FORGET. Across the 152-idea backlog there are about
# nine implemented arms, and association's six all measure as no-ops. A shell loop cannot author an
# arm. So the expected overnight outcome is STALLED within a couple of iterations, and this reports
# that as NO IMPLEMENTED ARM LEFT -- an agent must author arms -- with its own exit code (4),
# distinct from COMPLETE. A dry queue must never read like a finished one.
#
# Usage:
#   af-ml-campaign-queue.sh [--config <file>] [--praxis <dir>] [--log <file>]
#                           [--only <name>[,<name>...]] [--max-iterations N]
#                           [--continue-past-incomplete] [--dry-run] [--help]
#
# Exit codes -- each is a different thing that happened, and they are not interchangeable:
#   0  every queued campaign is COMPLETE (including ones already complete on entry)
#   2  usage or config error, INCLUDING a refused campaign (see REFUSALS below)
#   3  stopped: a campaign came back BLOCKED
#   4  stopped: a campaign came back STALLED -- no implemented arm left, an agent must author arms
#   5  stopped: a campaign hit --max-iterations without completing
#   6  stopped: a campaign failed preflight (missing corpus, dead dispatch, unreadable space file)
#   7  another queue run holds the lock -- two campaigns at once contend for CPU and registry locks
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Default to the checkout this script lives in, not a fixed /workspace/praxis. LOG is derived from
# PRAXIS's parent, so a hardcoded devbox path made every laptop invocation die at the lock with
# "tee: /workspace/...: No such file or directory" -- loud, but only because the lock happened to be
# unwritable too, which is not a guarantee worth resting on.
PRAXIS="${PRAXIS:-$(cd "$HERE/../.." && pwd)}"
CONFIG="$HERE/af-ml-campaign-queue.conf"
LOOP="${AF_QUEUE_LOOP:-$HERE/af-ml-campaign-loop.sh}"   # test seam: point at a fake loop to
PREFLIGHT="${AF_QUEUE_PREFLIGHT:-$HERE/af-ml-campaign-preflight.sh}"  # exercise the tree without compute
LOG=""; ONLY=""; MAX_ITER=40; CONTINUE=0; DRY=0

usage () { sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2;;
    --praxis) PRAXIS="$2"; shift 2;;
    --log) LOG="$2"; shift 2;;
    --only) ONLY="$2"; shift 2;;
    --max-iterations) MAX_ITER="$2"; shift 2;;
    --continue-past-incomplete) CONTINUE=1; shift;;
    --dry-run) DRY=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2;;
  esac
done

[ -n "$LOG" ] || LOG="$(dirname "$PRAXIS")/af-ml-campaign-queue.log"
export PRAXIS_DB_DISABLED="${PRAXIS_DB_DISABLED:-1}"

mkdir -p "$(dirname "$LOG")" 2>/dev/null
say () { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"; }

[ -f "$CONFIG" ] || { echo "no campaign table at $CONFIG (pass --config)" >&2; exit 2; }
[ -x "$LOOP" ]   || { echo "no campaign loop at $LOOP" >&2; exit 2; }

# ---------------------------------------------------------------------------- refusals
# Two campaigns must never be driven by this queue, and both are refusals rather than skips: a skip
# scrolls past at 03:00 and the morning report shows a queue that ran.
#
#   association model-1d148ef55231 -- src/assoc_lab holds arms.py corpus.py hota.py train.py and NO
#     campaign.py. af-ml-campaign-loop.sh drives a COMPOSING module; there is nothing here for it to
#     drive. Association is agent-one-arm by design, and its six known toggles all measure as
#     non-moves anyway (cmc is a literal no-op, act30 is dead code, every buffer value scores within
#     0.001 HOTA), so a loop over them would burn the night reproducing a flat line.
#   C1 court-marking model-16659b6b8e45 -- NOT READY. Never queued, never supervised.
#
# TODO(P-2, CODEX-ML-CAMPAIGN-FOUNDATION-AUDIT-PLAN.md #7.1): these model ids duplicate the
# `refused` map in the versioned sports CampaignSpec manifest at
# sports_analysis/src/sports_analysis/experimentation/portfolio/preflight_manifest.json, which
# knowledge/ml_registry/preflight.py already reads generically. This function stays a literal
# case match (rather than reading that JSON) until af-ml-campaign-queue.sh itself is retired
# under P-3 -- keep both in sync by hand until then.
refused_reason () {
  case "$1|$2" in
    association*|*\|model-1d148ef55231)
      echo "association has NO composing module (src/assoc_lab has no campaign.py), so the campaign loop cannot drive it. It is agent-one-arm by design.";;
    *court*|*\|model-16659b6b8e45)
      echo "C1 court-marking is NOT READY. It must never be queued or supervised.";;
    *) echo "";;
  esac
}

# ---------------------------------------------------------------------------- one campaign at a time
LOCK="${LOG}.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  holder="$(cat "$LOCK/pid" 2>/dev/null || echo '?')"
  if [ "$holder" != "?" ] && kill -0 "$holder" 2>/dev/null; then
    echo "another af-ml-campaign-queue run holds $LOCK (pid $holder). Two campaigns at once contend for CPU and hold registry locks." >&2
    exit 7
  fi
  say "[queue] clearing stale lock $LOCK (pid $holder is gone)"
  rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null || { echo "cannot take $LOCK" >&2; exit 7; }
fi
echo "$$" > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

registry () { (cd "$PRAXIS" && uv run python -m knowledge.ml_registry.cli "$@"); }

# ---------------------------------------------------------------------------- parse the table
# The campaign list is DATA. Fields are pipe-separated because a dispatch string contains spaces:
#   name|repo|space_file|model_id|stages|ledger|corpus|dispatch
# repo is the working directory the dispatch runs in. It is a field rather than a derivation
# because the labs do not live where the registry does: det_lab and contact_lab import only from
# /workspace/sports_analysis, while the CLI that adjudicates them runs in /workspace/praxis. The
# first devbox dry-run of this queue failed exactly there -- "det_lab.campaign does not import" --
# with every path in the table correct.
NAMES=(); REPOS=(); SPACES=(); MODELS=(); STAGEL=(); LEDGERS=(); CORPORA=(); DISPATCHES=()
lineno=0; parse_error=0
while IFS= read -r raw || [ -n "$raw" ]; do
  lineno=$((lineno+1))
  line="${raw%%$'\r'}"
  case "$line" in ''|'#'*) continue;; esac
  IFS='|' read -r f_name f_repo f_space f_model f_stages f_ledger f_corpus f_dispatch <<< "$line"
  f_name="$(echo "${f_name:-}" | xargs)"; f_repo="$(echo "${f_repo:-}" | xargs)"
  f_space="$(echo "${f_space:-}" | xargs)"
  f_model="$(echo "${f_model:-}" | xargs)"; f_stages="$(echo "${f_stages:-}" | xargs)"
  f_ledger="$(echo "${f_ledger:-}" | xargs)"; f_corpus="$(echo "${f_corpus:-}" | xargs)"
  f_dispatch="$(echo "${f_dispatch:-}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
  if [ -z "$f_name" ] || [ -z "$f_repo" ] || [ -z "$f_space" ] || [ -z "$f_model" ] \
     || [ -z "$f_stages" ] || [ -z "$f_dispatch" ]; then
    echo "$CONFIG:$lineno: need name|repo|space|model|stages|ledger|corpus|dispatch -- got: $line" >&2
    parse_error=1; continue
  fi
  NAMES+=("$f_name"); REPOS+=("$f_repo"); SPACES+=("$f_space"); MODELS+=("$f_model"); STAGEL+=("$f_stages")
  LEDGERS+=("$f_ledger"); CORPORA+=("$f_corpus"); DISPATCHES+=("$f_dispatch")
done < "$CONFIG"
[ "$parse_error" = 0 ] || exit 2
[ "${#NAMES[@]}" -gt 0 ] || { echo "$CONFIG declares no campaigns" >&2; exit 2; }

# Refusals are checked for the WHOLE table before anything runs. Finding out at 04:00 that entry
# three was never runnable wastes the window the queue existed to use.
refusal_found=0
for i in "${!NAMES[@]}"; do
  why="$(refused_reason "${NAMES[$i]}" "${MODELS[$i]}")"
  if [ -n "$why" ]; then
    echo "REFUSED ${NAMES[$i]} (${MODELS[$i]}): $why" >&2
    refusal_found=1
  fi
done
if [ "$refusal_found" = 1 ]; then
  say "[queue] REFUSED a campaign in $CONFIG -- nothing dispatched. Remove the entry to run the rest."
  exit 2
fi

selected () {
  [ -n "$ONLY" ] || return 0
  case ",$ONLY," in *",$1,"*) return 0;; esac
  return 1
}

# ---------------------------------------------------------------------------- preflight
# Run before EACH campaign, not once at the top: a corpus can be reaped, a ledger moved, or a lab
# module broken by an edit made while campaign one was running for six hours. Starting a long
# campaign and dying on a missing corpus wastes exactly the window this queue existed to use.
#
# af-ml-campaign-preflight.sh in this directory is owned by another agent and answers a strictly
# larger question than these inline checks: it asks each lab's OWN corpus resolver and counts
# un-run implemented arms. Its published contract is
#   --campaign <name> [--repo <dir>] [--praxis <dir>]
#   0 READY  1 NOT_READY  2 usage  3 REFUSED  4 NO_RUNNABLE_ARMS
# and its exit 4 is the same fact this queue calls a dry queue: green, and still nothing an
# unattended loop can dispatch. Exit 2 or 127 means it does not know this campaign (or is not
# there), and we degrade to the inline checks rather than being blocked on it.
preflight_inline () {
  local name="$1" repo="$2" space="$3" model="$4" ledger="$5" corpus="$6" dispatch="$7" fail=0
  [ -d "$repo" ] || { echo "  repo missing: $repo"; fail=1; }
  [ -f "$space" ] || { echo "  space file missing: $space"; fail=1; }
  if [ -n "$ledger" ] && [ ! -f "$ledger" ]; then echo "  ledger missing: $ledger"; fail=1; fi
  if [ -n "$corpus" ] && [ ! -e "$corpus" ]; then
    echo "  corpus absent on this machine: $corpus"; fail=1; fi
  if [ -f "$space" ] && ! registry campaign-status --space-file "$space" --model-id "$model" --json >/dev/null 2>&1; then
    echo "  campaign-status failed for $model"; fail=1; fi
  # Imported from the LAB repo, which is where the loop evals the dispatch. Importing it from
  # $PRAXIS reports a missing module for every lab -- which is what the first devbox run did.
  local mod; mod="$(echo "$dispatch" | sed -n 's/.*-m \([A-Za-z0-9_.]*\).*/\1/p')"
  if [ -n "$mod" ] && [ -d "$repo" ]; then
    if ! (cd "$repo" && uv run python -c "import importlib,sys; importlib.import_module(sys.argv[1])" "$mod" >/dev/null 2>&1); then
      echo "  dispatch module does not import in $repo: $mod"; fail=1; fi
  fi
  return $fail
}

# Returns: 0 ready, 1 not ready, 3 refused, 4 no runnable arm (a dry queue, not a failure).
preflight () {
  local name="$1" repo="$2" space="$3" model="$4" stages="$5" ledger="$6" corpus="$7" dispatch="$8"
  if [ -x "$PREFLIGHT" ]; then
    local out rc
    out="$("$PREFLIGHT" --campaign "$name" --repo "$repo" --praxis "$PRAXIS" 2>&1)"
    rc=$?
    case "$rc" in
      0|1|3|4) echo "$out" | sed 's/^/  /'; return "$rc";;
      *) echo "  af-ml-campaign-preflight.sh does not know '$name' (exit $rc); using inline checks";;
    esac
  fi
  preflight_inline "$name" "$repo" "$space" "$model" "$ledger" "$corpus" "$dispatch"
}

# A dispatch still carrying ARM / TAG / STAGE placeholders is a TEMPLATE, not a command. Running it
# literally fails inside the lab (an unimplemented arm is a hard error there, never a skip), and the
# queue would report that as a broken dispatch. It is neither: it is the honest limiter -- nobody has
# authored which arm runs next, and a shell loop cannot decide that. Say so in those words.
is_template () { echo " $1 " | grep -Eq '[[:space:]](ARM|TAG|STAGE)[[:space:]]|=(ARM|TAG|STAGE)$'; }

# ---------------------------------------------------------------------------- run the queue
say "[queue] starting: config=$CONFIG campaigns=${#NAMES[@]} continue_past_incomplete=$CONTINUE dry_run=$DRY log=$LOG"
ran=0; completed=0; skipped=0; final_rc=0
for i in "${!NAMES[@]}"; do
  name="${NAMES[$i]}"; repo="${REPOS[$i]}"; space="${SPACES[$i]}"; model="${MODELS[$i]}"; stages="${STAGEL[$i]}"
  ledger="${LEDGERS[$i]}"; corpus="${CORPORA[$i]}"; dispatch="${DISPATCHES[$i]}"
  selected "$name" || { say "[queue] $name: not selected by --only"; continue; }

  # Resumable: a campaign that is already COMPLETE is not re-run. campaign-complete is the same
  # authority the loop uses, so the queue and the loop can never disagree about what finished.
  if registry campaign-complete --space-file "$space" --model-id "$model" --stages "$stages" >/dev/null 2>&1; then
    say "[queue] $name: SKIP -- already COMPLETE"; skipped=$((skipped+1)); completed=$((completed+1)); continue
  fi

  say "[queue] $name: preflight ($model)"
  pf="$(preflight "$name" "$repo" "$space" "$model" "$stages" "$ledger" "$corpus" "$dispatch")"
  pf_rc=$?
  printf '%s\n' "$pf" | grep -q '[^[:space:]]' && printf '%s\n' "$pf" | tee -a "$LOG"
  if [ "$pf_rc" = 3 ]; then
    say "[queue] $name: REFUSED by preflight -- it must not be supervised. Nothing dispatched."
    final_rc=2; break
  fi
  if [ "$pf_rc" = 1 ]; then
    say "[queue] $name: PREFLIGHT FAILED -- not dispatched"
    final_rc=6
    [ "$CONTINUE" = 1 ] || { say "[queue] STOPPING at $name. Fix the above, then re-run; completed campaigns are skipped."; break; }
    continue
  fi

  if [ "$pf_rc" = 4 ] || is_template "$dispatch"; then
    say "[queue] $name: NO IMPLEMENTED ARM LEFT TO RUN -- an agent must author arms."
    if [ "$pf_rc" = 4 ]; then
      say "[queue]   preflight is green and counts no un-run implemented arm left to dispatch."
      say "[queue]   Hand $name to af-ml-supervise; a shell queue cannot author the next arm."
    else
      say "[queue]   its dispatch is a template, not a command: $dispatch"
      say "[queue]   A shell queue cannot choose the arm. Run af-ml-supervise on $name, or give it a"
      say "[queue]   concrete dispatch in $CONFIG."
      # Substituting an arm here would be worse than stopping: af-ml-campaign-loop.sh re-evals the
      # SAME AF_DISPATCH every iteration, so a fixed --arm/--tag pair appends the same ledger key
      # twice, and unique keys are exactly what preflight's LEDGER check is guarding. Choosing a
      # different arm per iteration is the authoring step, and it is an agent's job.
      runnable="$(printf '%s\n' "$pf" | sed -n 's/.*arms_runnable=\([^ ]*\).*/\1/p' | head -1)"
      [ -n "$runnable" ] && say "[queue]   preflight says these implemented arms are un-run: $runnable"
    fi
    final_rc=4
    [ "$CONTINUE" = 1 ] || { say "[queue] STOPPING at $name."; break; }
    continue
  fi

  if [ "$DRY" = 1 ]; then
    say "[queue] $name: DRY RUN -- would run, from $repo: AF_DISPATCH=\"$dispatch\" $LOOP --space-file $space --model-id $model --stages $stages --praxis $PRAXIS --max-iterations $MAX_ITER"
    continue
  fi

  say "[queue] $name: START stages=$stages"
  start=$SECONDS
  # cd into the lab repo: the loop evals AF_DISPATCH in its own cwd, and each lab module imports
  # only from its own repo.
  (cd "$repo" && AF_DISPATCH="$dispatch" "$LOOP" --space-file "$space" --model-id "$model" \
      --stages "$stages" --praxis "$PRAXIS" --max-iterations "$MAX_ITER") 2>&1 | tee -a "$LOG"
  rc="${PIPESTATUS[0]}"
  ran=$((ran+1))
  say "[queue] $name: END after $((SECONDS-start))s loop_exit=$rc"

  case "$rc" in
    0) say "[queue] $name: COMPLETE -- advancing"; completed=$((completed+1)); continue;;
    3) say "[queue] $name: BLOCKED -- running more arms cannot fix it. See the diagnosis above."; final_rc=3;;
    4) say "[queue] $name: STALLED -- NO IMPLEMENTED ARM LEFT TO RUN, an agent must author arms."
       say "[queue]   This is NOT a finished campaign. The loop's own campaign-complete output above"
       say "[queue]   names what is unauthored. Hand $name to af-ml-supervise."
       final_rc=4;;
    5) say "[queue] $name: hit --max-iterations $MAX_ITER without completing"; final_rc=5;;
    *) say "[queue] $name: loop exited $rc -- treating as not COMPLETE"; final_rc=3;;
  esac
  if [ "$CONTINUE" = 1 ]; then
    say "[queue] --continue-past-incomplete: advancing past $name anyway"
  else
    say "[queue] STOPPING at $name. Re-run after fixing; already-COMPLETE campaigns are skipped."
    break
  fi
done

say "[queue] done: ${completed}/${#NAMES[@]} COMPLETE (${skipped} already complete on entry, ${ran} loop run(s)) exit=$final_rc"
exit "$final_rc"
