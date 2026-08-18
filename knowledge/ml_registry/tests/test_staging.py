"""Tests for backlog staging. Fixtures are plain dicts; nothing touches a registry or a database."""

from __future__ import annotations

from knowledge.ml_registry.staging import eligible, open_stage, stage_progress

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
