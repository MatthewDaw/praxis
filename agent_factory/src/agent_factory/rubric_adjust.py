"""U7 (Phase 2): auto-adjust rubrics from accumulated review signal — as PROPOSALS, human-gated.

The factory's own graded verdicts are a calibration signal. This module reads that signal and
proposes edits to the seeded library — it never mutates anything on its own, and the apply path is
inert without explicit confirmation. Two hard invariants from the plan:

* **Bias toward loosening/clarifying, never tightening, when a check is not converging.** A check
  that keeps tripping the U6 non-convergence/cap guard is miscalibrated — its bar is too high or its
  guidance too vague. The correct response is to lower it or clarify it, not to demand more.
* **Never adjust a check that is in-flight.** A rubric pinned to an in-progress ticket is frozen on
  that ticket (U6), so editing the file cannot corrupt it — but we still DEFER the proposal until
  the ticket releases, so a human never reasons about a moving library.

This module is PURE (aggregation + proposal + text-apply); the Praxis reads that gather
observations and the in-flight set live in ``tools/rubric_adjust_review.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .rubric import Defect, Rubric

DEFAULT_MIN_OBS = 3          # need this many observations before proposing anything
DEFAULT_STEP = 0.05          # threshold nudge per proposal
DEFAULT_NONCONV_FRAC = 0.5   # >= this fraction non-converged => the check is miscalibrated-high
DEFAULT_RECUR_MIN = 2        # a defect theme recurring on >= this many tickets is systemic

LOOSEN = "loosen_axis"
STRENGTHEN = "strengthen_axis"
CLARIFY = "clarify"
ADD_AXIS = "add_axis"


@dataclass(frozen=True)
class GradedObservation:
    """One ticket's outcome for one seeded check: its axis scores, credible defects, and whether it
    CONVERGED (False if it tripped the U6 cap/non-convergence block)."""

    check_id: str
    converged: bool
    axis_scores: dict[str, float] = field(default_factory=dict)
    defects: tuple[Defect, ...] = ()


@dataclass(frozen=True)
class Signal:
    check_id: str
    n_obs: int
    n_nonconverged: int
    axis_means: dict[str, float]
    defect_themes: dict[str, int]  # normalized problem -> distinct-ticket count

    @property
    def nonconverged_frac(self) -> float:
        return self.n_nonconverged / self.n_obs if self.n_obs else 0.0


@dataclass(frozen=True)
class Proposal:
    check_id: str
    kind: str
    rationale: str
    axis: str = ""
    from_value: float | None = None
    to_value: float | None = None

    @property
    def is_numeric(self) -> bool:
        return self.kind in (LOOSEN, STRENGTHEN)


def _theme(problem: str) -> str:
    return " ".join(str(problem).strip().casefold().split())[:60]


def observations_from_tickets(tickets: list[dict]) -> tuple[list[GradedObservation], set[str]]:
    """PURE: fold ticket fact dicts into ``(observations, in_flight_check_ids)``.

    Reads the documented ticket meta contract (``build_state`` / ``pinned_checks`` with graded
    entries carrying ``source_check_id`` + cached ``verdict``; see factory-state-contract.md). A
    ticket counts as non-converged for its checks iff ``build_state == "blocked"`` (the U6
    escalation terminus); a check on an ``in_progress`` ticket is in-flight and its proposals are
    deferred at apply time. Kept here (not in the CLI) so it is importable and unit-tested."""
    observations: list[GradedObservation] = []
    in_flight: set[str] = set()
    for t in tickets:
        meta = t.get("meta") or {}
        build_state = str(meta.get("build_state") or "")
        converged = build_state != "blocked"
        for entry in (meta.get("pinned_checks") or []):
            if str(entry.get("kind") or "") != "graded":
                continue
            src = str(entry.get("source_check_id") or "").strip()
            if not src:
                continue
            verdict = entry.get("verdict") or {}
            defects = tuple(
                Defect(problem=str(d.get("problem") or ""), remedy=str(d.get("remedy") or ""),
                       confidence=int(d.get("confidence") or 0), file=str(d.get("file") or ""),
                       line=d.get("line"))
                for d in (verdict.get("defects") or [])
            )
            observations.append(GradedObservation(
                check_id=src, converged=converged,
                axis_scores=dict(verdict.get("axis_scores") or {}), defects=defects))
            if build_state == "in_progress":
                in_flight.add(src)
    return observations, in_flight


def aggregate(observations: list[GradedObservation], floor: int = 5) -> dict[str, Signal]:
    """Fold per-ticket observations into one :class:`Signal` per check. Defect themes count DISTINCT
    observations (a theme that recurs across tickets, not one noisy ticket repeating itself)."""
    buckets: dict[str, list[GradedObservation]] = {}
    for o in observations:
        buckets.setdefault(o.check_id, []).append(o)

    signals: dict[str, Signal] = {}
    for check_id, obs in buckets.items():
        axis_totals: dict[str, list[float]] = {}
        theme_counts: dict[str, int] = {}
        for o in obs:
            for name, score in o.axis_scores.items():
                axis_totals.setdefault(name, []).append(score)
            seen_here = {_theme(d.problem) for d in o.defects if d.confidence >= floor and d.problem}
            for t in seen_here:
                theme_counts[t] = theme_counts.get(t, 0) + 1
        signals[check_id] = Signal(
            check_id=check_id,
            n_obs=len(obs),
            n_nonconverged=sum(1 for o in obs if not o.converged),
            axis_means={n: sum(v) / len(v) for n, v in axis_totals.items()},
            defect_themes=theme_counts,
        )
    return signals


def propose(rubric: Rubric, signal: Signal, *, min_obs: int = DEFAULT_MIN_OBS,
            step: float = DEFAULT_STEP, nonconv_frac: float = DEFAULT_NONCONV_FRAC,
            recur_min: int = DEFAULT_RECUR_MIN) -> list[Proposal]:
    """Turn one check's signal into adjustment proposals. Empty when the check is well-calibrated or
    there isn't enough signal yet."""
    if signal.n_obs < min_obs:
        return []
    by_name = {a.name: a for a in rubric.axes}

    # NON-CONVERGENCE DOMINATES -> loosen the binding (lowest-mean) axis + flag the guidance. Never
    # strengthen here: a check that can't be satisfied should be made easier or clearer, not harder.
    if signal.nonconverged_frac >= nonconv_frac:
        proposals: list[Proposal] = []
        scored = {n: m for n, m in signal.axis_means.items() if n in by_name}
        if scored:
            binding = min(scored, key=scored.get)
            cur = by_name[binding].threshold
            proposals.append(Proposal(
                check_id=signal.check_id, kind=LOOSEN, axis=binding,
                from_value=cur, to_value=round(max(0.0, cur - step), 4),
                rationale=(f"{signal.n_nonconverged}/{signal.n_obs} tickets failed to converge; "
                           f"axis {binding!r} is the binding constraint (mean {scored[binding]:.2f})")))
        proposals.append(Proposal(
            check_id=signal.check_id, kind=CLARIFY,
            rationale=("repeated non-convergence suggests the criterion/judge_prompt is too vague — "
                       "clarify what a pass looks like (human authors the wording)")))
        return proposals

    # CONVERGING but a defect theme keeps slipping through -> the coverage is too lenient. Strengthen
    # the highest-scoring axis (defects coexist with a high score => that bar is too low), or, if no
    # axis fits, suggest a human-authored new axis for the theme.
    recurring = {t: c for t, c in signal.defect_themes.items() if c >= recur_min}
    if recurring:
        top_theme = max(recurring, key=recurring.get)
        scored = {n: m for n, m in signal.axis_means.items() if n in by_name}
        if scored:
            lenient = max(scored, key=scored.get)
            cur = by_name[lenient].threshold
            return [Proposal(
                check_id=signal.check_id, kind=STRENGTHEN, axis=lenient,
                from_value=cur, to_value=round(min(1.0, cur + step), 4),
                rationale=(f"defect theme {top_theme!r} recurred on {recurring[top_theme]} tickets "
                           f"while axis {lenient!r} scored high (mean {scored[lenient]:.2f}) — raise its bar"))]
        return [Proposal(
            check_id=signal.check_id, kind=ADD_AXIS,
            rationale=(f"defect theme {top_theme!r} recurred on {recurring[top_theme]} tickets with no "
                       f"axis covering it — human authors a new axis"))]
    return []


