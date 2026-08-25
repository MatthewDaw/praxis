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


def adopt_run_and_promote(registry: Registry, *, run_id: str, model_id: str, reason: str,
                          model_version: dict[str, object],
                          adjudication_evidence: Mapping[str, object] | None = None) -> bool:
    """Atomically adopt a completed run, create its immutable version, and move champion."""
    return registry._adopt_run_and_promote(
        run_id=run_id, model_id=model_id, reason=reason, model_version=model_version,
        adjudication_evidence=adjudication_evidence,
        capability=_ADJUDICATOR_CAPABILITY,
    )


def supersede_run(registry: Registry, *, run_id: str, reason: str) -> None:
    registry._supersede_run(run_id=run_id, reason=reason, capability=_ADJUDICATOR_CAPABILITY)


def record_ratchet_evidence(registry: Registry, payload: dict[str, object]) -> None:
    registry._record_ratchet_evidence(payload, capability=_ADJUDICATOR_CAPABILITY)


def invalidate_adoption(registry: Registry, payload: dict[str, object]) -> None:
    registry._invalidate_adoption(payload, capability=_ADJUDICATOR_CAPABILITY)
