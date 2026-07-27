"""R72: the mailbox records posted and surfaced timestamps and the job view marks a message
undelivered when it never reaches a ticket boundary — so an answer that cannot reach a session
blocked mid-ticket is visible as undelivered rather than silently pending.

Acceptance condition under test: given a message posted to a session that never reaches a ticket
boundary, the job view shows it undelivered with its posted timestamp and no surfaced timestamp.
"""

from __future__ import annotations

import json

import pytest

from knowledge.serve.box_service_mailbox import (
    delivery_status,
    job_mailbox_view,
    mailbox_path,
    mark_surfaced,
    post_message,
)
from knowledge.serve.box_service_models import Job, JobState


def _job(tmp_path) -> Job:
    return Job(
        id="job-1",
        project="proj",
        snapshot="snap",
        state=JobState.RUNNING,
        worktree_path=str(tmp_path),
    )


def test_message_never_surfaced_is_undelivered_with_posted_timestamp_only(tmp_path):
    job = _job(tmp_path)
    post_message(job, "are you stuck?", now=100.0)

    view = job_mailbox_view(job)

    assert len(view) == 1
    entry = view[0]
    assert entry["text"] == "are you stuck?"
    assert entry["posted_at"] == 100.0
    assert entry["surfaced_at"] is None
    assert entry["status"] == "undelivered"


def test_message_surfaced_at_a_ticket_boundary_is_delivered(tmp_path):
    job = _job(tmp_path)
    post_message(job, "status?", now=100.0)

    mark_surfaced(job, now=150.0)
    view = job_mailbox_view(job)

    entry = view[0]
    assert entry["posted_at"] == 100.0
    assert entry["surfaced_at"] == 150.0
    assert entry["status"] == "delivered"


def test_delivery_status_pure_function():
    assert delivery_status(surfaced_at=None) == "undelivered"
    assert delivery_status(surfaced_at=2.0) == "delivered"


def test_post_message_refuses_empty_text(tmp_path):
    job = _job(tmp_path)
    with pytest.raises(ValueError):
        post_message(job, "   ")


def test_mailbox_file_persists_structured_entries(tmp_path):
    job = _job(tmp_path)
    post_message(job, "hello", now=5.0)

    raw = json.loads(mailbox_path(job).read_text())
    assert raw == [{"text": "hello", "posted_at": 5.0, "surfaced_at": None}]


def test_second_message_posted_after_first_is_surfaced_stays_pending_alone(tmp_path):
    """A message that arrives after a boundary already drained the mailbox is not swept up by that
    earlier surfacing — each message's undelivered/delivered status is judged independently."""
    job = _job(tmp_path)
    post_message(job, "first", now=1.0)
    mark_surfaced(job, now=2.0)
    post_message(job, "second", now=3.0)

    view = job_mailbox_view(job)

    assert view[0]["status"] == "delivered"
    assert view[1]["status"] == "undelivered"
    assert view[1]["surfaced_at"] is None
