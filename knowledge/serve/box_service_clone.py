"""Per-repo clone-on-first-sight (R10): the box keeps ONE clone per repo, with
a checked-out main worktree, created the first time a job arrives for that
repo — never re-created on a later job for the same repo. The main worktree
is the repo's single integration and push point (R32-R34); job worktrees are
created separately from this clone (R11, R12) and are not this module's
concern.

Filesystem existence is the source of truth for "has the box seen this repo
before" — not an in-memory registry — because the box service is a long-lived
process that can restart (R43); a registry that lived only in memory would
re-clone on every restart even though the clone is still on disk.

Every git call routes through an injectable ``runner`` — same call signature
as ``subprocess.run`` — mirroring ``session_launcher.SessionLauncher``'s seam,
so the "clone on first sight, never twice" contract is assertable without a
real git remote or filesystem (a fake ``path_exists`` stands in for disk
state).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

#: Same shape as ``subprocess.run`` — the seam a fake runner replaces in tests.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]
PathExists = Callable[[str], bool]


class RepoCloneError(RuntimeError):
    """Raised when the underlying git call fails. Never silently swallowed
    (R17: refuse rather than degrade)."""


@dataclass(frozen=True)
class RepoClone:
    """Where a repo's local clone and checked-out main worktree live on the
    box (R10)."""

    origin_url: str
    clone_path: str
    main_worktree_path: str


def repo_slug(origin_url: str) -> str:
    """A stable, filesystem-safe identifier for ``origin_url``, so the same
    repo always resolves to the same on-disk paths across restarts."""
    return hashlib.sha256(origin_url.encode("utf-8")).hexdigest()[:16]


class RepoCloneManager:
    """Ensures exactly one clone + checked-out main worktree exists per repo
    (R10), created lazily on first sight of a job for that repo."""

    def __init__(
        self,
        clones_root: str,
        *,
        runner: Runner = subprocess.run,
        path_exists: PathExists | None = None,
    ) -> None:
        self._clones_root = clones_root
        self._runner = runner
        self._path_exists = path_exists or os.path.isdir

    def _paths_for(self, origin_url: str) -> RepoClone:
        slug = repo_slug(origin_url)
        return RepoClone(
            origin_url=origin_url,
            clone_path=os.path.join(self._clones_root, f"{slug}.git"),
            main_worktree_path=os.path.join(self._clones_root, slug, "main"),
        )

    def ensure(self, origin_url: str) -> tuple[RepoClone, bool]:
        """Return the :class:`RepoClone` for ``origin_url`` and whether a
        NEW clone + main worktree was just created.

        If the main worktree already exists on disk, the box has seen this
        repo before: the existing paths are returned unchanged and
        ``created`` is ``False`` — no second clone is made. Otherwise a bare
        clone of ``origin_url`` is made and its main worktree is checked out,
        both for the first time, and ``created`` is ``True``.
        """
        paths = self._paths_for(origin_url)
        if self._path_exists(paths.main_worktree_path):
            return paths, False

        clone_proc = self._runner(
            ["git", "clone", "--bare", origin_url, paths.clone_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if clone_proc.returncode != 0:
            raise RepoCloneError(f"clone failed for {origin_url}: {clone_proc.stderr.strip()}")

        worktree_proc = self._runner(
            ["git", "worktree", "add", paths.main_worktree_path],
            cwd=paths.clone_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if worktree_proc.returncode != 0:
            raise RepoCloneError(
                f"main worktree creation failed for {origin_url}: {worktree_proc.stderr.strip()}"
            )

        return paths, True
