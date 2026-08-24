"""Phase 2 SELECT: the reuse-rung ladder, the N-way comparison, and the tie policy (R3a).

Praxis could only ever compare ONE candidate against the standing champion -- an arm was
adjudicated pairwise and its approach was recorded in ``runs.family``, an opaque TEXT
column nothing could rank. Phase 2 needs the other shape: several drafted approaches
measured on the same split, compared against EACH OTHER, and one of them chosen. Two
things have to exist for that choice to be more than a leaderboard read.

**The ladder is a field, not prose.** :data:`REUSE_RUNGS` is the closed vocabulary a
candidate declares in :attr:`Candidate.rung` -- 0 the incumbent, 1 existing weights behind
an adapter, 2 an existing implementation fine-tuned on our data, 3 a build from a published
description, 4 novel code. A rung outside it is refused by name, which is the whole
difference between a first-class field and the free-text column it replaces.

**The rope decides what "better" means.** Choosing the maximum of N noisy measurements
overstates the winner: with four candidates and an unstable metric, "best" can mean
"luckiest". So everything within one rope of the top is TIED, and ties resolve DOWN the
ladder -- the lowest rung wins, least code. Without that rule the shiniest result wins and
reuse is advisory.

The rope is supplied by the caller rather than computed here, because it is a property of
the CAMPAIGN, not of this comparison: it comes from
:func:`knowledge.ml_registry.policy_gate.compute_campaign_rope`, a bootstrap of the metric
over the scoring corpus's own ``split_unit``, recomputed at registration. That is what
makes a cold start ordinary -- no champion has to repeat a run for the tie test to be
defined -- and what keeps a deterministic arm (sigma 0) from refusing: nothing here divides
by a candidate's own spread.

A candidate's ``sigma`` is not ignored either. Two measurements are differenced to compare
them, and that difference carries both their noise, so the band between a pair is the
campaign rope OR the combined spread of the two numbers, whichever is wider. A gap smaller
than the noise in the numbers being differenced is not evidence, whatever the corpus rope
says.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field as dataclass_field
import math

from knowledge.ml_registry.schema import RegistryValidationError

#: The reuse ladder. A LOWER rung is less code and more reuse, and is what a tie resolves
#: to. Rung 0 is the incumbent, which enters SURVEY as an ordinary candidate so "is there
#: something better than what we already have" is answered by this same comparison.
REUSE_RUNGS: dict[int, str] = {
    0: "the incumbent, entered as an ordinary candidate",
    1: "existing weights work as-is, adapter only",
    2: "existing implementation, fine-tuned on our data",
    3: "built from a published description that fully specifies it",
    4: "novel code",
}

#: The meta key a candidate arm declares its rung under.
RUNG_FIELD = "rung"

#: The winner was the top of the leaderboard, beyond the rope of everything else.
RESOLVED_BY_MARGIN = "margin"
#: The winner was NOT the top of the leaderboard: it tied with it and sits lower on the ladder.
RESOLVED_BY_RUNG = "rung"

DIRECTIONS = ("maximize", "minimize")


@dataclass(frozen=True)
class Candidate:
    """One measured arm: its metric value on the search split, the spread of that value,
    and the rung of the ladder it was built from."""

    candidate_id: str
    rung: int
    value: float
    sigma: float = 0.0
    #: The approach family, used only to keep at most one runner-up alive to convergence:
    #: the winner at low tuning is not guaranteed to be the winner at high tuning, so ONE
    #: alternative survives, and only when it is a genuinely different approach.
    family: str = ""


@dataclass(frozen=True)
class Selection:
    """The outcome of one N-way comparison."""

    winner: str
    #: Every candidate within one band of the top, best-first. Always contains the winner.
    tied: tuple[str, ...]
    #: Every candidate, best-first; ties inside the ranking are ordered down the ladder.
    ranked: tuple[str, ...]
    rope: float
    resolved_by: str
    #: The best-ranked survivor from a different family, or None when every other candidate
    #: shares the winner's family.
    runner_up: str | None = None
    #: The tied candidates' rungs, for the record a campaign keeps of why it chose.
    rungs: dict[str, int] = dataclass_field(default_factory=dict)


def validate_rung(value: object, *, candidate_id: str = "") -> int:
    """Return ``value`` as a rung on :data:`REUSE_RUNGS`, refusing anything else by name."""
    where = f" for candidate {candidate_id!r}" if candidate_id else ""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RegistryValidationError(
            f"reuse rung{where} must be an integer on the ladder {sorted(REUSE_RUNGS)}, "
            f"got {value!r}; declare which rung the candidate was built from",
            field=RUNG_FIELD,
        )
    if value not in REUSE_RUNGS:
        raise RegistryValidationError(
            f"reuse rung {value!r}{where} is not on the ladder {sorted(REUSE_RUNGS)}; "
            f"declare one of {', '.join(f'{k} ({v})' for k, v in REUSE_RUNGS.items())}",
            field=RUNG_FIELD,
        )
    return value


def _finite(value: object, field: str, where: str = "") -> float:
    """``value`` as a finite number, refused BY NAME otherwise. ``where`` names the
    candidate the value came from, and is empty for a campaign-level input."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RegistryValidationError(
            f"{field}{where} must be a finite number, got {value!r}", field=field
        )
    return float(value)


