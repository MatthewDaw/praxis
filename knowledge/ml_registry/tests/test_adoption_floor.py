"""The adoption floor: a gain of 0.5% IS a win, whatever the rope measures.

The rope and the floor answer different questions and are allowed to disagree loudly. The
rope is MEASURED -- `sigmas` x the spread of the baseline's own replicates, which in this
project's real campaigns runs from 0.000790 (association, 0.08%) to 0.188458
(contact_point, 18.8%). At the wide end it declares a genuine 5% improvement "practically
equivalent" and throws it away, which is the failure this module pins shut. The floor is
DECLARED, once, with the rest of the judge, and says how big a gain this campaign cares
about at all.

Nothing here asserts a changed MEASUREMENT: `measure_rope` and `comparison_rope` keep
reporting exactly what they reported before, and one test below says so in as many words.
What changed is the DECISION taken with those numbers, in the one place an adoption verdict
is decided (`verdict.adjudicate_verdict`).
"""

from __future__ import annotations

import pytest

from knowledge.ml_registry.floor import (
    ADOPTION_FLOOR_FIELD,
    DEFAULT_ADOPTION_FLOOR,
    FLOOR_ADOPTION_INSIDE_ROPE_FIELD,
    adoption_gain,
    baseline_values,
    comparison_rope,
    declared_adoption_floor,
    describe_rope,
    measure_rope,
    register_model_with_baseline,
)
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.testing.rope_fixtures import (
    ROPE_COMMITS,
    rope_ledger,
    rope_replicates,
)
from knowledge.ml_registry.verdict import (
    VERDICT_ADOPTED,
    VERDICT_PARKED,
    VERDICT_REJECTED,
    LedgerRow,
    adjudicate_verdict,
)
from knowledge.ml_registry.write_path import RegistrySpace, register_idea, register_trial

#: contact_point's real measured rope -- 18.8 percentage points of replicate spread. The
#: campaign this rule exists for: at this bar a 5% improvement is "practically equivalent".
NOISY_ROPE = 0.188458
#: association's real measured rope -- 0.08 percentage points. A 0.5% gain is far OUTSIDE
#: this one, so the same floor adoption carries no audit mark here.
QUIET_ROPE = 0.000790

BASELINE_AT = 0.60
THROUGHPUT = 1000.0
TRIAL_COMMIT = "arm"
ALL_COMMITS = frozenset({*ROPE_COMMITS, TRIAL_COMMIT})


def _model(
    rope: float, *, direction: str = "maximize", at: float = BASELINE_AT, **extra: object
) -> tuple[RegistrySpace, str, dict[str, float]]:
    """A registered model whose baseline rows measure exactly ``rope``, baselined at ``at``."""
    space = RegistrySpace()
    ledger = rope_ledger(rope, at=at)
    meta: dict[str, object] = {
        "metric": "hota",
        "direction": direction,
        "win_condition": {"metric_at_least": 0.99},
        "baseline": ROPE_COMMITS[0],  # value == `at` exactly
        "diff_size_limit": 800,
        "baseline_runs": list(ROPE_COMMITS),
        **extra,
    }
    model_id = register_model_with_baseline(
        space, meta, ledger,
        ledger_throughputs={commit: THROUGHPUT for commit in ledger},
    )
    return space, model_id, ledger


def _adjudicate(
    space: RegistrySpace, model_id: str, ledger: dict[str, float], trial_value: float
) -> tuple[str, dict[str, object]]:
    """Run one arm scoring ``trial_value`` through the full verdict; return it and the trial."""
    idea_id = register_idea(
        space,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "arm"},
    )
    trial_id = register_trial(
        space,
        {
            "model_id": model_id, "idea_id": idea_id, "commit": TRIAL_COMMIT, "status": "running",
            "throughput": THROUGHPUT, "diff_lines": 100,
        },
        ALL_COMMITS,
    )
    rows = {c: LedgerRow(value=v, throughput=THROUGHPUT, diff_lines=0) for c, v in ledger.items()}
    rows[TRIAL_COMMIT] = LedgerRow(value=trial_value, throughput=THROUGHPUT, diff_lines=100)
    return adjudicate_verdict(space, trial_id, rows), space.get(trial_id).meta


