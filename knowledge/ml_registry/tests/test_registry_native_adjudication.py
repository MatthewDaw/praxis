from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from knowledge.ml_registry import Registry
from knowledge.ml_registry.contracts import ContractError
from knowledge.ml_registry.floor import (
    FLOOR_ADOPTION_INSIDE_ROPE_FIELD,
    FLOOR_ADOPTION_UNSUPPORTED_BY_INTERVAL_FIELD,
)
from knowledge.ml_registry.domain import VALID_RUN_STATUS_VERDICT_PAIRS
from knowledge.ml_registry.services.registry_adjudication import adjudicate_against_champion
from knowledge.ml_registry.services.registry_aliases import adopt_run_and_promote
from knowledge.ml_registry.services.registry_aliases import record_ratchet_evidence
from knowledge.ml_registry.services.registry_ratchet import consider_rejection, reconcile_registry_space_requeue
from knowledge.ml_registry.services.registry_runs import complete_run
from knowledge.ml_registry.storage import RegistryError
from knowledge.ml_registry.write_path import Fact, RegistrySpace


REPO = Path(__file__).resolve().parents[3]
SHA = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], check=True,
                     capture_output=True, text=True).stdout.strip()
DIFF = "d" * 64
FAIRNESS = {"dataset_digest": "dataset-v1", "split_digest": "split-v1", "seed": 17,
            "harness_digest": "harness-v1", "preprocessing_digest": "preprocess-v1"}


def metrics(value: float, *, validity: str = "valid", throughput: float = 3.5,
            unit: str = "rows_per_second") -> dict[str, object]:
    return {"metric": value, "validity": validity, "throughput": throughput,
            "throughput_unit": unit, "memory_gb": 1.25, "cpu_time": 8.0,
            "load": {"start_1m": 0.2, "end_1m": 0.4}}


def create_run(registry: Registry, run_id: str, value: float, *, experiment_id: str = "campaign",
               idea_id: str | None = None, params: dict[str, object] | None = None,
               **metric_overrides: object) -> None:
    registry.create_run(
        run_id=run_id, experiment_id=experiment_id, idea_id=idea_id or f"idea-{run_id}", stage="representation",
        family="linear", params=params or {}, metrics={}, code_ref={"schema_version": 1, "repo": str(REPO),
        "sha": SHA, "base_sha": SHA, "diff_hash": DIFF, "diff_lines": 1},
        device_fingerprint="cpu:test", status="running", verdict=None, started_at=1,
        finished_at=None, claim_owner="trainer", heartbeat_at=1,
    )
    values = metrics(value)
    values.update(metric_overrides)
    complete_run(registry, run_id=run_id, metrics=values)


def registry_with_champion(tmp_path: Path) -> Registry:
    registry = Registry(tmp_path)
    registry.create_experiment(experiment_id="campaign", spec_digest="a" * 64,
        stages=["representation"], metric="f1", direction="maximize",
        win_condition={"metric_at_least": 0.9}, rope=0.01, baseline_throughput=3.3)
    registry.register_model(model_id="model", family="linear", sport_scope="shared", axis="a01",
                            protocol="Detector", extends=None)
    create_run(registry, "baseline", 0.68)
    artifact = registry.create_artifact(run_id="baseline", kind="checkpoint", content=b"base", schema_version="1")
    adopt_run_and_promote(registry, run_id="baseline", model_id="model", reason="bootstrap",
        model_version={"version": 1, "artifact_id": artifact, "checksum": artifact,
        "family_version": "linear@1", "code_sha": SHA, "preprocessing_hash": "prep",
        "calibration": {}, "thresholds": {},
        "compat_result": {"head_sha": SHA, "passed": True, "at": 1}, "status": "active"})
    return registry


