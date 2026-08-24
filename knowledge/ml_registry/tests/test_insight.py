"""R17 acceptance: the campaign supervisor files an af-learn lesson only for an insight the
registry shows held across more than one model, at model close or on a confirmed cross-trial
pattern, and never at trial granularity -- with the lesson (and any drafted check) bound to
the ml-research project rather than the funding project, and each drafted check carrying its
originating model id and a narrow ``applies_to``.

Covers, directly against :mod:`knowledge.ml_registry.insight` and its wiring through
:mod:`knowledge.ml_registry.supervisor`:

* a fixture whose insight appears on only one model files no lesson.
* a fixture whose insight recurs across two models files EXACTLY one lesson, carrying the
  cross-model trial counts read from the registry query.
* a model that reached a terminal verdict by a pure registry write (no trial ever ran) is
  NOT a confirming model, and the filed lesson reports the verdict mix rather than calling
  a twice-rejected insight one that "held".
* no fixture files a lesson at trial granularity -- a voided trial, and a model's own
  repeated trials on the SAME idea, never file anything by themselves.
* a lesson filed from a run funded by another project lands in the ml-research space/project
  and never targets the funding project.
* a model-specific drafted check's ``applies_to`` is narrow to the originating model id, so it
  cannot gate an unrelated project's ML ticket.
"""

from __future__ import annotations

from knowledge.ml_registry.insight import (
    ML_RESEARCH_PROJECT,
    build_lesson_payload,
    cross_model_insight,
    insight_key,
    maybe_file_cross_model_lesson,
    sweep_cross_model_lessons,
)
from knowledge.ml_registry.lifecycle import reject_idea
from knowledge.ml_registry.supervisor import dispatch_trial, supervise_campaign
from knowledge.ml_registry.verdict import LedgerRow
from knowledge.ml_registry.write_path import (
    SEEDED,
    Fact,
    RegistrySpace,
    register_idea,
    register_model,
    register_trial,
)

from knowledge.ml_registry.testing.rope_fixtures import rope_ledger_rows

BASELINE_COMMIT = "commit-abc123"

#: The rope's evidence for every fixture below: four rows measuring exactly 0.01.
ROPE_ROWS = rope_ledger_rows(0.01, at=1.0, throughput=1200)

MODEL_META = {
    "metric": "val_bpb",
    "direction": "minimize",
    "win_condition": "beats baseline by the rope",
    "baseline": BASELINE_COMMIT,
    # The rope's evidence, measuring 0.01 -- the bar these scripted verdicts were written
    # against.
    "baseline_runs": list(ROPE_ROWS),
    "baseline_throughput": 1200,
    "diff_size_limit": 800,
    "max_trials": 5,
    "max_discovered_ideas": 2,
}

LEDGER: dict[str, LedgerRow] = {BASELINE_COMMIT: LedgerRow(value=1.0, throughput=1200, diff_lines=0)}
LEDGER.update(ROPE_ROWS)
LEDGER.update({f"c{i}": LedgerRow(value=0.5, throughput=1200, diff_lines=100) for i in range(1, 20)})
LEDGER.update({f"lose{i}": LedgerRow(value=5000.0, throughput=1200, diff_lines=100) for i in range(1, 10)})


def _model(space: RegistrySpace, **overrides: object) -> str:
    return register_model(space, {**MODEL_META, **overrides})


def _idea(space: RegistrySpace, model_id: str, axis: str, description: str, origin: str = SEEDED) -> str:
    return register_idea(
        space, {"model_id": model_id, "origin": origin, "axis": axis, "description": description}
    )


# Terminal by default. These tests give one idea SEVERAL trials, which is realistic only as
# retries over time -- register_trial allows an idea just one trial IN FLIGHT, because two at once
# means one question being answered twice concurrently. "running" was an incidental helper default,
# not something any test here asserts on: insight counts an idea's trials excluding voided ones and
# never distinguishes running from succeeded, so this changes no expectation.
def _trial(space: RegistrySpace, model_id: str, idea_id: str, commit: str,
           status: str = "succeeded") -> str:
    trial_id = register_trial(
        space,
        {"model_id": model_id, "idea_id": idea_id, "commit": commit, "status": status,
         "throughput": LEDGER[commit].throughput, "diff_lines": LEDGER[commit].diff_lines},
        frozenset(LEDGER.keys()),
    )
    return trial_id


