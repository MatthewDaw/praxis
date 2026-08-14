"""Cross-model insight detection and the af-learn lesson-filing gate (R17).

Builds on R8's campaign supervisor (:mod:`knowledge.ml_registry.supervisor`) and R3/R4's
idea lifecycle + query surface (:mod:`knowledge.ml_registry.lifecycle`). An "insight" is the
recurring (axis, description) pair behind an idea -- the SAME idea shape tried, independently,
against more than one registered model. A single model's own repeated trials on one idea never
constitute a filed lesson by themselves, no matter how many times it is retried: only recurrence
across >= 2 DISTINCT models, each with at least one TERMINAL-verdict idea sharing that insight,
confirms the pattern worth teaching the factory.

:func:`maybe_file_cross_model_lesson` is the ONE gate every filing path funnels through, called:

* at model close (:func:`~knowledge.ml_registry.supervisor.supervise_campaign`'s close handling)
  -- the natural "look back over everything this campaign learned" moment, or
* immediately after a trial's :func:`~knowledge.ml_registry.verdict.adjudicate_verdict` side
  effects land an idea in a TERMINAL status (adopted/rejected/parked) -- a "confirmed cross-trial
  pattern" the moment it becomes visible, without waiting for the whole campaign to close.

Neither call site fires from the TRIAL alone: :func:`cross_model_insight` is recomputed fresh
from the registry every time and only ever returns a result once at least one idea sharing the
insight key has reached a terminal verdict; a still-running/claimed/untried idea never counts. A
voided trial leaves its idea untried, so it can never itself confirm anything. Filing itself is
further gated by :func:`_already_filed`, so a pattern that stays confirmed across many later
calls (further trials, further close events) files EXACTLY ONCE.

The actual write is never performed here: :data:`LessonFiler` is an injected callable (the same
seam :mod:`knowledge.ml_registry.supervisor` uses for its own ``Dispatcher``/``IdeaGenerator``),
so this module stays provable without live Praxis/auth infrastructure. A real caller wires it to
``agent_factory.ingestion_api.ingest`` (or ``write_lesson`` directly). :func:`build_lesson_payload`
is what enforces the two binding rules a filed lesson/check must obey regardless of which project
happened to fund the trial that surfaced it:

* the lesson/check always targets ``project=ML_RESEARCH_PROJECT`` -- never whatever project a
  cross-project ticket (:mod:`knowledge.ml_registry.cross_project`) happened to fund the trial
  under, so it lands in (and is queryable from) the ml-research space and never pollutes another
  project's ``building-validation`` snapshot.
* the drafted check's ``applies_to`` names ONLY the originating model id -- never a project-wide
  or universal tag -- so it can never gate an unrelated project's ticket, which only ever
  resolves checks by ITS OWN tags/surfaces/wildcard (see
  ``agent_factory/hooks/_ticket_state.py::resolve_validation_requirements``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from knowledge.ml_registry.schema import IDEA, TRIAL, TRIAL_STATUS_VOIDED
from knowledge.ml_registry.write_path import Fact, RegistrySpace

# The ml-research project's own Praxis space -- a filed lesson/check ALWAYS lands here,
# regardless of which project's ticket (via knowledge.ml_registry.cross_project) happened to
# fund the campaign that surfaced it.
ML_RESEARCH_PROJECT = "ml-research"

INSIGHT_FILING_CATEGORY = "insight_filing"

# An idea's insight is only "confirmed" once it has reached one of these statuses -- untried
# or still-claimed carries no confirmed learning yet, and a voided trial never advances its
# idea past untried, so it can never contribute here.
TERMINAL_IDEA_STATUSES = frozenset({"adopted", "rejected", "parked"})

# A confirmed insight must recur across at least this many DISTINCT models before it is worth
# teaching the factory -- one model's own experience is not yet a pattern.
MIN_DISTINCT_MODELS = 2


def _normalized_meta_field(idea: Fact, field: str) -> str:
    """Whitespace/case-collapsed value of one ``idea.meta`` field -- the shared normalization
    :func:`insight_key` applies identically to every field it keys on."""
    return " ".join(str(idea.meta.get(field) or "").strip().casefold().split())


def insight_key(idea: Fact) -> str:
    """The canonical identity of an idea's insight: its normalized ``(axis, description)``.

    Two ideas -- on the same model or different ones -- carry the SAME insight iff they
    normalize to the same key. Whitespace/case differences never fragment the same insight
    into two.
    """
    axis = _normalized_meta_field(idea, "axis")
    description = _normalized_meta_field(idea, "description")
    return f"{axis}::{description}"


@dataclass(frozen=True)
class CrossModelInsight:
    """One insight's CURRENT cross-model recurrence, recomputed fresh each call -- never
    cached across dispatches or close events."""

    key: str
    model_trial_counts: dict[str, int]  # model_id -> non-voided trial count behind this insight

    @property
    def distinct_models(self) -> frozenset[str]:
        return frozenset(self.model_trial_counts)

    @property
    def confirmed(self) -> bool:
        return len(self.distinct_models) >= MIN_DISTINCT_MODELS


def _non_voided_trial_count(space: RegistrySpace, idea_id: str) -> int:
    return sum(
        1
        for t in space.list_facts(TRIAL)
        if t.meta.get("idea_id") == idea_id and t.meta.get("status") != TRIAL_STATUS_VOIDED
    )


def cross_model_insight(space: RegistrySpace, key: str) -> Optional[CrossModelInsight]:
    """The CURRENT cross-model recurrence for one insight ``key``, read fresh from ``space``.

    Only ideas that have reached a :data:`TERMINAL_IDEA_STATUSES` verdict count -- an idea
    still untried/claimed carries no confirmed insight yet, matching a trial's own
    per-idea/per-model terminal status rather than merely having been attempted.
    Returns ``None`` when no terminal-status idea anywhere carries this key.
    """
    if not key.strip(":"):
        return None
    trial_counts: dict[str, int] = {}
    for idea in space.list_facts(IDEA):
        if insight_key(idea) != key:
            continue
        if str(idea.meta.get("status")) not in TERMINAL_IDEA_STATUSES:
            continue
        model_id = str(idea.meta.get("model_id"))
        trial_counts[model_id] = trial_counts.get(model_id, 0) + _non_voided_trial_count(space, idea.id)
    if not trial_counts:
        return None
    return CrossModelInsight(key=key, model_trial_counts=trial_counts)


def _already_filed(space: RegistrySpace, key: str) -> bool:
    return any(
        f.meta.get("insight_key") == key for f in space.list_facts(INSIGHT_FILING_CATEGORY)
    )


def _mark_filed(space: RegistrySpace, key: str, *, model_id: str, lesson_id: object) -> None:
    space.insert(
        INSIGHT_FILING_CATEGORY,
        {"insight_key": key, "originating_model_id": model_id, "lesson_id": lesson_id},
    )


def build_lesson_payload(insight: CrossModelInsight, model_id: str) -> dict[str, object]:
    """The af-learn lesson + drafted-check payload for a CONFIRMED cross-model insight.

    Always targets :data:`ML_RESEARCH_PROJECT` (never a funding project) and scopes the
    drafted check's ``applies_to`` to ONLY the originating model id -- narrow enough that it
    can never gate an unrelated project's ticket, which resolves checks by its own tags/
    surfaces/wildcard, never by another project's model id.
    """
    counts_text = ", ".join(
        f"{mid}: {n} trial(s)" for mid, n in sorted(insight.model_trial_counts.items())
    )
    lesson_text = (
        f"Insight {insight.key!r} held across {len(insight.distinct_models)} distinct models "
        f"({counts_text}) -- originating model {model_id!r}."
    )
    return {
        "lesson_text": lesson_text,
        "project": ML_RESEARCH_PROJECT,
        "source": f"ml-registry-insight:{model_id}",
        "meta": {
            "insight_key": insight.key,
            "model_trial_counts": dict(insight.model_trial_counts),
            "originating_model_id": model_id,
            "applies_to": [model_id],
        },
    }


LessonFiler = Callable[[dict[str, object]], dict[str, object]]


def maybe_file_cross_model_lesson(
    space: RegistrySpace,
    model_id: str,
    idea: Fact,
    *,
    lesson_filer: Optional[LessonFiler] = None,
) -> Optional[dict[str, object]]:
    """The ONE gate every af-learn filing path funnels through.

    Called after ``idea`` reaches a terminal verdict (a confirmed cross-trial pattern, the
    moment it becomes visible) or at model close (a final sweep). NEVER fires from a trial in
    isolation: an idea still untried/claimed, or an insight confined to a single model, files
    nothing. A pattern already filed (:func:`_already_filed`) files nothing a second time, so
    a confirmed insight files EXACTLY ONCE across however many later calls observe it.

    Returns the ``lesson_filer``'s result dict, or ``None`` when nothing was filed (no
    ``lesson_filer`` supplied, the insight is unconfirmed, or it was already filed).
    """
    if lesson_filer is None:
        return None
    key = insight_key(idea)
    insight = cross_model_insight(space, key)
    if insight is None or not insight.confirmed:
        return None
    if _already_filed(space, key):
        return None
    payload = build_lesson_payload(insight, model_id)
    result = lesson_filer(payload)
    lesson_id = result.get("lesson_id") if isinstance(result, dict) else None
    _mark_filed(space, key, model_id=model_id, lesson_id=lesson_id)
    return result


def sweep_cross_model_lessons(
    space: RegistrySpace, model_id: str, *, lesson_filer: Optional[LessonFiler] = None
) -> list[dict[str, object]]:
    """At model close: evaluate every terminal-status idea THIS model contributed and file a
    lesson for each newly-confirmed cross-model insight (idempotent per insight -- see
    :func:`maybe_file_cross_model_lesson`). Returns the list of filer results actually filed
    (empty when nothing new was confirmed).
    """
    filed: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    for idea in space.list_facts(IDEA):
        if str(idea.meta.get("model_id")) != model_id:
            continue
        if str(idea.meta.get("status")) not in TERMINAL_IDEA_STATUSES:
            continue
        key = insight_key(idea)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        result = maybe_file_cross_model_lesson(space, model_id, idea, lesson_filer=lesson_filer)
        if result is not None:
            filed.append(result)
    return filed