def test_a_gain_at_exactly_the_floor_adopts_against_a_rope_that_would_reject_it() -> None:
    """THE CASE THE RULE EXISTS FOR. contact_point's baseline scatters by 18.8 points, so a
    +0.5-point arm is 37x inside its rope and used to park. The floor is inclusive: 0.5% is
    stated as a win, so exactly 0.5% is one."""
    space, model_id, ledger = _model(NOISY_ROPE)
    values = baseline_values(space.get(model_id).meta, ledger)
    assert comparison_rope(space.get(model_id).meta, values, BASELINE_AT) == pytest.approx(NOISY_ROPE)

    verdict, _ = _adjudicate(space, model_id, ledger, BASELINE_AT + DEFAULT_ADOPTION_FLOOR)

    assert verdict == VERDICT_ADOPTED
    assert space.get(model_id).meta["baseline"] == TRIAL_COMMIT  # and the baseline advanced


def test_a_gain_just_under_the_floor_falls_through_to_the_rope_and_the_rope_decides() -> None:
    """Below the floor NOTHING is decided by it -- the rope answers, exactly as it did
    before, and both of its answers are reachable. Against contact_point's 18.8-point rope a
    +0.4999-point arm is stagnant; against association's 0.079-point rope the SAME gain is a
    clear win. One delta, two verdicts, decided entirely by the measured spread."""
    under = DEFAULT_ADOPTION_FLOOR - 1e-6

    space, model_id, ledger = _model(NOISY_ROPE)
    noisy_verdict, _ = _adjudicate(space, model_id, ledger, BASELINE_AT + under)

    space, model_id, ledger = _model(QUIET_ROPE)
    quiet_verdict, _ = _adjudicate(space, model_id, ledger, BASELINE_AT + under)

    assert noisy_verdict == VERDICT_PARKED
    assert quiet_verdict == VERDICT_ADOPTED


def test_a_campaign_declaring_its_own_floor_is_judged_on_that_number_not_the_default() -> None:
    """Declared with the judge, like `sigmas`: one field, read once, and it GOVERNS -- a gain
    that would clear the 0.5% default parks under a campaign that asked for 2%."""
    assert declared_adoption_floor({}) == DEFAULT_ADOPTION_FLOOR == 0.005
    assert declared_adoption_floor({ADOPTION_FLOOR_FIELD: 0.02}) == 0.02

    space_a, model_a, ledger_a = _model(NOISY_ROPE, **{ADOPTION_FLOOR_FIELD: 0.02})
    space_b, model_b, ledger_b = _model(NOISY_ROPE, **{ADOPTION_FLOOR_FIELD: 0.02})
    assert space_a.get(model_a).meta[ADOPTION_FLOOR_FIELD] == 0.02

    # +1% clears the DEFAULT floor and would have adopted under it; this campaign said 2%.
    assert _adjudicate(space_a, model_a, ledger_a, BASELINE_AT + 0.01)[0] == VERDICT_PARKED
    assert _adjudicate(space_b, model_b, ledger_b, BASELINE_AT + 0.02)[0] == VERDICT_ADOPTED


@pytest.mark.parametrize("floor", [0.0, -0.005, "not a number"])
def test_a_floor_that_names_no_gain_is_refused_at_registration_naming_the_field(floor) -> None:
    """The floor is declared BEFORE the baseline, so registration is the moment an unusable
    one has to be refused -- the same shape, and the same choke point, as `sigmas`."""
    with pytest.raises(RegistryValidationError) as excinfo:
        _model(NOISY_ROPE, **{ADOPTION_FLOOR_FIELD: floor})
    assert excinfo.value.field == ADOPTION_FLOOR_FIELD


