"""The eval-case schema (frozen) and the run/result contracts.

An :class:`EvalCase` is authored as data (YAML) and loaded into this model. A
deterministic check is a *reference* to a callable under
``evals/deterministic_checks/``; the rubric is inline. The result models define
what a graded run produces and what gets written to the baseline.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


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


class EvalCase(BaseModel):
    """A single eval case — exactly these fields for the MVP."""

    id: str
    seed_prompt: str  # instruction handed to the agent
    target_commit: str  # desired end state / reference
    start_commit: str | None = None  # optional; None => clean baseline
    repo: str | None = None  # where the commits live (defaults to this repo)
    fixture_path: str | None = None  # abs path to a dir copied into the box as start state; set by load_case
    seeded_insight: SeededInsight = Field(default_factory=SeededInsight)
    deterministic_checks: list[DeterministicCheckRef] = Field(default_factory=list)
    rubric: Rubric | None = None

    @model_validator(mode="after")
    def _at_least_one_grader(self) -> "EvalCase":
        if not self.deterministic_checks and self.rubric is None:
            raise ValueError("a case needs deterministic_checks and/or a rubric")
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
