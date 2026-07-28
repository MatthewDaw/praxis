"""Job worktree cleanup once merged and its tail has persisted (R40).

A job's own worktree (``box_service_job_worktree.JobWorktreeManager``) is
deleted only after (a) its work has merged into the repo's main worktree
(``box_service_integrate.run_integration_sequence``) and (b) its final
activity tail has persisted (``box_service_activity_tail.ActivityTailStore``)
— never before, and never as a side effect of session reaping
(``session_launcher.SessionLauncher.terminate`` only reaps the background
agent session; it has no knowledge of worktrees at all, so a job whose
integration has not yet run keeps its worktree across a reap).

Deletion runs ``git worktree remove`` against the repo's bare clone path
(never the job worktree itself), so only the one job path is detached — the
clone and its checked-out main worktree (``box_service_clone.RepoClone``)
are untouched and persist across jobs.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from knowledge.serve.box_service_activity_tail import ActivityTailStore
from knowledge.serve.box_service_hook_trail import HookTrailManager
from knowledge.serve.box_service_models import Job, JobState, mark_terminal

#: Same shape as ``subprocess.run`` — the seam a fake runner replaces in tests.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class WorktreeCleanupError(RuntimeError):
    """Raised when the underlying ``git worktree remove`` call fails. Never
    silently swallowed (R17: refuse rather than degrade)."""


class WorktreeCleanupNotReadyError(WorktreeCleanupError):
    """Raised when cleanup is attempted before both of its preconditions
    hold: the job's work has merged into the main worktree, and its activity
    tail has persisted. Worktree deletion must never precede integration
    (R40) — this is the refusal that enforces it."""


def cleanup_job_worktree(
    job: Job,
    *,
    merged: bool,
    tail_store: ActivityTailStore,
    clone_path: str,
    runner: Runner = subprocess.run,
) -> bool:
    """Delete ``job``'s own worktree now that it is merged and its tail has
    persisted; a no-op (returns ``False``) if the job has no recorded
    worktree. Raises :class:`WorktreeCleanupNotReadyError` — never deletes —
    if either precondition does not yet hold, so a caller can never tear
    down out of order. Runs from ``clone_path`` (the repo's bare clone),
    never the job worktree or the main worktree, so only this one job path
    is removed and everything else the clone owns persists across jobs.
    """
    if job.worktree_path is None:
        return False
    if not merged:
        raise WorktreeCleanupNotReadyError(
            f"job {job.id!r} has not merged into the main worktree; refusing to delete its worktree"
        )
    if not tail_store.has_entry(job.tail_ref):
        raise WorktreeCleanupNotReadyError(
            f"job {job.id!r} has no persisted activity tail; refusing to delete its worktree"
        )

    proc = runner(
        ["git", "worktree", "remove", "--force", job.worktree_path],
        cwd=clone_path, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise WorktreeCleanupError(
            f"git worktree remove failed for job {job.id!r}: {proc.stderr.strip()}"
        )
    job.worktree_path = None
    return True


def reap_and_cleanup(
    job: Job,
    *,
    final_tail_chunk: str,
    tail_store: ActivityTailStore,
    terminal_state: JobState,
    terminal_reason: str,
    merged: bool,
    clone_path: str,
    hook_trail_manager: HookTrailManager | None = None,
    runner: Runner = subprocess.run,
) -> bool:
    """The one path that ends a job's on-disk footprint: persist its final
    activity tail, record its terminal event, delete the on-disk hook trail
    (R66), and only THEN attempt worktree cleanup (R40) — the tail and
    terminal event are always durable before teardown is attempted. Distinct
    from ``SessionLauncher.terminate`` (session reaping), which only reaps
    the background agent session and never touches the worktree — reaping
    alone can never delete a job's worktree.
    """
    tail_store.append(job, final_tail_chunk)
    mark_terminal(job, terminal_state, terminal_reason)
    # Delete the on-disk hook trail BEFORE worktree cleanup (R66):
    # the persisted activity tail survives; the disposable hook trail is gone.
    if hook_trail_manager is not None:
        hook_trail_manager.delete(job)
    return cleanup_job_worktree(
        job, merged=merged, tail_store=tail_store, clone_path=clone_path, runner=runner
    )
