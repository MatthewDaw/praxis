#!/usr/bin/env bash
# VERIFY THE MERGED TREE, THEN PUBLISH IT. The missing half of every build run.
#
# WHY THIS EXISTS. af-ticket-loop.sh never pushes -- deliberately, so an unattended loop cannot
# publish work nobody has looked at. The consequence is that DRIFT IS THE DEFAULT: every run ends
# with its integration branch ahead of the remote, and the loop's own end-of-run summary says so
# ("N commit(s) AHEAD of origin (unpushed) ... You must verify the merged tree and push before this
# work is real"). It said that, correctly, into a log, and then there was no tool to do it -- so the
# instruction was carried out by hand, or not at all, and a straggler check that passes locally
# proves nothing about what the remote holds.
#
# WHAT VERIFY MEANS HERE, and why it is not "the suite is green". This repository, like most real
# ones, is NOT green: it carries pre-existing failures that belong to no ticket. A gate that
# demands green refuses forever and teaches everyone to pass --force. So the question asked is the
# only one that can be answered honestly and acted on:
#
#     does pushing this INTRODUCE a failure that the published branch does not already have?
#
# Answered by running the gates on HEAD and, only if HEAD is red, on the baseline the remote
# already holds -- and comparing the FAILURE SETS. A red tree that is red in exactly the ways the
# remote is already red is publishable; one new failure is not. Same rule the round verifier uses,
# for the same reason.
#
# USAGE
#   af-verify-and-push.sh <worktree> [--remote origin] [--branch <current>]
#                         [--gate "<command>"]...   (repeatable; else AF_VERIFY_GATES, else detected)
#                         [--dry-run]               (verify and report; never push)
#
# EXIT CODES
#   0  pushed (or --dry-run and it would have)
#   1  usage / not a git worktree / dirty tree
#   2  REFUSED: this push would introduce failures the remote does not have
#   3  REFUSED: no gate could be determined, so "verified" would be a word with nothing behind it
#   4  push itself failed (rejected, no remote, credentials)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WT=""; REMOTE="origin"; BRANCH=""; DRY=0; GATES=()
while [ $# -gt 0 ]; do
  case "$1" in
    --remote) REMOTE="$2"; shift 2;;
    --branch) BRANCH="$2"; shift 2;;
    --gate)   GATES+=("$2"); shift 2;;
    --dry-run) DRY=1; shift;;
    -h|--help) sed -n '1,35p' "$HERE/$(basename "${BASH_SOURCE[0]}")"; exit 1;;
    -*) echo "unknown flag: $1" >&2; exit 1;;
    *) [ -z "$WT" ] && WT="$1" || { echo "unexpected argument: $1" >&2; exit 1; }; shift;;
  esac
done

say(){ printf '[verify-push] %s\n' "$*"; }

[ -n "$WT" ] || { echo "usage: af-verify-and-push.sh <worktree> [--remote R] [--branch B] [--gate CMD]... [--dry-run]" >&2; exit 1; }
cd "$WT" 2>/dev/null || { say "not a directory: $WT"; exit 1; }
git rev-parse --git-dir >/dev/null 2>&1 || { say "not a git worktree: $WT"; exit 1; }