class _RecordingFiler:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(payload)
        return {"lesson_id": f"lesson-{len(self.calls)}"}


def _get(space: RegistrySpace, fact_id: str) -> Fact:
    """``RegistrySpace.get`` narrowed from ``Fact | None`` to ``Fact`` -- every caller here
    already just registered ``fact_id`` itself, so a miss is a fixture bug, not a case to
    handle; asserting narrows the type for mypy instead of re-checking ``is not None`` at
    each call site."""
    fact = space.get(fact_id)
    assert fact is not None
    return fact


def _meta_of(payload: dict[str, object]) -> dict[str, object]:
    """A filed payload's ``meta`` sub-dict, narrowed from ``object`` to ``dict[str, object]``
    -- the payload shape :func:`~knowledge.ml_registry.insight.build_lesson_payload` always
    produces, so this is a type narrowing, not a runtime check."""
    meta = payload["meta"]
    assert isinstance(meta, dict)
    return meta


def _model_trial_counts(meta: dict[str, object]) -> dict[str, int]:
    """``meta["model_trial_counts"]`` narrowed from ``object`` to ``dict[str, int]``."""
    counts = meta["model_trial_counts"]
    assert isinstance(counts, dict)
    return counts


# --------------------------------------------------------------------------- insight_key / cross_model_insight


def test_insight_confined_to_one_model_files_no_lesson() -> None:
    space = RegistrySpace()
    model_id = _model(space)
    idea_id = _idea(space, model_id, "architecture", "use a wider residual stream")
    _trial(space, model_id, idea_id, "c1")
    reject_idea(space, idea_id, "did not beat baseline")

    filer = _RecordingFiler()
    result = maybe_file_cross_model_lesson(space, model_id, _get(space, idea_id), lesson_filer=filer)

    assert result is None
    assert filer.calls == []
    insight = cross_model_insight(space, insight_key(_get(space, idea_id)))
    assert insight is not None
    assert not insight.confirmed
    assert insight.distinct_models == frozenset({model_id})


def test_insight_confirmed_across_two_models_files_exactly_one_lesson_with_cross_model_trial_counts() -> None:
    space = RegistrySpace()
    model_a = _model(space)
    model_b = _model(space)

    idea_a = _idea(space, model_a, "architecture", "use a wider residual stream")
    _trial(space, model_a, idea_a, "c1")
    _trial(space, model_a, idea_a, "c2")
    reject_idea(space, idea_a, "did not beat baseline")  # model_a's insight is now terminal

    idea_b = _idea(space, model_b, "architecture", "USE A WIDER RESIDUAL STREAM")  # same insight, different case
    _trial(space, model_b, idea_b, "c3")

    filer = _RecordingFiler()
    reject_idea(space, idea_b, "also did not beat baseline")  # model_b's insight now ALSO terminal -> confirmed

    result = maybe_file_cross_model_lesson(space, model_b, _get(space, idea_b), lesson_filer=filer)

    assert result == {"lesson_id": "lesson-1"}
    assert len(filer.calls) == 1
    payload = filer.calls[0]
    assert _model_trial_counts(_meta_of(payload)) == {model_a: 2, model_b: 1}

    # Filing again for the SAME confirmed insight (e.g. a later close sweep) must not re-file.
    result_again = maybe_file_cross_model_lesson(space, model_b, _get(space, idea_b), lesson_filer=filer)
    assert result_again is None
    assert len(filer.calls) == 1


def test_no_fixture_files_a_lesson_at_trial_granularity() -> None:
    """A voided trial, and a model's own repeated trials on the SAME idea (still only one
    model represented), never file anything -- confirmation requires >= 2 DISTINCT models,
    never merely >= 1 trial or >= 1 model."""
    space = RegistrySpace()
    model_id = _model(space)
    idea_id = _idea(space, model_id, "architecture", "widen the residual stream")

    # A voided trial never advances the idea past untried.
    _trial(space, model_id, idea_id, "c1", status="voided")
    assert _get(space, idea_id).meta.get("status") in (None, "untried")
    filer = _RecordingFiler()
    assert maybe_file_cross_model_lesson(space, model_id, _get(space, idea_id), lesson_filer=filer) is None
    assert filer.calls == []

    # Many trials, still one model: reaching terminal status alone (no second model) never files.
    _trial(space, model_id, idea_id, "c2")
    _trial(space, model_id, idea_id, "c3")
    reject_idea(space, idea_id, "still did not beat baseline")
    assert maybe_file_cross_model_lesson(space, model_id, _get(space, idea_id), lesson_filer=filer) is None
    assert filer.calls == []


