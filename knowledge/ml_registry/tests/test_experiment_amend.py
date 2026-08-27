"""The amend seam: a registered win condition may tighten, and nothing else may move.

Experiments are otherwise immutable. This path exists because a campaign that learns its
declared bar was too loose -- a constant predictor matching the champion -- has to put the
new gate ON THE EXPERIMENT, not in a local sweep helper, and has to do it with evidence
rather than a silent overwrite. Loosening is refused by name.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge.ml_registry.storage import Registry, RegistryError, replay_projection


def _experiment(registry: Registry, **overrides: object) -> None:
    values = dict(
        experiment_id="campaign",
        spec_digest="d" * 64,
        stages=["representation"],
        metric="score",
        direction="maximize",
        win_condition={"metric_at_least": 0.0, "minimum_pairs": 12},
        rope=0.01,
        baseline_throughput=1.0,
    )
    values.update(overrides)
    registry.create_experiment(**values)


def _win(registry: Registry) -> dict[str, object]:
    row = registry.rows("experiments")[0]
    value = row["win_condition"]
    return json.loads(value) if isinstance(value, str) else dict(value)


def test_tightening_a_win_condition_projects_and_emits_the_old_and_new(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    tighter = {"metric_at_least": 0.0, "minimum_pairs": 12, "constant_control_margin_at_least": 0.005}
    registry.amend_experiment(
        "campaign",
        reason="constant predictor matches the champion; adoption must clear it",
        win_condition=tighter,
        spec_digest="e" * 64,
    )
    row = registry.rows("experiments")[0]
    assert _win(registry) == tighter
    assert row["spec_digest"] == "e" * 64
    # Frozen fields stay put.
    assert row["metric"] == "score"
    assert row["rope"] == 0.01
    assert row["baseline_throughput"] == 1.0
    event = next(
        event for event in reversed(registry.list_events())
        if event.event_type == "experiment_amended"
    )
    assert event.payload["reason"].startswith("constant predictor")
    assert event.payload["old"]["win_condition"] == {
        "metric_at_least": 0.0, "minimum_pairs": 12,
    }
    assert event.payload["new"]["win_condition"] == tighter
    assert event.payload["old"]["spec_digest"] == "d" * 64
    assert event.payload["new"]["spec_digest"] == "e" * 64


def test_raising_a_numeric_floor_is_a_tightening(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    registry.amend_experiment(
        "campaign",
        reason="raise the paired-lower-bound floor",
        win_condition={"metric_at_least": 0.01, "minimum_pairs": 12},
    )
    assert _win(registry)["metric_at_least"] == 0.01


def test_lowering_a_numeric_floor_is_refused_and_the_row_is_untouched(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    with pytest.raises(RegistryError, match="cannot loosen metric_at_least"):
        registry.amend_experiment(
            "campaign",
            reason="would loosen",
            win_condition={"metric_at_least": -0.1, "minimum_pairs": 12},
        )
    assert _win(registry) == {"metric_at_least": 0.0, "minimum_pairs": 12}
    assert not any(event.event_type == "experiment_amended" for event in registry.list_events())


def test_dropping_a_constraint_is_refused(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    with pytest.raises(RegistryError, match="cannot drop constraints"):
        registry.amend_experiment(
            "campaign",
            reason="would drop a floor",
            win_condition={"metric_at_least": 0.0},
        )


def test_rewriting_a_non_floor_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry, win_condition={"metric_at_least": 0.0, "decision": "argmax"})
    with pytest.raises(RegistryError, match="cannot rewrite 'decision'"):
        registry.amend_experiment(
            "campaign",
            reason="would change meaning",
            win_condition={"metric_at_least": 0.0, "decision": "threshold"},
        )


def test_an_identical_win_condition_is_not_an_amendment(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    with pytest.raises(RegistryError, match="requires a tightening"):
        registry.amend_experiment(
            "campaign",
            reason="nothing changed",
            win_condition={"metric_at_least": 0.0, "minimum_pairs": 12},
        )


def test_spec_digest_alone_is_refused(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    with pytest.raises(RegistryError, match="requires a win_condition tightening"):
        registry.amend_experiment("campaign", reason="hash only", spec_digest="f" * 64)


def test_unknown_fields_are_refused_naming_them(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    with pytest.raises(RegistryError, match="cannot rewrite \\['rope'\\]"):
        registry.amend_experiment(
            "campaign",
            reason="would move the rope",
            win_condition={"metric_at_least": 0.0, "minimum_pairs": 12, "extra": True},
            rope=0.5,
        )


def test_missing_experiment_and_empty_reason_are_refused(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    with pytest.raises(RegistryError, match="requires a reason"):
        registry.amend_experiment(
            "campaign",
            reason="   ",
            win_condition={"metric_at_least": 0.0, "minimum_pairs": 12, "extra": True},
        )
    with pytest.raises(RegistryError, match="unknown experiment"):
        registry.amend_experiment(
            "missing",
            reason="tighten",
            win_condition={"metric_at_least": 0.0, "minimum_pairs": 12, "extra": True},
        )


def test_a_direct_update_is_still_refused(tmp_path: Path) -> None:
    """Workers without the amend event still cannot touch the row."""
    import sqlite3

    registry = Registry(tmp_path)
    _experiment(registry)
    with registry._connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match="experiments are immutable"):
            db.execute("UPDATE experiments SET rope=1.0 WHERE experiment_id='campaign'")


def test_replay_reconstructs_an_amended_experiment(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    tighter = {"metric_at_least": 0.0, "minimum_pairs": 12, "constant_control_margin_at_least": 0.005}
    registry.amend_experiment(
        "campaign", reason="tighten", win_condition=tighter, spec_digest="e" * 64,
    )
    before = dict(registry.rows("experiments")[0])
    (tmp_path / "registry.sqlite3").unlink(missing_ok=True)
    replay_projection(tmp_path)
    recovered = Registry(tmp_path)
    after = dict(recovered.rows("experiments")[0])
    assert after["experiment_id"] == before["experiment_id"]
    assert after["spec_digest"] == "e" * 64
    assert json.loads(after["win_condition"]) == tighter
    assert after["metric"] == "score"
    assert any(
        event.event_type == "experiment_amended" for event in recovered.list_events()
    )
