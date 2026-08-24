from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import shutil

from knowledge.ml_registry.storage import Registry, RegistryError


class CampaignOperationsError(RegistryError):
    """A destructive campaign operation did not satisfy its safety preconditions."""


class CampaignOperations:
    """Runner-owned cleanup operations over disposable campaign state."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.campaign_state = registry.root.resolve()

    def delete_campaign_state(self, *, durable_trace_roots: Sequence[str | Path]) -> None:
        """Delete disposable state only after a live landing and externalized traces."""
        landed: dict[str, bool] = {}
        for event in self.registry.list_events():
            if event.event_type == "campaign_landed":
                landed[str(event.payload["model_id"])] = True
            elif event.event_type == "campaign_unpromoted":
                landed[str(event.payload["model_id"])] = False
        if not any(landed.values()):
            raise CampaignOperationsError("campaign_state deletion requires a landing commit")
        roots = tuple(Path(root).resolve() for root in durable_trace_roots)
        if not roots:
            raise CampaignOperationsError("campaign_state deletion requires durable trace roots")
        for root in roots:
            if root == self.campaign_state or self.campaign_state in root.parents:
                raise CampaignOperationsError(
                    "dead-end registry and rejected-arm diffs must live outside campaign_state"
                )
            if not root.exists():
                raise CampaignOperationsError(f"durable trace root does not exist: {root}")
        shutil.rmtree(self.campaign_state)
