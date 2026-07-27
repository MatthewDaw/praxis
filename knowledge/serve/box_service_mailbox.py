"""Mailbox (R28/R72): the operator posts a message to a running remote job; delivery is by a
per-dispatch injected Stop hook that surfaces every still-pending message at the session's next
ticket boundary. A message posted to a session that never reaches a ticket boundary — the session
is stuck, dead, or the job ends first — must not silently sit "pending" forever: the mailbox
records BOTH a ``posted_at`` timestamp (stamped on post) and a ``surfaced_at`` timestamp (stamped
only when a ticket boundary actually drains and shows it), and the job view (:func:`job_mailbox_view`)
derives an explicit ``"undelivered"`` status from ``surfaced_at is None`` rather than ever inferring
delivery from elapsed time or session liveness.

``post_message`` is the one function a website handler and an MCP tool are both thin callers of
(mirrors ``box_service_resume.resume_job``'s single-function-of-truth shape — see that module's
docstring). ``mark_surfaced`` is what an injected Stop hook calls at a ticket boundary to drain and
timestamp every still-pending message; it is idempotent — messages already surfaced are untouched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal

from knowledge.serve.box_service_models import Job

#: The mailbox file's fixed name inside a job's own worktree — a launched job already has a
#: dedicated ``worktree_path``; this rides on it rather than inventing a second storage path.
MAILBOX_FILENAME = ".af-build-mailbox.json"

DeliveryStatus = Literal["delivered", "undelivered"]


def mailbox_path(job: Job) -> Path:
    """The mailbox file address for ``job``. Raises if the job has no worktree yet — nothing
    dispatched has nowhere to deliver a message into."""
    if not job.worktree_path:
        raise ValueError(f"job {job.id!r} has no worktree_path — cannot address its mailbox")
    return Path(job.worktree_path) / MAILBOX_FILENAME


def _read(job: Job) -> list[dict]:
    path = mailbox_path(job)
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _write(job: Job, entries: list[dict]) -> None:
    mailbox_path(job).write_text(json.dumps(entries))


def post_message(job: Job, text: str, *, now: float | None = None) -> Job:
    """Post ``text`` to ``job``'s mailbox (the operator-facing action — callable identically from
    a website handler and an MCP tool, R28), stamping ``posted_at`` immediately. ``surfaced_at``
    starts ``None`` and stays that way until :func:`mark_surfaced` runs at an actual ticket
    boundary — never backfilled by post time or by mere elapsed time. Refuses an empty message."""
    if not text.strip():
        raise ValueError("cannot post an empty mailbox message")
    entries = _read(job)
    entries.append({"text": text, "posted_at": now if now is not None else time.time(), "surfaced_at": None})
    _write(job, entries)
    return job


def mark_surfaced(job: Job, *, now: float | None = None) -> Job:
    """Stamp ``surfaced_at`` on every not-yet-surfaced message in ``job``'s mailbox — called by an
    injected Stop hook when it drains pending messages at a ticket boundary. Messages already
    surfaced are left untouched (idempotent across repeated boundaries)."""
    stamp = now if now is not None else time.time()
    entries = _read(job)
    for entry in entries:
        if entry.get("surfaced_at") is None:
            entry["surfaced_at"] = stamp
    _write(job, entries)
    return job


def delivery_status(*, surfaced_at: float | None) -> DeliveryStatus:
    """The single rule the job view and the mailbox agree on: a message is ``"delivered"`` iff it
    has been surfaced at a ticket boundary, ``"undelivered"`` otherwise — never inferred from how
    long ago it was posted."""
    return "delivered" if surfaced_at is not None else "undelivered"


def job_mailbox_view(job: Job) -> list[dict]:
    """The job view's mailbox section (R72): every posted message with its ``posted_at``,
    ``surfaced_at`` (``None`` if never surfaced), and derived ``status``. A message that never
    reaches a ticket boundary keeps ``surfaced_at is None`` forever and therefore always renders
    ``"undelivered"`` with only its posted timestamp — it is never silently dropped or shown as
    merely pending."""
    return [
        {
            "text": entry["text"],
            "posted_at": entry["posted_at"],
            "surfaced_at": entry.get("surfaced_at"),
            "status": delivery_status(surfaced_at=entry.get("surfaced_at")),
        }
        for entry in _read(job)
    ]
