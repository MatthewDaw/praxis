"""Reclassify a rejected or parked run as abandoned: the judge never fairly saw the hypothesis.

A rejection is a verdict the judge reached. Abandoned is a decision taken before it could --
fitted on a superseded mute base, killed mid-fit, scored against a broken incumbent. The
canonical seam is ``abandon_run``; candidate code never writes a verdict. Abandoned is not
an answering verdict, so the idea goes back on the backlog.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowledge.ml_registry.domain import VALID_RUN_STATUS_VERDICT_PAIRS
from knowledge.ml_registry.services.completeness import campaign_coverage
from knowledge.ml_registry.services.registry_aliases import (
    abandon_run,
    adopt_run_and_promote,
    adjudicate_run,
    invalidate_adoption,
)
from knowledge.ml_registry.storage import RegistryError
from knowledge.ml_registry.tests.test_registry_completeness import (
    _fixture,
    _idea,
    _run,
    _view,
)
from knowledge.ml_registry.tests.test_registry_native_adjudication import (
    create_run,
    promotion,
    registry_with_champion,
)


def test_abandoned_is_a_legal_succeeded_pair() -> None:
    assert ("succeeded", "abandoned") in VALID_RUN_STATUS_VERDICT_PAIRS


def test_a_rejected_run_can_be_abandoned(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "mute-arm", 0.0)
    adjudicate_run(
        registry, run_id="mute-arm", verdict="rejected", status="succeeded",
        reason="scored 0.0 against champion",
    )
    abandon_run(
        registry, run_id="mute-arm",
        reason="fitted on the superseded unweighted baseline; the hypothesis was never tested",
    )
    row = next(r for r in registry.rows("runs") if r["run_id"] == "mute-arm")
    assert row["status"] == "succeeded"
    assert row["verdict"] == "abandoned"
    events = [e for e in registry.list_events() if e.event_type == "run_abandoned"]
    assert len(events) == 1
    assert "never tested" in events[0].payload["reason"]


def test_a_parked_run_can_be_abandoned(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "parked-arm", 0.0685)
    adjudicate_run(
        registry, run_id="parked-arm", verdict="parked", status="succeeded",
        reason="interval includes zero",
    )
    abandon_run(
        registry, run_id="parked-arm",
        reason="measured on the mute base; parked is not an answer until re-run on the champion",
    )
    row = next(r for r in registry.rows("runs") if r["run_id"] == "parked-arm")
    assert row["verdict"] == "abandoned"


def test_abandonment_is_idempotent(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "mute-arm", 0.0)
    adjudicate_run(
        registry, run_id="mute-arm", verdict="rejected", status="succeeded", reason="zero",
    )
    abandon_run(registry, run_id="mute-arm", reason="mute base")
    count = len(registry.list_events())
    abandon_run(registry, run_id="mute-arm", reason="mute base")
    assert len(registry.list_events()) == count


def test_an_adopted_champion_cannot_be_abandoned(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    with pytest.raises(RegistryError, match="rejected, parked, or superseded"):
        abandon_run(registry, run_id="baseline", reason="no")


def test_an_invalidated_adoption_can_be_recorded_as_abandoned(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "superseded-slate", 0.9)
    adopt_run_and_promote(
        registry, run_id="superseded-slate", model_id="model", reason="measured win",
        model_version=promotion(registry, "superseded-slate"),
    )
    invalidate_adoption(registry, {
        "model_id": "model",
        "invalidated_version": 2,
        "parent_version": 1,
        "adoption_run_id": "superseded-slate",
        "evidence_run_ids": [],
        "invalidated_lineage_id": "model@2",
        "requeue_idea_ids": ["idea-superseded-slate"],
        "reason": "judge slate changed",
    })
    abandon_run(
        registry, run_id="superseded-slate",
        reason="measured on a slate that is no longer judged",
    )
    row = next(r for r in registry.rows("runs") if r["run_id"] == "superseded-slate")
    assert (row["status"], row["verdict"]) == ("succeeded", "abandoned")


def test_a_reason_is_required(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "mute-arm", 0.0)
    adjudicate_run(
        registry, run_id="mute-arm", verdict="rejected", status="succeeded", reason="zero",
    )
    with pytest.raises(RegistryError, match="reason"):
        abandon_run(registry, run_id="mute-arm", reason="  ")


def test_abandoned_does_not_answer_the_idea(tmp_path: Path) -> None:
    space, registry, binding = _fixture(tmp_path)
    idea = _idea(space, binding, "arm")
    _run(registry, idea, "mute", verdict="rejected")
    abandon_run(
        registry, run_id="mute",
        reason="fitted on a superseded mute base; the hypothesis was never tested",
    )
    out = campaign_coverage(_view(space, registry, binding), registry, min_measured=1)
    assert out["coverage"][0]["measured"] == 0
    assert "stage_open" in {item["kind"] for item in out["blocking"]}
