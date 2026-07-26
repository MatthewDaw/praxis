"""Launching a claimed job's background session (R13): the box service never
hand-manages a ``tmux`` session — a job's lifecycle is owned by the built-in
Claude Code background daemon that :mod:`session_launcher` wraps, and the
session is launched with its cwd pinned to the job's own worktree (R11).

This module is the thin glue between a :class:`~knowledge.serve.box_service_models.Job`
row and the named session-launcher seam: it never shells out itself (all
subprocess calls happen inside :class:`SessionLauncher`, which routes through
an injectable runner), so the "no tmux session is created" half of the R13
acceptance condition holds by construction — the only external command this
path can ever issue is the ``claude`` CLI call ``SessionLauncher`` makes.
"""

from __future__ import annotations

from knowledge.serve.box_service_models import Job, JobState, SessionInfo
from knowledge.serve.session_launcher import SessionLauncher

#: Default command a launched job session runs (the box service's per-job
#: build entry point).
DEFAULT_JOB_COMMAND = "/af-build"


def launch_job_session(
    job: Job,
    launcher: SessionLauncher,
    *,
    command: str = DEFAULT_JOB_COMMAND,
) -> Job:
    """Launch ``job``'s background session at its own worktree path, in place.

    ``job.worktree_path`` must already be set (R11 creates it before a job is
    launched). Records the returned session id on the job and moves it to
    ``RUNNING``. Raises :class:`~knowledge.serve.session_launcher.SessionLauncherError`
    (never silently swallowed) if the launcher itself fails.
    """
    if not job.worktree_path:
        raise ValueError(f"job {job.id!r} has no worktree_path — cannot launch a session")
    job.session_id = launcher.launch(cwd=job.worktree_path, command=command, name=job.id)
    job.state = JobState.RUNNING
    return job


def find_job_session(job: Job, sessions: list[SessionInfo]) -> SessionInfo | None:
    """Return ``job``'s live session from an externally polled listing
    (:meth:`SessionLauncher.list`), or ``None`` once the session is gone —
    the daemon listing is the sole source of truth for whether it still
    exists, independent of the job row's own bookkeeping."""
    if job.session_id is None:
        return None
    for session in sessions:
        if session.session_id == job.session_id:
            return session
    return None
