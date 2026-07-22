"""Graded-rubric types and the pure min-of-axes verdict math.

A **graded check** is the second kind of validation check (alongside binary exit-code
checks). Its pass/fail is not an exit code but a subjective, multi-axis judgment — yet the
subjectivity is encapsulated here so the verdict still reduces to a single ``passed`` boolean
that flows through the existing coverage gate (``_ticket_state.all_validations_passed``)
untouched.

Design invariants (see docs/plans/2026-07-21-001-feat-graded-rubric-checks-plan.md):

* **min-of-axes, not weighted average.** A check passes only if EVERY declared axis clears its
  own threshold; one weak axis is never masked by strong ones. Axis importance is expressed as
  a higher per-axis threshold — never a weight (weights are incompatible with ``min``).
* **positive-evidence-of-defect to FAIL.** A graded check may only fail on concrete evidence: a
  below-threshold axis score (a numeric judgment against a declared criterion) OR a located
  defect (file/line + problem + remedy). Vague dissatisfaction with no located defect and no
  failing axis passes — this is the primary guard against thrashing the forcibly-continue loop.
* **confidence floor.** Defects below the floor are dropped, not failed on — a low-confidence
  would-be-failure never reopens the loop.

This module is PURE: no LLM calls, no I/O. The judge wrapper (``graded_verdict``) produces axis
scores + defects and calls :func:`evaluate` here.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Axis:
    """One scored dimension of a rubric. ``score >= threshold`` is a pass on this axis."""

    name: str
    threshold: float  # in [0, 1]
    guidance: str = ""


@dataclass(frozen=True)
class Anchors:
    """Literal, copy-pasted code exemplars that pin the judge's taste for a subjective check.

    ``good`` demonstrates the standard met; ``slop`` demonstrates it violated. These are verbatim
    text injected into the judge prompt (see :func:`graded_verdict.build_judge_prompt`) — NO scoring,
    NO versioning, no infrastructure. Their whole job is reproducibility: the same snippets always
    frame the same judgment.
    """

    good: tuple[str, ...] = ()
    slop: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rubric:
    """A graded check's rubric: the axes to score, the confidence floor, and judge guidance."""

    axes: tuple[Axis, ...]
    confidence_floor: int = 5  # defects with confidence < floor are dropped
    criterion: str = ""
    judge_prompt: str = ""
    anchors: Anchors | None = None  # optional calibration exemplars; absent -> None (byte-compatible)

    def axis_names(self) -> tuple[str, ...]:
        return tuple(a.name for a in self.axes)


@dataclass(frozen=True)
class Defect:
    """A located, actionable problem the judge found. ``confidence`` is 1..10."""

    problem: str
    remedy: str
    confidence: int
    file: str = ""
    line: int | None = None


@dataclass(frozen=True)
class Verdict:
    """The outcome of grading one code-state against one rubric."""

    passed: bool
    axis_scores: dict[str, float] = field(default_factory=dict)
    defects: tuple[Defect, ...] = ()  # credible defects only (>= floor)
    min_axis: float | None = None
    reason: str = ""


def rubric_from_dict(data: dict) -> Rubric:
    """Build a :class:`Rubric` from a plain dict (TOML/JSON/Praxis-meta shape).

    Raises ``ValueError`` on a malformed rubric so bad definitions fail loudly at load/pin time
    rather than silently degrading a verdict at VERIFY time.
    """
    raw_axes = data.get("axes") or []
    if not raw_axes:
        raise ValueError("graded rubric requires at least one axis")
    axes: list[Axis] = []
    for a in raw_axes:
        name = str(a.get("name") or "").strip()
        if not name:
            raise ValueError("rubric axis requires a name")
        try:
            threshold = float(a.get("threshold"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"axis {name!r} requires a numeric threshold") from exc
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"axis {name!r} threshold must be in [0, 1], got {threshold}")
        axes.append(Axis(name=name, threshold=threshold, guidance=str(a.get("guidance") or "")))
    if len({a.name for a in axes}) != len(axes):
        raise ValueError("rubric axis names must be unique")
    floor = int(data.get("confidence_floor", 5))
    if not 1 <= floor <= 10:
        raise ValueError(f"confidence_floor must be in [1, 10], got {floor}")
    return Rubric(
        axes=tuple(axes),
        confidence_floor=floor,
        criterion=str(data.get("criterion") or ""),
        judge_prompt=str(data.get("judge_prompt") or ""),
        anchors=_anchors_from_dict(data.get("anchors")),
    )


def _anchors_from_dict(raw: object) -> Anchors | None:
    """Parse the optional ``anchors`` block. Absent -> None (byte-compatible with pre-anchor
    rubrics); a malformed block raises ``ValueError`` so a bad definition fails loudly at load."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("rubric anchors must be a mapping of {good:[...], slop:[...]}")
    sides: dict[str, tuple[str, ...]] = {}
    for side in ("good", "slop"):
        vals = raw.get(side, [])
        if not isinstance(vals, (list, tuple)):
            raise ValueError(f"rubric anchors {side!r} must be a list of strings")
        sides[side] = tuple(str(v) for v in vals)
    return Anchors(good=sides["good"], slop=sides["slop"])


def rubric_to_dict(rubric: Rubric) -> dict:
    """Serialize a :class:`Rubric` back to the plain-dict shape :func:`rubric_from_dict` accepts.

    The inverse of :func:`rubric_from_dict` — used to FREEZE a seeded rubric onto a pinned validation
    (``_ticket_state``) so VERIFY reads back an identical target. Omits ``anchors`` entirely when
    absent, so a no-anchor rubric round-trips byte-for-byte.
    """
    data: dict = {
        "axes": [{"name": a.name, "threshold": a.threshold, "guidance": a.guidance}
                 for a in rubric.axes],
        "confidence_floor": rubric.confidence_floor,
        "criterion": rubric.criterion,
        "judge_prompt": rubric.judge_prompt,
    }
    if rubric.anchors is not None:
        data["anchors"] = {"good": list(rubric.anchors.good), "slop": list(rubric.anchors.slop)}
    return data


def evaluate(rubric: Rubric, axis_scores: dict[str, float], defects: list[Defect]) -> Verdict:
    """Compute the verdict from judge output. PURE — the whole subjective->boolean reduction.

    Passing requires ALL of:
      * every declared axis has a score AND that score >= the axis threshold (min-of-axes), and
      * no credible defect (>= confidence floor) remains.

    A failure is ALWAYS backed by concrete evidence — a named below-threshold axis or a located
    defect — never by absence. A missing axis score is treated as a fail (the judge did not do
    its job), surfaced in ``reason``; callers that want to distinguish malformed output from a
    genuine fail should validate the raw judge output first (see ``graded_verdict``).
    """
    scored = {name: axis_scores[name] for a in rubric.axes if (name := a.name) in axis_scores}
    min_axis = min(scored.values()) if scored else None

    missing = [a.name for a in rubric.axes if a.name not in axis_scores]
    below = [
        a.name for a in rubric.axes if a.name in axis_scores and axis_scores[a.name] < a.threshold
    ]
    credible = tuple(d for d in defects if d.confidence >= rubric.confidence_floor)

    if missing:
        return Verdict(False, scored, credible, min_axis,
                       reason=f"axes not scored: {', '.join(missing)}")
    if below:
        return Verdict(False, scored, credible, min_axis,
                       reason=f"axes below threshold: {', '.join(below)}")
    if credible:
        return Verdict(False, scored, credible, min_axis,
                       reason=f"{len(credible)} located defect(s) at/above confidence floor")
    return Verdict(True, scored, (), min_axis, reason="all axes clear threshold; no located defects")
