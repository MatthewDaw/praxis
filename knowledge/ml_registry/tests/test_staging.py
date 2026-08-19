"""Tests for backlog staging. Fixtures are plain dicts; nothing touches a registry or a database."""

from __future__ import annotations

from knowledge.ml_registry.staging import (
    StagingStuck, eligible, next_queue, open_stage, stage_progress,
)

STAGES = ("representation", "architecture", "tuning")


def _items():
    return [
        {"id": "R1", "axis": "representation"},
        {"id": "R2", "axis": "representation"},
        {"id": "M1", "axis": "architecture"},
        {"id": "T1", "axis": "tuning"},
    ]


def test_earliest_unanswered_stage_is_the_open_one() -> None:
    assert open_stage(_items(), set(), STAGES) == "representation"


def test_a_stage_closes_on_ANY_verdict_not_on_an_adoption() -> None:
    """The load-bearing rule. A stage that answered 'none of these help' is settled -- the first
    campaign this was built for had seven representation arms and zero adoptions. Gating on
    adoption would wedge a campaign behind any inert axis forever."""
    all_rejected = {"R1", "R2"}
    assert open_stage(_items(), all_rejected, STAGES) == "architecture"


def test_a_partially_answered_stage_stays_open() -> None:
    assert open_stage(_items(), {"R1"}, STAGES) == "representation"


def test_tuning_does_not_open_before_architecture_is_answered() -> None:
    """The waste this exists to prevent: tuning a hyperparameter for a model about to be replaced."""
    assert open_stage(_items(), {"R1", "R2"}, STAGES) == "architecture"
    assert open_stage(_items(), {"R1", "R2", "M1"}, STAGES) == "tuning"


def test_items_in_an_unlisted_stage_are_not_stranded() -> None:
    """A mis-typed or newly-invented stage must surface as work, not vanish from the queue."""
    items = _items() + [{"id": "X1", "axis": "invented_later"}]
    assert open_stage(items, {"R1", "R2", "M1", "T1"}, STAGES) == "invented_later"


def test_exhausted_backlog_returns_none() -> None:
    assert open_stage(_items(), {"R1", "R2", "M1", "T1"}, STAGES) is None


def test_stage_gate_and_dependency_gate_mean_different_things() -> None:
    """depends_on gates on one idea WINNING; stage gates on a whole question being ANSWERED."""
    compo = {"id": "C1", "axis": "representation", "depends_on": ["R1"]}
    # R1 answered but rejected -> the composition arm is not meaningful
    assert not eligible(compo, answered_ids={"R1"}, adopted_ids=set(), stage="representation")
    # R1 adopted -> it is
    assert eligible(compo, answered_ids={"R1"}, adopted_ids={"R1"}, stage="representation")


def test_stage_none_disables_the_stage_gate() -> None:
    t1 = {"id": "T1", "axis": "tuning"}
    assert eligible(t1, answered_ids=set(), adopted_ids=set(), stage=None)
    assert not eligible(t1, answered_ids=set(), adopted_ids=set(), stage="representation")


def test_explicit_stage_overrides_axis() -> None:
    item = {"id": "A", "axis": "sequence", "stage": "representation"}
    assert eligible(item, answered_ids=set(), adopted_ids=set(), stage="representation")


def test_progress_reports_closed_and_empty_stages() -> None:
    prog = {p["stage"]: p for p in stage_progress(_items(), {"R1", "R2"}, STAGES)}
    assert prog["representation"]["closed"] and prog["representation"]["answered"] == 2
    assert not prog["architecture"]["closed"]


def test_a_dependent_of_a_non_adopted_idea_is_unreachable() -> None:
    """depends_on gates on ADOPTION, so a parked dependency kills its dependents permanently."""
    from knowledge.ml_registry.staging import unreachable

    items = [{"id": "R01", "axis": "representation"},
             {"id": "R03", "axis": "representation"},
             {"id": "R07", "axis": "representation", "depends_on": ["R01", "R03"]}]
    # R01 answered but NOT adopted -> R07 can never run
    assert unreachable(items, {"R01", "R03"}, {"R03"}) == {"R07"}


