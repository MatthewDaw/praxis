"""A foreign tracker id must not veto integration of a finished ticket's branch.

`integrate_round` decides whether a worker branch is safe to merge by reading requirement ids out of
the commit subjects in `HEAD..$branch` and requiring each one to be `finished` in Praxis. The "(ID)"
suffix is a convention the whole world shares, not a factory signature -- a host repo's own history
is full of "(BES-115)"-style tracker ids, and that history lands in the range whenever the
integration branch has drifted behind the base workers branch from.

Before the `known_ids` filter, every such id was asked about as if it were a ticket, answered "not
finished" because Praxis had never heard of it, and vetoed the merge. Observed 2026-07-31 on
proposed-side-buildout: `consolidate/all-work` sat 351 commits behind upstream, so each worker branch
dragged in ~26 BES-* ids, every branch was skipped as unproven, and four green rounds landed nothing
while Praxis went on reporting the tickets finished.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"

# The id scan as it appears in integrate_round: trailing "(ID)" on a commit subject.
SCAN = r"""sed -n 's/.*(\([A-Za-z][A-Za-z0-9_-]*[0-9][0-9]*\))[[:space:]]*$/\1/p' | sort -u"""

# The filter under test, lifted verbatim in shape from integrate_round.
FILTER = """
ids=""
for i in $raw; do
  case "$known" in *" $i "*) ids="${ids:+$ids }$i" ;; esac
done
"""


def _run(subjects: list[str], known: str, fin: str) -> tuple[str, str]:
    """Return (kept_ids, verdict) for a branch whose commits have these subjects."""
    script = f"""
set -euo pipefail
known=" {known} "
fin=" {fin} "
raw=$(printf '%s\\n' {" ".join(repr(s) for s in subjects)} | {SCAN})
{FILTER}
if [ -z "$ids" ]; then echo "IDS="; echo "VERDICT=skip-no-ticket"; exit 0; fi
ok=1
for i in $ids; do
  case "$fin" in *" $i "*) ;; *) ok=0 ;; esac
done
echo "IDS=$ids"
[ "$ok" = "1" ] && echo "VERDICT=merge" || echo "VERDICT=skip-unfinished"
"""
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = dict(line.split("=", 1) for line in r.stdout.strip().splitlines())
    return out["IDS"], out["VERDICT"]


def test_foreign_tracker_ids_do_not_veto_a_finished_ticket():
    """The regression: 351 commits of host history alongside one real ticket commit."""
    subjects = [
        "feat(api): structured closed-vocabulary preference fields on profile + score (R35)",
        "fix(api): tighten consent flag rollout (BES-115)",
        "chore(ops): rotate dev flags (BES-118)",
        "docs(adr): ADR 0006 asserts a BAA that was never signed",
    ]
    ids, verdict = _run(subjects, known="R35 R41 R44", fin="R35")
    assert ids == "R35", f"foreign ids leaked into the gate: {ids}"
    assert verdict == "merge"


def test_owned_but_unfinished_ticket_still_vetoes():
    """The safety property the check exists for must survive the fix."""
    subjects = [
        "feat(api): half-built thing (R41)",
        "chore: unrelated host commit (BES-115)",
    ]
    ids, verdict = _run(subjects, known="R35 R41", fin="R35")
    assert ids == "R41"
    assert verdict == "skip-unfinished"


def test_branch_naming_only_foreign_ids_is_unproven():
    """No id we own means provenance cannot be established -- skip, never merge blind."""
    subjects = ["fix(api): something upstream (BES-115)", "Merge pull request #150 from org/branch"]
    ids, verdict = _run(subjects, known="R35 R41", fin="R35 R41")
    assert ids == ""
    assert verdict == "skip-no-ticket"


def test_mixed_owned_ids_require_all_finished():
    subjects = [
        "feat: part one (R35)",
        "feat: part two (R44)",
        "chore: host noise (BES-9)",
    ]
    ids, verdict = _run(subjects, known="R35 R44", fin="R35")
    assert ids == "R35 R44"
    assert verdict == "skip-unfinished", "an unfinished sibling id must still block"

    _, verdict_ok = _run(subjects, known="R35 R44", fin="R35 R44")
    assert verdict_ok == "merge"


def test_substring_ids_are_not_confused():
    """R4 must not match R41: the space-delimited membership test has to be exact."""
    subjects = ["feat: thing (R4)"]
    ids, verdict = _run(subjects, known="R41 R44", fin="R41 R44")
    assert ids == "", "R4 is not a known ticket and must not be admitted via substring match"
    assert verdict == "skip-no-ticket"


@pytest.mark.parametrize("fn", ["known_ids", "finished_ids"])
def test_provenance_queries_are_outage_guarded(fn):
    """Both reads feed a merge decision, so an outage must abort integration, never silently
    produce an empty id set -- an empty `known` would skip every branch as unproven."""
    src = SCRIPT.read_text()
    assert f"praxis_q {fn}" in src, f"{fn} is not routed through praxis_q"


def test_integration_aborts_rather_than_merging_on_praxis_outage():
    src = SCRIPT.read_text()
    assert "SKIPPING INTEGRATION this round" in src
    # The guard must precede the merge loop, not follow it.
    assert src.index("SKIPPING INTEGRATION this round") < src.index("git merge --no-edit")