def register_adjudication_policy(
    registry: Registry, *, method: str = "paired_bootstrap_percentile",
    aggregation: str = "mean", adoption_floor: float | None = None,
) -> None:
    floor = {} if adoption_floor is None else {"adoption_floor": adoption_floor}
    registry.register_campaign_spec({
        "schema_version": 1,
        "campaign_id": "campaign",
        "model_id_policy": "existing_registered_model",
        "axis": "a01",
        "sport_scope": ["shared"],
        "target_ontology": "fixture",
        "metric": {
            **floor,
            "name": "f1",
            "direction": "maximize",
            "operating_point": {"selection": "frozen", "threshold": .5},
            "aggregation": [{"level": "unit", "unit": "unit_id", "minimum_sample": 2}],
            "scoring_corpus": "fixture",
            "split_unit": "unit_id",
            "adjudication": {
                "method": method,
                "resamples": 500,
                "confidence_level": .95,
                "seed": 17,
                "aggregation": aggregation,
            },
        },
        "stages": [{"name": "representation"}],
        "corpora": [{"id": "fixture", "roles": ["scoring"], "split_unit": "unit_id"}],
        "requires": [],
        "produces": [{"artifact_type": "checkpoint", "schema_version": "1"}],
        "supervision": {"mode": "composing"},
        "resources": {"lane": "cpu"},
        "isolation": {"state_root": "state/campaign"},
        "production": {"protocol": "Detector"},
        "extends": [],
        "deterministic_incumbent": None,
        "learned_escalation": False,
        "rope": None,
    }, scoring_corpora={"fixture": [
        {"unit_id": "one", "f1": .67},
        {"unit_id": "two", "f1": .69},
    ]})


def paired_evidence(candidate: list[float], champion: list[float]) -> dict[str, object]:
    return {
        "candidate_run_id": "candidate",
        "champion_run_id": "baseline",
        "resamples": 500,
        "confidence_level": .95,
        "seed": 17,
        "units": [
            {"unit_id": f"unit-{index}", "candidate": candidate_value,
             "champion": champion_value}
            for index, (candidate_value, champion_value) in enumerate(
                zip(candidate, champion, strict=True)
            )
        ],
    }


def promotion(registry: Registry, run_id: str, version: int = 2) -> dict[str, object]:
    artifact = registry.create_artifact(run_id=run_id, kind="checkpoint",
                                        content=f"winner:{run_id}".encode(), schema_version="1")
    return {"version": version, "artifact_id": artifact, "checksum": artifact,
            "family_version": "linear@1", "code_sha": SHA, "preprocessing_hash": "prep",
            "calibration": {}, "thresholds": {},
            "compat_result": {"head_sha": SHA, "passed": True, "at": 2}, "status": "active"}


def test_typed_metrics_reject_missing_invalid_and_nonfinite_measurements(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    registry.create_experiment(experiment_id="campaign", spec_digest="a" * 64, stages=["s"], metric="f1",
        direction="maximize", win_condition={}, rope=0.01, baseline_throughput=1)
    registry.create_run(run_id="run", experiment_id="campaign", idea_id="i", stage="s", family="f",
        params={}, metrics={}, code_ref={"schema_version": 1, "repo": str(REPO), "sha": SHA,
        "base_sha": SHA, "diff_hash": DIFF, "diff_lines": 0}, device_fingerprint="cpu", status="running",
        verdict=None, started_at=1, finished_at=None, claim_owner="trainer", heartbeat_at=1)
    with pytest.raises(RegistryError, match="missing=.*cpu_time"):
        complete_run(registry, run_id="run", metrics={"metric": 1})
    bad = metrics(float("nan"))
    with pytest.raises(RegistryError, match="finite"):
        complete_run(registry, run_id="run", metrics=bad)


# RETARGETED by the adoption floor. The champion scores 0.68, so the old "parked" fixture
# (0.685) is a gain of EXACTLY 0.005 -- the floor -- and is now a win on this path too; it is
# kept, as an adoption, rather than deleted. 0.684 is a gain of 0.004, under the floor and
# inside this experiment's 0.01 rope, which is what the park case was always testing.
@pytest.mark.parametrize(("value", "validity", "expected", "status"), [
    (0.70, "valid", "adopted", "succeeded"),
    (0.685, "valid", "adopted", "succeeded"),   # exactly the floor: adopted, rope notwithstanding
    (0.684, "valid", "parked", "succeeded"),    # under the floor: the rope decides, and parks
    (0.66, "valid", "rejected", "succeeded"),
    (0.72, "invalid", "voided", "voided"),
])
def test_registry_adjudicator_owns_verdict_and_compares_current_champion(
    tmp_path: Path, value: float, validity: str, expected: str, status: str,
) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "candidate", value, validity=validity)
    inputs = promotion(registry, "candidate") if expected == "adopted" else None
    assert adjudicate_against_champion(registry, run_id="candidate", model_id="model",
                                      reason="external comparison", promotion=inputs) == expected
    row = next(row for row in registry.rows("runs") if row["run_id"] == "candidate")
    assert row["verdict"] == expected and row["status"] == status
    champion = next(row for row in registry.rows("aliases") if row["alias"] == "champion")
    assert champion["version"] == (2 if expected == "adopted" else 1)


