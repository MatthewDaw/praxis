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

# --------------------------------------------------------------------------- argument resolution
# This wrapper was calling the engine with flags the engine no longer has. `knowledge.ml_registry
# .preflight` was generalized from a hardcoded CAMPAIGNS table to a versioned MANIFEST, gaining
# required --manifest and --project-root, and this script kept passing --repo. Every invocation
# therefore died with "the following arguments are required" and exit 2 -- a campaign runner whose
# readiness gate could not run at all, which nobody noticed because nothing had called it since.
CAMPAIGNS=()
PRAXIS=""; REPO=""; MANIFEST=""; ALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --praxis)       PRAXIS="$2"; shift 2;;
    --repo|--project-root) REPO="$2"; shift 2;;
    --manifest)     MANIFEST="$2"; shift 2;;
    --campaign)     CAMPAIGNS+=("$2"); shift 2;;
    --all)          ALL=1; shift;;
    -h|--help)      sed -n '1,45p' "$HERE/$(basename "${BASH_SOURCE[0]}")"; exit 2;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done

[ -n "$PRAXIS" ] || PRAXIS="$PRAXIS_DEFAULT"
if [ -z "$REPO" ]; then
  for cand in "${AF_SPORTS_REPO:-}" /workspace/sports_analysis "$HOME/Documents/official_repos/sports_analysis"; do
    [ -n "$cand" ] && [ -d "$cand" ] && { REPO="$cand"; break; }
  done
fi
if [ -z "$MANIFEST" ]; then
  for cand in "${AF_ML_PREFLIGHT_MANIFEST:-}" \
              "$REPO/.af/ml-preflight-manifest.json" \
              "$PRAXIS/knowledge/ml_registry/preflight-manifest.json"; do
    [ -n "$cand" ] && [ -f "$cand" ] && { MANIFEST="$cand"; break; }
  done
fi

# Every refusal speaks the documented stdout contract, so a queue runner parsing this never has to
# special-case "the wrapper itself was misconfigured" -- that was previously an argparse usage dump
# on stderr and nothing at all on stdout.
if [ -z "$REPO" ] || [ ! -d "$REPO" ]; then
  echo "PREFLIGHT ALL RESULT NOT_READY exit=2 detail=project_root_not_found"
  echo "no project checkout; pass --repo <path> or set AF_SPORTS_REPO" >&2
  exit 2
fi
# The existence test applies however the path was resolved. Validating only the DISCOVERED path let
# an explicit `--manifest /typo.json` fall through to the reader and come back "unreadable", which
# sends an operator looking for a corrupt file instead of a wrong path.
if [ -n "$MANIFEST" ] && [ ! -f "$MANIFEST" ]; then
  echo "PREFLIGHT ALL RESULT NOT_READY exit=2 detail=manifest_not_found"
  echo "no preflight manifest at $MANIFEST" >&2
  exit 2
fi
if [ -z "$MANIFEST" ]; then
  echo "PREFLIGHT ALL RESULT NOT_READY exit=2 detail=manifest_not_found"
  echo "no preflight manifest. Pass --manifest, or set AF_ML_PREFLIGHT_MANIFEST, or place one at" >&2
  echo "  $REPO/.af/ml-preflight-manifest.json" >&2
  exit 2
fi
if [ "$ALL" != 1 ] && [ "${#CAMPAIGNS[@]}" -eq 0 ]; then
  echo "PREFLIGHT ALL RESULT NOT_READY exit=2 detail=pass --campaign NAME or --all"
  echo "give --campaign NAME (repeatable) or --all" >&2
  exit 2
fi

AF_PY="${AF_PYTHON:-$PRAXIS/.venv/bin/python}"
[ -x "$AF_PY" ] || AF_PY="python3"

# --------------------------------------------------------------------------- CHECK: COMPUTE
# THE GAP THIS CLOSES. The engine asks seven questions -- STATUS, COMPLETE, LEDGER, SEED, CORPUS,
# DISPATCH, STRUCTURE -- and every one is about corpora and bookkeeping. None is about the HARDWARE.
# This box has no GPU (no driver, no /dev/nvidia*, torch installed as the +cpu build), and a
# GPU-declared campaign run here does not crash: ML code falls back to CPU silently and by design.
# The arm runs, produces numbers, and those numbers are recorded against a campaign claiming to have
# measured a GPU regime. That is a measurement of a different thing, reported as real. A crash would
# have been kinder.
#
# So COMPUTE runs FIRST and is decided per campaign, before any corpus is touched: a campaign the
# host cannot satisfy is REFUSED (exit 3, the same code project policy uses for "must not be
# supervised") and is EXCLUDED from the delegated run rather than merely annotated. The remaining
# campaigns still go through the engine, because one unrunnable campaign must not stop the queue --
# a refusal is skipped and reported, never fatal.
#
# `device` is an OPTIONAL manifest field defaulting to cpu, so a manifest that predates this says
# cpu and nothing about its behaviour changes.
names_file="$(mktemp)"; clean_manifest="$(mktemp)"
trap 'rm -f "$names_file" "$clean_manifest"' EXIT

