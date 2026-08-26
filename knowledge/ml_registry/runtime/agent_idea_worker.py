"""Typed handoff between a claimed registry IDEA and an authoring agent."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Mapping, Sequence

from knowledge.ml_registry.schema import IDEA
from knowledge.ml_registry.write_path import Fact


class AgentIdeaWorkerError(ValueError):
    """The agent failed to produce an auditable trial recipe."""


@dataclass(frozen=True)
class IdeaContract:
    """The only typed translation of a prose IDEA before project dispatch."""

    schema_version: int
    idea_id: str
    description: str
    axis: str
    stage: str
    depends_on: tuple[str, ...]

    @classmethod
    def from_fact(cls, idea: Fact, *, stage: str) -> "IdeaContract":
        if idea.category != IDEA:
            raise AgentIdeaWorkerError(f"{idea.id!r} is not an IDEA fact")
        description = idea.meta.get("description")
        axis = idea.meta.get("axis")
        if not isinstance(description, str) or not description.strip():
            raise AgentIdeaWorkerError(f"IDEA {idea.id!r} has no description")
        if not isinstance(axis, str) or not axis.strip():
            raise AgentIdeaWorkerError(f"IDEA {idea.id!r} has no axis")
        dependencies = idea.meta.get("depends_on", ())
        if not isinstance(dependencies, (list, tuple)) or not all(
            isinstance(item, str) and item for item in dependencies
        ):
            raise AgentIdeaWorkerError(f"IDEA {idea.id!r} has invalid depends_on")
        if not isinstance(stage, str) or not stage:
            raise AgentIdeaWorkerError(f"IDEA {idea.id!r} has no selected stage")
        return cls(1, idea.id, description, axis, stage, tuple(dependencies))

    def to_mapping(self) -> dict[str, object]:
        result = asdict(self)
        result["depends_on"] = list(self.depends_on)
        return result


@dataclass(frozen=True)
class AgentRecipeHandoff:
    """Evidence an agent must emit before its project arm may be dispatched."""

    schema_version: int
    idea_id: str
    recipe: Mapping[str, object]
    commit: str
    evidence: Mapping[str, object]

    @classmethod
    def load(cls, path: Path, *, contract: IdeaContract) -> "AgentRecipeHandoff":
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentIdeaWorkerError(f"agent did not write readable handoff {path}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise AgentIdeaWorkerError("agent handoff schema_version must be 1")
        if raw.get("idea_id") != contract.idea_id:
            raise AgentIdeaWorkerError("agent handoff idea_id does not match claimed IDEA")
        recipe, commit, evidence = raw.get("recipe"), raw.get("commit"), raw.get("evidence")
        if not isinstance(recipe, dict) or not recipe:
            raise AgentIdeaWorkerError("agent handoff requires a non-empty recipe object")
        if not isinstance(commit, str) or not commit.strip():
            raise AgentIdeaWorkerError("agent handoff requires a commit")
        if not isinstance(evidence, dict) or not evidence:
            raise AgentIdeaWorkerError("agent handoff requires an evidence object")
        return cls(1, contract.idea_id, recipe, commit, evidence)


class AgentIdeaWorker:
    """Invoke a configured agent in a declared isolated Git worktree."""

    def __init__(self, *, command: Sequence[str], working_directory: Path) -> None:
        if not command or not all(isinstance(item, str) and item for item in command):
            raise AgentIdeaWorkerError("agent idea worker command must be a non-empty argv sequence")
        self.command = tuple(command)
        self.working_directory = working_directory.resolve()

    def _assert_isolated_worktree(self) -> None:
        try:
            root = subprocess.run(
                ["git", "-C", str(self.working_directory), "rev-parse", "--show-toplevel"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            entries = subprocess.run(
                ["git", "-C", str(self.working_directory), "worktree", "list", "--porcelain"],
                check=True, capture_output=True, text=True,
            ).stdout.splitlines()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise AgentIdeaWorkerError("agent working_directory must be a Git worktree") from exc
        roots = [line.removeprefix("worktree ") for line in entries if line.startswith("worktree ")]
        if not roots or Path(root).resolve() != self.working_directory or Path(roots[0]).resolve() == self.working_directory:
            raise AgentIdeaWorkerError("agent working_directory must be an isolated linked Git worktree")

    def prepare(self, *, contract: IdeaContract, handoff_path: Path) -> AgentRecipeHandoff:
        self._assert_isolated_worktree()
        contract_path = handoff_path.with_name("idea-contract.json")
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(json.dumps(contract.to_mapping(), indent=2, sort_keys=True) + "\n")
        environment = dict(os.environ)
        environment.update({
            "AF_ML_IDEA_CONTRACT": str(contract_path),
            "AF_ML_IDEA_HANDOFF": str(handoff_path),
        })
        try:
            result = subprocess.run(self.command, cwd=self.working_directory, env=environment,
                                    stdin=subprocess.DEVNULL, check=False)
        except OSError as exc:
            raise AgentIdeaWorkerError("unable to launch configured agent idea worker") from exc
        if result.returncode != 0:
            raise AgentIdeaWorkerError(f"agent idea worker exited {result.returncode}")
        return AgentRecipeHandoff.load(handoff_path, contract=contract)