@pytest.mark.parametrize(("candidate_values", "expected"), [
    ([.62, .67, .72, .79], "adopted"),
    ([.58, .67, .68, .79], "parked"),
    ([.58, .63, .68, .75], "rejected"),
])
def test_paired_campaign_uses_frozen_interval_instead_of_scalar_rope(
    tmp_path: Path, candidate_values: list[float], expected: str,
) -> None:
    registry = registry_with_champion(tmp_path)
    register_adjudication_policy(registry)
    champion_values = [.60, .65, .70, .77]
    create_run(registry, "candidate", sum(candidate_values) / len(candidate_values))
    inputs = promotion(registry, "candidate") if expected == "adopted" else None

    verdict = adjudicate_against_champion(
        registry,
        run_id="candidate",
        model_id="model",
        reason="paired comparison",
        promotion=inputs,
        paired_evidence=paired_evidence(candidate_values, champion_values),
    )

    assert verdict == expected
    event = next(
        event for event in reversed(registry.list_events())
        if event.event_type in {"run_adopted", "run_adjudicated"}
        and event.payload["run_id"] == "candidate"
    )
    evidence = event.payload["adjudication_evidence"]
    assert evidence["method"] == "paired_bootstrap_percentile"
    assert evidence["unit_count"] == 4
    assert evidence["resamples"] == 500
    assert len(evidence["units"]) == 4
    if expected == "adopted":
        assert evidence["interval"][0] > 0
    elif expected == "rejected":
        assert evidence["interval"][1] < 0
    else:
        assert evidence["interval"][0] <= 0 <= evidence["interval"][1]


# ---------------------------------------------------------------------------
# THE ADOPTION FLOOR on the canonical registry's paired path -- the branch every real
# campaign takes. The champion scores 0.68. `FLOOR_UNITS` are four paired units whose
# aggregate gain is EXACTLY the 0.005 default floor and whose per-unit deltas
# (+.10, -.09, +.08, -.07) scatter so widely that the 95% bootstrap interval is
# [-0.080, +0.090] -- it straddles zero, so before the floor existed this parked. That is
# the case the rule exists for: a real gain thrown away because the evidence around it is
# noisy.
# ---------------------------------------------------------------------------
CHAMPION_UNITS = [.60, .65, .70, .77]
FLOOR_UNITS = [.70, .56, .78, .70]        # aggregate .685 -- a gain of exactly 0.005
UNDER_FLOOR_UNITS = [.70, .56, .78, .696]  # aggregate .684 -- a gain of 0.004, just under
WORSE_UNITS = [.58, .63, .68, .75]         # aggregate .660 -- interval entirely negative
CLEAN_WIN_UNITS = [.62, .67, .72, .79]     # aggregate .700 -- interval entirely positive


def _adjudicate_paired(registry: Registry, candidate_units: list[float], *, adopt: bool) -> str:
    create_run(registry, "candidate", sum(candidate_units) / len(candidate_units))
    return adjudicate_against_champion(
        registry, run_id="candidate", model_id="model", reason="paired comparison",
        promotion=promotion(registry, "candidate") if adopt else None,
        paired_evidence=paired_evidence(candidate_units, CHAMPION_UNITS),
    )


def _evidence(registry: Registry) -> dict:
    event = next(
        event for event in reversed(registry.list_events())
        if event.event_type in {"run_adopted", "run_adjudicated"}
        and event.payload["run_id"] == "candidate"
    )
    return event.payload["adjudication_evidence"]


