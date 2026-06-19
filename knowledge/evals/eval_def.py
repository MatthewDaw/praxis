"""The eval-case schema (frozen) and the run/result contracts.

An :class:`EvalCase` is authored as data (YAML) and loaded into this model. A
deterministic check is a *reference* to a callable under
``evals/deterministic_checks/``; the rubric is inline. The result models define
what a graded run produces and what gets written to the baseline.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from knowledge.evals.repo.repo_task_def import RepoTask


class DeterministicCheckRef(BaseModel):
    """Points at a registered callable in ``evals/deterministic_checks/``.

    ``ref`` is a ``"module.path:function"`` string; ``params`` are passed to the
    callable as keyword arguments, so a single check function can be reused
    across cases with different expectations.
    """

    name: str
    ref: str
    params: dict = Field(default_factory=dict)


class RubricItem(BaseModel):
    id: str
    criterion: str  # what the LLM judge evaluates
    weight: float = 1.0


class Rubric(BaseModel):
    id: str
    items: list[RubricItem]


class SeededInsight(BaseModel):
    """Knowledge pre-loaded before the run."""

    via_ingestor: list[str] = Field(default_factory=list)  # each fed to Ingestor.ingest()
    direct_to_graph: list[str] = Field(default_factory=list)  # each written to KnowledgeGraph.write()


# Which slice of the pipeline a case exercises. None => the full agent pipeline
# (seed knowledge -> run the agent -> grade). A component value runs *only* that
# piece deterministically, with no agent involved.
Component = Literal["knowledge_graph", "ingestion", "graph_reader"]


class EvalCase(BaseModel):
    """A single eval case.

    Set ``component`` to scope the case to one part of the algorithm; leave it
    ``None`` for a full end-to-end pipeline run through the agent.
    """

    id: str
    component: Component | None = None
    substrate: Literal["in_memory", "vector"] = "in_memory"  # which knowledge trio to wire
    seed_prompt: str | None = None  # full-pipeline: agent instruction; reader case: context hint
    target_commit: str | None = None  # full-pipeline: desired end state / reference
    start_commit: str | None = None  # optional; None => clean baseline
    repo: str | None = None  # where the commits live (defaults to this repo)
    code_task: RepoTask | None = None  # real-repo (SWE-bench-style) task: clone+test oracle
    fixture_path: str | None = None  # abs path to a dir copied into the box as start state; set by load_case
    seeded_insight: SeededInsight = Field(default_factory=SeededInsight)
    deterministic_checks: list[DeterministicCheckRef] = Field(default_factory=list)
    rubric: Rubric | None = None
    # Filesystem dir the case was loaded from; set by load_case. Used to find a
    # fixtures/ subdir to mount into the sealed box. Not authored in YAML.
    source_dir: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> "EvalCase":
        if not self.deterministic_checks and self.rubric is None:
            raise ValueError("a case needs deterministic_checks and/or a rubric")
        if self.component is None and (not self.seed_prompt or not self.target_commit):
            raise ValueError(
                "a full-pipeline case (no component) needs seed_prompt and target_commit"
            )
        return self


# --- the contracts a registered check and a run must satisfy ---


class EvalContext(BaseModel):
    """What graders see: the agent's produced output and where it ran."""

    case_id: str
    output: str  # produced diff / files / text
    checkout_path: str | None = None  # working dir the run happened in


class CheckResult(BaseModel):
    name: str
    passed: bool
    evidence: str = ""


# A deterministic check is:  Callable[[EvalContext, **params], CheckResult]
# resolved from a DeterministicCheckRef.ref and called with ref.params as kwargs.


class CaseResult(BaseModel):
    case_id: str
    checks: list[CheckResult] = Field(default_factory=list)
    rubric_score: float | None = None
    passed: bool  # overall verdict for the baseline row