def propose_all(rubrics: dict[str, Rubric], signals: dict[str, Signal], **kw) -> list[Proposal]:
    """Proposals across every check that has both a rubric and a signal."""
    out: list[Proposal] = []
    for check_id, sig in signals.items():
        if check_id in rubrics:
            out.extend(propose(rubrics[check_id], sig, **kw))
    return out


# --------------------------------------------------------------------------- apply (human-gated)

@dataclass(frozen=True)
class ApplyResult:
    text: str
    applied: tuple[Proposal, ...]
    skipped: tuple[tuple[Proposal, str], ...]  # (proposal, reason)


def _set_axis_threshold(toml_text: str, check_id: str, axis: str, value: float) -> str | None:
    """Scoped text edit: set ``threshold`` of ``axis`` within the ``[[check]]`` block whose
    ``check_id`` matches. Returns the new text, or ``None`` if the target block/axis wasn't found.
    Operates on text (not a TOML round-trip) so the curated file's comments and layout survive."""
    blocks = re.split(r"(?m)^(?=\[\[check\]\])", toml_text)
    for i, block in enumerate(blocks):
        if not re.search(r"(?m)^\[\[check\]\]", block):
            continue
        if not re.search(rf'(?m)^\s*check_id\s*=\s*"{re.escape(check_id)}"\s*$', block):
            continue
        axis_parts = re.split(r"(?m)^(?=\s*\[\[check\.axes\]\])", block)
        changed = False
        for j, part in enumerate(axis_parts):
            if re.search(r"(?m)^\s*\[\[check\.axes\]\]", part) and \
                    re.search(rf'(?m)^\s*name\s*=\s*"{re.escape(axis)}"\s*$', part):
                new_part, n = re.subn(r"(?m)^(\s*threshold\s*=\s*).+$",
                                      rf"\g<1>{value}", part, count=1)
                if n:
                    axis_parts[j] = new_part
                    changed = True
        if changed:
            blocks[i] = "".join(axis_parts)
            return "".join(blocks)
    return None


def apply_proposals(toml_text: str, proposals: list[Proposal], *,
                    in_flight: set[str], confirm: bool) -> ApplyResult:
    """Apply NUMERIC proposals to the seeded-library text. Inert unless ``confirm`` is True (no
    silent mutation). Skips any proposal whose check is in-flight (deferred) and any non-numeric
    proposal (clarify/add_axis need human authorship)."""
    if not confirm:
        return ApplyResult(toml_text, (), tuple((p, "unconfirmed") for p in proposals))

    text = toml_text
    applied: list[Proposal] = []
    skipped: list[tuple[Proposal, str]] = []
    for p in proposals:
        if p.check_id in in_flight:
            skipped.append((p, "in-flight: deferred until the ticket releases"))
            continue
        if not p.is_numeric:
            skipped.append((p, f"{p.kind}: needs human authorship"))
            continue
        new_text = _set_axis_threshold(text, p.check_id, p.axis, p.to_value)
        if new_text is None:
            skipped.append((p, "target check/axis not found in library"))
            continue
        text = new_text
        applied.append(p)
    return ApplyResult(text, tuple(applied), tuple(skipped))