def test_a_registry_only_verdict_with_no_trial_is_not_a_confirming_model() -> None:
    """``cli reject-idea`` is a pure registry write. An idea rejected that way never ran a
    trial, so it is bookkeeping and not evidence: it must not enter ``distinct_models`` (it
    would arrive with a trial count of 0) and must not push the insight to confirmed."""
    space = RegistrySpace()
    model_a = _model(space)
    model_b = _model(space)

    idea_a = _idea(space, model_a, "architecture", "use a wider residual stream")
    for commit in ("c1", "c2", "c3"):
        _trial(space, model_a, idea_a, commit)
    reject_idea(space, idea_a, "did not beat baseline")

    # Same normalized insight on model_b, rejected WITHOUT ever dispatching a trial.
    idea_b = _idea(space, model_b, "architecture", "use a wider residual stream")
    reject_idea(space, idea_b, "rejected by hand, never tried")

    insight = cross_model_insight(space, insight_key(_get(space, idea_b)))
    assert insight is not None
    assert insight.distinct_models == frozenset({model_a})
    assert model_b not in insight.model_trial_counts
    assert not insight.confirmed

    filer = _RecordingFiler()
    assert maybe_file_cross_model_lesson(space, model_b, _get(space, idea_b), lesson_filer=filer) is None
    assert filer.calls == []


def test_lesson_text_reports_the_verdict_mix_and_never_calls_two_rejections_a_hold() -> None:
    """Recurrence across two models is not agreement that the insight WORKED. An insight
    rejected on both models must read as rejected on both -- the filed lesson may never
    claim it "held"."""
    space = RegistrySpace()
    model_a = _model(space)
    model_b = _model(space)
    idea_a = _idea(space, model_a, "optimizer", "try a cosine schedule")
    _trial(space, model_a, idea_a, "c1")
    reject_idea(space, idea_a, "no gain")
    idea_b = _idea(space, model_b, "optimizer", "try a cosine schedule")
    _trial(space, model_b, idea_b, "c2")
    reject_idea(space, idea_b, "no gain")

    insight = cross_model_insight(space, insight_key(_get(space, idea_b)))
    assert insight is not None and insight.confirmed
    assert insight.verdict_mix == {"rejected": 2}

    payload = build_lesson_payload(insight, model_b)
    lesson_text = payload["lesson_text"]
    assert isinstance(lesson_text, str)
    assert "held" not in lesson_text
    assert "rejected on 2 model(s)" in lesson_text
    assert _meta_of(payload)["verdict_mix"] == {"rejected": 2}


def test_lesson_filed_from_a_run_funded_by_another_project_lands_in_ml_research_not_the_funding_project() -> None:
    space = RegistrySpace()
    model_a = _model(space)
    model_b = _model(space)
    idea_a = _idea(space, model_a, "data", "augment with synthetic pairs")
    _trial(space, model_a, idea_a, "c1")
    reject_idea(space, idea_a, "no gain")
    idea_b = _idea(space, model_b, "data", "augment with synthetic pairs")
    _trial(space, model_b, idea_b, "c2")
    reject_idea(space, idea_b, "no gain")

    insight = cross_model_insight(space, insight_key(_get(space, idea_b)))
    assert insight is not None and insight.confirmed
    payload = build_lesson_payload(insight, model_b)

    # The funding project (whatever ticket happened to dispatch this trial) is NEVER the
    # target -- the payload always names the ml-research project, regardless of what a
    # caller might otherwise have assumed from cross-project ticket linkage.
    assert payload["project"] == ML_RESEARCH_PROJECT


