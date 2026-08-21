from __future__ import annotations

from knowledge.ml_registry.storage.registry import (
    Registry,
    _CHAMPION_CAPABILITY,
    _PRODUCTION_CAPABILITY,
)


def move_champion(registry: Registry, *, model_id: str, version: int, reason: str,
                  ratchet: bool = False) -> None:
    """Alias write seam owned by adjudication and ratchet services."""
    registry._set_alias(model_id=model_id, alias="champion", version=version,
                        set_by="ratchet" if ratchet else "adjudicate", reason=reason,
                        capability=_CHAMPION_CAPABILITY)


def move_production(registry: Registry, *, model_id: str, version: int, reason: str) -> None:
    """Alias write seam called only by ``services.finalize``."""
    registry._set_alias(model_id=model_id, alias="production", version=version, set_by="finalize",
                        reason=reason, capability=_PRODUCTION_CAPABILITY)
