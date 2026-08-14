"""R5 acceptance: cross-project model linkage via meta.experiment_id."""

from __future__ import annotations

import pytest

from knowledge.ml_registry.cross_project import TicketIndex, model_to_projects, project_to_models
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.write_path import RegistrySpace, register_model

MODEL_META = {
    "metric": "val_bpb",
    "direction": "minimize",
    "win_condition": "beats baseline by noise_floor",
    "baseline": "commit-abc123",
    "noise_floor": 0.01,
    "baseline_throughput": 1200,
    "diff_size_limit": 800,
    "experiment_id": "exp-42",
}


def test_model_to_projects_and_project_to_models_resolve_from_either_side():
    registry = RegistrySpace()
    register_model(registry, MODEL_META)

    index = TicketIndex()
    index.add("project-alpha", "ticket-1", {"experiment_id": "exp-42"})
    index.add("project-beta", "ticket-7", {"experiment_id": "exp-42"})

    assert model_to_projects(registry, index, "exp-42") == ["project-alpha", "project-beta"]
    assert project_to_models(registry, index, "project-alpha") == ["exp-42"]
    assert project_to_models(registry, index, "project-beta") == ["exp-42"]


def test_model_to_projects_refuses_an_unregistered_experiment_id():
    registry = RegistrySpace()
    index = TicketIndex()
    index.add("project-alpha", "ticket-1", {"experiment_id": "exp-nope"})

    with pytest.raises(RegistryValidationError) as excinfo:
        model_to_projects(registry, index, "exp-nope")
    assert excinfo.value.field == "experiment_id"


def test_project_to_models_ignores_tickets_referencing_no_registered_model():
    registry = RegistrySpace()
    register_model(registry, MODEL_META)

    index = TicketIndex()
    index.add("project-alpha", "ticket-1", {"experiment_id": "exp-unregistered"})

    assert project_to_models(registry, index, "project-alpha") == []
