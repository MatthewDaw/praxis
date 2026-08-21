from __future__ import annotations

from knowledge.ml_registry.storage.registry import Registry, _PRODUCTION_CAPABILITY


class RegistryFinalizeService:
    """The sole §5.11 writer of the ``production`` alias."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def move_production(self, *, model_id: str, version: int, reason: str) -> None:
        effective = self.registry.effective_model_version(model_id, version)
        if effective["effective_status"] != "active":
            raise ValueError("finalize refuses an incompatible model version")
        self.registry._set_alias(
            model_id=model_id, alias="production", version=version, set_by="finalize",
            reason=reason, capability=_PRODUCTION_CAPABILITY,
        )
