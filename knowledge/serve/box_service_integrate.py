"""Job-branch integration into the repo's main worktree (R32, R33, R34): the
box service's ONE further merge, one level up from af-build's in-session
per-ticket integration.

Integration is two-level and the levels are owned by different actors (R32).
Per-ticket worktrees are integrated onto the job worktree **inside the
session** by af-build, followed by its WORK-review panel — unchanged existing
behavior this module never repeats. The box service never re-does that merge;
it integrates the finished job branch exactly once, one level up, into the
repo's main worktree (``box_service_clone.RepoCloneManager``'s output, R10).

All work merges into the main worktree; pushes happen only from there (R33).
This module resets the main worktree to the job's intended PR base and merges
the job branch — it never pushes itself, leaving that to the caller, from the
main worktree only.

A merge conflict fails the integration, preserves the job branch untouched,
and never leaves the main worktree partially merged (R34): a conflicting
merge is aborted before :class:`MergeConflictError` is raised, so the caller
records ``box_service_failures.FailureClass.MERGE_CONFLICT`` against a main
worktree unchanged from before the attempt.

Two same-repo jobs finishing simultaneously must not interleave their
reset/merge sequences against the one shared main worktree, and integration
must refuse — never reset — a main worktree that is dirty or holds a commit
not yet pushed to the PR base, since a reset would silently discard real
work (the ``integration-serialized-per-repo`` check). ``RepoIntegrationLock``
is an in-memory advisory lock, keyed by repo, carrying a holder id, a
heartbeat, and an expiry — the same holder/heartbeat/expiry shape as every
other lease in the system (the job claim lease, the job-control lease) so a
dead holder never permanently strands the resource it held.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from knowledge.serve.box_service_clone import RepoClone

#: Same shape as ``subprocess.run`` — the seam a fake runner replaces in tests.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Clock = Callable[[], float]

#: Default staleness window for the per-repo integration lock, mirroring the
#: shape (not the value) of ``_ticket_state.DEFAULT_LEASE_TTL_S``.
DEFAULT_LOCK_TTL_S = 300.0


class IntegrationError(RuntimeError):
    """Base class for a refused or failed integration. Never silently
    swallowed (R17: refuse rather than degrade)."""


class IntegrationLockedError(IntegrationError):
    """A different, live holder already has this repo's integration in
    flight; reset/merge sequences for the same repo never interleave."""


class MainWorktreeDirtyError(IntegrationError):
    """Refuse to reset a main worktree that has uncommitted changes or a
    commit not yet pushed to the PR base — resetting would silently discard
    real work."""


class MergeConflictError(IntegrationError):
    """The job branch conflicts merging into the main worktree (R34). The
    branch is preserved and the main worktree is left exactly where it was
    before the merge attempt — never partially merged."""


@dataclass(frozen=True)
class IntegrationResult:
    """The outcome of the box service's single merge of a job branch into the
    repo's main worktree (R32, R33)."""

    repo_clone: RepoClone
    job_branch: str
    merged_sha: str


@dataclass
class _LockEntry:
    holder_id: str
    heartbeat_at: float
    ttl: float


class RepoIntegrationLock:
    """An advisory, per-repo lock so two same-repo jobs finishing at once
    never interleave their reset/merge sequences against the one shared main
    worktree.

    Carries a holder id, heartbeat, and expiry, like every other lease in the
    system: a stale entry (no heartbeat within its ``ttl``) is reclaimable by
    a new holder rather than stranding the repo forever behind a dead holder.
    """

    def __init__(self, *, clock: Clock = time.monotonic, ttl: float = DEFAULT_LOCK_TTL_S) -> None:
        self._clock = clock
        self._ttl = ttl
        self._entries: dict[str, _LockEntry] = {}

    def _is_stale(self, entry: _LockEntry) -> bool:
        return (self._clock() - entry.heartbeat_at) > entry.ttl

    def acquire(self, repo_key: str, holder_id: str) -> bool:
        """Take the lock for ``repo_key``. ``True`` iff ``holder_id`` now
        holds it — because it already did, the lock was free, or the
        previous holder's entry is stale. ``False`` iff a different, live
        holder has it."""
        entry = self._entries.get(repo_key)
        if entry is not None and entry.holder_id != holder_id and not self._is_stale(entry):
            return False
        self._entries[repo_key] = _LockEntry(
            holder_id=holder_id, heartbeat_at=self._clock(), ttl=self._ttl
        )
        return True

    def heartbeat(self, repo_key: str, holder_id: str) -> bool:
        """Bump the lock's liveness; ``False`` iff ``holder_id`` does not
        currently hold ``repo_key``."""
        entry = self._entries.get(repo_key)
        if entry is None or entry.holder_id != holder_id:
            return False
        entry.heartbeat_at = self._clock()
        return True

    def release(self, repo_key: str, holder_id: str) -> bool:
        """Give up the lock; ``False`` iff ``holder_id`` does not currently
        hold ``repo_key`` (a no-op, never raises)."""
        entry = self._entries.get(repo_key)
        if entry is None or entry.holder_id != holder_id:
            return False
        del self._entries[repo_key]
        return True

    def held_by(self, repo_key: str) -> str | None:
        """The live holder of ``repo_key``, or ``None`` if free/stale."""
        entry = self._entries.get(repo_key)
        if entry is None or self._is_stale(entry):
            return None
        return entry.holder_id


