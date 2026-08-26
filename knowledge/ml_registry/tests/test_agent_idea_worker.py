from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge.ml_registry.runtime.agent_idea_worker import (
    AgentIdeaWorker,
    AgentIdeaWorkerError,
    IdeaContract,
)
from knowledge.ml_registry.write_path import Fact


def _idea() -> Fact:
    return Fact("idea-a", "idea", {
        "model_id": "model-a", "origin": "seeded", "axis": "data",
        "description": "mine hard negatives", "depends_on": ["idea-prev"],
    }, frozenset())


def test_contract_preserves_prose_and_never_infers_an_arm() -> None:
    contract = IdeaContract.from_fact(_idea(), stage="representation")
    assert contract.to_mapping() == {
        "schema_version": 1, "idea_id": "idea-a", "description": "mine hard negatives",
        "axis": "data", "stage": "representation", "depends_on": ["idea-prev"],
    }


def test_worker_requires_recipe_commit_and_evidence_before_returning(tmp_path: Path, monkeypatch) -> None:
    worker = AgentIdeaWorker(command=["worker"], working_directory=tmp_path)
    monkeypatch.setattr(worker, "_assert_isolated_worktree", lambda: None)
    def run(*_args, **kwargs):
        handoff = Path(kwargs["env"]["AF_ML_IDEA_HANDOFF"])
        handoff.write_text(json.dumps({"schema_version": 1, "idea_id": "idea-a", "recipe": {"arm": "one"}, "commit": "abc", "evidence": {"test": "ok"}}))
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr("knowledge.ml_registry.runtime.agent_idea_worker.subprocess.run", run)
    handoff = worker.prepare(contract=IdeaContract.from_fact(_idea(), stage="representation"), handoff_path=tmp_path / "handoff.json")
    assert handoff.commit == "abc"
    assert json.loads((tmp_path / "idea-contract.json").read_text())["description"] == "mine hard negatives"


def test_worker_refuses_success_exit_without_machine_readable_handoff(tmp_path: Path, monkeypatch) -> None:
    worker = AgentIdeaWorker(command=["worker"], working_directory=tmp_path)
    monkeypatch.setattr(worker, "_assert_isolated_worktree", lambda: None)
    monkeypatch.setattr("knowledge.ml_registry.runtime.agent_idea_worker.subprocess.run", lambda *_a, **_k: type("Result", (), {"returncode": 0})())
    with pytest.raises(AgentIdeaWorkerError, match="did not write readable handoff"):
        worker.prepare(contract=IdeaContract.from_fact(_idea(), stage="representation"), handoff_path=tmp_path / "handoff.json")
