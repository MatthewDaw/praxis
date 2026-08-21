from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from knowledge.ml_registry.storage.registry import Registry, _TRAINER_CAPABILITY


def complete_run(registry: Registry, *, run_id: str, metrics: Mapping[str, Any]) -> None:
    """Trainer-owned transition from running to complete; it cannot adjudicate."""
    registry._complete_run(run_id=run_id, metrics=metrics, capability=_TRAINER_CAPABILITY)
