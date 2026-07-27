"""The deterministic push guard (R33 / the ``no-push-outside-integration`` build check): refuses
a target repo differing from the job's allowlisted origin, a ref outside the reserved per-job
integration namespace, a force update, and an update of an existing remote ref."""

from __future__ import annotations

from knowledge.serve.box_service_push_guard import (
    RESERVED_INTEGRATION_REF_PREFIX,
    PushRequest,
    evaluate_push,
)

ALLOWED_ORIGIN = "git@github.com:acme/widgets.git"
ALLOWED_REF = f"{RESERVED_INTEGRATION_REF_PREFIX}job-1"


def make_request(**overrides) -> PushRequest:
    defaults = dict(
        target_repo=ALLOWED_ORIGIN,
        ref=ALLOWED_REF,
        force=False,
        remote_ref_exists=False,
    )
    defaults.update(overrides)
    return PushRequest(**defaults)


def test_allows_a_first_push_of_a_reserved_ref_to_the_allowlisted_origin():
    decision = evaluate_push(make_request(), allowlisted_origin=ALLOWED_ORIGIN)

    assert decision.allowed is True
    assert decision.reason is None


def test_refuses_a_target_repo_differing_from_the_allowlisted_origin():
    decision = evaluate_push(
        make_request(target_repo="git@github.com:evil/widgets.git"),
        allowlisted_origin=ALLOWED_ORIGIN,
    )

    assert decision.allowed is False
    assert "allowlisted origin" in decision.reason


def test_refuses_a_ref_outside_the_reserved_integration_namespace():
    decision = evaluate_push(
        make_request(ref="refs/heads/main"), allowlisted_origin=ALLOWED_ORIGIN
    )

    assert decision.allowed is False
    assert "namespace" in decision.reason


def test_refuses_a_force_update():
    decision = evaluate_push(make_request(force=True), allowlisted_origin=ALLOWED_ORIGIN)

    assert decision.allowed is False
    assert "force" in decision.reason


def test_refuses_an_update_of_an_existing_remote_ref():
    decision = evaluate_push(
        make_request(remote_ref_exists=True), allowlisted_origin=ALLOWED_ORIGIN
    )

    assert decision.allowed is False
    assert "first-and-only" in decision.reason


def test_target_repo_check_takes_precedence_over_other_refusals():
    # Wrong repo AND a force update AND an existing ref: the target-repo mismatch is reported.
    decision = evaluate_push(
        make_request(
            target_repo="git@github.com:evil/widgets.git", force=True, remote_ref_exists=True
        ),
        allowlisted_origin=ALLOWED_ORIGIN,
    )

    assert decision.allowed is False
    assert "allowlisted origin" in decision.reason
