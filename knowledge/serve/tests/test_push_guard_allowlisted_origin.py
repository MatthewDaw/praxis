"""Acceptance test for ticket R36 (ad8da5f18aac4c72a802b8adc70a509a):

Given a push attempt, deterministic non-model code refuses before contacting the remote when the
ref is outside the reserved per-job-unique namespace, when the target repo differs from the job's
allowlisted origin, when the push is a force update, or when the remote ref already exists; the
only path to the dispatched base branch is a pull request, and no dispatch-payload field or
configuration flag alters any of these outcomes.

R33/R49 already prove the namespace/force/existing-ref refusals and the PR-only path (see
``test_box_service_integrate.py`` / ``test_box_service_group_integrate.py``). What neither covered
is the "target repo differs from the job's allowlisted origin" clause at the INTEGRATION-SEQUENCE
level: both sequences previously called ``evaluate_push`` with the SAME field
(``target.origin_repo``) as both the request's ``target_repo`` and the trusted
``allowlisted_origin`` — a mismatch could never be constructed, so the refusal this ticket's
acceptance names was unreachable in the wiring (only provable at the pure ``evaluate_push`` unit
level). ``IntegrationTarget``/``GroupIntegrationTarget`` now carry ``allowlisted_origin`` as a field
genuinely independent of ``origin_repo``, so a real divergence between what a caller resolved as
the push target and the job's registered origin is refused here too.
"""

from __future__ import annotations

import dataclasses

import pytest

from knowledge.serve.box_service_group_integrate import (
    GroupIntegrationTarget,
    run_group_integration_sequence,
)
from knowledge.serve.box_service_integrate import (
    IntegrationTarget,
    PublishRefusedError,
    RepoIntegrationLock,
    run_integration_sequence,
)
from knowledge.serve.dispatch import DispatchPayload

ALLOWED_ORIGIN = "git@github.com:acme/widgets.git"
DRIFTED_ORIGIN = "git@github.com:acme/widgets-fork.git"


@dataclasses.dataclass
class Proc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclasses.dataclass
class ScriptedRunner:
    merge_ok: bool = True
    calls: list = dataclasses.field(default_factory=list)

    def __call__(self, args, cwd, capture_output=True, text=True, check=False):
        self.calls.append((tuple(args), cwd))
        sub = args[1] if len(args) > 1 else None
        if sub in ("status", "fetch", "log", "reset", "config"):
            return Proc()
        if sub == "merge" and "--abort" not in args and "--squash" not in args:
            return Proc(returncode=0 if self.merge_ok else 1)
        if sub == "merge" and "--squash" in args:
            return Proc()
        if sub in ("merge", "commit"):
            return Proc()
        if sub == "rev-parse":
            return Proc(stdout="cafef00d\n")
        if sub == "push":
            return Proc()
        raise AssertionError(f"unexpected git call: {args}")


def make_solo_target(**overrides) -> IntegrationTarget:
    defaults = dict(
        main_worktree_path="/repos/widgets/main",
        origin_repo=ALLOWED_ORIGIN,
        allowlisted_origin=ALLOWED_ORIGIN,
        job_branch="job/job-1",
        pr_base="main",
        integration_ref="refs/heads/integrate/job-1",
    )
    defaults.update(overrides)
    return IntegrationTarget(**defaults)


def make_group_target(**overrides) -> GroupIntegrationTarget:
    defaults = dict(
        main_worktree_path="/repos/widgets/main",
        origin_repo=ALLOWED_ORIGIN,
        allowlisted_origin=ALLOWED_ORIGIN,
        member_branches=["job/job-1", "job/job-2"],
        member_job_ids=["job-1", "job-2"],
        pr_base="main",
        integration_ref="refs/heads/integrate/group-1",
    )
    defaults.update(overrides)
    return GroupIntegrationTarget(**defaults)


def test_solo_integration_refuses_when_the_push_target_diverges_from_the_jobs_allowlisted_origin():
    lock = RepoIntegrationLock()
    target = make_solo_target(origin_repo=DRIFTED_ORIGIN)
    runner = ScriptedRunner()
    pr_calls = []

    with pytest.raises(PublishRefusedError) as excinfo:
        run_integration_sequence(
            target, holder_id="holder-1", lock=lock, runner=runner,
            pr_creator=lambda t, sha: pr_calls.append(1) or "unused",
        )

    assert "allowlisted origin" in str(excinfo.value)
    assert pr_calls == []
    assert not any(c[0][1] == "push" for c in runner.calls)
    # The lock is still released so a corrected retry is not stranded.
    assert lock.acquire(target.main_worktree_path, "holder-2") is True


def test_solo_integration_allows_when_the_push_target_matches_the_jobs_allowlisted_origin():
    lock = RepoIntegrationLock()
    target = make_solo_target()
    runner = ScriptedRunner()

    result = run_integration_sequence(
        target, holder_id="holder-1", lock=lock, runner=runner,
        pr_creator=lambda t, sha: "https://github.com/acme/widgets/pull/1",
    )

    assert result.pushed_ref == target.integration_ref
    assert any(c[0][1] == "push" for c in runner.calls)


def test_group_integration_refuses_when_the_push_target_diverges_from_the_jobs_allowlisted_origin():
    lock = RepoIntegrationLock()
    target = make_group_target(origin_repo=DRIFTED_ORIGIN)
    runner = ScriptedRunner()
    pr_calls = []

    with pytest.raises(PublishRefusedError) as excinfo:
        run_group_integration_sequence(
            target, holder_id="holder-1", lock=lock, runner=runner,
            pr_creator=lambda t, sha: pr_calls.append(1) or "unused",
        )

    assert "allowlisted origin" in str(excinfo.value)
    assert pr_calls == []
    assert not any(c[0][1] == "push" for c in runner.calls)


def test_no_dispatch_payload_field_can_supply_an_override_for_the_refusal_outcomes():
    """R6's dispatch payload carries no field named/shaped like a bypass knob (force/skip/allow),
    so nothing an untrusted caller supplies at dispatch time can move any of the four refusal
    outcomes evaluated here or in ``box_service_push_guard.evaluate_push``."""
    field_names = {f.name for f in dataclasses.fields(DispatchPayload)}
    suspicious = {n for n in field_names if any(
        kw in n for kw in ("force", "skip", "bypass", "allow_", "override", "ref")
    )}
    assert suspicious == set()


def test_evaluate_push_and_run_integration_sequence_accept_no_configuration_flag_seam():
    """The guard's refusal reasons are a pure function of the request + the trusted allowlisted
    origin — no keyword on either sequence lets a caller flip a refusal into an allow without also
    changing the underlying (namespace/force/existing-ref/origin) fact being checked."""
    import inspect

    solo_params = set(inspect.signature(run_integration_sequence).parameters)
    group_params = set(inspect.signature(run_group_integration_sequence).parameters)
    for params in (solo_params, group_params):
        assert not {"skip_guard", "bypass_guard", "disable_push_guard"} & params
