"""Box-level integration of a finished job branch into the repo's main worktree (R33).

All work merges into the repo's main worktree, and the outbound publish (the ref update to the
remote plus opening the pull request) happens only from there — never from a job worktree. The
reset-to-PR-base, merge, remote-ref update, and pull-request opening are one sequence, serialized
per repo under :class:`RepoIntegrationLock` held for the WHOLE sequence (never released and
reacquired mid-sequence), so two same-repo jobs finishing at once never interleave their sequences
against the one shared main worktree.

Integration refuses — never resets — a main worktree that is dirty or holds a commit not yet
reflected upstream at the intended PR base, since a reset would silently discard real work; the
refusal records ``needs-attention`` on the job (``box_service_failures``) and leaves the main
worktree untouched. A conflicting merge is aborted before raising, so a caller's
``MERGE_CONFLICT`` failure record always corresponds to a main worktree left exactly where it was
before the attempt.

The remote-ref update and pull-request opening are guarded by the shared, reusable
``box_service_push_guard`` core (target-repo / namespace / force / existing-ref refusal) before
either is attempted, and both route their outbound call through argv lists rather than a shell
string, so no literal command text for either lands in the source tree outside this module and its
tests.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from knowledge.serve.box_service_failures import FailureClass, record_failure
from knowledge.serve.box_service_models import Job
from knowledge.serve.box_service_push_guard import PushRequest, evaluate_push

#: Same shape as ``subprocess.run`` — the seam a fake runner replaces in tests.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Clock = Callable[[], float]
#: A pull-request-creation seam: given the target and the merged sha, returns the created PR's URL.
PrCreator = Callable[["IntegrationTarget", str], str]

#: Default staleness window for the per-repo integration lock, mirroring the shape (not the value)
#: of every other lease in the system (holder id, heartbeat, expiry).
DEFAULT_LOCK_TTL_S = 300.0


class IntegrationError(RuntimeError):
    """Base class for a refused or failed integration. Never silently swallowed (R17: refuse
    rather than degrade)."""


class IntegrationLockedError(IntegrationError):
    """A different, live holder already has this repo's integration in flight; reset/merge/
    publish sequences for the same repo never interleave."""


class MainWorktreeDirtyError(IntegrationError):
    """Refuse to reset a main worktree that has uncommitted changes or a commit not yet reflected
    upstream at the PR base — resetting would silently discard real work."""


class MergeConflictError(IntegrationError):
    """The job branch conflicts merging into the main worktree (R34). The branch is preserved and
    the main worktree is left exactly where it was before the merge attempt — never partially
    merged."""


class PublishRefusedError(IntegrationError):
    """The push guard refused the outbound ref update — wrong target repo, ref outside the
    reserved integration namespace, a force update, or an already-existing remote ref."""


@dataclass(frozen=True)
class IntegrationTarget:
    """Everything one integration sequence needs, resolved ahead of time so this module owns no
    lookup of its own — just the main worktree it must run in (never a job worktree)."""

    main_worktree_path: str
    origin_repo: str
    job_branch: str
    pr_base: str
    integration_ref: str


@dataclass(frozen=True)
class IntegrationResult:
    merged_sha: str
    pushed_ref: str
    pr_url: str


@dataclass
class _LockEntry:
    holder_id: str
    heartbeat_at: float
    ttl: float


class RepoIntegrationLock:
    """An advisory, per-repo lock so two same-repo jobs finishing at once never interleave their
    reset/merge/publish sequences against the one shared main worktree.

    Carries a holder id, heartbeat, and expiry, like every other lease in the system: a stale
    entry (no heartbeat within its ``ttl``) is reclaimable by a new holder rather than stranding
    the repo forever behind a dead holder.
    """

    def __init__(self, *, clock: Clock = time.monotonic, ttl: float = DEFAULT_LOCK_TTL_S) -> None:
        self._clock = clock
        self._ttl = ttl
        self._entries: dict[str, _LockEntry] = {}

    def _is_stale(self, entry: _LockEntry) -> bool:
        return (self._clock() - entry.heartbeat_at) > entry.ttl

    def acquire(self, repo_key: str, holder_id: str) -> bool:
        """Take the lock for ``repo_key``. ``True`` iff ``holder_id`` now holds it — because it
        already did, the lock was free, or the previous holder's entry is stale. ``False`` iff a
        different, live holder has it."""
        entry = self._entries.get(repo_key)
        if entry is not None and entry.holder_id != holder_id and not self._is_stale(entry):
            return False
        self._entries[repo_key] = _LockEntry(
            holder_id=holder_id, heartbeat_at=self._clock(), ttl=self._ttl
        )
        return True

    def heartbeat(self, repo_key: str, holder_id: str) -> bool:
        entry = self._entries.get(repo_key)
        if entry is None or entry.holder_id != holder_id:
            return False
        entry.heartbeat_at = self._clock()
        return True

    def release(self, repo_key: str, holder_id: str) -> bool:
        """Give up the lock; ``False`` iff ``holder_id`` does not currently hold ``repo_key`` (a
        no-op, never raises)."""
        entry = self._entries.get(repo_key)
        if entry is None or entry.holder_id != holder_id:
            return False
        del self._entries[repo_key]
        return True

    def held_by(self, repo_key: str) -> str | None:
        entry = self._entries.get(repo_key)
        if entry is None or self._is_stale(entry):
            return None
        return entry.holder_id


def _run_git(runner: Runner, cwd: str, *args: str) -> str:
    proc = runner(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise IntegrationError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def default_pr_creator(target: IntegrationTarget, merged_sha: str) -> str:
    """The default pull-request-creation seam: the GitHub CLI's PR subcommand, invoked as
    separate argv elements (never a single shell string) from the main worktree only."""
    args = [
        "gh", "pr", "create",
        "--base", target.pr_base,
        "--head", target.integration_ref,
        "--title", f"Integrate {target.job_branch}",
        "--body", f"Merged {merged_sha}",
    ]
    proc = subprocess.run(args, cwd=target.main_worktree_path, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise IntegrationError(f"pull-request creation failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def run_integration_sequence(
    target: IntegrationTarget,
    *,
    holder_id: str,
    lock: RepoIntegrationLock,
    runner: Runner = subprocess.run,
    pr_creator: PrCreator = default_pr_creator,
    force_publish: bool = False,
    remote_ref_exists: bool = False,
    job: Job | None = None,
) -> IntegrationResult:
    """Run the box service's ONE reset -> merge -> remote-ref-update -> pull-request-opening
    sequence for a finished job branch, entirely from ``target.main_worktree_path`` and never a
    job worktree (R33).

    Serialized per repo via ``lock`` HELD FOR THE WHOLE SEQUENCE (acquired once, released once in
    ``finally``) so two same-repo jobs finishing simultaneously never interleave. Raises
    :class:`IntegrationLockedError` if a different, live holder already has this repo's
    integration in flight.

    Refuses (:class:`MainWorktreeDirtyError`) rather than resetting a main worktree that has
    uncommitted changes or a commit not yet reflected upstream at ``target.pr_base`` — an
    operator's work must never be silently discarded by a reset. When ``job`` is given, the
    refusal is also recorded as ``FailureClass.MAIN_WORKTREE_DIRTY`` (needs-attention), and the
    main worktree is left completely untouched (no reset attempted).

    On merge conflict, aborts the merge, records ``FailureClass.MERGE_CONFLICT`` on ``job`` (when
    given), and raises :class:`MergeConflictError` preserving ``target.job_branch`` with the main
    worktree exactly where it was before the attempt (R34).

    The remote-ref update and pull-request creation are each checked against
    ``box_service_push_guard.evaluate_push`` first; a refusal raises
    :class:`PublishRefusedError` and neither the remote ref nor a pull request is touched.
    """
    repo_key = target.main_worktree_path
    if not lock.acquire(repo_key, holder_id):
        raise IntegrationLockedError(
            f"integration for {repo_key!r} is already in flight under a different holder"
        )
    try:
        cwd = target.main_worktree_path

        status = _run_git(runner, cwd, "status", "--porcelain")
        if status.strip():
            if job is not None:
                record_failure(job, FailureClass.MAIN_WORKTREE_DIRTY)
            raise MainWorktreeDirtyError(
                f"main worktree {cwd!r} has uncommitted changes, refusing to reset"
            )

        tracking_ref = f"refs/remotes/origin/{target.pr_base}"
        _run_git(runner, cwd, "fetch", "origin", f"+{target.pr_base}:{tracking_ref}")

        unpushed = _run_git(runner, cwd, "log", f"{tracking_ref}..HEAD", "--oneline")
        if unpushed.strip():
            if job is not None:
                record_failure(job, FailureClass.MAIN_WORKTREE_DIRTY)
            raise MainWorktreeDirtyError(
                f"main worktree {cwd!r} holds a commit not yet reflected at origin/{target.pr_base}, "
                "refusing to reset"
            )

        _run_git(runner, cwd, "reset", "--hard", tracking_ref)

        merge_proc = runner(
            ["git", "merge", "--no-ff", target.job_branch],
            cwd=cwd, capture_output=True, text=True, check=False,
        )
        if merge_proc.returncode != 0:
            runner(["git", "merge", "--abort"], cwd=cwd, capture_output=True, text=True, check=False)
            if job is not None:
                record_failure(job, FailureClass.MERGE_CONFLICT)
            raise MergeConflictError(
                f"merge of {target.job_branch!r} into {cwd!r} conflicted, job branch preserved"
            )

        merged_sha = _run_git(runner, cwd, "rev-parse", "HEAD").strip()

        decision = evaluate_push(
            PushRequest(
                target_repo=target.origin_repo,
                ref=target.integration_ref,
                force=force_publish,
                remote_ref_exists=remote_ref_exists,
            ),
            allowlisted_origin=target.origin_repo,
        )
        if not decision.allowed:
            raise PublishRefusedError(f"publish refused: {decision.reason}")

        _run_git(runner, cwd, "push", "origin", f"HEAD:{target.integration_ref}")
        pr_url = pr_creator(target, merged_sha)

        return IntegrationResult(merged_sha=merged_sha, pushed_ref=target.integration_ref, pr_url=pr_url)
    finally:
        lock.release(repo_key, holder_id)
