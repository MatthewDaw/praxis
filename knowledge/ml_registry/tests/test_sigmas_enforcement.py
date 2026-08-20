"""`sigmas` as an ENFORCED statement rather than decoration.

THE INCIDENT. The court-marking campaign registered noise_floor 0.012481 -- the RAW
bootstrap SD, ONE sigma -- while its record declared `sigmas: 2`. Both numbers sat in the
same dict. Nothing compared them, because `sigmas` appeared nowhere in the write path or
the verdict path: bootstrap multiplied by it once and from then on it was inert provenance.
Its adopt/park band was therefore half the width its own record claimed -- roughly a 16%
one-sided false-adoption rate per null arm instead of ~2.5% -- and it was found by a human
audit rather than by the registry.

These tests pin BOTH directions, because a false refusal blocks correct campaigns and is
worse than the defect:
  * a floor that contradicts its own declared sigmas is refused, naming both numbers;
  * every legitimate combination registers -- an externally-measured paired-delta floor
    nothing here can divide, a deliberate 1-sigma bar, and all four live campaigns.
"""

from __future__ import annotations

import statistics

import pytest

from knowledge.ml_registry.floor import (
    CONSERVATIVE_SIGMAS,
    DEFAULT_SIGMAS,
    NOISE_FLOOR_FIELD,
    NOISE_FLOOR_METHOD_FIELD,
    NOISE_FLOOR_SIGMA_FIELD,
    SIGMAS_BASIS_DECLARED_UNIT,
    SIGMAS_BASIS_FIELD,
    SIGMAS_BASIS_NONE,
    SIGMAS_BASIS_RECOMPUTED,
    SIGMAS_BASIS_UNVERIFIED,
    SIGMAS_FIELD,
    SIGMAS_REASON_FIELD,
    check_declared_sigmas,
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
        "noise_floor": 0.01,
        "baseline_throughput": statistics.mean(BASELINE_VALUES),
        "diff_size_limit": 800,
    }
    meta.update(extra)
    return meta


def _space(tmp_path) -> RegistrySpace:
    return RegistrySpace.load(tmp_path / "space.json")


# --- the mismatch is CAUGHT ------------------------------------------------------------

def test_the_court_marking_defect_is_refused_with_both_numbers_named(tmp_path):
    """floor 0.012481 declaring sigmas 2 -- the raw one-sigma SD wearing a two-sigma label.

    The record carries the one-sigma dispersion it was derived from, which is exactly what
    lets the registry do the multiplication the campaign did not.
    """
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model(
            _space(tmp_path),
            _meta(**{
                NOISE_FLOOR_FIELD: 0.012481,
                SIGMAS_FIELD: 2.0,
                NOISE_FLOOR_SIGMA_FIELD: 0.012481,
                NOISE_FLOOR_METHOD_FIELD: "bootstrap",
            }),
        )
    message = str(excinfo.value)
    assert excinfo.value.field == NOISE_FLOOR_FIELD
    assert "0.012481" in message          # what is stored
    assert "0.024962" in message          # what the declaration implies
    assert "2.0" in message               # the sigmas claimed
    assert "court-marking" in message.lower()


def test_the_mismatch_is_caught_in_the_generous_direction_too(tmp_path):
    """A floor WIDER than its declared sigmas is equally a record nobody can read: it parks
    arms the campaign believes it is measuring."""
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model(
            _space(tmp_path),
            _meta(**{NOISE_FLOOR_FIELD: 0.04, SIGMAS_FIELD: 1.0,
                     NOISE_FLOOR_SIGMA_FIELD: 0.01,
                     NOISE_FLOOR_METHOD_FIELD: "bootstrap"}),
        )
    assert excinfo.value.field == NOISE_FLOOR_FIELD


def test_a_declared_sigmas_is_recomputed_against_the_baseline_runs(tmp_path):
    """The other verifiable case: the floor IS the repeat stdev, so sigmas is checkable
    from the ledger alone and a record declaring 2 while carrying 1 cannot register."""
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model_with_baseline(
            _space(tmp_path),
            _meta(**{NOISE_FLOOR_FIELD: BASELINE_SD, SIGMAS_FIELD: 2.0,
                     "baseline_runs": BASELINE_RUNS}),
            dict(LEDGER),
        )
    assert excinfo.value.field == NOISE_FLOOR_FIELD


@pytest.mark.parametrize("bad", ["two", 0.0, -1.0])
def test_a_sigmas_that_describes_no_bar_is_refused(bad):
    with pytest.raises(RegistryValidationError) as excinfo:
        check_declared_sigmas(_meta(**{SIGMAS_FIELD: bad}))
    assert excinfo.value.field == SIGMAS_FIELD


# --- and every legitimate record still registers ---------------------------------------