# `device` is OURS, not the engine's. load_manifest refuses unknown keys outright, and the engine
# lives in the PROJECT tree that build workers are editing right now -- widening its schema from
# here would be a tooling change reaching into project code, and would collide. So the wrapper
# reads `device`, answers the compute question itself, and hands the engine a manifest with the key
# stripped. The engine sees exactly the schema it has always seen.
if ! "$AF_PY" - "$MANIFEST" "$ALL" "$clean_manifest" "${CAMPAIGNS[@]+"${CAMPAIGNS[@]}"}" > "$names_file" <<'PYEOF'
import json, sys
path, want_all, clean_out, wanted = sys.argv[1], sys.argv[2] == "1", sys.argv[3], sys.argv[4:]
try:
    payload = json.loads(open(path).read())
except Exception as exc:
    print(f"!ERROR unreadable manifest {path}: {exc}")
    raise SystemExit(0)
raw = [c for c in (payload.get("campaigns") or []) if c.get("name")]
camps = {c["name"]: c for c in raw}
names = list(camps) if want_all else wanted
for n in names:
    if n not in camps:
        # Not our call to make: the engine owns "unknown campaign" (it refuses with exit 3 and a
        # message naming the manifest). Pass it through with the default lane so that stays true.
        print(f"{n}\tcpu")
        continue
    print(f"{n}\t{str(camps[n].get('device') or 'cpu').strip().lower()}")
payload["campaigns"] = [{k: v for k, v in c.items() if k != "device"} for c in raw]
open(clean_out, "w").write(json.dumps(payload))
PYEOF
then
  echo "PREFLIGHT ALL RESULT NOT_READY exit=2 detail=manifest_unreadable"
  exit 2
fi
if grep -q '^!ERROR' "$names_file"; then
  echo "PREFLIGHT ALL RESULT NOT_READY exit=2 detail=manifest_unreadable"
  sed -n 's/^!ERROR //p' "$names_file" >&2
  exit 2
fi

runnable=(); n_refused=0
while IFS=$'\t' read -r cname cdev; do
  [ -n "$cname" ] || continue
  if evidence=$("$AF_PY" -m agent_factory.host_capability "$cdev" --quiet 2>&1); then
    echo "PREFLIGHT $cname COMPUTE PASS device=$cdev detail=${evidence//$'\n'/\\n}"
    runnable+=("$cname")
  else
    echo "PREFLIGHT $cname COMPUTE FAIL device=$cdev detail=${evidence//$'\n'/\\n}"
    echo "PREFLIGHT $cname RESULT REFUSED exit=3 pass=0 fail=1 runnable_arms=0"
    "$AF_PY" -m agent_factory.host_capability "$cdev" --subject "campaign '$cname'" >&2 || true
    n_refused=$((n_refused + 1))
  fi
done < "$names_file"

if [ "${#runnable[@]}" -eq 0 ]; then
  echo "PREFLIGHT ALL RESULT REFUSED exit=3 campaigns=$n_refused ready=0"
  exit 3
fi

# --------------------------------------------------------------------------- delegate the rest
cd "$PRAXIS" || { echo "no praxis checkout at $PRAXIS" >&2; exit 2; }
[ -s "$clean_manifest" ] || cp "$MANIFEST" "$clean_manifest"
DELEGATE=(--manifest "$clean_manifest" --project-root "$REPO" --praxis "$PRAXIS")
for c in "${runnable[@]}"; do DELEGATE+=(--campaign "$c"); done

PRAXIS_DB_DISABLED=1 uv run python -m knowledge.ml_registry.preflight "${DELEGATE[@]}"
rc=$?

# Severity ordering is the engine's own: 0 READY < 4 NO_RUNNABLE_ARMS < 1 NOT_READY < 3 REFUSED.
# A compute refusal outranks whatever the survivors reported, because "this host is wrong for it"
# is the most actionable thing the caller can be told.
if [ "$n_refused" -gt 0 ]; then
  echo "PREFLIGHT ALL RESULT REFUSED exit=3 campaigns=$((n_refused + ${#runnable[@]})) ready=0 detail=$n_refused refused on COMPUTE"
  exit 3
fi
exit "$rc"