def test_on_a_minimize_metric_the_floor_reads_a_decrease_as_the_improvement() -> None:
    """DIRECTION, and getting it backwards would adopt every regression on every minimize
    campaign. contact_point IS a minimize metric (0.188458 is its measured rope). A drop of
    one floor is a win; a RISE of exactly the same size is a regression, clears nothing, and
    is judged by the rope like any other non-win."""
    assert adoption_gain("minimize", 0.60, 0.595) == pytest.approx(0.005)
    assert adoption_gain("minimize", 0.60, 0.605) == pytest.approx(-0.005)
    assert adoption_gain("maximize", 0.60, 0.605) == pytest.approx(0.005)

    space, model_id, ledger = _model(NOISY_ROPE, direction="minimize")
    improved, _ = _adjudicate(space, model_id, ledger, BASELINE_AT - DEFAULT_ADOPTION_FLOOR)

    space, model_id, ledger = _model(NOISY_ROPE, direction="minimize")
    regressed, _ = _adjudicate(space, model_id, ledger, BASELINE_AT + DEFAULT_ADOPTION_FLOOR)

    # And a regression that is genuinely big for its campaign still REJECTS: the floor never
    # rescues a worsening arm, it only ever decides one that improved.
    space, model_id, ledger = _model(QUIET_ROPE, direction="minimize")
    big_regression, _ = _adjudicate(space, model_id, ledger, BASELINE_AT + DEFAULT_ADOPTION_FLOOR)

    assert improved == VERDICT_ADOPTED
    assert regressed == VERDICT_PARKED
    assert big_regression == VERDICT_REJECTED


def test_a_floor_adoption_inside_the_measured_rope_is_stamped_and_one_outside_it_is_not() -> None:
    """The audit mark. A +0.6-point gain against an 18.8-point rope is adopted -- that is the
    decision -- and is also honestly indistinguishable from noise, so the trial carries the
    one fact the rule overrode. The identical gain against association's 0.079-point rope is
    outside the noise and carries nothing."""
    gain = 0.006

    space, model_id, ledger = _model(NOISY_ROPE)
    noisy_verdict, noisy_trial = _adjudicate(space, model_id, ledger, BASELINE_AT + gain)

    space, model_id, ledger = _model(QUIET_ROPE)
    quiet_verdict, quiet_trial = _adjudicate(space, model_id, ledger, BASELINE_AT + gain)

    assert noisy_verdict == quiet_verdict == VERDICT_ADOPTED
    assert noisy_trial[FLOOR_ADOPTION_INSIDE_ROPE_FIELD] is True
    assert FLOOR_ADOPTION_INSIDE_ROPE_FIELD not in quiet_trial


def test_describe_rope_reports_the_floor_beside_the_bar_and_says_when_it_is_inside_it() -> None:
    """A human handed only the rope reads a verdict the rope did not decide. `describe_rope`
    is the surface that explains a bar, so it carries both numbers -- and when the floor sits
    inside the measured rope it says so, because that campaign's adoptions will routinely be
    stamped and that is a finding about the HARNESS, not about any arm."""
    noisy = describe_rope({"direction": "maximize"}, rope_replicates(NOISY_ROPE), BASELINE_AT)
    quiet = describe_rope({"direction": "maximize"}, rope_replicates(QUIET_ROPE), BASELINE_AT)

    assert noisy[ADOPTION_FLOOR_FIELD] == DEFAULT_ADOPTION_FLOOR
    assert noisy["floor_inside_measured_rope"] is True
    assert FLOOR_ADOPTION_INSIDE_ROPE_FIELD in str(noisy["floor_caveat"])
    assert quiet["floor_inside_measured_rope"] is False
    assert "floor_caveat" not in quiet


def test_the_floor_changes_no_measurement_only_the_decision_taken_with_it() -> None:
    """The constraint, pinned: the rope keeps measuring replicate spread and keeps REPORTING
    it, at the default floor and at a declared one alike. Two facts, both visible, allowed to
    disagree -- the previous attempt at this rule clamped the measurement instead and was
    correctly caught by 20 tests asserting what it measures."""
    values = rope_replicates(NOISY_ROPE)
    for meta in ({"direction": "maximize"}, {"direction": "maximize", ADOPTION_FLOOR_FIELD: 0.05}):
        assert measure_rope(values) == pytest.approx(NOISY_ROPE)
        assert comparison_rope(meta, values, BASELINE_AT) == pytest.approx(NOISY_ROPE)
        assert describe_rope(meta, values, BASELINE_AT)["measured_rope"] == pytest.approx(NOISY_ROPE)
