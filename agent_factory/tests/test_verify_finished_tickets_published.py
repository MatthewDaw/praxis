"""OPS-12 canonical driver: the finished-ticket published-branch audit must be ACTIONABLE.

Regression coverage for the two NOISE classes that drowned the genuine findings (an unexpanded
``$VAR`` path and an absolute since-deleted-worktree path) plus the fail-closed guarantee for a
genuinely never-published check file.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_SCRIPT = (Path(__file__).resolve().parents[1]
           / "scripts" / "checks" / "verify_finished_tickets_on_published_branch.py")
_spec = importlib.util.spec_from_file_location("verify_finished_tickets", _SCRIPT)
verify = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(verify)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo whose ``main`` branch publishes ``tests/test_published.py`` and, at a DIFFERENT
    path, ``src/test_moved.py`` (so a worktree path naming ``tests/test_moved.py`` still resolves by
    basename)."""
    r = tmp_path / "repo"
    (r / "tests").mkdir(parents=True)
    (r / "src").mkdir(parents=True)
    (r / "tests" / "test_published.py").write_text("def test_ok():\n    pass\n")
    (r / "src" / "test_moved.py").write_text("VALUE = 1\n")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "Test")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


def _ticket(check_id: str, run: str, req_id: str = "T1") -> dict:
    return {"requirement_id": req_id, "build_state": "finished",
            "pinned_checks": [{"meta": {"check_id": check_id, "run": run}}]}


def test_unexpanded_var_path_is_labelled_not_absent(repo: Path) -> None:
    """A recorded ``$VAR`` path can never name a real branch blob -- it must be labelled a
    placeholder, NOT reported as never-published (148 of the audit's 265 findings were this)."""
    tickets = [_ticket("c1", "pytest $TESTDIR/tests/test_ghost.py -q")]
    result = verify.audit(tickets, repo, "main")
    assert result["never_published"] == []
    assert len(result["placeholder"]) == 1
    assert "$VAR" in result["placeholder"][0]


def test_deleted_worktree_absolute_path_resolves_by_suffix(repo: Path) -> None:
    """An absolute path inside a since-deleted worktree resolves to its repo-relative suffix and,
    when that suffix IS published, is not flagged (102 of the 265 findings were this)."""
    wt = "/workspace/proj/.claude/worktrees/agent-xyz/tests/test_published.py"
    tickets = [_ticket("c2", f"pytest {wt} -q")]
    result = verify.audit(tickets, repo, "main")
    assert result["never_published"] == []


def test_worktree_path_resolves_by_basename_when_file_relocated(repo: Path) -> None:
    """When the repo-relative suffix has moved but the file is still published elsewhere, the
    basename lane resolves it -- a relocation is not a never-published gap."""
    wt = "/workspace/proj/.claude/worktrees/agent-abc/tests/test_moved.py"
    tickets = [_ticket("c3", f"pytest {wt} -q")]
    result = verify.audit(tickets, repo, "main")
    assert result["never_published"] == []


def test_genuinely_never_published_file_is_flagged_fail_closed(repo: Path) -> None:
    """The high-signal case: a finished ticket whose pinned check names a file that is truly not on
    the branch (and not resolvable by suffix or basename) must be flagged -- fail closed."""
    tickets = [_ticket("c4", "pytest tests/test_never_published.py -q", req_id="OBS-11")]
    result = verify.audit(tickets, repo, "main")
    assert len(result["never_published"]) == 1
    assert "OBS-11" in result["never_published"][0]
    assert "tests/test_never_published.py" in result["never_published"][0]


def test_main_exits_1_only_on_a_genuine_gap_amid_noise(repo: Path, tmp_path: Path) -> None:
    """End to end: a mixed set (placeholder + resolved worktree + one genuine gap) exits 1, and the
    genuine gap is the ONLY thing on the failing channel -- the noise does not gate."""
    tickets = [
        _ticket("c1", "pytest $TESTDIR/tests/test_ghost.py -q", req_id="NOISE-1"),
        _ticket("c2", "pytest /workspace/p/.claude/worktrees/w/tests/test_published.py -q",
                req_id="NOISE-2"),
        _ticket("c4", "pytest tests/test_never_published.py -q", req_id="OBS-11"),
    ]
    tj = tmp_path / "tickets.json"
    tj.write_text(json.dumps(tickets))
    rc = verify.main(["--project", "demo", "--branch", "main",
                      "--repo-root", str(repo), "--tickets-json", str(tj)])
    assert rc == 1


def test_main_exits_0_when_only_noise(repo: Path, tmp_path: Path) -> None:
    tickets = [
        _ticket("c1", "pytest $TESTDIR/tests/test_ghost.py -q", req_id="NOISE-1"),
        _ticket("c2", "pytest /workspace/p/.claude/worktrees/w/tests/test_published.py -q",
                req_id="NOISE-2"),
    ]
    tj = tmp_path / "tickets.json"
    tj.write_text(json.dumps(tickets))
    rc = verify.main(["--project", "demo", "--branch", "main",
                      "--repo-root", str(repo), "--tickets-json", str(tj)])
    assert rc == 0
