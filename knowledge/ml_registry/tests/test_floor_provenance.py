"""The rope's PROVENANCE against how trials are actually compared (guard_rope_provenance).

The detection campaign's bar came from resampling WHICH EVAL FRAMES were scored, then
dispatched every trial PAIRED on the baseline's own draw -- where exactly that variance
cancels. 34 trials, zero adoptions, five real wins (0.6203, 0.6177, 0.6159,
0.6138, 0.6123 against 0.6076) filed stagnant, and the composition mechanism never opened.
These tests pin BOTH directions: the mismatch is refused, and a legitimate campaign is not.
"""

from __future__ import annotations

import pytest

from knowledge.ml_registry.floor import (
    ROPE_VARIES_EVAL_SAMPLE,
    ROPE_VARIES_PAIRED_DELTA,
    ROPE_VARIES_RUN_REPEAT,
    ROPE_VARIES_FIELD,
    TRIAL_COMPARISON_FIELD,
    TRIAL_COMPARISON_PAIRED,
    TRIAL_COMPARISON_UNPAIRED,
    adjudicate_trial,
    guard_rope_provenance,
    register_model_with_baseline,
)
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.write_path import (
    RegistrySpace,
    mutate_model,
    register_idea,
    register_model,
    register_trial,
)

BASE_META: dict[str, object] = {
    "metric": "map50",
    "direction": "maximize",
    "win_condition": {"metric_at_least": 0.9},
    "baseline": "b1",
    "baseline_throughput": 1.0,
    "diff_size_limit": 800,
}


def _meta(**extra: object) -> dict[str, object]:
    return {**BASE_META, **extra}


def _recomputed_meta(**extra: object) -> dict[str, object]:
    """``_meta`` minus the two fields register_model_with_baseline recomputes itself."""
    meta = _meta(**extra)
    meta.pop("baseline_throughput")
    return meta


# --- the guard itself, both directions ------------------------------------------------

def test_paired_trials_against_an_eval_sample_rope_are_refused() -> None:
    with pytest.raises(RegistryValidationError) as excinfo:
        guard_rope_provenance(
            _meta(
                **{
                    TRIAL_COMPARISON_FIELD: TRIAL_COMPARISON_PAIRED,
                    ROPE_VARIES_FIELD: ROPE_VARIES_EVAL_SAMPLE,
                }
            )
        )
    assert excinfo.value.field == ROPE_VARIES_FIELD
    assert "CANCELS" in str(excinfo.value)


def test_unpaired_trials_against_a_paired_delta_rope_are_refused() -> None:
    with pytest.raises(RegistryValidationError) as excinfo:
        guard_rope_provenance(
            _meta(
                **{
                    TRIAL_COMPARISON_FIELD: TRIAL_COMPARISON_UNPAIRED,
                    ROPE_VARIES_FIELD: ROPE_VARIES_PAIRED_DELTA,
                }
            )
        )
    assert excinfo.value.field == ROPE_VARIES_FIELD


@pytest.mark.parametrize(
    "comparison,varies",
    [
        # The two campaigns that are RIGHT. A false refusal here would block a correct
        # campaign, which is worse than the defect this guard closes.
        (TRIAL_COMPARISON_PAIRED, ROPE_VARIES_PAIRED_DELTA),
        (TRIAL_COMPARISON_UNPAIRED, ROPE_VARIES_EVAL_SAMPLE),
        # run_repeat noise neither cancels under pairing nor vanishes without it, so it is
        # defensible either way and the guard refuses only what it can be sure about.
        (TRIAL_COMPARISON_PAIRED, ROPE_VARIES_RUN_REPEAT),
        (TRIAL_COMPARISON_UNPAIRED, ROPE_VARIES_RUN_REPEAT),
    ],
)
def test_legitimate_combinations_are_not_refused(comparison: str, varies: str) -> None:
    guard_rope_provenance(
        _meta(**{TRIAL_COMPARISON_FIELD: comparison, ROPE_VARIES_FIELD: varies})
    )


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {TRIAL_COMPARISON_FIELD: TRIAL_COMPARISON_PAIRED},
        {ROPE_VARIES_FIELD: ROPE_VARIES_EVAL_SAMPLE},
        {TRIAL_COMPARISON_FIELD: "", ROPE_VARIES_FIELD: ""},
    ],
)
def test_an_undeclared_or_half_declared_record_has_no_opinion(extra: dict[str, object]) -> None:
    guard_rope_provenance(_meta(**extra))


@pytest.mark.parametrize(
    "field,value",
    [
        (ROPE_VARIES_FIELD, "bootstrap"),
        (ROPE_VARIES_FIELD, "sampling"),
        (TRIAL_COMPARISON_FIELD, "same_seed"),
    ],
)
def test_a_word_outside_the_vocabulary_is_refused_rather_than_read_as_silence(field: str, value: str) -> None:
    with pytest.raises(RegistryValidationError) as excinfo:
        guard_rope_provenance(_meta(**{field: value}))
    assert excinfo.value.field == field


