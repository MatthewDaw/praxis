"""Acceptance test for ticket R12 (b9d7b42ec37e4a539fdf2649747dae5d): given a build session
inside a job worktree, a push to any ref fails to authenticate because no credential helper
resolves for the network URL, and the configured push URL for the job namespace points at the
box's local mirror; the box service itself still pushes successfully from the main worktree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from knowledge.serve.box_service_clone import RepoClone
from knowledge.serve.box_service_job_worktree import JobWorktreeManager
from knowledge.serve.box_service_push_auth import (
    PushAuthError,
    authenticated_push_url,
    job_worktree_credential_helper,
    job_worktree_pushurl,
    lock_job_worktree_to_local_mirror,
    push_main_worktree,
)


def _git(*args: str, cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout


def _network_origin_with_one_commit(tmp_path) -> str:
    """A throwaway repo standing in for the real, network-hosted origin."""
    origin = str(tmp_path / "network-origin")
    subprocess.run(["git", "init", "--bare", "-b", "main", origin], check=True, capture_output=True, text=True)
    seed = str(tmp_path / "seed")
    subprocess.run(["git", "clone", origin, seed], check=True, capture_output=True, text=True)
    _git("config", "user.email", "box@example.com", cwd=seed)
    _git("config", "user.name", "Box Service", cwd=seed)
    (tmp_path / "seed" / "file.txt").write_text("v1\n")
    _git("add", "file.txt", cwd=seed)
    _git("commit", "-m", "first", cwd=seed)
    _git("push", "origin", "main", cwd=seed)
    return origin


def _bare_clone(origin: str, tmp_path) -> RepoClone:
    clone_path = str(tmp_path / "box" / "repo.git")
    subprocess.run(
        ["git", "clone", "--bare", origin, clone_path], check=True, capture_output=True, text=True
    )
    return RepoClone(
        origin_url=origin,
        clone_path=clone_path,
        main_worktree_path=str(tmp_path / "box" / "repo" / "main"),
    )


def test_authenticated_push_url_embeds_the_token_for_a_real_network_url():
    url = authenticated_push_url("https://github.com/acme/widgets.git", "s3cr3t")
    assert url == "https://x-access-token:s3cr3t@github.com/acme/widgets.git"


def test_authenticated_push_url_leaves_a_hostless_local_path_unchanged():
    # A bare local filesystem path (what these tests use as an origin stand-in) has no network
    # authority to embed a credential into.
    assert authenticated_push_url("/tmp/some/local/repo.git", "s3cr3t") == "/tmp/some/local/repo.git"


def test_job_worktree_push_lands_only_in_the_local_mirror_never_the_network_origin(tmp_path):
    origin = _network_origin_with_one_commit(tmp_path)
    repo_clone = _bare_clone(origin, tmp_path)
    build_base_sha = _git("rev-parse", "main", cwd=repo_clone.clone_path).strip()

    job = JobWorktreeManager().ensure(repo_clone, "job-1", build_base_sha)
    lock_job_worktree_to_local_mirror(job, repo_clone)

    # The job namespace's configured push URL points at the box's local mirror, not the network.
    assert job_worktree_pushurl(job.path) == repo_clone.clone_path
    assert job_worktree_pushurl(job.path) != origin

    # No credential helper resolves inside the job worktree for a push aimed at the network URL.
    assert job_worktree_credential_helper(job.path) == ""

    # A real push from the job worktree (git's default remote+branch resolution) lands in the
    # local mirror only — the network origin never sees it.
    _git("checkout", "-b", "jobs/job-1", cwd=job.path)
    Path(job.path, "job.txt").write_text("job work\n")
    _git("add", "job.txt", cwd=job.path)
    _git("commit", "-m", "job work", cwd=job.path)
    _git("push", "origin", "jobs/job-1", cwd=job.path)

    # `repo_clone.clone_path` is itself bare, so a push into it lands as an ordinary branch ref
    # there — this is "the box's local mirror" the job namespace's push URL points at.
    mirror_refs = _git("branch", cwd=repo_clone.clone_path)
    assert "jobs/job-1" in mirror_refs

    network_refs = _git("branch", "-a", cwd=origin)
    assert "jobs/job-1" not in network_refs


def test_main_worktree_push_reaches_the_network_origin_authenticated_with_a_resolved_token(
    tmp_path,
):
    origin = _network_origin_with_one_commit(tmp_path)
    repo_clone = _bare_clone(origin, tmp_path)
    subprocess.run(
        ["git", "worktree", "add", repo_clone.main_worktree_path, "main"],
        cwd=repo_clone.clone_path,
        check=True,
        capture_output=True,
        text=True,
    )
    _git("config", "user.email", "box@example.com", cwd=repo_clone.main_worktree_path)
    _git("config", "user.name", "Box Service", cwd=repo_clone.main_worktree_path)
    (tmp_path / "box" / "repo" / "main" / "integrated.txt").write_text("integrated\n")
    _git("add", "integrated.txt", cwd=repo_clone.main_worktree_path)
    _git("commit", "-m", "integration commit", cwd=repo_clone.main_worktree_path)

    push_main_worktree(repo_clone, "main", token_resolver=lambda: "fake-token-value")

    network_refs = _git("log", "main", "--oneline", cwd=origin)
    assert "integration commit" in network_refs


def test_main_worktree_push_refuses_rather_than_push_unauthenticated_when_no_token_resolves(
    tmp_path,
):
    origin = _network_origin_with_one_commit(tmp_path)
    repo_clone = _bare_clone(origin, tmp_path)

    try:
        push_main_worktree(repo_clone, "main", token_resolver=lambda: None)
        raise AssertionError("expected PushAuthError")
    except PushAuthError as exc:
        assert "no GitHub token" in str(exc)
