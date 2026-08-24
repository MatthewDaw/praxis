from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge.ml_registry.contracts import CampaignOutcome, CampaignOutcomeRecord
from knowledge.ml_registry.operations import CampaignOperations, CampaignOperationsError
from knowledge.ml_registry.runner import (
    CampaignDispatch,
    deregister_campaign,
    register_campaign_for_run,
    run_registered_campaigns,
)
from knowledge.ml_registry.services.registry_finalize import (
    ConvergePromoter,
    RegistryFinalizationError,
    RegistryFinalizeService,
)
from knowledge.ml_registry.storage import Registry
from knowledge.ml_registry.tests.test_campaign_runner import _fixture, _measured
from knowledge.ml_registry.tests.test_converge_promote import _view
from knowledge.ml_registry.tests.test_registry_native_adjudication import (
    create_run,
    promotion,
    registry_with_champion,
)


def _promoted_registry(
    root: Path,
    *,
    inverse: list[str] | None = None,
) -> tuple[Registry, ConvergePromoter]:
    registry = registry_with_champion(root)
    RegistryFinalizeService(registry).move_production(
        model_id="model", version=1, reason="existing production",
    )
    create_run(registry, "converge", .72)
    promoter = ConvergePromoter(
        registry,
        compatibility_loader=lambda _version, path, _head: path.read_bytes() == b"winner:converge",
        landing_commit=lambda finalized: f"commit-{finalized.model_version.version}",
        landing_commit_inverse=(
            None if inverse is None else lambda commit: inverse.append(commit) or f"revert-{commit}"
        ),
        min_measured=1,
    )
    promoter.run(
        _view(registry), run_id="converge", version=2, reason="full-length winner",
        promotion=promotion(registry, "converge"),
    )
    return registry, promoter


def test_unpromote_returns_commit_and_alias_as_one_result(tmp_path: Path) -> None:
    inverse_calls: list[str] = []
    registry, promoter = _promoted_registry(tmp_path, inverse=inverse_calls)

    result = promoter.unpromote(model_id="model", reason="bad campaign registration")

    assert result.landing_commit == "revert-commit-2"
    assert result.production_alias.version == 1
    assert inverse_calls == ["commit-2"]
    assert {row["alias"]: row["version"] for row in registry.rows("aliases")} == {
        "champion": 1,
        "production": 1,
    }


def test_unpromote_restores_both_aliases_when_landing_inverse_fails(tmp_path: Path) -> None:
    registry, promoter = _promoted_registry(tmp_path)

    def fail_inverse(_commit: str) -> str:
        raise RuntimeError("landing inverse failed")

    promoter.landing_commit_inverse = fail_inverse
    with pytest.raises(RegistryFinalizationError, match="landing inverse failed"):
        promoter.unpromote(model_id="model", reason="bad campaign registration")

    assert {row["alias"]: row["version"] for row in registry.rows("aliases")} == {
        "champion": 2,
        "production": 2,
    }


def test_deregistered_campaign_is_abandoned_and_runner_continues(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    for campaign_id in ("removed", "kept"):
        spec, corpora = _fixture(campaign_id)
        assert register_campaign_for_run(registry, spec, scoring_corpora=corpora)
    abandoned = deregister_campaign(registry, "removed", reason="registered in error")
    calls: list[str] = []

    def drive(dispatch: CampaignDispatch) -> CampaignOutcomeRecord:
        calls.append(dispatch.campaign.campaign_id)
        return _measured(dispatch)

    report = run_registered_campaigns(registry, drive)

    assert abandoned.outcome is CampaignOutcome.ABANDONED
    assert abandoned.reason == "registered in error"
    assert calls == ["kept"]
    assert [outcome.outcome for outcome in report.outcomes] == [
        CampaignOutcome.ABANDONED,
        CampaignOutcome.MEASURED,
    ]


def test_campaign_state_cleanup_requires_landing_and_preserves_external_traces(
    tmp_path: Path,
) -> None:
    state = tmp_path / "campaign_state"
    registry, _promoter = _promoted_registry(state)
    durable = tmp_path / "durable-telemetry"
    durable.mkdir()
    (durable / "dead-ends.jsonl").write_text(json.dumps({"trial": "rejected"}) + "\n")
    (durable / "rejected-arm.diff").write_text("diff --git a/model.py b/model.py\n")

    CampaignOperations(registry).delete_campaign_state(
        durable_trace_roots=(durable,),
    )

    assert not state.exists()
    assert (durable / "dead-ends.jsonl").exists()
    assert (durable / "rejected-arm.diff").exists()


def test_campaign_state_cleanup_refuses_before_landing_or_with_nested_traces(
    tmp_path: Path,
) -> None:
    state = tmp_path / "campaign_state"
    registry = Registry(state)
    operations = CampaignOperations(registry)

    with pytest.raises(CampaignOperationsError, match="landing commit"):
        operations.delete_campaign_state(durable_trace_roots=(tmp_path / "durable",))

    registry, _promoter = _promoted_registry(state)
    nested = state / "dead-end-registry"
    nested.mkdir()
    with pytest.raises(CampaignOperationsError, match="outside campaign_state"):
        CampaignOperations(registry).delete_campaign_state(
            durable_trace_roots=(nested,),
        )
    assert state.exists()
