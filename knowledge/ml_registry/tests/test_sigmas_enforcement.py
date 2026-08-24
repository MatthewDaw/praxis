"""`sigmas` as the multiplier the registry APPLIES, rather than a claim it checks.

THE INCIDENT. The court-marking campaign registered a floor of 0.012481 -- the RAW
bootstrap SD, ONE sigma -- while its record declared `sigmas: 2`. Both numbers sat in the
same dict and nothing compared them, so its adopt/park band was half the width its own
record claimed (~16% one-sided false adoption per null arm instead of ~2.5%) and a human
audit found it rather than the registry.

R3a closed that by construction rather than by checking: there is no stored threshold left
for a declared `sigmas` to contradict. The registry measures the rope from the model's own
baseline rows and multiplies by this field itself, at every comparison. So what these tests
pin now is the multiplier's own contract -- a value that cannot describe a bar is refused,
a declared one is applied exactly, and a deliberately loose bar over a large backlog is
VISIBLE rather than silent, which is the half of the incident that survives the retirement.
"""

from __future__ import annotations

import statistics

import pytest

from knowledge.ml_registry.floor import (
    CONSERVATIVE_SIGMAS,
    DEFAULT_SIGMAS,
    SIGMAS_FIELD,
    SIGMAS_REASON_FIELD,
    baseline_values,
    comparison_rope,
    declared_sigmas,
    register_model_with_baseline,
)
from knowledge.ml_registry.report import (
    LOOSE_BAR_BACKLOG_THRESHOLD,
    campaign_status,
    format_status,
)
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.write_path import RegistrySpace, register_idea, register_model

BASELINE_VALUES = [0.60, 0.62, 0.61, 0.63]
BASELINE_RUNS = [f"sha:b{i}" for i in range(len(BASELINE_VALUES))]
LEDGER = dict(zip(BASELINE_RUNS, BASELINE_VALUES))
BASELINE_SD = statistics.stdev(BASELINE_VALUES)


def _meta(**extra: object) -> dict[str, object]:
    meta: dict[str, object] = {
        "metric": "map50",
        "direction": "maximize",
        "win_condition": {"metric_at_least": 0.9},
        "baseline": BASELINE_RUNS[0],
        "baseline_runs": BASELINE_RUNS,
        "baseline_throughput": statistics.mean(BASELINE_VALUES),
        "diff_size_limit": 800,
    }
    meta.update(extra)
    return meta


def _space(tmp_path) -> RegistrySpace:
    return RegistrySpace.load(tmp_path / "space.json")


def _registered(space: RegistrySpace, **extra: object) -> dict[str, object]:
    meta = _meta(**extra)
    meta.pop("baseline_throughput")
    return space.get(register_model_with_baseline(space, meta, dict(LEDGER))).meta


# --- the multiplier's own contract ------------------------------------------------------

@pytest.mark.parametrize("bad", ["two", 0.0, -1.0])
def test_a_sigmas_that_describes_no_bar_is_refused(bad):
    with pytest.raises(RegistryValidationError) as excinfo:
        declared_sigmas(_meta(**{SIGMAS_FIELD: bad}))
    assert excinfo.value.field == SIGMAS_FIELD


def test_the_court_marking_contradiction_is_now_unreachable(tmp_path):
    """A record cannot declare 2 and carry a one-sigma bar, because the bar is not carried:
    it is measured here, from these rows, times this field."""
    stored = _registered(_space(tmp_path), **{SIGMAS_FIELD: 2.0})

    assert not [key for key in stored if "floor" in key]
    assert comparison_rope(stored, baseline_values(stored, LEDGER), 0.60) == pytest.approx(
        2.0 * BASELINE_SD
    )


def test_a_record_declaring_no_sigmas_gets_the_standing_default(tmp_path):
    stored = _registered(_space(tmp_path))
    assert comparison_rope(stored, baseline_values(stored, LEDGER), 0.60) == pytest.approx(
        DEFAULT_SIGMAS * BASELINE_SD
    )


def test_one_sigma_is_the_standing_default():
    """Changed deliberately, with the trade recorded in bootstrap's module docstring: a
    2-sigma bar over a noisy metric is one nothing can clear."""
    assert DEFAULT_SIGMAS == 1.0
    assert CONSERVATIVE_SIGMAS == 2.0


def test_the_default_change_does_not_reinterpret_an_explicit_two(tmp_path):
    """All four live campaigns declare sigmas 2.0 EXPLICITLY. Moving the DEFAULT to 1 must
    not silently rescale them: a declared value is a declaration, never a default."""
    stored = _registered(_space(tmp_path), **{SIGMAS_FIELD: 2.0})
    assert stored[SIGMAS_FIELD] == 2.0
    assert comparison_rope(stored, baseline_values(stored, LEDGER), 0.60) == pytest.approx(
        2.0 * BASELINE_SD
    )


