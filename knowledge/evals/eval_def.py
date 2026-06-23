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


def align_per_item(rubric: "Rubric", raw_per_item: dict | None) -> dict[str, float]:
    """Map a judge's returned scores onto the rubric's item ids.

    Prefers exact-id keys; falls back to positional mapping when the model returned
    different keys (e.g. ``"1"``, ``"2"`` — it scores in the order presented) but the
    right count. Returns a score for every rubric item (missing -> 0.0), so the
    keys always match the ids ``weighted_overall`` expects.
    """
    ids = [it.id for it in rubric.items]
    scores = {k: float(v) for k, v in (raw_per_item or {}).items()}
    if any(i in scores for i in ids):  # the model used the real ids
        return {i: scores.get(i, 0.0) for i in ids}
    if ids and len(scores) == len(ids):  # positional fallback (wrong/numeric keys)
        return dict(zip(ids, scores.values()))
    return {i: scores.get(i, 0.0) for i in ids}


def weighted_overall(rubric: "Rubric", per_item: dict[str, float]) -> float:
    """Weighted average of per-item scores using the rubric's declared weights.

    Computed in the harness, never asked of the judge model — the LLM only returns
    per-criterion scores, so the declared weights are authoritative (an LLM left to
    "compute the weighted average" returns the unweighted mean). Missing items count
    as 0; zero total weight yields 0. Clamped to [0, 1].
    """
    total = sum(it.weight for it in rubric.items)
    if not total:
        return 0.0
    weighted = sum(per_item.get(it.id, 0.0) * it.weight for it in rubric.items)
    return max(0.0, min(1.0, weighted / total))


def rubric_score_schema(rubric: "Rubric") -> dict:
    """JSON Schema forcing a judge to return ``{per_item: {<id>: number, ...}}`` with
    exactly the rubric's ids as keys.

    Used by both judges for structured output (OpenRouter ``response_format`` and the
    Claude CLI ``--json-schema``), so the model can't drift to positional keys. No
    ``minimum``/``maximum`` — strict structured outputs reject range keywords; the
    0..1 range is asked in the prompt and clamped downstream.
    """
    ids = [it.id for it in rubric.items]
    return {
        "type": "object",
        "properties": {
            "per_item": {
                "type": "object",
                "properties": {i: {"type": "number"} for i in ids},
                "required": ids,
                "additionalProperties": False,
            }
        },
        "required": ["per_item"],
        "additionalProperties": False,
    }


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
    needs: list[str] = Field(default_factory=list)  # runner capabilities required (e.g. "sandbox"); a backend that can't provide them skips the case
    xfail: str | None = None  # if set, the case is expected to fail (reason = the unbuilt capability); a real fail reports XFAIL, an unexpected pass reports XPASS
    model: str | None = None  # pin the runner's model (e.g. "openai/gpt-4o-mini", "sonnet"); None => the backend's default. NB: model ids are backend-specific
    output_file: str | None = None  # box-relative artifact whose content is graded; None => runner default. Only sandbox runners honor it
    reader: Literal["whole_file", "retrieving"] = "whole_file"  # graph reader to wire: dump-everything vs relevance-ranked
    embedder: Literal["fake", "cached", "live"] = "fake"  # vector source: offline Fake / committed real-vector cache / online real embedder
    reader_top_k: int | None = None  # override RetrievingReader.top_k; None => reader default
    reader_abs_floor: float | None = None  # override RetrievingReader.abs_floor (existence floor); 0 disables it (isolation). None => default
    reader_rel_ratio: float | None = None  # override RetrievingReader.rel_ratio (keep within X% of top); 0 disables it (isolation). None => default
    ingest_model: str | None = None  # OpenRouter chat model for ingestion distillation (PromptIngestor's LLM); None => passthrough line-split. Needs OPENROUTER_API_KEY
    ingest_state: Literal["proposed", "active"] = "proposed"  # lifecycle state for via_ingestor facts (mirrors write_policy SeedState). "proposed" (default) = staged, gated out of retrieval; "active" = endorsed/retrievable (e.g. an applicant's established background)
    merge_model: str | None = None  # OpenRouter chat model for the dedup MergeJudge; None => exact-dedup only. Replayed from a committed merge verdict cassette (or a live key)
    conflict_model: str | None = None  # OpenRouter chat model for the ConflictFlagger's ConflictJudge; None => no conflict flagging. Replayed from a committed conflict verdict cassette (or a live key)
    tag_model: str | None = None  # Tier-B (gated): OpenRouter chat model for the AspectTagger's AspectJudge; None => no aspect tags. Replayed from a committed aspect verdict cassette (or a live key)
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
        if self.reader == "retrieving":
            if self.embedder == "fake":
                raise ValueError(
                    "reader 'retrieving' needs a real embedder (embedder: cached|live); "
                    "FakeEmbedder cannot rank meaningfully"
                )
            if self.substrate != "vector":
                raise ValueError("reader 'retrieving' needs substrate: vector (a SearchableGraph)")
        return self


