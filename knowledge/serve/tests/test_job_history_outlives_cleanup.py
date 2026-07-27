"""Acceptance test for ticket R42 (10eda994c2d74130928775ce9d945444), AE14: "Given a job that
completed, when the operator looks at the box, no session and no job worktree for that job remain,
while the repo's main worktree persists and the job's history in Praxis is intact."

This exercises the full cleanup path end to end — ``JobStore`` (R1's queryable job row),
``SessionLauncher.terminate`` (R39's session reap), and ``box_service_worktree_cleanup.reap_and_cleanup``
(R40/R41's ordered worktree teardown) — together, then asserts every piece of the job's history the
operator can look up is STILL queryable afterward: the job row by id, its terminal state and reason,
and its stored activity tail. Cleanup destroys execution artifacts (the session, the worktree) and
never history (R42).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from knowledge.serve.box_service_activity_tail import ActivityTailStore
from knowledge.serve.box_service_clone import RepoClone
from knowledge.serve.box_service_job_worktree import JobWorktreeManager
from knowledge.serve.box_service_models import JobState
from knowledge.serve.box_service_store import JobStore
from knowledge.serve.box_service_worktree_cleanup import reap_and_cleanup
from knowledge.serve.job_authz import JobPrincipal, PrincipalKind
from knowledge.serve.session_launcher import SessionLauncher


def _git(*args: str, cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout


def _origin_with_one_commit(tmp_path) -> tuple[str, str]:
    origin = str(tmp_path / "origin")
    subprocess.run(["git", "init", "-b", "main", origin], check=True, capture_output=True, text=True)
    _git("config", "user.email", "box@example.com", cwd=origin)
    _git("config", "user.name", "Box Service", cwd=origin)
    (tmp_path / "origin" / "file.txt").write_text("v1\n")
    _git("add", "file.txt", cwd=origin)
    _git("commit", "-m", "first", cwd=origin)
    sha = _git("rev-parse", "HEAD", cwd=origin).strip()
    return origin, sha


def _bare_clone_with_main_worktree(origin: str, tmp_path) -> RepoClone:
    clone_path = str(tmp_path / "box" / "repo.git")
    main_worktree_path = str(tmp_path / "box" / "repo" / "main")
    subprocess.run(
        ["git", "clone", "--bare", origin, clone_path], check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "worktree", "add", main_worktree_path],
        cwd=clone_path, check=True, capture_output=True, text=True,
    )
    return RepoClone(origin_url=origin, clone_path=clone_path, main_worktree_path=main_worktree_path)


def test_job_row_states_terminal_state_and_tail_are_queryable_after_session_and_worktree_cleanup(
    tmp_path,
):
    origin, sha = _origin_with_one_commit(tmp_path)
    repo_clone = _bare_clone_with_main_worktree(origin, tmp_path)

    store = JobStore()
    job = store.create(project="p", snapshot="s")
    job.session_id = "sess-1"
    job.state = JobState.RUNNING
    job.worktree_path = JobWorktreeManager().ensure(repo_clone, job.id, sha).path

    tail_store = ActivityTailStore()

    # The session is reaped (R39) ...
    launcher = SessionLauncher(
        runner=lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr="")
    )
    assert launcher.terminate(job.session_id) is True

    # ... and, now that the job has merged, its worktree is torn down too (R40/R41): the final
    # tail and terminal event persist before the ``git worktree remove`` call.
    reap_and_cleanup(
        job,
        final_tail_chunk="job finished\n",
        tail_store=tail_store,
        terminal_state=JobState.COMPLETED,
        terminal_reason="merged",
        merged=True,
        clone_path=repo_clone.clone_path,
    )

    # Execution artifacts are gone ...
    assert job.worktree_path is None
    assert not Path(repo_clone.clone_path).parent.joinpath("jobs", job.id).exists()
    # ... while the repo's main worktree persists across jobs ...
    assert Path(repo_clone.main_worktree_path).is_dir()

    # ... and the job's history is still queryable, by id, from the SAME store the job was
    # created in — cleanup never removes the row.
    queried = store.get(job.id)
    assert queried is job
    assert queried.state == JobState.COMPLETED
    assert queried.failure_reason == "merged"

    # ... and its stored activity tail is still readable, org-scope authorized, even though the
    # session that produced it is gone and does not need to exist for this read.
    principal = JobPrincipal(kind=PrincipalKind.OPERATOR, id="op-1", org_id=job.org)
    assert tail_store.has_entry(queried.tail_ref)
    tail = tail_store.read(queried, principal, session_alive=False)
    assert "job finished" in tail