def test_an_externally_measured_floor_is_admitted_and_labelled_unverified(tmp_path):
    """The legitimate case the sibling campaigns are in: a paired-delta floor measured
    outside praxis, with no one-sigma dispersion declared. praxis has one number and no way
    to divide it, so it does NOT guess -- it admits the record and says it checked nothing.
    """
    space = _space(tmp_path)
    model_id = register_model(space, _meta(**{
        NOISE_FLOOR_FIELD: 0.0041, SIGMAS_FIELD: 2.0,
        NOISE_FLOOR_METHOD_FIELD: "bootstrap",
        "noise_floor_varies": "paired_delta", "trial_comparison": "paired",
    }))
    assert space.get(model_id).meta[SIGMAS_BASIS_FIELD] == SIGMAS_BASIS_UNVERIFIED


def test_declaring_the_one_sigma_dispersion_makes_it_verifiable(tmp_path):
    """The remedy, and the whole point of the field: declare what the floor was multiplied
    up FROM and the registry does the multiplication itself."""
    space = _space(tmp_path)
    model_id = register_model(space, _meta(**{
        NOISE_FLOOR_FIELD: 0.024962, SIGMAS_FIELD: 2.0,
        NOISE_FLOOR_SIGMA_FIELD: 0.012481,
        NOISE_FLOOR_METHOD_FIELD: "bootstrap",
    }))
    assert space.get(model_id).meta[SIGMAS_BASIS_FIELD] == SIGMAS_BASIS_DECLARED_UNIT


def test_a_record_declaring_no_sigmas_is_untouched(tmp_path):
    """Every model registered before this guard existed. There is no claim to check."""
    space = _space(tmp_path)
    model_id = register_model(space, _meta())
    assert space.get(model_id).meta[SIGMAS_BASIS_FIELD] == SIGMAS_BASIS_NONE


def test_recomputation_stamps_what_it_actually_verified(tmp_path):
    space = _space(tmp_path)
    meta = _meta(**{SIGMAS_FIELD: 2.0, "baseline_runs": BASELINE_RUNS})
    meta.pop("baseline_throughput")
    meta[NOISE_FLOOR_FIELD] = 2.0 * BASELINE_SD
    model_id = register_model_with_baseline(space, meta, dict(LEDGER))
    assert space.get(model_id).meta[SIGMAS_BASIS_FIELD] == SIGMAS_BASIS_RECOMPUTED


def test_a_hand_written_recomputed_stamp_is_not_taken_as_evidence(tmp_path):
    """The stamp says what the REGISTRY checked. Only the path that did the arithmetic may
    claim it; a record that simply asserts it, with a floor measured elsewhere, is
    downgraded to the truth rather than believed."""
    space = _space(tmp_path)
    model_id = register_model(space, _meta(**{
        NOISE_FLOOR_FIELD: 0.0041, SIGMAS_FIELD: 2.0,
        NOISE_FLOOR_METHOD_FIELD: "bootstrap",
        SIGMAS_BASIS_FIELD: SIGMAS_BASIS_RECOMPUTED,
    }))
    assert space.get(model_id).meta[SIGMAS_BASIS_FIELD] == SIGMAS_BASIS_UNVERIFIED


# --- a deliberate loose bar registers, survives, and is VISIBLE ------------------------

def test_one_sigma_is_the_standing_default():
    """Changed deliberately, with the trade recorded in bootstrap's module docstring: a
    2-sigma bar over a noisy metric is one nothing can clear."""
    assert DEFAULT_SIGMAS == 1.0
    assert CONSERVATIVE_SIGMAS == 2.0


def test_a_deliberate_one_sigma_campaign_registers_and_keeps_its_reason(tmp_path):
    """It must NOT require lying about sigmas to get a low floor through -- which is exactly
    what a registry that refused a declared 1 would produce."""
    space = _space(tmp_path)
    reason = ("explore bias: 2 sigma over a selection-based metric was a 7pp bar no arm "
              "cleared in 34 trials; 15.9% vs 2.3% one-sided false adoption accepted")
    model_id = register_model(space, _meta(**{
        NOISE_FLOOR_FIELD: 0.0703, SIGMAS_FIELD: 1.0,
        NOISE_FLOOR_SIGMA_FIELD: 0.0703,
        NOISE_FLOOR_METHOD_FIELD: "bootstrap",
        SIGMAS_REASON_FIELD: reason,
        "noise_floor_varies": "paired_delta", "trial_comparison": "paired",
    }))
    stored = space.get(model_id).meta
    assert stored[SIGMAS_FIELD] == 1.0
    assert stored[SIGMAS_REASON_FIELD] == reason
    assert stored[SIGMAS_BASIS_FIELD] == SIGMAS_BASIS_DECLARED_UNIT

    status = campaign_status(space, model_id)
    assert status["sigmas"] == 1.0 and status["sigmas_reason"] == reason
    assert "sigmas=1.0" in format_status(status)