# --- the contracts a registered check and a run must satisfy ---


class Artifact(BaseModel):
    """A file the agent produced in the box, relative to its root.

    ``status`` is computed against the start state mounted before the run, so a
    runner without a real working dir (single-shot, fake) reports no artifacts.
    """

    path: str  # box-relative, posix ("calculator.py")
    status: Literal["created", "modified"]  # vs the mounted start state


class EvalContext(BaseModel):
    """What graders see: the agent's produced output and where it ran.

    The trailing fields are *provenance* for the run transcript — they don't
    affect grading. A runner with nothing to report (e.g. ``FakeRunner``) leaves
    them ``None`` / empty.
    """

    case_id: str
    output: str  # produced diff / files / text
    checkout_path: str | None = None  # working dir the run happened in
    raw_response: str | None = None  # full agent CLI stdout (json: result + cost/usage/turns)
    output_source: str | None = None  # which artifact `output` came from
    injected_knowledge: str | None = None  # what the graph reader fed into the system prompt
    artifacts: list[Artifact] = Field(default_factory=list)  # files the agent created/modified (sandbox runners only)


class CheckResult(BaseModel):
    name: str
    passed: bool
    evidence: str = ""


# A deterministic check is:  Callable[[EvalContext, **params], CheckResult]
# resolved from a DeterministicCheckRef.ref and called with ref.params as kwargs.


class JudgeResult(BaseModel):
    """What a rubric judge returns: the overall score plus its provenance."""

    overall: float  # weighted average in [0, 1] — the value that drives the verdict
    per_item: dict[str, float] = Field(default_factory=dict)  # per-criterion scores
    raw_response: str | None = None  # the judge's full CLI stdout


class CaseResult(BaseModel):
    case_id: str
    checks: list[CheckResult] = Field(default_factory=list)
    rubric_score: float | None = None
    passed: bool  # raw verdict: did checks + rubric pass? (independent of xfail)
    xfail_reason: str | None = None  # carried from the case; drives PASS/FAIL/XFAIL/XPASS


class AgentRun(BaseModel):
    """The agent half of a transcript: what it produced and the raw response."""

    raw_response: str | None = None
    output: str = ""
    output_source: str | None = None
    artifacts: list[Artifact] = Field(default_factory=list)  # files created/modified in the box


class RunTranscript(BaseModel):
    """The full, verbose record of one case in one run.

    Written per-case to ``results/runs/<run_id>/<case_id>.json`` — the scoreboard
    (``baseline.jsonl``) stays compact; this carries everything for debugging:
    the raw agent + judge responses (cost/usage/turns ride along inside them),
    the injected knowledge, and the graded verdict.
    """

    run_id: str
    case_id: str
    seed_prompt: str
    injected_knowledge: str = ""
    agent: AgentRun
    judge: JudgeResult | None = None
    verdict: CaseResult