def test_an_adopted_dependency_leaves_the_dependent_alive() -> None:
    from knowledge.ml_registry.staging import unreachable

    items = [{"id": "R01", "axis": "representation"},
             {"id": "R07", "axis": "representation", "depends_on": ["R01"]}]
    assert unreachable(items, {"R01"}, {"R01"}) == set()


def test_an_unanswered_dependency_is_not_yet_dead() -> None:
    """Unreachable means IMPOSSIBLE, not merely 'not ready'."""
    from knowledge.ml_registry.staging import unreachable

    items = [{"id": "R01", "axis": "representation"},
             {"id": "R07", "axis": "representation", "depends_on": ["R01"]}]
    assert unreachable(items, set(), set()) == set()


def test_chains_collapse_in_one_pass() -> None:
    """A fixpoint, so a chain does not take one campaign invocation per link to die off."""
    from knowledge.ml_registry.staging import unreachable

    items = [{"id": "A", "axis": "x"},
             {"id": "B", "axis": "x", "depends_on": ["A"]},
             {"id": "C", "axis": "x", "depends_on": ["B"]},
             {"id": "D", "axis": "x", "depends_on": ["C"]}]
    assert unreachable(items, {"A"}, set()) == {"B", "C", "D"}


def test_unreachable_items_let_their_stage_close() -> None:
    """The whole point: without this the stage stays open with an empty queue forever, and the
    campaign exits 0 looking finished."""
    from knowledge.ml_registry.staging import open_stage, unreachable

    items = [{"id": "R01", "axis": "representation"},
             {"id": "R07", "axis": "representation", "depends_on": ["R01"]},
             {"id": "M01", "axis": "architecture"}]
    answered = {"R01"}
    assert open_stage(items, answered, STAGES) == "representation"        # wedged
    answered |= unreachable(items, answered, set())
    assert open_stage(items, answered, STAGES) == "architecture"          # freed


def test_next_queue_unions_unreachable_and_opens_the_next_stage() -> None:
    """R01 parked (answered, not adopted) kills R07; architecture opens with M01."""
    items = [{"id": "R01", "axis": "representation"},
             {"id": "R07", "axis": "representation", "depends_on": ["R01"]},
             {"id": "M01", "axis": "architecture"}]
    stage, queue, blocked = next_queue(items, {"R01"}, set(), STAGES)
    assert stage == "architecture"
    assert [i["id"] for i in queue] == ["M01"]
    assert "R07" in blocked


def test_a_dependency_that_is_not_a_registered_idea_is_unreachable() -> None:
    """Prose preconditions and typos are not 'not yet answered' -- they can never be adopted."""
    from knowledge.ml_registry.staging import unreachable

    items = [{"id": "S06", "axis": "gate", "depends_on": ["player tracks on the same frames"]},
             {"id": "A01", "axis": "gate"}]
    assert unreachable(items, set(), set()) == {"S06"}


def test_next_queue_frees_a_stage_held_by_a_missing_dependency() -> None:
    """The live incident: S06/S07 depended on strings that were not ideas, so they
    were leftover-and-ineligible and StagingStuck wedged the campaign."""
    items = [{"id": "S06", "axis": "representation", "depends_on": ["s40 detcensus"]},
             {"id": "M01", "axis": "architecture"}]
    stage, queue, blocked = next_queue(items, set(), set(), STAGES)
    assert "S06" in blocked
    assert stage == "architecture"
    assert [i["id"] for i in queue] == ["M01"]


def test_next_queue_raises_when_a_stage_is_stuck() -> None:
    """A cycle: each leftover depends on the other, neither is adopted, neither can run."""
    items = [{"id": "R04", "axis": "representation", "depends_on": ["R05"]},
             {"id": "R05", "axis": "representation", "depends_on": ["R04"]},
             {"id": "M01", "axis": "architecture"}]
    try:
        next_queue(items, set(), set(), STAGES)
    except StagingStuck as exc:
        assert exc.stage == "representation"
        assert set(exc.leftover) == {"R04", "R05"}
        return
    raise AssertionError("expected StagingStuck")


def test_next_queue_exhausted_returns_none() -> None:
    stage, queue, blocked = next_queue(_items(), {"R1", "R2", "M1", "T1"}, set(), STAGES)
    assert stage is None
    assert queue == []
    assert blocked == set()


