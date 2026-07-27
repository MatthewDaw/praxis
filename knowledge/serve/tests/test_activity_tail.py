"""R25 acceptance: a job whose session has been reaped still has its recent
activity readable from the object store referenced by the job row; a live
session's deeper fetch returns more than the stored tail; no tail content is
ever carried as a Praxis fact — the job row holds only an opaque ref.
"""

from __future__ import annotations

import dataclasses

import pytest

from knowledge.serve.box_service_activity_tail import ActivityTailStore
from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.job_authz import AuthorizationError, JobPrincipal, PrincipalKind

ORG = "org-a"


def _job(**overrides) -> Job:
    defaults = dict(
        id="job-1",
        project="proj-a",
        snapshot="prd-proj-a",
        state=JobState.RUNNING,
        run_owner="box-1",
        org=ORG,
    )
    defaults.update(overrides)
    return Job(**defaults)


def _principal(org: str = ORG) -> JobPrincipal:
    return JobPrincipal(kind=PrincipalKind.OPERATOR, id="operator-1", org_id=org)


def test_job_row_carries_only_an_opaque_ref_never_tail_content():
    field_names = {f.name for f in dataclasses.fields(Job)}
    assert "tail_ref" in field_names
    assert field_names.isdisjoint({"tail_content", "tail_text", "tail_bytes", "activity_tail"})


def test_reaped_session_activity_still_readable_from_the_object_store():
    store = ActivityTailStore()
    job = _job()
    store.append(job, "line one\n")
    store.append(job, "line two\n")

    # the session is gone (reaped / box unreachable / process died) — no live
    # fetch is available, only the stored tail off the job row's tail_ref.
    tail = store.read(job, _principal(), session_alive=False)

    assert tail == "line one\nline two\n"
    assert job.tail_ref is not None


def test_live_session_deeper_fetch_returns_more_than_the_stored_tail():
    store = ActivityTailStore(byte_cap=20)
    job = _job()
    store.append(job, "x" * 20)
    stored = store.read(job, _principal(), session_alive=False)

    deeper = "x" * 20 + "the full untrimmed transcript, way longer than the cap"
    live = store.read(
        job, _principal(), session_alive=True, live_fetch=lambda: deeper
    )

    assert len(live) > len(stored)
    assert live == deeper


def test_read_is_org_scope_authorized():
    store = ActivityTailStore()
    job = _job()
    store.append(job, "secret activity")

    with pytest.raises(AuthorizationError):
        store.read(job, _principal(org="org-b"), session_alive=False)


def test_unauthenticated_read_is_refused_through_the_same_authorization_path():
    """R85: a tail read with no principal at all (no credential) is refused
    via the same ``job_authz.authorize`` path that guards job rows — not an
    unhandled crash from touching a ``None`` principal's ``org_id``."""
    store = ActivityTailStore()
    job = _job()
    store.append(job, "secret activity")

    with pytest.raises(AuthorizationError):
        store.read(job, None, session_alive=False)
    with pytest.raises(AuthorizationError):
        store.read_stored(job, None)
