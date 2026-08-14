"""af-ml-ideate: seed a model's starting idea set (R6).

Seeds a model's STARTING idea set -- not an exhaustive plan -- by sweeping a fixed,
nine-value CLOSED axis set:

* six GENERATIVE axes -- theoretical math, ablation, supplements, ML architectures,
  non-ML learning methods, and a ce-ideate breadth pass (:data:`GENERATIVE_AXES`).
* three RETRIEVAL axes -- current code, prior trials already in the registry, and the
  af-learn lesson space (:data:`RETRIEVAL_AXES`).

Builds on R2's write path (:mod:`knowledge.ml_registry.write_path`): every idea this module
writes goes through :func:`~knowledge.ml_registry.write_path.register_idea` with
``origin="seeded"`` and a ``meta.axis`` drawn from :data:`IDEATION_AXES`, so it is subject to
the exact same schema validation and lifecycle as every other idea in the registry -- ideation
adds no side channel of its own.

Every external decision (what a generative axis proposes, what a retrieval axis retrieves, and
-- interactive mode only -- whether the human confirms a candidate) is an INJECTED callable, the
same seam :mod:`knowledge.ml_registry.supervisor` uses for its own ``Dispatcher``/
``IdeaGenerator``: this module stays provable without a live LLM, a live code index, or a live
af-learn/Praxis connection. A real caller (the af-ml-ideate skill) wires ``generator`` to an
LLM-backed proposal per axis, ``retriever`` to the current-code/prior-trials/af-learn-lesson
lookups, and (interactively) ``confirm`` to a human prompt.

Batch vs. interactive is ONE seam (``confirm``): :func:`always_confirm` (batch -- every proposed
candidate is written) or a human-backed confirmer (interactive -- only confirmed candidates are
written). Both modes write ideas of IDENTICAL shape; only which candidates get through differs.

Every retrieval axis records an :class:`ExecutionReceipt` -- the query issued, the count
returned, and the ids retrieved -- regardless of whether it yielded a seedable idea, so a
retrieval axis that legitimately found nothing still proves it ran.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.write_path import SEEDED, RegistrySpace, register_idea

# --- the nine-value closed axis set (R6's acceptance floor) ---

THEORETICAL_MATH = "theoretical_math"
ABLATION = "ablation"
SUPPLEMENTS = "supplements"
ML_ARCHITECTURES = "ml_architectures"
NON_ML_METHODS = "non_ml_methods"
CE_IDEATE_BREADTH = "ce_ideate_breadth"

GENERATIVE_AXES: tuple[str, ...] = (
    THEORETICAL_MATH,
    ABLATION,
    SUPPLEMENTS,
    ML_ARCHITECTURES,
    NON_ML_METHODS,
    CE_IDEATE_BREADTH,
)

CURRENT_CODE = "current_code"
PRIOR_TRIALS = "prior_trials"
AF_LEARN_LESSONS = "af_learn_lessons"

RETRIEVAL_AXES: tuple[str, ...] = (CURRENT_CODE, PRIOR_TRIALS, AF_LEARN_LESSONS)

# The full nine-value closed set every seeded idea's meta.axis must be drawn from.
IDEATION_AXES: tuple[str, ...] = GENERATIVE_AXES + RETRIEVAL_AXES


# A generator proposes candidate idea metas for ONE generative axis, given the model fact's own
# meta (its metric, win_condition, ...). Each candidate need only carry enough to become an idea
# meta -- at minimum "description" -- this module stamps model_id/origin/axis itself.
GeneratorFn = Callable[[str, dict[str, object]], list[dict[str, object]]]

# A retriever answers ONE retrieval axis: the query it issued and the rows it retrieved. This
# module derives the execution receipt from the retriever's own count/ids -- a retriever never
# fabricates its own receipt, so the receipt is always an honest reflection of what came back.
RetrieverFn = Callable[[str, dict[str, object]], "RetrievalResult"]

# Interactive confirmation seam: given the axis and a candidate idea meta, return True to seed
# it. Batch mode passes :func:`always_confirm`, which always returns True.
ConfirmFn = Callable[[str, dict[str, object]], bool]


@dataclass(frozen=True)
class RetrievalResult:
    """One retrieval axis's raw yield: the query issued and the rows it returned.

    Each row is a dict carrying at least ``"id"`` (the source id this row was retrieved from --
    a prior trial's fact id, a lesson id, a code symbol/path) and enough to become an idea meta
    (at minimum ``"description"``).
    """

    query: str
    rows: tuple[dict[str, object], ...] = ()

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(str(row.get("id")) for row in self.rows)

    @property
    def count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class ExecutionReceipt:
    """Proof a retrieval axis actually ran: the query issued, the count returned, and the ids
    retrieved -- R6's acceptance condition for each of the three retrieval axes."""

    axis: str
    query: str
    count: int
    ids: tuple[str, ...]

    def to_meta(self) -> dict[str, object]:
        return {"axis": self.axis, "query": self.query, "count": self.count, "ids": list(self.ids)}


def always_confirm(axis: str, candidate: dict[str, object]) -> bool:
    """The batch-mode confirmer: every proposed candidate is written, unconditionally."""
    return True


def _seed_idea(space: RegistrySpace, model_id: str, axis: str, candidate: dict[str, object]) -> str:
    meta = dict(candidate)
    meta["model_id"] = model_id
    meta["origin"] = SEEDED
    meta["axis"] = axis
    return register_idea(space, meta)


def _confirm_and_seed(
    space: RegistrySpace,
    model_id: str,
    axis: str,
    candidates: Iterable[dict[str, object]],
    confirm: ConfirmFn,
) -> list[str]:
    """Run ``confirm`` over each candidate and seed the ones it lets through -- the one loop
    shared by both the generative and retrieval sweeps below."""
    return [
        _seed_idea(space, model_id, axis, candidate)
        for candidate in candidates
        if confirm(axis, candidate)
    ]


def sweep_generative_axes(
    space: RegistrySpace,
    model_id: str,
    model_meta: dict[str, object],
    generator: GeneratorFn,
    *,
    confirm: ConfirmFn = always_confirm,
    axes: Iterable[str] = GENERATIVE_AXES,
) -> dict[str, list[str]]:
    """Sweep every generative axis, writing a SEEDED idea for each candidate ``confirm`` lets
    through. Returns ``{axis: [idea_id, ...]}`` -- an axis whose generator proposed nothing, or
    whose candidates were all declined, lands with an empty list rather than being omitted, so
    the caller can see every mandated axis ran even when it yielded nothing.
    """
    written: dict[str, list[str]] = {}
    for axis in axes:
        written[axis] = _confirm_and_seed(space, model_id, axis, generator(axis, model_meta), confirm)
    return written


def sweep_retrieval_axes(
    space: RegistrySpace,
    model_id: str,
    model_meta: dict[str, object],
    retriever: RetrieverFn,
    *,
    confirm: ConfirmFn = always_confirm,
    axes: Iterable[str] = RETRIEVAL_AXES,
) -> tuple[dict[str, list[str]], list[ExecutionReceipt]]:
    """Sweep every retrieval axis: issue its query, record an :class:`ExecutionReceipt`
    regardless of yield (a receipt proves the axis ran, independent of whether it produced a
    seedable idea), then seed a confirmed idea per retrieved row.
    """
    written: dict[str, list[str]] = {}
    receipts: list[ExecutionReceipt] = []
    for axis in axes:
        result = retriever(axis, model_meta)
        receipts.append(ExecutionReceipt(axis=axis, query=result.query, count=result.count, ids=result.ids))
        candidates = ({k: v for k, v in row.items() if k != "id"} for row in result.rows)
        written[axis] = _confirm_and_seed(space, model_id, axis, candidates, confirm)
    return written, receipts


@dataclass(frozen=True)
class SeedRun:
    """The full result of one seeding pass: every axis's written idea ids plus the retrieval
    axes' execution receipts."""

    written: dict[str, list[str]]
    receipts: tuple[ExecutionReceipt, ...]

    @property
    def idea_ids(self) -> list[str]:
        return [idea_id for ids in self.written.values() for idea_id in ids]


def seed_campaign(
    space: RegistrySpace,
    model_id: str,
    *,
    generator: GeneratorFn,
    retriever: RetrieverFn,
    confirm: ConfirmFn = always_confirm,
) -> SeedRun:
    """Seed a model's starting idea set: sweep all nine mandated axes (six generative, three
    retrieval) and return every idea written plus each retrieval axis's execution receipt.

    ``confirm`` is the ONE seam distinguishing batch from interactive seeding -- batch passes
    :func:`always_confirm` (every candidate is written); interactive passes a confirmer backed
    by the human. Both write ideas of IDENTICAL shape (``origin="seeded"``, a ``meta.axis`` drawn
    from :data:`IDEATION_AXES`) -- only which candidates get through differs. This run is not
    required to enumerate every idea the loop will eventually try -- a generator/retriever may
    legitimately propose nothing on an axis for this model.
    """
    model = space.get(model_id)
    if model is None:
        raise RegistryValidationError(f"model {model_id!r} was never registered", field="model_id")
    model_meta = dict(model.meta)

    written = sweep_generative_axes(space, model_id, model_meta, generator, confirm=confirm)
    retrieval_written, receipts = sweep_retrieval_axes(space, model_id, model_meta, retriever, confirm=confirm)
    written.update(retrieval_written)

    return SeedRun(written=written, receipts=tuple(receipts))
