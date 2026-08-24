"""A ticket branch's own work is not "everything not on the integration ref".

A worktree is created from the repo's DEFAULT branch (refs/remotes/origin/HEAD), not from the
branch the round integrates into — the round prompt says exactly that and tells every worker to
rebase. So a ticket branch necessarily carries every commit the default branch has that the
integration ref does not, and judging its work by `HEAD..$branch` attributes all of them to the
ticket.

Measured 2026-08-24, praxis. `af-build/r4b-20260824` and `af-build/r3a-20260824` each showed
**20 commits in HEAD..branch and ZERO unreachable from main** — every one an unrelated tooling
commit pushed to main while the build ran. The driver reported

    WARNING: STRANDING af-build/r4b-20260824 (20 commit(s)) — no commit subject ends in a praxis
    ticket id, so provenance cannot be established and this work will NOT be merged

which is literally true and entirely misleading: none of it was the ticket's work and nothing was
stranded. Both branches were then queued to the conflict resolver, which would have landed main's
tooling into the integration branch dressed as ticket recovery.

The bug scales with how far the default branch runs ahead of the integration ref — so it is
invisible on a quiet repo and constant on a busy one.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"

_FUNCS = ("af_base_ref", "af_branch_has_own_work", "af_owned_ids")


def _function(name: str) -> str:
    text = SCRIPT.read_text()
    start = text.index(f"\n{name}(){{")
    end = text.index("\n}\n", start)
    return text[start + 1 : end + 3]


def _sh(cmd: str, cwd: Path) -> str:
    return subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main (the default branch) running AHEAD of the integration ref, as it does during a build."""
    r = tmp_path / "r"
    r.mkdir()
    _sh("git init -q -b main && git config user.email t@t && git config user.name t", r)
    (r / "a.txt").write_text("base\n")
    _sh("git add -A && git commit -qm base", r)
    _sh("git checkout -q -b build/research-engine", r)          # the integration ref forks here
    _sh("git checkout -q main", r)
    for i in range(3):                                          # tooling commits land on main
        (r / f"tool{i}.txt").write_text("x\n")
        _sh(f"git add -A && git commit -qm 'fix(tooling): unrelated {i}'", r)
    _sh("git checkout -q build/research-engine", r)
    return r


def _run(repo: Path, body: str) -> subprocess.CompletedProcess[str]:
    program = (
        textwrap.dedent(
            """
            set -uo pipefail
            say(){ echo "$*"; }
            INTEGRATION_REF=build/research-engine
            """
        )
        + "\n".join(_function(f) for f in _FUNCS)
        + "\n" + body
    )
    return subprocess.run(["bash", "-c", program], cwd=repo, capture_output=True, text=True,
                          timeout=60)


def _ticket_branch(repo: Path, name: str, *, own_work: bool) -> None:
    """A worker's branch: cut from the DEFAULT branch, as the real worktree flow does."""
    _sh(f"git checkout -q -b {name} main", repo)
    if own_work:
        (repo / "feature.txt").write_text("the ticket's work\n")
        _sh("git add -A && git commit -qm 'feat(scope): the real work (R4b)'", repo)
    _sh("git checkout -q build/research-engine", repo)


# ------------------------------------------------------------------------------ the regression --

def test_a_branch_carrying_only_base_commits_has_no_work_of_its_own(repo: Path):
    """THE REGRESSION, in its measured shape: commits in HEAD..branch, none of them the ticket's."""
    _ticket_branch(repo, "af-build/r4b", own_work=False)

    total = _sh("git rev-list --count HEAD..af-build/r4b", repo).strip()
    assert total == "3", "precondition: the branch does carry the base's commits"

    res = _run(repo, 'if af_branch_has_own_work af-build/r4b; then echo WORK; else echo NONE; fi')
    assert res.stdout.strip() == "NONE", res.stdout + res.stderr


def test_a_branch_with_real_work_still_has_work(repo: Path):
    """The guard must not make every branch invisible — that would strand real work silently."""
    _ticket_branch(repo, "af-build/r4b", own_work=True)

    res = _run(repo, 'if af_branch_has_own_work af-build/r4b; then echo WORK; else echo NONE; fi')
    assert res.stdout.strip() == "WORK", res.stdout + res.stderr


def test_provenance_reads_only_the_branches_own_commits(repo: Path):
    """The STRANDING warning fires off af_owned_ids. The base ref is passed as its OWN argument —
    smuggling it into the range as extra words would change how every other caller's argument is
    parsed, which broke eight reaping tests when tried."""
    _ticket_branch(repo, "af-build/r4b", own_work=True)

    res = _run(repo, 'af_owned_ids "HEAD..af-build/r4b" " R4b R3a " "$(af_base_ref || true)"')
    assert res.stdout.strip() == "R4b", res.stdout + res.stderr


def test_the_count_the_stranding_warning_reports_excludes_the_base(repo: Path):
    """The mechanism, in the shape that produced the false alarm: the ticket's own commit had
    already landed, only base commits remained, and the warning still counted them and announced
    them as stranded work with no ticket id."""
    _ticket_branch(repo, "af-build/r4b", own_work=False)

    assert _sh("git rev-list --count HEAD..af-build/r4b", repo).strip() == "3"
    own = _run(repo, 'base=$(af_base_ref || true); '
                     'git rev-list --count "HEAD..af-build/r4b" ${base:+"^$base"}')
    assert own.stdout.strip() == "0", own.stdout + own.stderr


# ------------------------------------------------------------------------------- the base ref ---

def test_when_the_integration_ref_is_the_default_branch_nothing_is_excluded(repo: Path):
    """The sports_analysis shape: main IS the integration ref, so there is no base to subtract and
    behaviour must be exactly what it always was."""
    res = _run(repo, 'INTEGRATION_REF=main; if af_base_ref; then echo EXCLUDES; else echo NONE; fi')
    assert res.stdout.strip() == "NONE", res.stdout + res.stderr


def test_an_unanswerable_count_reports_the_branch_rather_than_hiding_it(repo: Path):
    """FAILS SAFE TOWARDS REPORTING. Every caller uses this to decide whether to REPORT or LAND a
    branch, so an unanswerable count must widen the report, never silence it.

    Not hypothetical: when a harness sourced af_stragglers without this predicate, the resulting 127
    made the `|| continue` beside it skip every branch, and the invariant announced
    "straggler invariant HOLDS" with an unmerged branch in plain sight.
    """
    res = _run(repo, 'if af_branch_has_own_work refs/heads/does-not-exist; then echo WORK; '
                     'else echo NONE; fi')
    assert res.stdout.strip() == "WORK", res.stdout + res.stderr
