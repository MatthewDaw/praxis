"""Per-job worktree (R11): each job builds in its own worktree, created off
the repo's clone at the job's ``build_base_sha``, so concurrent jobs on the
same repo never share a working tree and never observe each other's
uncommitted files.

Distinct from ``box_service_clone.RepoCloneManager`` (the repo's single main
worktree, R10, the integration/push point) and ``dispatch.dispatch_job``
(which resolves ``build_base_sha`` at dispatch time, R7): this module only
creates/locates the per-job worktree that consumes those two outputs. Job
worktrees live under the repo clone's own directory, keyed by job id, so two
concurrent jobs against the same repo always resolve to two distinct paths
(R11, R12 — no network remote of their own, since they hang off the box's
local bare clone).

Every git call routes through an injectable ``runner`` — same call signature
as ``subprocess.run`` — mirroring the seam used by ``session_launcher`` and
``box_service_clone``, so the "distinct path per job, pinned to its own SHA"
contract is assertable without a real git remote.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from knowledge.serve.box_service_clone import RepoClone

#: Same shape as ``subprocess.run`` — the seam a fake runner replaces in tests.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]
PathExists = Callable[[str], bool]


class JobWorktreeError(RuntimeError):
    """Raised when the underlying ``git worktree add`` call fails. Never
    silently swallowed (R17: refuse rather than degrade)."""


@dataclass(frozen=True)
class JobWorktree:
    """Where one job's own worktree lives, and the SHA it was checked out at
    (R11)."""

    job_id: str
    path: str
    build_base_sha: str


class JobWorktreeManager:
    """Ensures each job has its own worktree off the repo's clone (R10's
    output), checked out at that job's ``build_base_sha`` (R7's output).
    Distinct job ids always resolve to distinct paths under the same repo
    clone, so concurrent jobs never share a working tree."""

    def __init__(
        self, *, runner: Runner = subprocess.run, path_exists: PathExists | None = None
    ) -> None:
        self._runner = runner
        self._path_exists = path_exists or os.path.isdir

    def _path_for(self, repo_clone: RepoClone, job_id: str) -> str:
        jobs_root = os.path.join(os.path.dirname(repo_clone.main_worktree_path), "jobs")
        return os.path.join(jobs_root, job_id)

    def ensure(self, repo_clone: RepoClone, job_id: str, build_base_sha: str) -> JobWorktree:
        """Return the job's own worktree, checked out at ``build_base_sha``.

        If the job's worktree path already exists on disk (e.g. a resumed
        job, R29), it is returned unchanged — no second ``git worktree add``
        is attempted. Otherwise a new worktree is added at that path from
        ``repo_clone``'s bare clone, detached at ``build_base_sha``.
        """
        path = self._path_for(repo_clone, job_id)
        if self._path_exists(path):
            return JobWorktree(job_id=job_id, path=path, build_base_sha=build_base_sha)

        proc = self._runner(
            ["git", "worktree", "add", "--detach", path, build_base_sha],
            cwd=repo_clone.clone_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise JobWorktreeError(
                f"job worktree creation failed for job {job_id!r} at "
                f"{build_base_sha}: {proc.stderr.strip()}"
            )
        return JobWorktree(job_id=job_id, path=path, build_base_sha=build_base_sha)
