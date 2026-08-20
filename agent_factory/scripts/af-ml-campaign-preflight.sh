#!/usr/bin/env bash
# READINESS PREFLIGHT: can this ml_registry campaign actually run RIGHT NOW, on THIS machine?
#
# WHY THIS EXISTS. af-ml-campaign-loop.sh answers "is the campaign FINISHED". Nothing answered
# "could it START". Measured: the three live campaigns are byte-identical in git on the laptop and
# on the devbox, and the thing that decides whether an arm can run -- the staged corpus -- is on
# neither side of git. A campaign whose registry is perfectly green refuses at the first arm
# because the pixels are not there, and the queue records that as a dispatch failure hours later.
# Everything here is READ-ONLY: campaign-status and campaign-complete never mutate a space, the
# ledger is read, and the corpus question is asked by calling each lab's OWN resolver.
#
# USAGE
#   af-ml-campaign-preflight.sh --all
#   af-ml-campaign-preflight.sh --campaign detection [--campaign association]
#   [--repo /workspace/sports_analysis] [--praxis /workspace/praxis]
#   Defaults: --praxis is the checkout this script lives in; --repo is $AF_SPORTS_REPO, else
#   /workspace/sports_analysis, else ~/Documents/official_repos/sports_analysis.
#
# CONTRACT FOR THE QUEUE RUNNER
#   stdout is the machine-readable result, one line per check:
#       PREFLIGHT <campaign> <CHECK> <PASS|FAIL|INFO> k=v ... [detail=<free text>]
#       PREFLIGHT <campaign> RESULT <READY|NOT_READY|REFUSED> exit=<n> pass=<n> fail=<n> runnable_arms=<n>
#       PREFLIGHT ALL RESULT <READY|NOT_READY|REFUSED> exit=<n> campaigns=<n> ready=<n>
#     Keys never contain spaces; `detail=` is always LAST on its line and may; embedded newlines
#     are escaped to a literal \n so one check is always exactly one line.
#   stderr is the human summary. Read stdout, drop stderr, and nothing is lost.
#   CHECKS: STATUS COMPLETE LEDGER SEED CORPUS DISPATCH STRUCTURE (SEED and STRUCTURE are INFO --
#     reported, never counted for or against readiness).
#
#   EXIT CODES, deliberately distinguishable -- a queue must tell "broken" from "nothing to run":
#       0  READY             every check passed AND at least one un-run implemented arm exists.
#       1  NOT_READY         at least one check FAILED.
#       2  usage error.
#       3  REFUSED           must not be supervised (C1 court-marking) or unknown campaign.
#       4  NO_RUNNABLE_ARMS  every check passed and there is NOTHING LEFT TO DISPATCH. Green, and
#                            still not something to hand an unattended loop: a shell loop cannot
#                            author an arm. Measured today that is association -- all 6 toggles
#                            are known non-moves -- which is why this is not folded into 0 or 1.
#   With --all the exit code is the worst over the campaigns, ordered 0 < 4 < 1 < 3.
#
#   Example: dispatch only what is actually runnable.
#       af-ml-campaign-preflight.sh --campaign detection >/tmp/pf.txt 2>/dev/null; rc=$?
#       [ "$rc" = 0 ] || exit 0   # 4 needs an agent, 1 needs a fix, 3 must never run
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRAXIS_DEFAULT="$(cd "$HERE/../.." && pwd)"

ARGS=()
PRAXIS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --praxis) PRAXIS="$2"; ARGS+=(--praxis "$2"); shift 2;;
    *) ARGS+=("$1"); shift;;
  esac
done
[ -n "$PRAXIS" ] || { PRAXIS="$PRAXIS_DEFAULT"; ARGS+=(--praxis "$PRAXIS"); }

cd "$PRAXIS" || { echo "no praxis checkout at $PRAXIS" >&2; exit 2; }
PRAXIS_DB_DISABLED=1 exec uv run python -m knowledge.ml_registry.preflight "${ARGS[@]}"