# A dirty tree cannot be verified: the gates would run over files the push will not carry, so a
# green result would be a statement about a tree that never existed anywhere else.
#
# ONE EXEMPTION, and it is not a loosening: `.af-loop.lock` is af-ticket-loop.sh's own bookkeeping,
# rewritten on every run and on every heartbeat. It is untracked on main but a worker committed it
# onto build/research-engine, where it IS tracked -- so for the entire duration of a build the
# worktree reports `M .af-loop.lock` and is never clean. This tool then refuses, correctly by its
# own rule, and the two DEADLOCK: the push half can only run when no loop is running, which is
# exactly when the loop's END-OF-RUN message tells you to run it, and relaunching the loop dirties
# the tree again. Observed three times on 2026-08-24, with the branch 24 commits ahead of origin and
# unpublishable because of it.
#
# Exempting it changes nothing about what is verified: it is not part of the work, no gate reads it,
# and it is excluded from the push by the same reasoning. Untracking it on the build branch is the
# other half of the fix and belongs on that branch at a round boundary; this makes the tool correct
# even where it is still tracked.
AF_LOOP_BOOKKEEPING='^ *[MADRCU?]* *\.af-loop\.lock$'
if [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null | grep -vE "$AF_LOOP_BOOKKEEPING" || true)" ]; then
  say "REFUSING: the worktree has uncommitted tracked changes. Verification would run over a tree"
  say "  that is not what would be pushed. Commit or stash them first:"
  # Same filter as the test above, and `sed -n` rather than `head` so the truncation cannot
  # SIGPIPE git under pipefail. Listing the exempted file as a REASON would send the reader
  # after the one thing this tool deliberately does not care about.
  git status --short --untracked-files=no | grep -vE "$AF_LOOP_BOOKKEEPING" | sed -n '1,10p' | sed 's/^/    /'
  exit 1
fi

[ -n "$BRANCH" ] || BRANCH=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || echo "")
[ -n "$BRANCH" ] || { say "REFUSING: HEAD is detached; name the branch with --branch"; exit 1; }

# ------------------------------------------------------------------------------ what to run ----
if [ "${#GATES[@]}" -eq 0 ] && [ -n "${AF_VERIFY_GATES:-}" ]; then
  # newline-separated, so a gate may contain spaces
  while IFS= read -r line; do [ -n "$line" ] && GATES+=("$line"); done <<< "$AF_VERIFY_GATES"
fi
if [ "${#GATES[@]}" -eq 0 ]; then
  # Detection is a CONVENIENCE, never a fallback to nothing: if it finds no gate the run is refused
  # (exit 3) rather than pushing something described as verified. "Verified" has to mean something
  # ran, or the word does more harm than saying nothing at all.
  [ -f pyproject.toml ] || [ -d tests ] && GATES+=("python -m pytest -q")
  [ -f package.json ] && grep -q '"test"' package.json 2>/dev/null && GATES+=("npm test --silent")
fi
if [ "${#GATES[@]}" -eq 0 ]; then
  say "REFUSING: no gate to run, and nothing was detected in $WT."
  say "  Name one explicitly:  --gate 'python -m pytest -q'   (repeatable)"
  say "  or set AF_VERIFY_GATES to a newline-separated list."
  say "  This is exit 3 and not a push, because a push announced as verified when nothing ran is"
  say "  worse than an unverified push that says so."
  exit 3
fi

# ------------------------------------------------------------------------- run them, twice at most
# One line per failing test, sorted, as the comparable unit. Deliberately conservative parsing: any
# line pytest/npm marks FAILED/ERROR, plus a synthetic marker when a gate dies without naming
# anything, so "the build blew up" can never compare equal to "the build was fine".
# WRITES to a file and RETURNS the status, rather than printing the failures and setting a global.
# `fails=$(run_gates)` runs the function in a command-substitution SUBSHELL, so any status it set on
# a global died with that subshell — under `set -u` that surfaced as "RUN_RC: unbound variable", and
# without `set -u` it would have silently read as green. The same shape as every other bug in this
# tree today: the answer computed correctly and then lost on the way back.
run_gates(){   # $1 = file to write one failure id per line into -> 0 = green, 1 = red
  local outfile="$1" g out rc named combined="" status=0
  for g in "${GATES[@]}"; do
    out=$(eval "$g" 2>&1); rc=$?
    if [ "$rc" != 0 ]; then
      status=1
      named=$(printf '%s\n' "$out" | sed -n 's/^\(FAILED\|ERROR\) \([^ ]*\).*/\2/p' | sort -u)
      # "the build blew up" must never compare equal to "the build was fine", so a gate that fails
      # without naming anything still contributes a comparable identity.
      [ -n "$named" ] || named="<gate exited $rc without naming a test: $g>"
      combined+="$named"$'\n'
    fi
  done
  printf '%s' "$combined" | sed '/^$/d' | sort -u > "$outfile"
  return "$status"
}