def test_staging_stuck_names_the_stage_and_the_leftover_ids() -> None:
    items = [{"id": "R04", "axis": "representation", "depends_on": ["R05"]},
             {"id": "R05", "axis": "representation", "depends_on": ["R04"]},
             {"id": "M01", "axis": "architecture"}]
    try:
        next_queue(items, set(), set(), STAGES)
    except StagingStuck as exc:
        message = str(exc)
        assert "representation" in message
        assert "R04" in message
        return
    raise AssertionError("expected StagingStuck")


def test_coverage_counts_only_arms_that_actually_RAN() -> None:
    """A stage answered by exclusions and dead dependencies has tested nothing."""
    from knowledge.ml_registry.staging import stage_coverage

    items = [{"id": f"M0{i}", "axis": "architecture"} for i in range(1, 6)]
    # M01/M04 ran; M02 skipped at registration, M05 unreachable, M03 a no-op vs the incumbent
    cov = {c["stage"]: c for c in stage_coverage(items, STAGES, measured_ids={"M01", "M04"})}
    arch = cov["architecture"]
    assert arch["total"] == 5
    assert arch["measured"] == 2
    assert arch["answered_without_running"] == 3
    assert arch["thin"] is True


def test_a_stage_with_enough_real_arms_is_not_thin() -> None:
    from knowledge.ml_registry.staging import stage_coverage

    items = [{"id": f"M0{i}", "axis": "architecture"} for i in range(1, 5)]
    cov = {c["stage"]: c for c in
           stage_coverage(items, STAGES, measured_ids={"M01", "M02", "M03"})}
    assert cov["architecture"]["thin"] is False


def test_an_empty_stage_is_not_thin() -> None:
    """Thin means 'closed on too little evidence', not 'has no items'."""
    from knowledge.ml_registry.staging import stage_coverage

    cov = {c["stage"]: c for c in stage_coverage([], STAGES, measured_ids=set())}
    assert all(not c["thin"] for c in cov.values())


def test_thin_stages_names_them_for_reporting() -> None:
    from knowledge.ml_registry.staging import stage_coverage, thin_stages

    items = ([{"id": f"R0{i}", "axis": "representation"} for i in range(1, 5)]
             + [{"id": "M01", "axis": "architecture"}])
    cov = stage_coverage(items, STAGES, measured_ids={"R01", "R02", "R03", "M01"})
    assert thin_stages(cov) == ["architecture"]


def test_a_stage_that_has_not_started_is_pending_not_thin() -> None:
    """Flagging unrun stages trains a reader to ignore the flag that matters. Measured on the
    first campaign to use this: three of five stages were flagged purely for not having started."""
    from knowledge.ml_registry.staging import stage_coverage

    items = ([{"id": f"R0{i}", "axis": "representation"} for i in range(1, 5)]
             + [{"id": f"M0{i}", "axis": "architecture"} for i in range(1, 4)])
    cov = {c["stage"]: c for c in stage_coverage(
        items, STAGES, measured_ids={"R01", "R02", "R03", "R04"},
        answered_ids={"R01", "R02", "R03", "R04"})}          # architecture untouched
    assert cov["representation"]["closed"] and not cov["representation"]["thin"]
    assert not cov["architecture"]["closed"]
    assert not cov["architecture"]["thin"]


def test_a_closed_stage_on_too_little_evidence_is_still_thin() -> None:
    from knowledge.ml_registry.staging import stage_coverage

    items = [{"id": f"M0{i}", "axis": "architecture"} for i in range(1, 6)]
    cov = {c["stage"]: c for c in stage_coverage(
        items, STAGES, measured_ids={"M01", "M04"},
        answered_ids={f"M0{i}" for i in range(1, 6)})}
    assert cov["architecture"]["closed"] and cov["architecture"]["thin"]


def test_without_answered_ids_it_falls_back_to_the_old_behaviour() -> None:
    from knowledge.ml_registry.staging import stage_coverage

    items = [{"id": "M01", "axis": "architecture"}]
    cov = {c["stage"]: c for c in stage_coverage(items, STAGES, measured_ids=set())}
    assert cov["architecture"]["thin"] is True