def test_a_floor_gain_adopts_on_the_paired_path_even_though_its_interval_straddles_zero(
    tmp_path: Path,
) -> None:
    """THE CASE THAT PARKS TODAY. The gain is exactly 0.5 points and the 95% paired interval
    runs from -0.080 to +0.090, so the evidence alone cannot distinguish it from noise. The
    floor decides, and the interval is recorded UNCHANGED beside the verdict it did not
    reach -- two facts, both durable, allowed to disagree."""
    registry = registry_with_champion(tmp_path)
    register_adjudication_policy(registry)

    assert _adjudicate_paired(registry, FLOOR_UNITS, adopt=True) == "adopted"

    evidence = _evidence(registry)
    assert evidence["interval"][0] < 0 < evidence["interval"][1]  # the interval still straddles
    assert evidence["point_estimate"] == pytest.approx(0.005)
    champion = next(row for row in registry.rows("aliases") if row["alias"] == "champion")
    assert champion["version"] == 2


def test_a_gain_under_the_floor_with_a_straddling_interval_still_parks(tmp_path: Path) -> None:
    """One thousandth of a point lower and the floor has nothing to say: the interval decides,
    exactly as it did before, and a straddling interval parks."""
    registry = registry_with_champion(tmp_path)
    register_adjudication_policy(registry)

    assert _adjudicate_paired(registry, UNDER_FLOOR_UNITS, adopt=False) == "parked"


def test_an_entirely_negative_interval_still_rejects_with_the_floor_in_place(
    tmp_path: Path,
) -> None:
    """The reject branch is untouched. A candidate 2 points WORSE has a gain of -0.02, which
    clears no floor in the improving direction, so the ordering of the two tests never gets a
    chance to matter here -- and must not be rearranged until it does."""
    registry = registry_with_champion(tmp_path)
    register_adjudication_policy(registry)

    assert _adjudicate_paired(registry, WORSE_UNITS, adopt=False) == "rejected"
    assert _evidence(registry)["interval"][1] < 0


def test_the_audit_mark_says_when_the_interval_did_not_support_a_floor_adoption(
    tmp_path: Path,
) -> None:
    """The mark is stamped on the adoption the evidence did not reach, and ONLY on it: a
    floor-sized gain whose interval is entirely positive was supported by the interval too,
    so there is nothing for an auditor to know. A campaign where most adoptions carry the
    mark is reporting a harness too noisy to steer by rather than a run of weak arms."""
    straddled = registry_with_champion(tmp_path / "straddled")
    register_adjudication_policy(straddled)
    assert _adjudicate_paired(straddled, FLOOR_UNITS, adopt=True) == "adopted"

    supported = registry_with_champion(tmp_path / "supported")
    register_adjudication_policy(supported)
    assert _adjudicate_paired(supported, CLEAN_WIN_UNITS, adopt=True) == "adopted"

    assert _evidence(straddled)[FLOOR_ADOPTION_UNSUPPORTED_BY_INTERVAL_FIELD] is True
    assert FLOOR_ADOPTION_UNSUPPORTED_BY_INTERVAL_FIELD not in _evidence(supported)


def test_a_campaign_declaring_its_own_floor_on_this_path_is_judged_on_that_number(
    tmp_path: Path,
) -> None:
    """Declared in the CampaignSpec's `metric` object -- the judge, frozen before any run --
    and read by the SAME `floor.declared_adoption_floor` the Praxis-space path uses. A 2%
    floor parks the gain the 0.5% default would have adopted."""
    registry = registry_with_champion(tmp_path)
    register_adjudication_policy(registry, adoption_floor=0.02)

    assert _adjudicate_paired(registry, FLOOR_UNITS, adopt=False) == "parked"


def test_a_floor_that_names_no_gain_is_refused_when_the_campaign_spec_is_registered(
    tmp_path: Path,
) -> None:
    """The floor is declared once, before the baseline, so registration is where an unusable
    one has to die -- the canonical-registry twin of `write_path.register_model`'s refusal."""
    registry = registry_with_champion(tmp_path)

    with pytest.raises(ContractError, match="adoption_floor"):
        register_adjudication_policy(registry, adoption_floor=0.0)


