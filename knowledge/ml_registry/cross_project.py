"""Cross-project model linkage (R5).

A project ticket (a Praxis requirement, living in its own project's ``prd-<project>``
space) can reference a registry model by carrying the same ``meta.experiment_id`` the
model fact was registered with. This module resolves that link in both directions:

* :func:`model_to_projects` -- given a model's ``experiment_id``, every project name
  whose ticket references it.
* :func:`project_to_models` -- given a project name, every ``experiment_id`` its
  tickets reference that is actually a registered model.

:class:`TicketIndex` is a JSON-persisted stand-in for tickets pulled from many project
spaces, mirroring :class:`~knowledge.ml_registry.write_path.RegistrySpace`'s own
JSON-persisted-space pattern so the acceptance condition is provable without live
cross-project Praxis reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from knowledge.ml_registry.schema import MODEL, RegistryValidationError
from knowledge.ml_registry.write_path import RegistrySpace

EXPERIMENT_ID_FIELD = "experiment_id"


@dataclass
class TicketIndex:
    """Tickets from possibly many project spaces, each carrying its own ``meta``."""

    tickets: list[dict] = field(default_factory=list)

    def add(self, project: str, ticket_id: str, meta: dict[str, object]) -> None:
        self.tickets.append({"project": project, "ticket_id": ticket_id, "meta": dict(meta)})

    def to_json(self) -> dict[str, object]:
        return {"tickets": self.tickets}

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> TicketIndex:
        index = cls()
        index.tickets = list(raw.get("tickets") or [])
        return index

    @classmethod
    def load(cls, path: Path) -> TicketIndex:
        if not path.exists():
            return cls()
        return cls.from_json(json.loads(path.read_text()))

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_json(), indent=2))


def _registered_experiment_ids(registry: RegistrySpace) -> set[str]:
    return {
        str(m.meta[EXPERIMENT_ID_FIELD])
        for m in registry.list_facts(MODEL)
        if m.meta.get(EXPERIMENT_ID_FIELD)
    }


def model_to_projects(registry: RegistrySpace, index: TicketIndex, experiment_id: str) -> list[str]:
    """Every distinct project name whose ticket carries ``experiment_id``.

    Refuses (naming the field) an ``experiment_id`` no registered model carries --
    resolving a model means the model must exist, not just that some ticket used the
    string.
    """
    if experiment_id not in _registered_experiment_ids(registry):
        raise RegistryValidationError(
            f"no registered model carries experiment_id {experiment_id!r}",
            field=EXPERIMENT_ID_FIELD,
        )
    return sorted(
        {
            str(t["project"])
            for t in index.tickets
            if t.get("meta", {}).get(EXPERIMENT_ID_FIELD) == experiment_id
        }
    )


def project_to_models(registry: RegistrySpace, index: TicketIndex, project: str) -> list[str]:
    """Every distinct registered-model ``experiment_id`` that ``project``'s tickets reference."""
    registered = _registered_experiment_ids(registry)
    return sorted(
        {
            str(t["meta"][EXPERIMENT_ID_FIELD])
            for t in index.tickets
            if t.get("project") == project and t.get("meta", {}).get(EXPERIMENT_ID_FIELD) in registered
        }
    )