def _run_git(runner: Runner, cwd: str, *args: str) -> str:
    proc = runner(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise IntegrationError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def integrate_job_branch(
    repo_clone: RepoClone,
    job_branch: str,
    pr_base: str,
    *,
    holder_id: str,
    lock: RepoIntegrationLock,
    runner: Runner = subprocess.run,
) -> IntegrationResult:
    """Perform the box service's single further merge (R32): reset the repo's
    main worktree to ``pr_base``, then merge ``job_branch`` into it — exactly
    one merge call, never a second, per-ticket merge (af-build already did
    that in-session). Never pushes (R33 leaves that to the caller, from the
    main worktree only).

    Serialized per repo via ``lock``: raises :class:`IntegrationLockedError`
    if a different, live holder already has this repo's integration in
    flight. Refuses (:class:`MainWorktreeDirtyError`) rather than resetting a
    main worktree that has uncommitted changes or holds a commit not yet
    pushed to ``origin/{pr_base}`` — an operator's work must never be
    silently discarded by a reset. On merge conflict, aborts the merge,
    raises :class:`MergeConflictError` preserving ``job_branch``, and leaves
    the main worktree exactly where it was before the attempt (R34).
    """
    repo_key = repo_clone.clone_path
    if not lock.acquire(repo_key, holder_id):
        raise IntegrationLockedError(
            f"integration for {repo_key!r} is already in flight under a different holder"
        )
    try:
        cwd = repo_clone.main_worktree_path

        status = _run_git(runner, cwd, "status", "--porcelain")
        if status.strip():
            raise MainWorktreeDirtyError(
                f"main worktree {cwd!r} has uncommitted changes, refusing to reset"
            )

        # Fetch into an explicit remote-tracking ref (rather than relying on
        # a pre-configured "origin/<branch>" name) so this never collides
        # with whatever branch happens to be checked out in the main
        # worktree itself, even when that branch shares ``pr_base``'s name.
        tracking_ref = f"refs/remotes/origin/{pr_base}"
        _run_git(runner, cwd, "fetch", "origin", f"+{pr_base}:{tracking_ref}")

        unpushed = _run_git(runner, cwd, "log", f"{tracking_ref}..HEAD", "--oneline")
        if unpushed.strip():
            raise MainWorktreeDirtyError(
                f"main worktree {cwd!r} holds a commit not yet pushed to origin/{pr_base}, "
                "refusing to reset"
            )

        _run_git(runner, cwd, "reset", "--hard", tracking_ref)

        merge_proc = runner(
            ["git", "merge", "--no-ff", job_branch],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if merge_proc.returncode != 0:
            runner(
                ["git", "merge", "--abort"], cwd=cwd, capture_output=True, text=True, check=False
            )
            raise MergeConflictError(
                f"merge of {job_branch!r} into {cwd!r} conflicted, job branch preserved"
            )

        merged_sha = _run_git(runner, cwd, "rev-parse", "HEAD").strip()
        return IntegrationResult(
            repo_clone=repo_clone, job_branch=job_branch, merged_sha=merged_sha
        )
    finally:
        lock.release(repo_key, holder_id)
