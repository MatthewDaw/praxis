from __future__ import annotations

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


def adjudicate_run(registry: Registry, *, run_id: str, verdict: str, status: str, reason: str) -> None:
    registry._adjudicate_run(run_id=run_id, verdict=verdict, status=status, reason=reason,
                             capability=_ADJUDICATOR_CAPABILITY)


def supersede_run(registry: Registry, *, run_id: str, reason: str) -> None:
    registry._supersede_run(run_id=run_id, reason=reason, capability=_ADJUDICATOR_CAPABILITY)
