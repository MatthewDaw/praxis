"""Acceptance test for ticket R12 (b9d7b42ec37e4a539fdf2649747dae5d): given a build session
inside a job worktree, a push to any ref fails to authenticate because no credential helper
resolves for the network URL, and the configured push URL for the job namespace points at the
box's local mirror; the box service itself still pushes successfully from the main worktree.

Also covers ticket abcc9694 (R37/R38): the account-wide PAT is fetched per integration rather
than cached for the process's lifetime, so a token revoked or rotated mid-run surfaces at the
next integration as a distinct ``PushCredentialRejectedError``/``FailureClass.PUSH_CREDENTIAL_
REJECTED`` naming the credential, rather than an opaque push error.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from knowledge.serve.box_service_clone import RepoClone
from knowledge.serve.box_service_failures import FailureClass
from knowledge.serve.box_service_job_worktree import JobWorktreeManager
from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.box_service_push_auth import (
    PushAuthError,
    PushCredentialRejectedError,
    authenticated_push_url,
    job_worktree_credential_helper,
    job_worktree_pushurl,
    lock_job_worktree_to_local_mirror,
    push_main_worktree,
)
from knowledge.serve.github_token import fetch_github_token_uncached


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


def test_push_main_worktree_defaults_to_the_uncached_per_integration_token_fetch():
    """The default resolver is NOT the process-lifetime-cached ``resolve_github_token`` — it is
    ``fetch_github_token_uncached``, which hits Secrets Manager fresh on every call, so a token
    rotated or revoked mid-run is picked up at the very next integration."""
    default_resolver = push_main_worktree.__kwdefaults__["token_resolver"]
    assert default_resolver is fetch_github_token_uncached


@dataclass
class _ScriptedRunner:
    """Returns a scripted result for the push call; records what it was invoked with."""

    returncode: int
    stderr: str = ""
    calls: list = field(default_factory=list)

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args=args, returncode=self.returncode, stdout="", stderr=self.stderr)


def _make_repo_clone() -> RepoClone:
    return RepoClone(
        origin_url="https://github.com/acme/widgets.git",
        clone_path="/box/repo.git",
        main_worktree_path="/box/repo/main",
    )


def test_push_main_worktree_raises_distinct_error_and_records_needs_attention_when_remote_rejects_the_credential():
    runner = _ScriptedRunner(
        returncode=1,
        stderr="remote: Invalid username or token.\nfatal: Authentication failed for 'https://github.com/acme/widgets.git/'",
    )
    job = Job(id="job-1", project="p", snapshot="prd-p", state=JobState.RUNNING)

    try:
        push_main_worktree(
            _make_repo_clone(), "main",
            token_resolver=lambda: "revoked-token-value",
            runner=runner,
            job=job,
        )
        raise AssertionError("expected PushCredentialRejectedError")
    except PushCredentialRejectedError as exc:
        assert isinstance(exc, PushAuthError)  # still catchable as the base push-auth error
        assert "praxis/github/token" in str(exc)  # names the credential, not an opaque error

    assert job.state == JobState.NEEDS_ATTENTION
    assert job.failure_reason is not None
    assert job.failure_reason.startswith(FailureClass.PUSH_CREDENTIAL_REJECTED.value)
    assert "praxis/github/token" in job.failure_reason


def test_push_main_worktree_an_unrelated_push_failure_stays_a_plain_push_auth_error():
    """A non-authentication push failure (e.g. a rejected non-fast-forward) must NOT be
    misreported as a credential rejection."""
    runner = _ScriptedRunner(returncode=1, stderr="! [rejected] main -> main (non-fast-forward)")
    job = Job(id="job-2", project="p", snapshot="prd-p", state=JobState.RUNNING)

    try:
        push_main_worktree(
            _make_repo_clone(), "main",
            token_resolver=lambda: "a-valid-token",
            runner=runner,
            job=job,
        )
        raise AssertionError("expected PushAuthError")
    except PushCredentialRejectedError:
        raise AssertionError("a non-fast-forward rejection must not be reported as a credential rejection")
    except PushAuthError:
        pass

    # An unrelated push failure never touches the job's failure state.
    assert job.state == JobState.RUNNING
