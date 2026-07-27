"""Unit coverage for the pure delivery-stage reconciliation decision (R62,
``box_service_delivery.reconcile_delivery``) — no git, no subprocess, no Praxis."""

from __future__ import annotations

from knowledge.serve.box_service_delivery import DeliveryAction, reconcile_delivery
from knowledge.serve.box_service_models import DeliveryStage


def test_not_started_always_runs_the_full_sequence():
    decision = reconcile_delivery(
        DeliveryStage.NOT_STARTED, remote_ref_exists=False, existing_pr_url=None
    )
    assert decision.action is DeliveryAction.RUN_FULL_SEQUENCE


def test_publishing_with_no_remote_ref_is_safe_to_rerun_from_the_top():
    # The push never landed, so nothing irreversible has happened — safe to redo everything.
    decision = reconcile_delivery(
        DeliveryStage.PUBLISHING, remote_ref_exists=False, existing_pr_url=None
    )
    assert decision.action is DeliveryAction.RUN_FULL_SEQUENCE


def test_publishing_with_remote_ref_and_no_pr_skips_push_and_opens_one_pr():
    # Crash right after the push landed: replay must never push again.
    decision = reconcile_delivery(
        DeliveryStage.PUBLISHING, remote_ref_exists=True, existing_pr_url=None
    )
    assert decision.action is DeliveryAction.SKIP_PUSH_OPEN_PR


def test_publishing_with_remote_ref_and_existing_pr_reuses_it():
    decision = reconcile_delivery(
        DeliveryStage.PUBLISHING,
        remote_ref_exists=True,
        existing_pr_url="https://github.com/acme/widgets/pull/9",
    )
    assert decision.action is DeliveryAction.REUSE_EXISTING_PR
    assert decision.pr_url == "https://github.com/acme/widgets/pull/9"


def test_opening_pr_with_remote_ref_and_no_pr_opens_exactly_one():
    # The exact "crash between push and pull-request creation" case: push confirmed, PR not yet.
    decision = reconcile_delivery(
        DeliveryStage.OPENING_PR, remote_ref_exists=True, existing_pr_url=None
    )
    assert decision.action is DeliveryAction.SKIP_PUSH_OPEN_PR


def test_opening_pr_with_remote_ref_and_existing_pr_reuses_it_never_opening_a_second():
    decision = reconcile_delivery(
        DeliveryStage.OPENING_PR,
        remote_ref_exists=True,
        existing_pr_url="https://github.com/acme/widgets/pull/9",
    )
    assert decision.action is DeliveryAction.REUSE_EXISTING_PR
    assert decision.pr_url == "https://github.com/acme/widgets/pull/9"


def test_opening_pr_with_no_remote_ref_is_unreconcilable():
    # The stage claims the branch was already published, but re-detection disagrees — this must
    # never be guessed or retried blind.
    decision = reconcile_delivery(
        DeliveryStage.OPENING_PR, remote_ref_exists=False, existing_pr_url=None
    )
    assert decision.action is DeliveryAction.NEEDS_ATTENTION
    assert decision.reason is not None


def test_delivered_with_findable_pr_is_already_delivered():
    decision = reconcile_delivery(
        DeliveryStage.DELIVERED,
        remote_ref_exists=True,
        existing_pr_url="https://github.com/acme/widgets/pull/9",
    )
    assert decision.action is DeliveryAction.ALREADY_DELIVERED
    assert decision.pr_url == "https://github.com/acme/widgets/pull/9"


def test_delivered_with_no_findable_pr_is_unreconcilable():
    decision = reconcile_delivery(
        DeliveryStage.DELIVERED, remote_ref_exists=True, existing_pr_url=None
    )
    assert decision.action is DeliveryAction.NEEDS_ATTENTION
    assert decision.reason is not None
