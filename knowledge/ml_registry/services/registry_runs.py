from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from knowledge.ml_registry.storage.registry import (
    Registry,
    _ADJUDICATOR_CAPABILITY,
    _TRAINER_CAPABILITY,
)


def complete_run(registry: Registry, *, run_id: str, metrics: Mapping[str, Any]) -> None:
    """Trainer-owned transition from running to complete; it cannot adjudicate."""
    registry._complete_run(run_id=run_id, metrics=metrics, capability=_TRAINER_CAPABILITY)


def supersede_run(registry: Registry, *, run_id: str, reason: str) -> None:
    """Adjudicator-owned interruption of an in-flight canonical run."""
    registry._supersede_run(run_id=run_id, reason=reason, capability=_ADJUDICATOR_CAPABILITY)