def test_drafted_check_applies_to_is_narrow_to_the_originating_model_not_an_unrelated_project() -> None:
    space = RegistrySpace()
    model_a = _model(space)
    model_b = _model(space)
    idea_a = _idea(space, model_a, "optimizer", "try a cosine schedule")
    _trial(space, model_a, idea_a, "c1")
    reject_idea(space, idea_a, "no gain")
    idea_b = _idea(space, model_b, "optimizer", "try a cosine schedule")
    _trial(space, model_b, idea_b, "c2")
    reject_idea(space, idea_b, "no gain")

    insight = cross_model_insight(space, insight_key(_get(space, idea_b)))
    assert insight is not None
    payload = build_lesson_payload(insight, model_b)

    # Narrow to the originating model id ONLY -- no project-wide or universal ("*") tag that
    # an unrelated project's ticket resolution (tag/surface/wildcard) could ever pick up. A
    # single exact-equality assertion covers "just the model id, nothing else" completely --
    # separately asserting what ISN'T in a one-element list is implied, not independent signal.
    applies_to = _meta_of(payload)["applies_to"]
    assert applies_to == [model_b]


# --------------------------------------------------------------------------- wiring through the supervisor


def test_dispatch_trial_files_a_lesson_only_once_the_second_model_confirms_the_pattern() -> None:
    space = RegistrySpace()
    model_a = _model(space)
    model_b = _model(space)
    _idea(space, model_a, "architecture", "shared insight")
    _idea(space, model_b, "architecture", "shared insight")

    filer = _RecordingFiler()

    def dispatcher(_space: RegistrySpace, _model: Fact, _idea: Fact) -> dict[str, object]:
        return {"commit": "lose1"}

    result_a = dispatch_trial(space, model_a, LEDGER, dispatcher, lesson_filer=filer)
    assert result_a["status"] == "rejected"
    assert filer.calls == []  # only one model so far -- unconfirmed

    result_b = dispatch_trial(space, model_b, LEDGER, dispatcher, lesson_filer=filer)
    assert result_b["status"] == "rejected"
    assert len(filer.calls) == 1  # now confirmed across two distinct models
    assert _model_trial_counts(_meta_of(filer.calls[0])) == {model_a: 1, model_b: 1}


def test_supervise_campaign_close_sweep_files_a_lesson_confirmed_only_at_close() -> None:
    """A campaign that closes on backlog-exhausted still runs the close sweep, which is
    where a pattern confirmed by another model's earlier (already-terminal) idea is caught,
    even though nothing about THIS campaign's own dispatches alone confirmed it."""
    space = RegistrySpace()
    model_a = _model(space)
    idea_a = _idea(space, model_a, "regularization", "add dropout before the head")
    _trial(space, model_a, idea_a, "c1")
    reject_idea(space, idea_a, "no gain")  # model_a's half of the insight, already terminal

    model_b = _model(space)
    _idea(space, model_b, "regularization", "add dropout before the head")

    filer = _RecordingFiler()

    def dispatcher(_space: RegistrySpace, _model: Fact, _idea: Fact) -> dict[str, object]:
        return {"commit": "lose2"}

    outcome = supervise_campaign(space, model_b, LEDGER, dispatcher, lesson_filer=filer)
    assert outcome["close"] in ("max_trials_reached", "backlog_exhausted")
    assert len(filer.calls) == 1
    assert _model_trial_counts(_meta_of(filer.calls[0])).keys() == {model_a, model_b}


def test_sweep_cross_model_lessons_is_idempotent_across_repeated_close_events() -> None:
    space = RegistrySpace()
    model_a = _model(space)
    model_b = _model(space)
    idea_a = _idea(space, model_a, "data", "dedup the corpus")
    _trial(space, model_a, idea_a, "c1")
    reject_idea(space, idea_a, "no gain")
    idea_b = _idea(space, model_b, "data", "dedup the corpus")
    _trial(space, model_b, idea_b, "c2")
    reject_idea(space, idea_b, "no gain")

    filer = _RecordingFiler()
    filed_first = sweep_cross_model_lessons(space, model_b, lesson_filer=filer)
    assert len(filed_first) == 1
    filed_second = sweep_cross_model_lessons(space, model_b, lesson_filer=filer)
    assert filed_second == []
    assert len(filer.calls) == 1