def test_the_floor_binds_on_the_legacy_scalar_rope_branch_too(tmp_path: Path) -> None:
    """Coherence: the registry must not answer "is this a win" two ways depending on which
    method a campaign declared. A gain of exactly the floor is adopted here as well, and it
    carries the rope path's own mark because it sits inside this experiment's 0.01 rope."""
    registry = registry_with_champion(tmp_path)
    register_adjudication_policy(registry, method="legacy_scalar_rope")
    create_run(registry, "candidate", .685)

    verdict = adjudicate_against_champion(
        registry, run_id="candidate", model_id="model", reason="declared legacy",
        promotion=promotion(registry, "candidate"),
    )

    assert verdict == "adopted"
    assert _evidence(registry)[FLOOR_ADOPTION_INSIDE_ROPE_FIELD] is True


def test_paired_campaign_refuses_missing_or_moved_judging_contract(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    register_adjudication_policy(registry)
    create_run(registry, "candidate", .70)
    evidence = paired_evidence([.62, .67, .72, .79], [.60, .65, .70, .77])

    with pytest.raises(RegistryError, match="requires explicit same-unit"):
        adjudicate_against_champion(
            registry, run_id="candidate", model_id="model", reason="missing evidence",
        )
    with pytest.raises(RegistryError, match="confidence_level differs"):
        adjudicate_against_champion(
            registry, run_id="candidate", model_id="model", reason="moved judge",
            paired_evidence={**evidence, "confidence_level": .90},
        )
    duplicate = dict(evidence)
    duplicate["units"] = [*evidence["units"][:1], *evidence["units"][:1]]
    with pytest.raises(RegistryError, match="repeats unit_id"):
        adjudicate_against_champion(
            registry, run_id="candidate", model_id="model", reason="duplicate",
            paired_evidence=duplicate,
        )
    assert next(row for row in registry.rows("runs") if row["run_id"] == "candidate")["verdict"] is None


def test_registered_legacy_campaign_must_name_scalar_rope_and_refuses_paired_input(
    tmp_path: Path,
) -> None:
    registry = registry_with_champion(tmp_path)
    register_adjudication_policy(registry, method="legacy_scalar_rope")
    # .684 is a gain of 0.004 against the 0.68 champion -- under the adoption floor, so this
    # test still asks its own question (a legacy spec refuses paired input) rather than
    # accidentally asking whether the floor binds.
    create_run(registry, "candidate", .684)
    evidence = paired_evidence([.60, .67], [.59, .66])
    with pytest.raises(RegistryError, match="legacy_scalar_rope"):
        adjudicate_against_champion(
            registry, run_id="candidate", model_id="model", reason="wrong evidence",
            paired_evidence=evidence,
        )
    assert adjudicate_against_champion(
        registry, run_id="candidate", model_id="model", reason="declared legacy",
    ) == "parked"


def test_adoption_requires_promotion_inputs_and_units_must_match(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "winner", .72)
    with pytest.raises(RegistryError, match="artifact and compatibility"):
        adjudicate_against_champion(registry, run_id="winner", model_id="model", reason="won")
    assert next(row for row in registry.rows("runs") if row["run_id"] == "winner")["verdict"] is None

    other = registry_with_champion(tmp_path / "other")
    create_run(other, "candidate", .72, throughput_unit="samples_per_second")
    with pytest.raises(RegistryError, match="incomparable"):
        adjudicate_against_champion(other, run_id="candidate", model_id="model", reason="won")


def test_exactly_eight_registry_tables_and_metrics_are_canonical_json(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    assert len(registry.table_names()) == 8
    stored = json.loads(next(row for row in registry.rows("runs") if row["run_id"] == "baseline")["metrics"])
    assert stored == metrics(.68)


def test_run_status_and_scientific_verdict_have_one_exhaustive_pair_matrix() -> None:
    assert VALID_RUN_STATUS_VERDICT_PAIRS == {
        ("running", None), ("complete", None), ("succeeded", "adopted"),
        ("succeeded", "rejected"), ("succeeded", "parked"), ("succeeded", "abandoned"),
        ("failed", None), ("voided", "voided"), ("superseded", None),
    }


def test_atomic_adoption_retry_is_idempotent_and_semantic_drift_is_refused(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "winner", .72)
    inputs = promotion(registry, "winner")
    assert adjudicate_against_champion(registry, run_id="winner", model_id="model",
                                      reason="won", promotion=inputs) == "adopted"
    count = len(registry.list_events())
    assert adjudicate_against_champion(registry, run_id="winner", model_id="model",
                                      reason="won", promotion=inputs) == "adopted"
    assert len(registry.list_events()) == count
    with pytest.raises(RegistryError, match="full semantic payload"):
        adjudicate_against_champion(registry, run_id="winner", model_id="model",
                                    reason="different", promotion=inputs)


def test_paired_adoption_retry_reuses_frozen_evidence_and_refuses_drift(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    register_adjudication_policy(registry)
    create_run(registry, "candidate", .72)
    inputs = promotion(registry, "candidate")
    evidence = paired_evidence([.71, .72, .73], [.67, .68, .69])
    assert adjudicate_against_champion(
        registry, run_id="candidate", model_id="model", reason="won",
        promotion=inputs, paired_evidence=evidence,
    ) == "adopted"
    count = len(registry.list_events())
    assert adjudicate_against_champion(
        registry, run_id="candidate", model_id="model", reason="won", promotion=inputs,
    ) == "adopted"
    assert len(registry.list_events()) == count
    drifted = dict(evidence)
    drifted["seed"] = 18
    with pytest.raises(RegistryError, match="drifted from its paired evidence"):
        adjudicate_against_champion(
            registry, run_id="candidate", model_id="model", reason="won",
            promotion=inputs, paired_evidence=drifted,
        )


def test_atomic_adoption_recovers_all_projections_after_event_boundary_crash(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "winner", .72)
    inputs = promotion(registry, "winner")

    def crash(event):
        if event.event_type == "run_adopted":
            raise RuntimeError("crash after atomic adoption event")

    registry.after_event = crash
    with pytest.raises(RuntimeError, match="atomic adoption event"):
        adjudicate_against_champion(registry, run_id="winner", model_id="model",
                                    reason="won", promotion=inputs)
    assert next(row for row in registry.rows("runs") if row["run_id"] == "winner")["status"] == "complete"
    assert not any(row["run_id"] == "winner" for row in registry.rows("model_versions"))
    assert next(row for row in registry.rows("aliases") if row["alias"] == "champion")["version"] == 1

    recovered = Registry(tmp_path)
    run = next(row for row in recovered.rows("runs") if row["run_id"] == "winner")
    assert (run["status"], run["verdict"]) == ("succeeded", "adopted")
    assert any(row["run_id"] == "winner" for row in recovered.rows("model_versions"))
    assert next(row for row in recovered.rows("aliases") if row["alias"] == "champion")["version"] == 2


def test_champion_baseline_cannot_cross_experiment_boundary(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    registry.create_experiment(experiment_id="other", spec_digest="b" * 64, stages=["representation"],
        metric="f1", direction="maximize", win_condition={}, rope=.01, baseline_throughput=3.3)
    create_run(registry, "other-run", .72, experiment_id="other")
    with pytest.raises(RegistryError, match="different experiment"):
        adjudicate_against_champion(registry, run_id="other-run", model_id="model", reason="crossed")


def adopt_version(registry: Registry, run_id: str, value: float, version: int) -> None:
    create_run(registry, run_id, value)
    adjudicate_against_champion(
        registry, run_id=run_id, model_id="model", reason="adoption",
        promotion=promotion(registry, run_id, version),
    )


def paired_rejection(registry: Registry, number: int, *, observed: float, counterfactual: float,
                     active_version: int = 2, parent_version: int = 1,
                     digest: str | None = None, validity: str = "valid") -> None:
    idea_id = f"idea-pair-{number}"
    digest = digest or f"sha256:pair-{number}"
    create_run(registry, f"cf-{number}", counterfactual, idea_id=idea_id,
               params={"evaluated_model_version": parent_version, "intervention_digest": digest,
                       **FAIRNESS},
               validity=validity)
    create_run(registry, f"observed-{number}", observed, idea_id=idea_id,
               params={"evaluated_model_version": active_version, "intervention_digest": digest,
                       "rejected_under_lineage_id": f"model@{active_version}", **FAIRNESS})
    adjudicate_against_champion(
        registry, run_id=f"observed-{number}", model_id="model", reason="paired rejection",
        counterfactual_run_id=f"cf-{number}", intervention_digest=digest,
    )


def test_registry_genuine_win_survives_three_noop_pairs_against_its_parent(tmp_path: Path) -> None:
    """Registry fixture for the old streak ratchet's genuine-win counterexample."""
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "real-win", .80, 2)

    for number in range(3):
        paired_rejection(registry, number, observed=.68, counterfactual=.68)

    champion = next(row for row in registry.rows("aliases") if row["alias"] == "champion")
    assert champion["version"] == 2
    assert not any(event.event_type == "adoption_invalidated" for event in registry.list_events())


def test_registry_ratchet_rolls_back_harm_inherited_by_three_distinct_children(tmp_path: Path) -> None:
    """Registry fixture for the old counterfactual rule's harmful-adoption blind spot."""
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "bad-adoption", .70, 2)

    for number in range(3):
        paired_rejection(registry, number, observed=.50, counterfactual=.68)

    champion = next(row for row in registry.rows("aliases") if row["alias"] == "champion")
    assert (champion["version"], champion["set_by"]) == (1, "ratchet")
    adoption = next(row for row in registry.rows("runs") if row["run_id"] == "bad-adoption")
    assert (adoption["status"], adoption["verdict"]) == ("superseded", None)
    rollback = [event for event in registry.list_events() if event.event_type == "adoption_invalidated"]
    assert len(rollback) == 1
    assert rollback[0].payload["evidence_run_ids"] == ["observed-0", "observed-1", "observed-2"]
    assert rollback[0].payload["requeue_idea_ids"] == ["idea-pair-0", "idea-pair-1", "idea-pair-2"]
    assert len(registry.table_names()) == 8


def test_ratchet_evidence_retry_is_idempotent_and_semantic_drift_is_refused(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "adoption", .70, 2)
    paired_rejection(registry, 0, observed=.50, counterfactual=.68)
    evidence = [event for event in registry.list_events()
                if event.event_type == "ratchet_evidence_recorded"]
    count = len(registry.list_events())
    assert consider_rejection(
        registry, run_id="observed-0", model_id="model",
        counterfactual_run_id="cf-0", intervention_digest="sha256:pair-0",
    ) is False
    assert len(registry.list_events()) == count
    drift = dict(evidence[0].payload)
    drift["evidence_digest"] = "0" * 64
    with pytest.raises(RegistryError, match="retry drifted"):
        record_ratchet_evidence(registry, drift)


def test_retry_of_pair_that_fired_rollback_is_idempotently_true(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "adoption", .70, 2)
    for number in range(3):
        paired_rejection(registry, number, observed=.50, counterfactual=.68)
    count = len(registry.list_events())
    assert consider_rejection(
        registry, run_id="observed-2", model_id="model",
        counterfactual_run_id="cf-2", intervention_digest="sha256:pair-2",
    ) is True
    assert len(registry.list_events()) == count


def test_ratchet_rollback_recovers_atomically_after_event_boundary_crash(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "bad-adoption", .70, 2)
    paired_rejection(registry, 0, observed=.50, counterfactual=.68)
    paired_rejection(registry, 1, observed=.50, counterfactual=.68)

    def crash(event):
        if event.event_type == "adoption_invalidated":
            raise RuntimeError("crash after rollback event")

    registry.after_event = crash
    with pytest.raises(RuntimeError, match="rollback event"):
        paired_rejection(registry, 2, observed=.50, counterfactual=.68)
    assert next(row for row in registry.rows("aliases") if row["alias"] == "champion")["version"] == 2

    recovered = Registry(tmp_path)
    champion = next(row for row in recovered.rows("aliases") if row["alias"] == "champion")
    adoption = next(row for row in recovered.rows("runs") if row["run_id"] == "bad-adoption")
    assert (champion["version"], champion["set_by"]) == (1, "ratchet")
    assert (adoption["status"], adoption["verdict"]) == ("superseded", None)


def test_stacked_adoption_records_current_champion_as_its_direct_parent(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "win-2", .70, 2)
    adopt_version(registry, "win-3", .72, 3)
    edge = next(row for row in registry.rows("lineage")
                if row["child_model_id"] == "model" and row["child_version"] == 3)
    assert (edge["parent_model_id"], edge["parent_version"], edge["kind"]) == (
        "model", 2, "derived_from")


def test_invalidated_version_is_effectively_superseded_and_cannot_be_production(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "adoption", .70, 2)
    for number in range(3):
        paired_rejection(registry, number, observed=.50, counterfactual=.68)
    assert registry.effective_model_version("model", 2)["effective_status"] == "superseded"
    from knowledge.ml_registry.services.registry_finalize import RegistryFinalizeService
    with pytest.raises(ValueError, match="incompatible"):
        RegistryFinalizeService(registry).move_production(
            model_id="model", version=2, reason="invalidated lineage must not ship")


def test_registry_space_requeue_reconciles_exact_lineage_once(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "adoption", .70, 2)
    for number in range(3):
        paired_rejection(registry, number, observed=.50, counterfactual=.68)
    rollback = next(event for event in registry.list_events() if event.event_type == "adoption_invalidated")
    space = RegistrySpace()
    for number in range(3):
        idea_id = f"idea-pair-{number}"
        space.facts[idea_id] = Fact(idea_id, "idea", {
            "model_id": "model", "origin": "seeded", "axis": "a", "description": idea_id,
            "status": "rejected", "rejection_reason": "bad bar",
            "rejected_under_lineage_id": "model@2",
        })
    unrelated = "idea-unrelated"
    space.facts[unrelated] = Fact(unrelated, "idea", {
        "model_id": "model", "origin": "seeded", "axis": "a", "description": unrelated,
        "status": "rejected", "rejected_under_lineage_id": "model@1",
    })
    first = reconcile_registry_space_requeue(registry, space, event_sequence=rollback.sequence)
    second = reconcile_registry_space_requeue(registry, space, event_sequence=rollback.sequence)
    assert first["newly_requeued_idea_ids"] == tuple(f"idea-pair-{i}" for i in range(3))
    assert second["newly_requeued_idea_ids"] == ()
    assert space.get(unrelated).meta["status"] == "rejected"


@pytest.mark.parametrize("fault", ["missing", "unfair", "digest", "wrong_parent"])
def test_missing_unfair_or_mismatched_paired_evidence_cannot_advance_ratchet(
    tmp_path: Path, fault: str,
) -> None:
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "adoption", .70, 2)
    idea_id = "idea-fault"
    digest = "sha256:expected"
    if fault != "missing":
        create_run(registry, "cf", .68, idea_id=idea_id,
                   params={"evaluated_model_version": 9 if fault == "wrong_parent" else 1,
                           "intervention_digest": "sha256:tampered" if fault == "digest" else digest,
                           **FAIRNESS},
                   validity="invalid" if fault == "unfair" else "valid")
    create_run(registry, "observed", .50, idea_id=idea_id,
               params={"evaluated_model_version": 2, "intervention_digest": digest,
                       "rejected_under_lineage_id": "model@2", **FAIRNESS})

    if fault == "missing":
        assert adjudicate_against_champion(
            registry, run_id="observed", model_id="model", reason="ordinary rejection") == "rejected"
    else:
        with pytest.raises(RegistryError, match="unfair|digest|version"):
            adjudicate_against_champion(
                registry, run_id="observed", model_id="model", reason="bad evidence",
                counterfactual_run_id="cf", intervention_digest=digest,
            )
    assert not any(event.event_type == "ratchet_evidence_recorded" for event in registry.list_events())
    assert next(row for row in registry.rows("aliases") if row["alias"] == "champion")["version"] == 2


@pytest.mark.parametrize("field", [
    "dataset_digest", "split_digest", "seed", "harness_digest", "preprocessing_digest",
])
def test_every_explicit_fairness_signature_field_must_match(tmp_path: Path, field: str) -> None:
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "adoption", .70, 2)
    observed_params = {"evaluated_model_version": 2, "intervention_digest": "sha256:pair",
                       "rejected_under_lineage_id": "model@2", **FAIRNESS}
    paired_params = {"evaluated_model_version": 1, "intervention_digest": "sha256:pair", **FAIRNESS}
    paired_params[field] = "different"
    create_run(registry, "cf", .68, idea_id="idea", params=paired_params)
    create_run(registry, "observed", .50, idea_id="idea", params=observed_params)
    with pytest.raises(RegistryError, match=field):
        adjudicate_against_champion(
            registry, run_id="observed", model_id="model", reason="unfair signature",
            counterfactual_run_id="cf", intervention_digest="sha256:pair",
        )
