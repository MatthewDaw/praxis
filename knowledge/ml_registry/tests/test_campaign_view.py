from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from knowledge.ml_registry.domain import CampaignBinding
from knowledge.ml_registry.services import CampaignViewError, build_campaign_view
from knowledge.ml_registry.storage import Registry
from knowledge.ml_registry.write_path import RegistrySpace


REPO = Path(__file__).resolve().parents[3]
SHA = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], check=True,
                     capture_output=True, text=True).stdout.strip()


def _stores(tmp_path: Path) -> tuple[RegistrySpace, Registry, CampaignBinding, str, str]:
    space = RegistrySpace()
    model_fact = space.insert("model", {"metric": "score"})
    first = space.insert("idea", {"model_id": model_fact, "id": "pretty", "stage": "representation"})
    second = space.insert("idea", {"model_id": model_fact, "id": "next", "axis": "architecture",
                                   "depends_on": ["pretty"]})
    registry = Registry(tmp_path)
    registry.create_experiment(experiment_id="campaign", spec_digest="d" * 64,
                               stages=["representation", "architecture"], metric="score",
                               direction="maximize", win_condition={"delta": 0.1},
                               rope=.01, baseline_throughput=1.0)
    registry.register_model(model_id="registered", family="f", sport_scope="shared", axis="a",
                            protocol="P", extends=None)
    registry.create_run(
        run_id="run", experiment_id="campaign", idea_id=first, stage="representation", family="f",
        params={}, metrics={}, code_ref={"schema_version": 1, "repo": str(REPO), "sha": SHA,
        "base_sha": SHA, "diff_hash": "d" * 64, "diff_lines": 0}, device_fingerprint="cpu",
        status="running", verdict=None, started_at=1, finished_at=None, claim_owner="w", heartbeat_at=1,
    )
    return space, registry, CampaignBinding("campaign", "registered", model_fact), first, second


def test_view_joins_only_on_fact_id_and_canonicalizes_dependencies(tmp_path: Path):
    space, registry, binding, first, second = _stores(tmp_path)
    view = build_campaign_view(space, registry, binding)
    assert [idea.fact_id for idea in view.ideas] == [first, second]
    assert view.ideas[0].display_id == "pretty"
    assert view.ideas[1].depends_on == (first,)
    assert view.runs[0]["idea_id"] == first


@pytest.mark.parametrize(("mutation", "message"), [
    (lambda space, registry, binding, first, second:
     space.get(second).meta.update(stage="invented"), "unknown stage"),
    (lambda space, registry, binding, first, second:
     space.get(second).meta.update(depends_on=["missing"]), "unknown dependency"),
])
def test_view_rejects_unknown_stage_or_dependency(tmp_path: Path, mutation, message):
    space, registry, binding, first, second = _stores(tmp_path)
    mutation(space, registry, binding, first, second)
    with pytest.raises(CampaignViewError, match=message):
        build_campaign_view(space, registry, binding)


def test_view_rejects_run_joined_by_display_tag(tmp_path: Path):
    space, registry, binding, first, _ = _stores(tmp_path)
    with registry._connect("run_superseded") as db:
        db.execute("DROP TRIGGER guard_runs_update")
        db.execute("UPDATE runs SET idea_id='pretty' WHERE run_id='run'")
    with pytest.raises(CampaignViewError, match="orphan run"):
        build_campaign_view(space, registry, binding)


def test_legacy_statuses_require_explicit_compatibility_migration():
    from knowledge.ml_registry.contracts import migrate_legacy_trial_state
    from knowledge.ml_registry.domain import TrialStatus

    with pytest.raises(ValueError):
        TrialStatus("stagnant")
    assert migrate_legacy_trial_state("stagnant") == ("succeeded", "parked")
    assert migrate_legacy_trial_state("errored") == ("failed", None)
