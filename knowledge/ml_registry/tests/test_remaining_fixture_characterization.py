"""Strict-red characterization for the remaining portfolio runtime fixtures.

These tests name the public service boundaries required by the revised foundation
plan.  They intentionally xfail until those boundaries exist; implementation must
make the assertions pass rather than weakening or replacing the contracts.
"""

from __future__ import annotations

from importlib import import_module

import pytest


def _public(module: str, name: str):
    value = getattr(import_module(module), name)
    assert callable(value), f"{module}.{name} must be public and callable"
    return value


@pytest.mark.xfail(
    strict=True,
    reason=(
        "fixture A: the two-root test does not yet prove distinct named leases or "
        "artifact-specific descendant blocking"
    ),
)
def test_fixture_a_two_roots_have_distinct_leases_and_child_waits_on_fit(tmp_path):
    scenario = _public("knowledge.ml_registry.testing.portfolio_fixtures", "three_node_scenario")(
        tmp_path
    )
    result = scenario.controller.tick()

    assert set(result.running) == {"R1", "R2"}
    assert scenario.lease("R1") != scenario.lease("R2")
    assert result.blocked["C1"] == "waiting on R1:fit"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "fixture B: one-trial refusal is not yet evidenced by registry events and the "
        "typed progress stream's maximum concurrent arm count"
    ),
)
def test_fixture_b_registry_events_and_progress_prove_one_arm_per_campaign(tmp_path):
    scenario = _public("knowledge.ml_registry.testing.portfolio_fixtures", "serial_arm_scenario")(
        tmp_path
    )
    scenario.attempt_overlapping_trials(idea_id="idea-1")

    assert scenario.refusal.reason_code == "trial_already_in_flight"
    assert scenario.registry_events("trial_refused", idea_id="idea-1")
    assert scenario.progress.maximum_concurrent_arms(campaign_id="R1") == 1


@pytest.mark.xfail(
    strict=True,
    reason=(
        "fixture C: finalize verification, atomic lease release, descendant refresh, "
        "and same-poll slot backfill do not yet share one controller transaction"
    ),
)
def test_fixture_c_verified_completion_backfills_in_the_same_poll(tmp_path):
    scenario = _public("knowledge.ml_registry.testing.portfolio_fixtures", "three_node_scenario")(
        tmp_path
    )
    first = scenario.controller.tick()
    assert set(first.running) == {"R1", "R2"}

    scenario.complete_with_production_alias("R1", artifact_type="fit")
    second = scenario.controller.tick()

    assert scenario.finalize_verifications("R1") == 1
    assert scenario.lease_owner("R1") is None
    assert "C1" in second.started
    assert set(second.running) == {"R2", "C1"}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "fixture D: each invalid completion claim must fail R1 with its exact finalize "
        "reason while C1 remains blocked on R1:fit"
    ),
)
@pytest.mark.parametrize(
    ("claim", "expected_reason"),
    [
        ("missing_outcome", "outcome"),
        ("missing_production_alias", "production alias"),
        ("checksum_mismatch", "checksum"),
        ("superseded_lineage", "superseded lineage"),
    ],
)
def test_fixture_d_invalid_completion_never_unlocks_descendant(
    tmp_path, claim, expected_reason,
):
    scenario = _public("knowledge.ml_registry.testing.portfolio_fixtures", "invalid_completion_scenario")(
        tmp_path, claim=claim
    )
    result = scenario.finish_root_and_poll()

    assert scenario.record("R1").state == "failed"
    assert expected_reason in scenario.record("R1").message.lower()
    assert result.blocked["C1"] == "waiting on R1:fit"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "fixture G: readiness has no canonical checksum, freshness, and active-lineage "
        "verification service for promoted artifacts"
    ),
)
@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("tamper_bytes", "checksum"),
        ("backdate_manifest", "stale"),
        ("supersede_trial", "superseded lineage"),
    ],
)
def test_fixture_g_invalid_promoted_artifact_blocks_consumer(
    tmp_path, mutation, expected_reason,
):
    scenario = _public("knowledge.ml_registry.testing.portfolio_fixtures", "promoted_artifact_scenario")(
        tmp_path
    )
    scenario.apply(mutation)

    readiness = _public("knowledge.ml_registry.services.readiness", "explain_readiness")(
        scenario.registry, "C1"
    )

    assert readiness.ready is False
    assert readiness.artifact_id == scenario.fit_artifact_id
    assert expected_reason in readiness.reason.lower()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "fixture J: named-device leases and throughput/cotenancy conflicts are not yet "
        "represented, so status cannot explain a scientifically invalid empty slot"
    ),
)
@pytest.mark.parametrize("conflict", ["shared_cuda_0", "exclusive_cpu_throughput"])
def test_fixture_j_conflicting_ready_pair_uses_one_slot_and_explains_other(tmp_path, conflict):
    scenario = _public("knowledge.ml_registry.testing.portfolio_fixtures", "resource_conflict_scenario")(
        tmp_path, conflict=conflict
    )
    result = scenario.controller.tick()
    status = scenario.controller.status()

    assert len(result.started) == 1
    assert len(status.slots) == 2
    empty = next(slot for slot in status.slots if slot.campaign_id is None)
    assert conflict in empty.reason_code
    assert status.ready_frontier == scenario.ready_campaign_ids


