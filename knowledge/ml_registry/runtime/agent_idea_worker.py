"""Typed handoff between a claimed registry IDEA and an authoring agent."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import subprocess
from typing import Callable, Mapping, Sequence

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
    recipe_path: Path | None = None

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

    def prepare(
        self,
        *,
        contract: IdeaContract,
        handoff_path: Path,
        heartbeat: Callable[[], None] | None = None,
        heartbeat_interval_s: float = 60.0,
    ) -> AgentRecipeHandoff:
        """Author one recipe, keeping an optional external lease alive while it runs.

        The callback is deliberately opaque to this typed handoff layer. The operator
        supplies the registry-backed claim heartbeat; callers outside an operator may
        omit it. A failed heartbeat aborts the author instead of allowing it to keep
        writing against a claim it no longer owns.
        """
        self._assert_isolated_worktree()
        if heartbeat is not None and heartbeat_interval_s <= 0:
            raise AgentIdeaWorkerError("agent heartbeat interval must be positive")
        contract_path = handoff_path.with_name("idea-contract.json")
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(json.dumps(contract.to_mapping(), indent=2, sort_keys=True) + "\n")
        environment = dict(os.environ)
        environment.update({
            "AF_ML_IDEA_CONTRACT": str(contract_path),
            "AF_ML_IDEA_HANDOFF": str(handoff_path),
        })
        returncode = self._run_author(
            environment,
            heartbeat=heartbeat,
            heartbeat_interval_s=heartbeat_interval_s,
        )
        if returncode != 0:
            raise AgentIdeaWorkerError(f"agent idea worker exited {returncode}")
        handoff = AgentRecipeHandoff.load(handoff_path, contract=contract)
        commit = self._validated_head_commit(handoff.commit)
        recipe_path = handoff_path.with_name("recipe.json")
        recipe_path.write_text(json.dumps(handoff.recipe, indent=2, sort_keys=True) + chr(10))
        return replace(handoff, commit=commit, recipe_path=recipe_path)

    def _run_author(
        self,
        environment: Mapping[str, str],
        *,
        heartbeat: Callable[[], None] | None,
        heartbeat_interval_s: float,
    ) -> int:
        """Run the configured author and call ``heartbeat`` for its whole lifetime."""
        if heartbeat is None:
            try:
                return subprocess.run(
                    self.command, cwd=self.working_directory, env=environment,
                    stdin=subprocess.DEVNULL, check=False,
                ).returncode
            except OSError as exc:
                raise AgentIdeaWorkerError("unable to launch configured agent idea worker") from exc
        try:
            # Claim immediately before the author starts, then periodically while it
            # remains alive. The callback itself owns atomic persistence.
            heartbeat()
            process = subprocess.Popen(
                self.command, cwd=self.working_directory, env=environment,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise AgentIdeaWorkerError("unable to launch configured agent idea worker") from exc
        except Exception as exc:
            raise AgentIdeaWorkerError("agent idea claim heartbeat failed before authoring") from exc
        try:
            while True:
                try:
                    return process.wait(timeout=heartbeat_interval_s)
                except subprocess.TimeoutExpired:
                    try:
                        heartbeat()
                    except Exception as exc:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        raise AgentIdeaWorkerError(
                            "agent idea claim heartbeat failed while authoring"
                        ) from exc
        finally:
            # No child may outlive a Python-level interruption between waits.
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def _validated_head_commit(self, declared: str) -> str:
        """Require the recipe to name the exact committed revision in this worktree."""
        try:
            commit = subprocess.run(
                ["git", "-C", str(self.working_directory), "rev-parse", "--verify",
                 f"{declared}^{{commit}}"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            head = subprocess.run(
                ["git", "-C", str(self.working_directory), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise AgentIdeaWorkerError(
                "agent handoff commit is not present in the authoring worktree"
            ) from exc
        if commit != head:
            raise AgentIdeaWorkerError(
                "agent handoff commit must be the current HEAD of the authoring worktree"
            )
        return commit
