"""A re-baseline is a promotion with no improvement verdict (constitution X.3).

Changing the judged vector (or the frozen slate) re-measures the champion under a
judge that did not produce the previous number. Recording that measurement as
``adopted`` claims a win nobody earned. The typed path is
``register_baseline_and_promote``; a previously mis-filed adoption is withdrawn
with ``reclassify_adoption_as_baseline`` without rolling the champion alias back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowledge.ml_registry.domain import VALID_RUN_STATUS_VERDICT_PAIRS
from knowledge.ml_registry.services.registry_aliases import (
    adopt_run_and_promote,
    reclassify_adoption_as_baseline,
    register_baseline_and_promote,
)
from knowledge.ml_registry.storage import RegistryError
from knowledge.ml_registry.tests.test_registry_native_adjudication import (
    create_run,
    promotion,
    registry_with_champion,
)


def test_baseline_is_a_legal_succeeded_pair() -> None:
    assert ("succeeded", "baseline") in VALID_RUN_STATUS_VERDICT_PAIRS


def test_register_baseline_promotes_without_an_adoption_verdict(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "rebaseline", 0.68)
    inputs = promotion(registry, "rebaseline")
    assert register_baseline_and_promote(
        registry, run_id="rebaseline", model_id="model",
        reason="re-baseline under amended vector; not a comparison to the previous scalars",
        model_version=inputs,
    ) is True
    row = next(r for r in registry.rows("runs") if r["run_id"] == "rebaseline")
    assert row["status"] == "succeeded"
    assert row["verdict"] == "baseline"
    alias = next(r for r in registry.rows("aliases") if r["alias"] == "champion")
    assert alias["version"] == 2
    assert "not a comparison" in alias["reason"]
    events = [e for e in registry.list_events() if e.event_type == "run_baselined"]
    assert len(events) == 1
    assert events[0].payload["run_id"] == "rebaseline"
    assert not any(
        e.event_type == "run_adopted" and e.payload.get("run_id") == "rebaseline"
        for e in registry.list_events()
    )


def test_baseline_registration_is_idempotent_and_semantic_drift_is_refused(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "rebaseline", 0.68)
    inputs = promotion(registry, "rebaseline")
    kwargs = dict(
        run_id="rebaseline", model_id="model",
        reason="re-baseline under amended vector", model_version=inputs,
    )
    assert register_baseline_and_promote(registry, **kwargs) is True
    count = len(registry.list_events())
    assert register_baseline_and_promote(registry, **kwargs) is False
    assert len(registry.list_events()) == count
    drifted = dict(inputs)
    drifted["preprocessing_hash"] = "other"
    with pytest.raises(RegistryError, match="drifted"):
        register_baseline_and_promote(
            registry, run_id="rebaseline", model_id="model",
            reason="re-baseline under amended vector", model_version=drifted,
        )


def test_reclassify_adoption_as_baseline_withdraws_the_win_and_keeps_the_champion(
    tmp_path: Path,
) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "false-adopt", 0.68)
    inputs = promotion(registry, "false-adopt")
    adopt_run_and_promote(
        registry, run_id="false-adopt", model_id="model",
        reason="re-baseline under merged vector judge; not a comparison",
        model_version=inputs,
    )
    reclassify_adoption_as_baseline(
        registry, run_id="false-adopt",
        reason="constitution X.3: a vector change never moves a number by itself; "
               "recording the re-baseline as adopted was a ledger lie",
    )
    row = next(r for r in registry.rows("runs") if r["run_id"] == "false-adopt")
    assert row["status"] == "succeeded"
    assert row["verdict"] == "baseline"
    alias = next(r for r in registry.rows("aliases") if r["alias"] == "champion")
    assert alias["version"] == 2
    events = [e for e in registry.list_events() if e.event_type == "adoption_reclassified_as_baseline"]
    assert len(events) == 1
    assert "ledger lie" in events[0].payload["reason"]


def test_reclassify_is_idempotent(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "false-adopt", 0.68)
    adopt_run_and_promote(
        registry, run_id="false-adopt", model_id="model",
        reason="re-baseline", model_version=promotion(registry, "false-adopt"),
    )
    reclassify_adoption_as_baseline(registry, run_id="false-adopt", reason="X.3")
    count = len(registry.list_events())
    reclassify_adoption_as_baseline(registry, run_id="false-adopt", reason="X.3")
    assert len(registry.list_events()) == count


def test_reclassify_refuses_a_run_that_was_not_adopted(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "parked-arm", 0.68)
    from knowledge.ml_registry.services.registry_aliases import adjudicate_run
    adjudicate_run(
        registry, run_id="parked-arm", verdict="parked", status="succeeded",
        reason="no gain",
    )
    with pytest.raises(RegistryError, match="improvement verdict"):
        reclassify_adoption_as_baseline(registry, run_id="parked-arm", reason="X.3")
