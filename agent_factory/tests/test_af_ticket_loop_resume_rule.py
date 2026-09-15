"""A timed-out ticket's retry must RESUME from the earlier attempt, not restart from scratch.

mvpvue T03 (2026-09-14): each retry cut a fresh tree from the integration ref, never looked at the
previous attempt's branch or its preserved uncommitted edits, redid the same work and hit the same
deadline. af_resume_rule points the retry's worker at the newest unmerged ref carrying the ticket id.
These tests run the SHIPPED function against a real repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def _function(name: str) -> str:
    text = SCRIPT.read_text()
    start = text.index(f"\n{name}(){{")
    end = text.index("\n}\n", start)
    return text[start + 1 : end + 3]


def _sh(cmd: str, cwd: Path) -> str:
    return subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def _repo(tmp: Path) -> Path:
    r = tmp / "repo"
    r.mkdir()
    _sh("git init -q -b build && git config user.email t@t && git config user.name t "
        "&& echo base > a.txt && git add a.txt && git commit -q -m base", r)
    return r


def _rule(repo: Path, *ids: str, max_age: str = "86400") -> str:
    prog = (f"WT={repo}; INTEGRATION_REF=build; AF_RESUME_MAX_AGE_S={max_age}\n"
            + _function("af_resume_rule") + "\naf_resume_rule " + " ".join(ids))
    return _sh(prog, repo)


def _attempt(repo: Path, ref: str) -> None:
    """An earlier attempt: a commit that is NOT on the integration branch, published at ``ref``."""
    _sh("git checkout -q -b tmp-attempt && echo partial > b.txt && git add b.txt "
        f"&& git commit -q -m 'partial T03 work' && git update-ref {ref} HEAD "
        "&& git checkout -q build && git branch -q -D tmp-attempt", repo)


def test_a_preserved_snapshot_is_offered_as_the_resume_point(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _attempt(repo, "refs/af-preserved/af-build-proj-T03-1789400000")
    out = _rule(repo, "T03")
    assert "RESUME, DO NOT RESTART, for ticket T03" in out
    assert "refs/af-preserved/af-build-proj-T03-1789400000" in out
    assert "git worktree add -b <your-branch> <path> refs/af-preserved/af-build-proj-T03-1789400000" in out
    assert "partial T03 work" in out


def test_an_unmerged_worker_branch_is_offered_too(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _attempt(repo, "refs/heads/af-build/proj-T03")
    assert "refs/heads/af-build/proj-T03" in _rule(repo, "T03")


def test_the_id_must_match_exactly(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _attempt(repo, "refs/af-preserved/af-build-proj-T03B-1789400000")
    assert _rule(repo, "T03") == ""
    assert "T03B" in _rule(repo, "T03B")


def test_merged_or_absent_work_gives_no_resume(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _sh("git update-ref refs/af-preserved/af-build-proj-T03-1789400000 HEAD", repo)  # already on build
    assert _rule(repo, "T03", "T04") == ""


def test_stale_work_is_not_offered(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _attempt(repo, "refs/af-preserved/af-build-proj-T03-1789400000")
    assert _rule(repo, "T03", max_age="-1") == ""


def test_the_rule_reaches_the_worker_prompt() -> None:
    text = SCRIPT.read_text()
    assert 'RESUME_RULE="$(af_resume_rule ${ids_csv//,/ })"' in text
    assert 'SWEEP_AMENDMENT="$SWEEP_AMENDMENT$RESUME_RULE"' in text


def test_work_is_found_by_the_ticket_id_in_its_commit_subjects(tmp_path: Path) -> None:
    """Grok clone salvage lands on worktree-agent-salvage-* -- no id in the name, only in the commits."""
    repo = _repo(tmp_path)
    _sh("git checkout -q -b tmp && echo w > c.txt && git add c.txt && git commit -q -m 'feat: data layer (T03)' "
        "&& git update-ref refs/heads/worktree-agent-salvage-subagent-1 HEAD && git checkout -q build "
        "&& git branch -q -D tmp", repo)
    out = _rule(repo, "T03")
    assert "refs/heads/worktree-agent-salvage-subagent-1" in out
    assert _rule(repo, "T0") == ""
