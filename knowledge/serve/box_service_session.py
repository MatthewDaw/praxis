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

import os

from knowledge.serve.box_service_backends import backend_session_credential
from knowledge.serve.box_service_models import Job, JobState, SessionInfo
from knowledge.serve.build_session_env import build_session_environment, default_job_home
from knowledge.serve.session_launcher import SessionLauncher

#: Default command a launched job session runs (the box service's per-job
#: build entry point).
DEFAULT_JOB_COMMAND = "/af-build"


def launch_job_session(
    job: Job,
    launcher: SessionLauncher,
    *,
    command: str = DEFAULT_JOB_COMMAND,
    extra_args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> Job:
    """Launch ``job``'s background session at its own worktree path, in place.

    ``job.worktree_path`` must already be set (R11 creates it before a job is
    launched). Records the returned session id on the job and moves it to
    ``RUNNING``. Raises :class:`~knowledge.serve.session_launcher.SessionLauncherError`
    (never silently swallowed) if the launcher itself fails.

    ``extra_args``/``env`` (R14: the per-dispatch plugin-dir/mcp-config/settings
    flags and Praxis/``FACTORY_PROJECT`` env from
    ``knowledge.serve.dispatch_launch``) are threaded straight through to
    :meth:`SessionLauncher.launch`; both default to ``None`` so a caller that
    passes neither still gets the allowlist-scrubbed R37 default below. Passing
    ``knowledge.serve.box_service_mailbox.dispatch_wiring(job)`` here is what makes THIS launch —
    and only a real job launch, never a local ``claude`` invocation — carry the per-dispatch
    mailbox-relay Stop hook (R28).

    When ``env`` is not given, the session is launched under an allowlist-scrubbed
    environment with its own ``HOME``, distinct from the box service's (R37) — see
    ``build_session_env`` — so no push credential, service token, or cloud-credential
    variable the box service's own process carries reaches the launched session. Pass an
    explicit ``env`` (e.g. R29's resumed-owner identity) to override this default.
    """
    if not job.worktree_path:
        raise ValueError(f"job {job.id!r} has no worktree_path — cannot launch a session")
    session_env = env if env is not None else build_session_environment(
        os.environ, home_dir=default_job_home(job.worktree_path)
    )
    # R88: inject the active model-backend's credential into the launched session's
    # environment so the session knows which API key to use.  Only the selected
    # backend's credential is exposed — the other is never set (exclusivity guarantee).
    # An unprovisioned box (no backend file yet, or no credential for the chosen
    # backend) gets an empty injection — the session will still fail fast if no
    # credential is available by any other path, rather than silently using the wrong
    # one.
    session_env = {**session_env, **backend_session_credential()}
    # R89: record which backend was active at launch time so the operator can later
    # confirm which billing meter each job used.  Reads the same persisted setting
    # R88's backend_session_credential() reads — no separate I/O.
    from knowledge.serve.box_service_backends import read_active_backend
    try:
        job.model_backend = read_active_backend()
    except (FileNotFoundError, ValueError):
        pass  # unprovisioned box — stays None, surfaced as "unknown" in views
    job.session_id = launcher.launch(
        cwd=job.worktree_path, command=command, name=job.id, extra_args=extra_args, env=session_env
    )
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