# --- where the guard sits -------------------------------------------------------------

def test_plain_register_model_refuses_the_mismatch() -> None:
    """The CLI `register-model` path checks nothing else at all, so it is the choke point."""
    space = RegistrySpace()
    with pytest.raises(RegistryValidationError):
        register_model(
            space,
            _meta(
                **{
                    TRIAL_COMPARISON_FIELD: TRIAL_COMPARISON_PAIRED,
                    ROPE_VARIES_FIELD: ROPE_VARIES_EVAL_SAMPLE,
                }
            ),
        )
    assert space.list_facts("model") == []


def test_register_model_with_baseline_refuses_the_detection_registration() -> None:
    """The live shape: a bar measured over eight frame-bootstrap draws, trials paired."""
    draws = [0.6076, 0.6011, 0.6152, 0.5893, 0.6208, 0.5977, 0.6104, 0.6039]
    ledger = {f"d{i}": v for i, v in enumerate(draws)}
    space = RegistrySpace()
    meta = _recomputed_meta(
        baseline="d0",
        baseline_runs=sorted(ledger),
        sigmas=2.0,
        **{
            ROPE_VARIES_FIELD: ROPE_VARIES_EVAL_SAMPLE,
            TRIAL_COMPARISON_FIELD: TRIAL_COMPARISON_PAIRED,
        },
    )
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model_with_baseline(space, meta, ledger)
    assert "34 trials" in str(excinfo.value)
    assert space.list_facts("model") == []

    # ... and the same registration, with the rope's provenance corrected to what the
    # paired comparison actually carries, registers fine.
    meta[ROPE_VARIES_FIELD] = ROPE_VARIES_PAIRED_DELTA
    assert register_model_with_baseline(space, meta, ledger)


def test_declaring_the_pairing_after_registration_is_not_a_way_around_the_guard() -> None:
    space = RegistrySpace()
    model_id = register_model(space, _meta(**{ROPE_VARIES_FIELD: ROPE_VARIES_EVAL_SAMPLE}))
    with pytest.raises(RegistryValidationError):
        mutate_model(
            space,
            model_id,
            {TRIAL_COMPARISON_FIELD: TRIAL_COMPARISON_PAIRED},
            source="operator",
        )
    assert TRIAL_COMPARISON_FIELD not in space.get(model_id).meta


# --- backward compatibility: the four live campaigns ----------------------------------

# Enough baseline rows for each live campaign to measure its own rope from.
LIVE_CAMPAIGNS = {
    "association": (0.0016, [0.7100, 0.7108, 0.7092, 0.7104]),
    "detection": (0.099758, [0.6076, 0.6011, 0.6152, 0.5893, 0.6208, 0.5977, 0.6104, 0.6039]),
    "contact_point": (0.007104, [0.812, 0.8156, 0.8084, 0.8138, 0.8102, 0.8171,
                                 0.8069, 0.8145, 0.8093, 0.8127, 0.8111, 0.8134]),
    "court_marking": (0.024962, [0.9012, 0.9101, 0.8954, 0.9066]),
}


@pytest.mark.parametrize("name", sorted(LIVE_CAMPAIGNS))
def test_the_four_live_campaigns_still_register_and_still_adjudicate(name: str) -> None:
    """An existing record that declares NEITHER new field must behave exactly as today."""
    bar, rows = LIVE_CAMPAIGNS[name]
    ledger = {f"{name}-b{i}": v for i, v in enumerate(rows)}
    ledger[f"{name}-arm"] = max(rows) + 10 * bar
    space = RegistrySpace()
    meta = _recomputed_meta(
        baseline=f"{name}-b0",
        baseline_runs=sorted(k for k in ledger if "-b" in k),
        sigmas=2.0,
    )
    model_id = register_model_with_baseline(space, meta, ledger)
    model = space.get(model_id)
    assert model.meta["baseline_runs"] == sorted(k for k in ledger if "-b" in k)
    assert ROPE_VARIES_FIELD not in model.meta
    assert TRIAL_COMPARISON_FIELD not in model.meta

    idea_id = register_idea(
        space,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "arm"},
    )
    trial_id = register_trial(
        space,
        {"model_id": model_id, "idea_id": idea_id, "commit": f"{name}-arm", "status": "running"},
        frozenset(ledger),
    )
    # An arm 10 bars clear of the best baseline row still adjudicates as a win, so the
    # judging path these records already run on is untouched by the new fields.
    assert adjudicate_trial(space, trial_id, ledger) == "succeeded"
    assert space.get(trial_id).meta["status"] == "succeeded"