def test_a_loose_bar_over_a_large_backlog_warns_and_does_not_block(tmp_path):
    """The precise condition the old 2-sigma default named. WARNED, never refused: a loose
    bar is a legitimate explore-bias choice, and silence about it is how court-marking ran
    a whole campaign at half the band its record claimed."""
    space = _space(tmp_path)
    model_id = register_model(space, _meta(**{
        NOISE_FLOOR_FIELD: 0.0703, SIGMAS_FIELD: 1.0, NOISE_FLOOR_SIGMA_FIELD: 0.0703,
        NOISE_FLOOR_METHOD_FIELD: "bootstrap",
    }))
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
    model_id = register_model(space, _meta(**{
        NOISE_FLOOR_FIELD: 0.0703, SIGMAS_FIELD: 1.0, NOISE_FLOOR_SIGMA_FIELD: 0.0703,
        NOISE_FLOOR_METHOD_FIELD: "bootstrap",
    }))
    register_idea(space, {"model_id": model_id, "origin": "seeded", "axis": "a",
                          "description": "the only idea"})
    assert not [d for d in campaign_status(space, model_id)["diagnoses"]
                if d["kind"] == "loose_bar_with_large_backlog"]


def test_a_two_sigma_campaign_is_never_warned(tmp_path):
    space = _space(tmp_path)
    model_id = register_model(space, _meta(**{
        NOISE_FLOOR_FIELD: 0.0041, SIGMAS_FIELD: 2.0, NOISE_FLOOR_METHOD_FIELD: "bootstrap",
    }))
    for i in range(LOOSE_BAR_BACKLOG_THRESHOLD * 2):
        register_idea(space, {"model_id": model_id, "origin": "seeded", "axis": "a",
                              "description": f"idea {i}"})
    assert not [d for d in campaign_status(space, model_id)["diagnoses"]
                if d["kind"] == "loose_bar_with_large_backlog"]


# --- the four LIVE campaigns ------------------------------------------------------------

@pytest.mark.parametrize(
    "name,floor,sigmas,varies,comparison",
    [
        ("association", 0.0016, 2.0, "eval_sample", "unpaired"),
        ("detection", 0.099758, 2.0, "eval_sample", "unpaired"),
        # Recently re-derived as a paired-delta floor.
        ("contact_point", 0.0041, 2.0, "paired_delta", "paired"),
        # ALREADY corrected to 2x its raw SD (0.012481) after the audit.
        ("court_marking", 0.024962, 2.0, "eval_sample", "unpaired"),
    ],
)
def test_the_live_campaigns_still_register(tmp_path, name, floor, sigmas, varies, comparison):
    """Three of these carry live trials. Their floors were all measured OUTSIDE praxis and
    none declares a one-sigma dispersion, so none can be verified here -- and none is
    refused for that. They register exactly as before, stamped honestly as unchecked."""
    space = _space(tmp_path)
    meta = _meta(**{
        "metric": f"{name}_metric",
        NOISE_FLOOR_FIELD: floor, SIGMAS_FIELD: sigmas,
        NOISE_FLOOR_METHOD_FIELD: "bootstrap",
        "noise_floor_varies": varies, "trial_comparison": comparison,
        "noise_floor_override_reason": "external study over more runs than the ledger holds",
        "baseline_runs": BASELINE_RUNS,
    })
    meta.pop("baseline_throughput")
    model_id = register_model_with_baseline(space, meta, dict(LEDGER))
    stored = space.get(model_id).meta
    assert stored[NOISE_FLOOR_FIELD] == floor          # unchanged by any of this
    assert stored[SIGMAS_FIELD] == sigmas
    assert stored[SIGMAS_BASIS_FIELD] == SIGMAS_BASIS_UNVERIFIED


def test_the_default_change_does_not_reinterpret_an_explicit_two(tmp_path):
    """All four live campaigns declare sigmas 2.0 EXPLICITLY. Moving the DEFAULT to 1 must
    not silently rescale them: a declared value is a declaration, never a default."""
    space = _space(tmp_path)
    meta = _meta(**{SIGMAS_FIELD: 2.0, "baseline_runs": BASELINE_RUNS})
    meta.pop("baseline_throughput")
    meta[NOISE_FLOOR_FIELD] = 2.0 * BASELINE_SD
    model_id = register_model_with_baseline(space, meta, dict(LEDGER))
    assert space.get(model_id).meta[NOISE_FLOOR_FIELD] == pytest.approx(2.0 * BASELINE_SD)
