"""A completed run must leave ZERO worker branches — not fewer, zero.

Worktree removal deliberately keeps the branch, but nothing ever revisited the branch afterwards, so
one was created per worker, per ticket, per round and lived forever. Measured on
/workspace/appeal_engine: 38 branches for ~11 tickets across three runs, 34 of them fully merged into
main and 4 "unmerged" — one already upstream by patch-id, two attempts the post-merge verifier
rejected and rebuilt, one a stale baseline re-anchor. None held recoverable work.

The cost is the lost signal, not the disk. "Unmerged branch" is supposed to mean THIS WORK NEVER
LANDED; buried under dozens of identical `worktree-agent-*` names it means nothing. `reap_branches`
deletes the residue so that a survivor is real — and hard-fails the round when a ticket reads
`finished` while its commits sit on a branch nothing ever merged.

The functions are lifted out of the script and executed against real scratch repos: these are git
semantics (ancestry vs patch-id, refusal to delete a checked-out branch), and a shape-copy test would
prove nothing about them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"

# say/praxis_q/regress_ticket are the driver's, and stubbed; everything under test is lifted verbatim.
HARNESS = """
set -uo pipefail
say(){ printf 'LOG %s\\n' "$*"; }
praxis_q(){ "$@"; }
# The driver discards praxis_q's stdout and logs its own line, so record the call out of band.
regress_ticket(){ printf 'REGRESS %s %s\\n' "$1" "$2" >> "$PWD/regress.log"; }
PROJECT=demo
WT="$PWD"
INTEGRATION_REF=main
"""


def _extract(*names: str) -> str:
    """Pull whole function definitions out of the driver by name."""
    src = SCRIPT.read_text().splitlines()
    out = []
    for name in names:
        start = next(i for i, l in enumerate(src) if l.startswith(f"{name}(){{"))
        end = next(i for i in range(start + 1, len(src)) if src[i] == "}")
        out.append("\n".join(src[start : end + 1]))
    assert len(out) == len(names)
    return "\n".join(out)


FUNCS = _extract("af_owned_ids", "af_is_worker_branch", "reap_branches")


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def _commit(repo: Path, fname: str, body: str, subject: str) -> str:
    (repo / fname).write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "--short", "HEAD")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _commit(r, "README", "base\n", "chore: base")
    return r


def _reap(repo: Path, known: str = "R8 R9 LADDER-1", finished: str = "") -> tuple[int, str]:
    script = f"""{HARNESS}
