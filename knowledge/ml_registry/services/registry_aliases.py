from __future__ import annotations

from collections.abc import Mapping

from knowledge.ml_registry.storage.registry import (
    Registry,
    _ADJUDICATOR_CAPABILITY,
    _CHAMPION_CAPABILITY,
)


def move_champion(registry: Registry, *, model_id: str, version: int, reason: str,
                  ratchet: bool = False) -> None:
    """Alias write seam owned by adjudication and ratchet services."""
    registry._set_alias(model_id=model_id, alias="champion", version=version,
                        set_by="ratchet" if ratchet else "adjudicate", reason=reason,
                        capability=_CHAMPION_CAPABILITY)


def adjudicate_run(
    registry: Registry, *, run_id: str, verdict: str, status: str, reason: str,
    adjudication_evidence: Mapping[str, object] | None = None,
) -> None:
    registry._adjudicate_run(run_id=run_id, verdict=verdict, status=status, reason=reason,
                             adjudication_evidence=adjudication_evidence,
                             capability=_ADJUDICATOR_CAPABILITY)


def adjudicate_invalidated_adoption(
    registry: Registry, *, run_id: str, verdict: str, reason: str,
    adjudication_evidence: Mapping[str, object] | None = None,
) -> None:
    """Record the corrected verdict after an invalid adoption has been rolled back."""
    registry._adjudicate_invalidated_adoption(
        run_id=run_id, verdict=verdict, reason=reason,
        adjudication_evidence=adjudication_evidence,
        capability=_ADJUDICATOR_CAPABILITY,
    )


def adopt_run_and_promote(registry: Registry, *, run_id: str, model_id: str, reason: str,
                          model_version: dict[str, object],
                          adjudication_evidence: Mapping[str, object] | None = None) -> bool:
    """Atomically adopt a completed run, create its immutable version, and move champion."""
    return registry._adopt_run_and_promote(
        run_id=run_id, model_id=model_id, reason=reason, model_version=model_version,
        adjudication_evidence=adjudication_evidence,
        capability=_ADJUDICATOR_CAPABILITY,
    )


def register_baseline_and_promote(registry: Registry, *, run_id: str, model_id: str, reason: str,
                                  model_version: dict[str, object]) -> bool:
    """Promote a re-measured champion as the campaign baseline with no improvement verdict.

    Use this after a vector or judge change. ``adopt_run_and_promote`` records a win;
    a re-baseline is not one (constitution X.3).
    """
    return registry._register_baseline_and_promote(
        run_id=run_id, model_id=model_id, reason=reason, model_version=model_version,
        capability=_ADJUDICATOR_CAPABILITY,
    )


def reclassify_adoption_as_baseline(registry: Registry, *, run_id: str, reason: str) -> None:
    """Withdraw an improvement verdict from a re-baseline that was filed as an adoption.

    Leaves the measurement, artifact and champion alias in place. Use when the ledger
    already recorded ``adopted`` for a judge-change re-measure; do not roll the alias
    back -- that would unseat the baseline the campaign is scoring against.
    """
    registry._reclassify_adoption_as_baseline(
        run_id=run_id, reason=reason, capability=_ADJUDICATOR_CAPABILITY,
    )


def supersede_run(registry: Registry, *, run_id: str, reason: str) -> None:
    registry._supersede_run(run_id=run_id, reason=reason, capability=_ADJUDICATOR_CAPABILITY)


def abandon_run(registry: Registry, *, run_id: str, reason: str) -> None:
    """Reclassify a rejected, parked, or superseded run through the adjudicator seam.

    Candidate code never writes a verdict. This is the canonical path for the case where
    the judge never fairly saw the hypothesis -- so a rejection it did not reach cannot
    later be cited as proof the approach fails. The idea goes back on the backlog:
    ``abandoned`` is not an answering verdict.
    """
    registry._abandon_run(run_id=run_id, reason=reason, capability=_ADJUDICATOR_CAPABILITY)


def record_ratchet_evidence(registry: Registry, payload: dict[str, object]) -> None:
    registry._record_ratchet_evidence(payload, capability=_ADJUDICATOR_CAPABILITY)


def invalidate_adoption(registry: Registry, payload: dict[str, object]) -> None:
    registry._invalidate_adoption(payload, capability=_ADJUDICATOR_CAPABILITY)