@pytest.mark.xfail(
    strict=True,
    reason=(
        "fixture E: reconciliation is required at intent-written, spawned, arm-running, "
        "outcome-written, finalizing, and finalized-before-release crash boundaries"
    ),
)
@pytest.mark.parametrize(
    "boundary",
    [
        "intent_written",
        "process_spawned",
        "arm_running",
        "outcome_written",
        "finalizing",
        "finalized_before_lease_release",
    ],
)
def test_fixture_e_restart_is_idempotent_at_every_persisted_boundary(tmp_path, boundary):
    scenario = _public("knowledge.ml_registry.testing.portfolio_fixtures", "restart_scenario")(
        tmp_path, crash_after=boundary
    )
    scenario.run_until_crash()
    scenario.restart_and_reconcile()

    assert scenario.live_process_group_count("R1") <= 1
    assert scenario.trial_count("R1", idea_id="idea-1") == 1
    assert scenario.alias_count("R1", alias="production") <= 1


@pytest.mark.xfail(
    strict=True,
    reason=(
        "fixture F: force/drain stop semantics, process-group death proof, trial "
        "supersession, lease release, and StopReport are not implemented"
    ),
)
@pytest.mark.parametrize("mode", ["force", "drain"])
def test_fixture_f_stop_owns_process_groups_and_arm_admission(tmp_path, mode):
    scenario = _public("knowledge.ml_registry.testing.portfolio_fixtures", "two_root_process_scenario")(
        tmp_path
    )
    scenario.controller.tick()
    report = scenario.controller.stop(mode=mode)

    assert scenario.all_owned_process_groups_dead()
    assert scenario.active_leases() == ()
    if mode == "force":
        assert report.forced_terminations == frozenset({"R1", "R2"})
        assert scenario.all_in_flight_trials_superseded(reason="operator force stop")
    else:
        assert report.gracefully_drained == frozenset({"R1", "R2"})
        assert scenario.new_arms_started_after_stop == 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "fixture O: registry integrity audit does not yet prove every version code_sha "
        "exists and every champion move has a reason-bearing event"
    ),
)
def test_fixture_o_integrity_audit_checks_code_refs_and_champion_events(tmp_path):
    scenario = _public("knowledge.ml_registry.testing.registry_fixtures", "registry_invariant_scenario")(
        tmp_path
    )
    report = _public("knowledge.ml_registry.services.integrity", "audit_registry")(
        scenario.registry, repo=scenario.repo
    )

    assert report.missing_code_shas == ()
    assert report.champion_moves_without_reason_event == ()