AF_KNOWN_IDS=" {known} "
AF_FINISHED_IDS=" {finished} "
{FUNCS}
reap_branches
"""
    r = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, timeout=120
    )
    return r.returncode, r.stdout


def _branches(repo: Path) -> list[str]:
    return sorted(
        _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines()
    )


def _worker_branch(repo: Path, name: str, fname: str, body: str, subject: str) -> None:
    """Build a worker branch the way a round does, and return to main."""
    _git(repo, "checkout", "-q", "-b", name)
    _commit(repo, fname, body, subject)
    _git(repo, "checkout", "-q", "main")


def test_a_merged_worker_branch_is_reaped_and_only_main_survives(repo: Path):
    """The headline invariant: `git branch --list | wc -l` is exactly 1 after a clean run."""
    for i, name in enumerate(["worktree-agent-aaa1", "worktree-agent-bbb2", "worktree-wf_cc3"]):
        _worker_branch(repo, name, f"f{i}.py", f"x = {i}\n", f"feat: thing {i} (R8)")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "--no-edit", "-q", name)

    rc, out = _reap(repo, finished="R8")

    assert rc == 0, out
    assert _branches(repo) == ["main"], "a finished ticket must own no branches"
    assert "3 branches reaped, 0 unmerged branches remain" in out


def test_patch_equivalent_branch_is_reaped_despite_not_being_an_ancestor(repo: Path):
    """`git cherry`, not ancestry. Three of the four appeal_engine survivors looked unmerged for
    exactly this reason: the work was upstream under a different sha."""
    _worker_branch(repo, "worktree-agent-dup", "a.py", "a = 1\n", "feat: a (R8)")
    # The integrator is a DIFFERENT identity from the worker (as in a real landing), and
    # naming it matters twice over: a bare CI runner has no git identity at all, so an
    # undecorated cherry-pick dies with "Committer identity unknown"; and reusing the
    # worker's identity would make the replayed commit byte-identical to the original
    # (same tree, parent, message, author) and collapse it to the SAME sha — destroying
    # the patch-equivalent-but-different-sha condition this test exists to exercise.
    _git(repo, "-c", "user.name=integrator", "-c", "user.email=i@i",
         "cherry-pick", _git(repo, "rev-parse", "worktree-agent-dup"))
    _commit(repo, "later.py", "later = 1\n", "chore: unrelated later commit")
    assert _git(repo, "rev-list", "--count", "main..worktree-agent-dup") == "1"

    rc, out = _reap(repo, finished="R8")

    assert rc == 0, out
    assert _branches(repo) == ["main"]
    assert "already upstream by patch-id" in out
    assert "1 branches reaped, 0 unmerged branches remain" in out


def test_superseded_attempt_is_reaped_and_names_its_replacement(repo: Path):
    """The verifier rejected this attempt and a later round rebuilt it; main carries the correction."""
    _worker_branch(repo, "worktree-agent-old", "a.py", "broken = 1\n", "feat: a (R8)")
    sha = _commit(repo, "a.py", "fixed = 1\n", "feat: a, rebuilt after regression (R8)")

    rc, out = _reap(repo, finished="R8")

    assert rc == 0, out
    assert _branches(repo) == ["main"]
    assert "a superseded attempt" in out and f"R8 superseded by {sha}" in out


def test_finished_ticket_whose_work_never_landed_fails_the_round(repo: Path):
    """The LADDER-1 case: caught by the verifier only by luck, invisible to branch bookkeeping."""
    _worker_branch(repo, "worktree-agent-ladder", "l.py", "l = 1\n", "feat: ladder (LADDER-1)")

    rc, out = _reap(repo, finished="LADDER-1")

    assert rc == 1, "a lying ticket must FAIL the round, not be reported and skimmed"
    assert "worktree-agent-ladder" in _branches(repo), "unique work is never deleted"
    assert "ROUND FAILED" in out
    assert (repo / "regress.log").read_text().strip() == "REGRESS LADDER-1 worktree-agent-ladder"
    assert "regressed LADDER-1 to incomplete" in out
    assert "feat: ladder (LADDER-1)" in out, "the missing commits must be named"
    assert "1 branches reaped, 1 unmerged branches remain: worktree-agent-ladder" not in out
    assert "0 branches reaped, 1 unmerged branches remain: worktree-agent-ladder" in out


def test_unfinished_ticket_with_unique_work_survives_and_is_named(repo: Path):
    """Work still in flight is not residue."""
    _worker_branch(repo, "worktree-agent-live", "n.py", "n = 1\n", "feat: partial (R9)")

    rc, out = _reap(repo, finished="R8")

    assert rc == 0, out
    assert "worktree-agent-live" in _branches(repo)
    assert "R9 is not finished" in out
    assert "0 branches reaped, 1 unmerged branches remain: worktree-agent-live" in out


def test_branch_with_no_owned_ticket_id_survives(repo: Path):
    """Provenance that cannot be established is a reason to keep, never to delete."""
    _worker_branch(repo, "worktree-agent-foreign", "b.py", "b = 1\n", "fix: upstream thing (BES-115)")

    rc, out = _reap(repo, known="R8", finished="R8")

    assert rc == 0, out
    assert "worktree-agent-foreign" in _branches(repo)
    assert "provenance cannot be established" in out


def test_human_branches_are_never_touched(repo: Path):
    """Only refs the factory itself mints are in scope — and `build/<X>` only when X is our ticket."""
    for name, subject in [
        ("fix/mypy-strict-unblock", "fix: mypy (R8)"),
        ("wip/type1-annotations", "wip: annotations (R8)"),
        ("build/login-redesign", "feat: login (R8)"),
        ("build/R8", "feat: old layout (R8)"),
    ]:
        _worker_branch(repo, name, f"{name.replace('/', '_')}.py", "x = 1\n", subject)
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "--no-edit", "-q", name)

    rc, out = _reap(repo, known="R8", finished="R8")

    assert rc == 0, out
    assert _branches(repo) == [
        "build/login-redesign",
        "fix/mypy-strict-unblock",
        "main",
        "wip/type1-annotations",
    ], "merged-looking human branches must survive; only build/<known-ticket> is ours"
    assert "1 branches reaped" in out


def test_a_branch_checked_out_in_a_live_worktree_is_left_alone(repo: Path, tmp_path: Path):
    """A worker's tree is still executing against it. git refuses anyway; skipping keeps the log
    honest, and is why the reap must run AFTER the worktree sweep."""
    _worker_branch(repo, "worktree-agent-busy", "c.py", "c = 1\n", "feat: c (R8)")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "--no-edit", "-q",
         "worktree-agent-busy")
    _git(repo, "worktree", "add", str(tmp_path / "live"), "worktree-agent-busy")

    rc, out = _reap(repo, finished="R8")

    assert rc == 0, out
    assert "worktree-agent-busy" in _branches(repo)
    assert "0 branches reaped, 0 unmerged branches remain" in out, (
        "an in-flight branch is neither reaped nor reported as an orphan"
    )


def test_the_integration_branch_itself_is_never_a_candidate(repo: Path):
    """Even if the checked-out branch were named like a worker's."""
    _git(repo, "checkout", "-q", "-b", "worktree-agent-integration")

    rc, out = _reap(repo, finished="R8")

    assert rc == 0, out
    assert "worktree-agent-integration" in _branches(repo)


