"""Reproduce candidate baselines on our own data, so phase 1 outputs runs rather than a document.

The other half of :mod:`knowledge.ml_registry.survey`. That half gathers a vetted technique
pool from the literature; this half turns candidate approaches into MEASURED baselines on our
own split, because a survey whose output is a reading list has not settled the
between-families question the plan asks phase 1 to settle.

Three properties hold the harness honest.

*Scores come from the ledger, never from the reproducer.* Praxis reads a results ledger and
never runs the harness itself, so :func:`reproduce_baselines` takes a ``reproduce`` seam that
returns the COMMIT a candidate was reproduced at and reads that commit's score out of the
external ledger through the same
:func:`~knowledge.ml_registry.contracts.ledger_v2.read_ledger_compatibility` projection every
other adjudication path uses. No number a reproducer reports about its own run can enter the
ranking, and an unfair row (``aborted`` / ``errored`` / ``budget_exhausted``) is not a score.

*The incumbent is a candidate, not a spectator.* A warm start passes ``incumbent=``; it is
stamped :data:`INCUMBENT_RUNG` and reproduced, scored and ranked by the identical path as
every challenger, so "is there something better than what we already have" is answered by the
same comparison as everything else. Rung zero is the incumbent's alone: an ordinary candidate
claiming it is refused rather than quietly promoted into the warm-start slot.

*A baseline that will not reproduce is recorded, not dropped.* Reproduction fails in three
distinguishable ways — the attempt raised, its commit has no scored row, or the row it has is
unfair — and each one lands in :meth:`BaselineSuite.ranking` carrying its reason. Dropping
them would leave a ranking that looks complete while quietly measuring the survivors only,
which is how a family gets ruled out by a build error rather than by a result.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from knowledge.ml_registry.contracts.ledger_v2 import (
    FAIR_LEDGER_STATUSES,
    LedgerCompatibilityProjection,
    read_ledger_compatibility,
)

#: The warm start's slot: what we already run, entered as an ordinary phase-1 candidate.
INCUMBENT_RUNG = 0
#: Rung 4 is novel code — the bottom of the plan's reuse ladder, so no rung sorts below it.
NOVEL_CODE_RUNG = 4


class BaselineReproductionError(ValueError):
    """A candidate baseline is not admissible to the campaign's reproduction harness."""


@dataclass(frozen=True)
class BaselineCandidate:
    """One approach to reproduce, at a stated reuse rung, in a stated family."""

    id: str
    family: str
    source_url: str
    rung: int

    def __post_init__(self) -> None:
        for field in ("id", "family", "source_url"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise BaselineReproductionError(f"baseline candidate requires non-empty {field}")
        if not INCUMBENT_RUNG <= self.rung <= NOVEL_CODE_RUNG:
            raise BaselineReproductionError(
                f"baseline {self.id!r} declares rung {self.rung}, outside "
                f"{INCUMBENT_RUNG}-{NOVEL_CODE_RUNG}",
            )


@dataclass(frozen=True)
class BaselineResult:
    """A candidate's outcome: a ledger-read score, or the reason it could not be reproduced."""

    candidate: BaselineCandidate
    commit: str | None = None
    score: float | None = None
    unreproduced_reason: str | None = None

    @property
    def reproduced(self) -> bool:
        return self.score is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.candidate.id,
            "family": self.candidate.family,
            "rung": self.candidate.rung,
            "source_url": self.candidate.source_url,
            "commit": self.commit,
            "status": "measured" if self.reproduced else "unreproduced",
            "score": self.score,
            "unreproduced_reason": self.unreproduced_reason,
        }


@dataclass(frozen=True)
class BaselineSuite:
    """A campaign's phase-1 baselines, complete only once enough families actually measured."""

    campaign_id: str
    results: tuple[BaselineResult, ...]
    minimum_families: int = 3

    @property
    def measured(self) -> tuple[BaselineResult, ...]:
        return tuple(entry for entry in self.results if entry.reproduced)

    @property
    def unreproduced(self) -> tuple[BaselineResult, ...]:
        return tuple(entry for entry in self.results if not entry.reproduced)

    @property
    def families(self) -> frozenset[str]:
        return frozenset(entry.candidate.family for entry in self.measured)

    @property
    def complete(self) -> bool:
        """Three baselines from ONE family do not settle a between-families question."""
        return len(self.families) >= self.minimum_families

    def ranking(self) -> tuple[BaselineResult, ...]:
        """Best score first, ties down the ladder; unreproduced candidates keep a place at the tail."""
        return tuple(sorted(self.measured, key=_measured_order)) + tuple(
            sorted(self.unreproduced, key=_candidate_order),
        )

    def to_dict(self) -> dict[str, object]:
        if self.complete:
            status = "complete"
        elif self.unreproduced and not self.measured:
            status = "failed"
        else:
            status = "incomplete"
        return {
            "campaign_id": self.campaign_id,
            "status": status,
            "minimum_families": self.minimum_families,
            "measured_families": sorted(self.families),
            "ranking": [entry.to_dict() for entry in self.ranking()],
        }


def _candidate_order(entry: BaselineResult) -> tuple[int, str]:
    return (entry.candidate.rung, entry.candidate.id)


def _measured_order(entry: BaselineResult) -> tuple[float, int, str]:
    return (-(entry.score or 0.0), *_candidate_order(entry))


BaselineReproducer = Callable[[BaselineCandidate], str]


def reproduce_baselines(
    campaign_id: str,
    candidates: Sequence[BaselineCandidate],
    reproduce: BaselineReproducer,
    ledger_path: Path,
    *,
    incumbent: BaselineCandidate | None = None,
    minimum_families: int = 3,
) -> BaselineSuite:
    """Reproduce every candidate — the incumbent first, at rung zero — and rank what measured."""
    if not campaign_id.strip():
        raise BaselineReproductionError("campaign_id must not be empty")
    for candidate in candidates:
        if candidate.rung == INCUMBENT_RUNG:
            raise BaselineReproductionError(
                f"baseline {candidate.id!r} claims rung {INCUMBENT_RUNG}, which is the "
                f"incumbent's alone",
            )
    entrants = list(candidates)
    if incumbent is not None:
        entrants.insert(0, replace(incumbent, rung=INCUMBENT_RUNG))
    ledger = read_ledger_compatibility(ledger_path)
    return BaselineSuite(
        campaign_id.strip(),
        tuple(_reproduce_one(candidate, reproduce, ledger) for candidate in entrants),
        minimum_families,
    )


def _reproduce_one(
    candidate: BaselineCandidate,
    reproduce: BaselineReproducer,
    ledger: LedgerCompatibilityProjection,
) -> BaselineResult:
    try:
        commit = reproduce(candidate)
    except Exception as exc:  # however a reproduction fails, that failure is data, not a crash
        return BaselineResult(candidate, unreproduced_reason=f"{type(exc).__name__}: {exc}")
    measurement = ledger.measurements.get(commit)
    if measurement is None:
        return BaselineResult(
            candidate, commit, unreproduced_reason=f"no scored row for commit {commit!r}",
        )
    if measurement.status.lower() not in FAIR_LEDGER_STATUSES:
        return BaselineResult(
            candidate, commit,
            unreproduced_reason=f"commit {commit!r} scored {measurement.status}, not a fair run",
        )
    return BaselineResult(candidate, commit, score=measurement.metric_value)
