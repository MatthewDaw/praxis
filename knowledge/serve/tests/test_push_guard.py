"""Tests for the deterministic push guard (box_service_push_guard): each of the four refusal
rules fires on its own, in the documented precedence order, and a request violating none of them
is allowed (no exception)."""

from __future__ import annotations

import pytest

from knowledge.serve.box_service_push_guard import PushRefused, PushRequest, guard_push

ORIGIN = "git@github.com:acme/widgets.git"
NAMESPACE = "refs/heads/jobs/"


def _request(**overrides) -> PushRequest:
    defaults = dict(
        ref="refs/heads/jobs/job-123",
        target_repo=ORIGIN,
        force=False,
        existing_refs=frozenset(),
    )
    defaults.update(overrides)
    return PushRequest(**defaults)


def test_allowed_push_raises_nothing():
    guard_push(_request(), job_namespace_prefix=NAMESPACE, allowlisted_origin=ORIGIN)


def test_target_repo_mismatch_is_refused():
    req = _request(target_repo="git@github.com:someone-else/fork.git")
    with pytest.raises(PushRefused, match="allowlisted origin"):
        guard_push(req, job_namespace_prefix=NAMESPACE, allowlisted_origin=ORIGIN)


def test_ref_outside_reserved_namespace_is_refused():
    req = _request(ref="refs/heads/main")
    with pytest.raises(PushRefused, match="reserved per-job namespace"):
        guard_push(req, job_namespace_prefix=NAMESPACE, allowlisted_origin=ORIGIN)


def test_force_update_is_refused():
    req = _request(force=True)
    with pytest.raises(PushRefused, match="[Ff]orce update"):
        guard_push(req, job_namespace_prefix=NAMESPACE, allowlisted_origin=ORIGIN)


def test_existing_remote_ref_is_refused():
    req = _request(existing_refs=frozenset({"refs/heads/jobs/job-123"}))
    with pytest.raises(PushRefused, match="already exists"):
        guard_push(req, job_namespace_prefix=NAMESPACE, allowlisted_origin=ORIGIN)