say "branch $BRANCH -> $REMOTE, gate(s): ${#GATES[@]}"
for g in "${GATES[@]}"; do say "  gate: $g"; done

head_file=$(mktemp); base_file=$(mktemp)
trap 'rm -f "$head_file" "$base_file"' EXIT
head_rc=0
run_gates "$head_file" || head_rc=1
head_fails=$(cat "$head_file")
if [ "$head_rc" = 0 ]; then
  say "gates GREEN on HEAD — nothing to attribute"
else
  say "gates RED on HEAD: $(printf '%s\n' "$head_fails" | sed '/^$/d' | wc -l) failure(s). Asking whether the REMOTE already has them."
fi

new_fails=""
if [ "$head_rc" != 0 ]; then
  # The baseline is what the remote ALREADY holds. A branch with no remote counterpart yet has no
  # published baseline, so every failure on it is new by definition -- which is the honest reading:
  # nobody has ever accepted these.
  if ! git rev-parse --verify --quiet "$REMOTE/$BRANCH" >/dev/null; then
    say "no $REMOTE/$BRANCH yet — nothing has been published, so every failure counts as introduced"
    new_fails="$head_fails"
  else
    base=$(git rev-parse --short "$REMOTE/$BRANCH")
    say "measuring the baseline at $REMOTE/$BRANCH ($base) in a scratch worktree"
    scratch=$(mktemp -d)
    if git worktree add -q --detach "$scratch" "$REMOTE/$BRANCH" 2>/dev/null; then
      pushd "$scratch" >/dev/null || true
      run_gates "$base_file" || true
      popd >/dev/null || true
      git worktree remove --force "$scratch" >/dev/null 2>&1 || rm -rf "$scratch"
      new_fails=$(comm -23 "$head_file" "$base_file")
      pre=$(comm -12 "$head_file" "$base_file" | wc -l)
      say "pre-existing on $REMOTE/$BRANCH: $pre — these are debt, and not this push's to answer"
    else
      say "could not check out $REMOTE/$BRANCH to measure the baseline; treating every failure as new"
      new_fails="$head_fails"
      rm -rf "$scratch"
    fi
  fi
fi

new_count=$(printf '%s\n' "$new_fails" | sed '/^$/d' | wc -l)
if [ "$new_count" -gt 0 ]; then
  say "REFUSING TO PUSH — this branch introduces $new_count failure(s) that $REMOTE/$BRANCH does not have:"
  printf '%s\n' "$new_fails" | sed '/^$/d' | head -25 | sed 's/^/    /'
  say "  Fix them, or land them on a branch someone reviews. Nothing was pushed; nothing was lost."
  exit 2
fi

# Say a NUMBER or say what is actually true. "? commit(s) to publish" is what this printed against
# a branch with no remote counterpart, which reads like a bug in the tool rather than the fact that
# nothing of this branch has ever been published.
if git rev-parse --verify --quiet "$REMOTE/$BRANCH" >/dev/null; then
  ahead=$(git rev-list --count "$REMOTE/$BRANCH..HEAD" 2>/dev/null || echo 0)
  say "VERIFIED: no failure introduced. $ahead commit(s) to publish."
else
  say "VERIFIED: no failure introduced. $REMOTE has no $BRANCH yet — this publishes the whole branch."
fi
if [ "$DRY" = 1 ]; then
  say "--dry-run: stopping here. This would have run: git push $REMOTE $BRANCH"
  exit 0
fi
if git push "$REMOTE" "$BRANCH"; then
  say "pushed $BRANCH -> $REMOTE. The work is real now."
  exit 0
fi
say "PUSH FAILED — the tree verified, the publish did not. Nothing local changed; re-run after fixing the remote."
exit 4
