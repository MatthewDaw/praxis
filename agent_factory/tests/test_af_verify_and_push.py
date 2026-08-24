"""The loop never pushes, so drift is the default. This is the tool that closes it.

af-ticket-loop.sh deliberately never pushes -- an unattended loop must not publish work nobody has
looked at. The cost is that every run ends with its integration branch ahead of the remote, and the
loop's own end-of-run summary says exactly that: "N commit(s) AHEAD of origin (unpushed) ... You
must verify the merged tree and push before this work is real." It said it into a log, and there
was no tool to do it.

The interesting design question is what "verified" may mean. Demanding a green suite refuses
forever on any real repository -- this one carries two dozen pre-existing failures belonging to no
ticket -- and a gate that always refuses teaches everyone to bypass it. So the question asked is
the only one that is both answerable and actionable: does pushing INTRODUCE a failure the published
branch does not already have?
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-verify-and-push.sh"


def _sh(cmd: str, cwd: Path) -> str:
    return subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout with a real remote, so 'what the remote already has' is a fact and not a mock."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _sh(f"git init -q --bare {origin}", tmp_path)
    _sh(f"git clone -q {origin} {work}", tmp_path)
    _sh("git config user.email t@t && git config user.name t", work)
    (work / "keep.txt").write_text("base\n")
    _sh("git add -A && git commit -qm base && git push -q origin HEAD:refs/heads/main", work)
    _sh("git branch -M main && git branch --set-upstream-to=origin/main main 2>/dev/null || true", work)
    _sh("git fetch -q origin", work)
    return work, origin


def _gate(script: str) -> str:
    """A gate that speaks pytest's FAILED vocabulary, without needing pytest."""
    return f"bash -c {script!r}"


def _run(work: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), str(work), *args],
                          capture_output=True, text=True, timeout=300)


def _remote_head(origin: Path) -> str:
    return subprocess.run(["git", "--git-dir", str(origin), "rev-parse", "main"],
                          capture_output=True, text=True).stdout.strip()


GREEN = _gate("exit 0")
FAILS_A = _gate("echo 'FAILED tests/test_a.py::test_one'; exit 1")
FAILS_AB = _gate("echo 'FAILED tests/test_a.py::test_one'; echo 'FAILED tests/test_b.py::test_two'; exit 1")


# ------------------------------------------------------------------------------ the happy path --

def test_a_green_tree_is_verified_and_published(repo):
    work, origin = repo
    before = _remote_head(origin)
    (work / "new.txt").write_text("work\n")
    _sh("git add -A && git commit -qm 'feat: something (R0a)'", work)

    res = _run(work, "--gate", GREEN)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "VERIFIED" in res.stdout
    assert _remote_head(origin) != before, "the commit should have been published"


def test_dry_run_verifies_and_publishes_nothing(repo):
    work, origin = repo
    before = _remote_head(origin)
    (work / "new.txt").write_text("work\n")
    _sh("git add -A && git commit -qm 'feat: something (R0a)'", work)

    res = _run(work, "--gate", GREEN, "--dry-run")

    assert res.returncode == 0
    assert "would have run" in res.stdout
    assert _remote_head(origin) == before


# --------------------------------------------------------------------------- the differential ----

def test_a_failure_the_remote_already_has_does_not_block_the_push(repo):
    """THE POINT. A tree red in exactly the ways the remote is already red is publishable —
    otherwise pre-existing debt makes the gate permanently unpassable and everyone learns to skip
    it."""
    work, origin = repo
    before = _remote_head(origin)
    (work / "new.txt").write_text("work\n")
    _sh("git add -A && git commit -qm 'feat: something (R0a)'", work)

    res = _run(work, "--gate", FAILS_A)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "pre-existing" in res.stdout
    assert "VERIFIED" in res.stdout
    assert _remote_head(origin) != before


def test_a_failure_only_this_commit_produces_refuses_the_push(repo):
    """A gate whose result depends on the TREE, so HEAD and the baseline genuinely differ."""
    work, origin = repo
    before = _remote_head(origin)
    gate = _gate(
        'if [ -f broke.txt ]; then echo "FAILED tests/test_new.py::test_regression"; exit 1; fi; exit 0'
    )
    (work / "broke.txt").write_text("this commit breaks something\n")
    _sh("git add -A && git commit -qm 'feat: breaks it (R0a)'", work)

    res = _run(work, "--gate", gate)

    assert res.returncode == 2, res.stdout + res.stderr
    assert "REFUSING TO PUSH" in res.stdout
    assert "test_new.py::test_regression" in res.stdout
    assert _remote_head(origin) == before, "nothing may be published when a failure is introduced"