def test_detached_head_compares_against_head_not_the_startup_sha(repo: Path):
    """INTEGRATION_REF is a fixed sha on a detached checkout — both build worktrees on the box are —
    and it does NOT move as integrate_round merges. Comparing against it would read every branch the
    round just landed as unique work, and a finished ticket's branch as a hard failure."""
    startup_sha = _git(repo, "rev-parse", "HEAD")
    _worker_branch(repo, "worktree-agent-det", "d.py", "d = 1\n", "feat: d (R8)")
    _git(repo, "checkout", "-q", "--detach", "main")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "--no-edit", "-q",
         "worktree-agent-det")
    assert _git(repo, "rev-parse", "HEAD") != startup_sha

    script = f"""{HARNESS.replace('INTEGRATION_REF=main', f'INTEGRATION_REF={startup_sha}')}
AF_KNOWN_IDS=" R8 "
AF_FINISHED_IDS=" R8 "
{FUNCS}
reap_branches
"""
    r = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, timeout=120
    )

    assert r.returncode == 0, r.stdout + r.stderr
    assert "worktree-agent-det" not in _branches(repo), "the merged branch must still be reaped"
    assert "ROUND FAILED" not in r.stdout


def test_keep_branches_knob_reports_without_deleting(repo: Path):
    _worker_branch(repo, "worktree-agent-keep", "k.py", "k = 1\n", "feat: k (R8)")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "--no-edit", "-q",
         "worktree-agent-keep")

    script = f"""{HARNESS}
AF_KEEP_BRANCHES=1
AF_KNOWN_IDS=" R8 "
AF_FINISHED_IDS=" R8 "
{FUNCS}
reap_branches
"""
    r = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, timeout=120
    )

    assert r.returncode == 0, r.stdout + r.stderr
    assert "worktree-agent-keep" in _branches(repo)
    assert "0 branches reaped, 1 unmerged branches remain" in r.stdout


def test_reaping_is_an_invariant_wired_to_the_exit_trap():
    """Same lesson as the worktree fix in 4b5d743: every abnormal exit — the DEPENDENCY STALL break,
    exits 3/4/5/6, an operator `tmux kill-session` — must still reap and still print the report."""
    src = SCRIPT.read_text()
    trap = src[src.index("af_cleanup_on_exit(){") : src.index("trap af_cleanup_on_exit")]
    assert "reap_branches" in trap, "reaping is not attached to the exit trap"
    assert trap.index("sweep_worktrees") < trap.index("reap_branches"), (
        "git refuses to delete a branch checked out in a worktree, so the sweep must come first"
    )
    assert "trap af_cleanup_on_exit EXIT INT TERM" in src


def test_the_round_reaps_after_it_purges():
    src = SCRIPT.read_text()
    body = src[src.index("  integrate_round\n") :]
    assert body.index("sweep_worktrees") < body.index("reap_branches")