def _non_negative(value: object, field: str, where: str = "") -> float:
    """``value`` as a finite number at least 0 -- the one rule both a candidate's own spread
    and the campaign rope are held to, since neither can be a negative width."""
    number = _finite(value, field, where)
    if number < 0:
        raise RegistryValidationError(
            f"{field}{where} must be at least 0, got {value!r}", field=field
        )
    return number


def _loss(direction: str, best: float, value: float) -> float:
    """How far ``value`` falls SHORT of ``best`` in the improving direction; never negative
    when ``best`` really is the best."""
    return best - value if direction == "maximize" else value - best


def select(candidates: Sequence[Candidate], *, direction: str, rope: float) -> Selection:
    """Choose among N candidates measured on the same split under the same protocol.

    Everything within one band of the top is tied, and the tie resolves down the ladder:
    the lowest rung wins, and a rung tie is settled by margin and then by id so the same
    fixture always chooses the same arm.
    """
    if direction not in DIRECTIONS:
        raise RegistryValidationError(
            f"direction must be one of {DIRECTIONS}, got {direction!r}", field="direction"
        )
    rope = _non_negative(rope, "rope")
    if not candidates:
        raise RegistryValidationError(
            "selection needs at least one measured candidate; a stage with no arms is not a "
            "tie, it never ran",
            field="candidates",
        )

    checked: list[Candidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate_id = str(candidate.candidate_id)
        if not candidate_id:
            raise RegistryValidationError("every candidate must carry an id", field="candidate_id")
        if candidate_id in seen:
            raise RegistryValidationError(
                f"candidate id {candidate_id!r} appears twice; each arm is compared once",
                field="candidate_id",
            )
        seen.add(candidate_id)
        where = f" for candidate {candidate_id!r}"
        checked.append(
            Candidate(
                candidate_id=candidate_id,
                rung=validate_rung(candidate.rung, candidate_id=candidate_id),
                value=_finite(candidate.value, "value", where),
                sigma=_non_negative(candidate.sigma, "sigma", where),
                family=candidate.family,
            )
        )

    best = max(c.value for c in checked) if direction == "maximize" else min(c.value for c in checked)
    ordered = sorted(checked, key=lambda c: (_loss(direction, best, c.value), c.rung, c.candidate_id))
    top = ordered[0]

    # A difference of two measurements carries both their noise, so a gap inside the
    # combined spread is not evidence even when the campaign rope is tighter than it.
    tied = [
        c for c in ordered
        if _loss(direction, top.value, c.value) <= max(rope, math.hypot(top.sigma, c.sigma))
    ]
    winner = min(tied, key=lambda c: (c.rung, _loss(direction, top.value, c.value), c.candidate_id))
    runner_up = next((c.candidate_id for c in ordered
                      if c.candidate_id != winner.candidate_id and c.family != winner.family), None)

    return Selection(
        winner=winner.candidate_id,
        tied=tuple(c.candidate_id for c in tied),
        ranked=tuple(c.candidate_id for c in ordered),
        rope=rope,
        resolved_by=RESOLVED_BY_MARGIN if winner is top else RESOLVED_BY_RUNG,
        runner_up=runner_up,
        rungs={c.candidate_id: c.rung for c in tied},
    )