# --- a deliberate loose bar registers, survives, and is VISIBLE ------------------------

def test_a_deliberate_one_sigma_campaign_registers_and_keeps_its_reason(tmp_path):
    """It must NOT require lying about sigmas to get a narrow bar through -- which is
    exactly what a registry that refused a declared 1 would produce."""
    space = _space(tmp_path)
    reason = ("explore bias: 2 sigma over a selection-based metric was a 7pp bar no arm "
              "cleared in 34 trials; 15.9% vs 2.3% one-sided false adoption accepted")
    model_id = register_model(space, _meta(**{
        SIGMAS_FIELD: 1.0,
        SIGMAS_REASON_FIELD: reason,
        "rope_varies": "paired_delta", "trial_comparison": "paired",
    }))
    stored = space.get(model_id).meta
    assert stored[SIGMAS_FIELD] == 1.0
    assert stored[SIGMAS_REASON_FIELD] == reason

    status = campaign_status(space, model_id)
    assert status["sigmas"] == 1.0 and status["sigmas_reason"] == reason
    assert "sigmas=1.0" in format_status(status)


def test_a_loose_bar_over_a_large_backlog_warns_and_does_not_block(tmp_path):
    """The precise condition the old 2-sigma default named. WARNED, never refused: a loose
    bar is a legitimate explore-bias choice, and silence about it is how court-marking ran
    a whole campaign at half the band its record claimed."""
    space = _space(tmp_path)
    model_id = register_model(space, _meta(**{SIGMAS_FIELD: 1.0}))
    for i in range(LOOSE_BAR_BACKLOG_THRESHOLD):
        register_idea(space, {"model_id": model_id, "origin": "seeded", "axis": "a",
                              "description": f"idea {i}"})
    loose = [d for d in campaign_status(space, model_id)["diagnoses"]
             if d["kind"] == "loose_bar_with_large_backlog"]
    assert len(loose) == 1
    assert loose[0]["severity"] == "info"          # advisory, not blocking
    assert "15.9%" in loose[0]["detail"] and "ratchet" in loose[0]["detail"].lower()


def test_a_small_backlog_at_one_sigma_says_nothing(tmp_path):
    """The concern is MULTIPLICITY. One arm at one sigma is a coin-flip nobody needs warning
    about, and an advisory that fires on every campaign is one nobody reads."""
    space = _space(tmp_path)
    model_id = register_model(space, _meta(**{SIGMAS_FIELD: 1.0}))
    register_idea(space, {"model_id": model_id, "origin": "seeded", "axis": "a",
                          "description": "the only idea"})
    assert not [d for d in campaign_status(space, model_id)["diagnoses"]
                if d["kind"] == "loose_bar_with_large_backlog"]


def test_a_two_sigma_campaign_is_never_warned(tmp_path):
    space = _space(tmp_path)
    model_id = register_model(space, _meta(**{SIGMAS_FIELD: 2.0}))
    for i in range(LOOSE_BAR_BACKLOG_THRESHOLD * 2):
        register_idea(space, {"model_id": model_id, "origin": "seeded", "axis": "a",
                              "description": f"idea {i}"})
    assert not [d for d in campaign_status(space, model_id)["diagnoses"]
                if d["kind"] == "loose_bar_with_large_backlog"]


# --- the four LIVE campaigns ------------------------------------------------------------

@pytest.mark.parametrize(
    "name,sigmas,varies,comparison",
    [
        ("association", 2.0, "eval_sample", "unpaired"),
        ("detection", 2.0, "eval_sample", "unpaired"),
        # Recently re-derived as a paired-delta bar.
        ("contact_point", 2.0, "paired_delta", "paired"),
        ("court_marking", 2.0, "eval_sample", "unpaired"),
    ],
)
def test_the_live_campaigns_still_register(tmp_path, name, sigmas, varies, comparison):
    """Three of these carry live trials. Each declared its bar OUTSIDE praxis; none of them
    now stores one, and each keeps adjudicating against `sigmas` times the spread its own
    baseline rows show."""
    stored = _registered(
        _space(tmp_path),
        metric=f"{name}_metric",
        **{SIGMAS_FIELD: sigmas, "rope_varies": varies, "trial_comparison": comparison},
    )
    assert stored[SIGMAS_FIELD] == sigmas
    assert comparison_rope(stored, baseline_values(stored, LEDGER), 0.60) == pytest.approx(
        sigmas * BASELINE_SD
    )