def test_a_gate_that_dies_without_naming_a_test_still_counts(repo):
    """'The build blew up' must never compare equal to 'the build was fine'."""
    work, origin = repo
    before = _remote_head(origin)
    gate = _gate('if [ -f broke.txt ]; then echo "Segmentation fault"; exit 139; fi; exit 0')
    (work / "broke.txt").write_text("boom\n")
    _sh("git add -A && git commit -qm 'feat: boom (R0a)'", work)

    res = _run(work, "--gate", gate)

    assert res.returncode == 2, res.stdout + res.stderr
    assert "without naming a test" in res.stdout
    assert _remote_head(origin) == before


# ------------------------------------------------------------------------------- the refusals ----

def test_no_determinable_gate_refuses_rather_than_pushing_unverified(tmp_path: Path):
    """"Verified" has to mean something ran. A push announced as verified when nothing ran is worse
    than an unverified push that says so."""
    work = tmp_path / "bare-project"
    work.mkdir()
    _sh("git init -q && git config user.email t@t && git config user.name t", work)
    (work / "a.md").write_text("no toolchain here\n")
    _sh("git add -A && git commit -qm base", work)

    res = _run(work)

    assert res.returncode == 3, res.stdout + res.stderr
    assert "no gate to run" in res.stdout


def test_a_dirty_tree_is_refused_before_anything_runs(repo):
    """Gates over uncommitted files describe a tree that exists nowhere else."""
    work, origin = repo
    before = _remote_head(origin)
    (work / "keep.txt").write_text("uncommitted edit\n")

    res = _run(work, "--gate", GREEN)

    assert res.returncode == 1
    assert "uncommitted" in res.stdout
    assert _remote_head(origin) == before


def test_a_detached_head_is_refused(repo):
    work, _origin = repo
    _sh("git checkout -q --detach", work)

    res = _run(work, "--gate", GREEN)

    assert res.returncode == 1
    assert "detached" in res.stdout


def test_an_unpublished_branch_treats_every_failure_as_introduced(repo):
    """Nothing has ever accepted these, so calling them pre-existing would be inventing a baseline."""
    work, _origin = repo
    _sh("git checkout -q -b brand-new", work)
    (work / "x.txt").write_text("x\n")
    _sh("git add -A && git commit -qm 'feat: x (R0a)'", work)

    res = _run(work, "--gate", FAILS_A)

    assert res.returncode == 2, res.stdout + res.stderr
    assert "nothing has been published" in res.stdout


# ------------------------------------------------------- the deadlock with the loop's own lock ---

def test_the_loops_lock_file_does_not_count_as_a_dirty_tree(repo):
    """THE DEADLOCK. `.af-loop.lock` is af-ticket-loop.sh's bookkeeping, rewritten every run and
    every heartbeat. It is untracked on main but a worker committed it onto build/research-engine,
    so for the whole duration of a build the worktree reports `M .af-loop.lock` and is never clean.

    This tool then refuses -- correctly by its own rule -- and the two deadlock: the push half can
    only run when no loop is running, which is exactly when the loop's END-OF-RUN message tells you
    to run it, and relaunching the loop dirties the tree again. Observed three times on 2026-08-24
    with the branch 24 commits ahead and unpublishable because of it.
    """
    work, origin = repo
    before = _remote_head(origin)
    (work / ".af-loop.lock").write_text("12345 demo 5432\n")
    _sh("git add -A && git commit -qm 'chore: track the lock the way the build branch does'", work)
    _sh("git push -q origin main", work)
    # ...and now the running loop rewrites it, exactly as it does on every heartbeat.
    (work / ".af-loop.lock").write_text("67890 demo 5432\n")
    (work / "real.txt").write_text("the work being published\n")
    _sh("git add real.txt && git commit -qm 'feat: real work (R0a)'", work)

    res = _run(work, "--gate", GREEN)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "uncommitted" not in res.stdout
    assert _remote_head(origin) != before


def test_a_genuinely_dirty_tree_is_still_refused_alongside_the_lock(repo):
    """The exemption must be exactly one file wide — otherwise it is a hole, not an exemption."""
    work, origin = repo
    (work / ".af-loop.lock").write_text("1 demo 1\n")
    (work / "src.txt").write_text("committed\n")
    _sh("git add -A && git commit -qm base2 && git push -q origin main", work)
    before = _remote_head(origin)   # AFTER the setup push, or this asserts against the setup
    (work / ".af-loop.lock").write_text("2 demo 1\n")
    (work / "src.txt").write_text("EDITED BUT NOT COMMITTED\n")

    res = _run(work, "--gate", GREEN)

    assert res.returncode == 1, res.stdout + res.stderr
    assert "uncommitted" in res.stdout
    assert "src.txt" in res.stdout
    assert ".af-loop.lock" not in res.stdout, "the exempted file must not be listed as a reason"
    assert _remote_head(origin) == before
