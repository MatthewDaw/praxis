"""Eval harness execution: load cases, run, grade, record a baseline.

For the MVP this single module carries M5 (check runner), M6 (rubric grader),
M7 (runner), and M8 (registry + baseline writer). Split into modules when they
grow.

CLI:

    uv run python -m knowledge.evals.run                   # real Claude Code over all cases
    uv run python -m knowledge.evals.run <case_id>         # real Claude Code, one case
    uv run python -m knowledge.evals.run --fake <case_id>  # offline FakeRunner (no credit)
    uv run python -m knowledge.evals.run --workers 4       # run cases concurrently (bound by rate limits)
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

import yaml

from knowledge.evals.claude_code import ClaudeCodeJudge, ClaudeCodeRunner
from knowledge.evals.eval_def import (
    AgentRun,
    CaseResult,
    CheckResult,
    DeterministicCheckRef,
    EvalCase,
    EvalContext,
    JudgeResult,
    Rubric,
    RunTranscript,
)
from knowledge.llm import openrouter_http
from knowledge.llm.embedder_variants import CachedEmbedder, OpenRouterEmbedder
from knowledge.observability import tracing
from knowledge.wiring import build_trio

HERE = Path(__file__).parent
CASES_DIR = HERE / "cases"
RESULTS_DIR = HERE / "results"
BASELINE_PATH = RESULTS_DIR / "baseline.jsonl"
RUNS_DIR = RESULTS_DIR / "runs"  # verbose per-run transcripts (gitignored)
EMBED_CACHE_DIR = HERE / "fixtures" / "embeddings"  # committed real-vector caches

# Overall verdict threshold for a rubric-only case.
PASS_THRESHOLD = 0.5


# --------------------------------------------------------------------------- #
# M7 — Runner
# --------------------------------------------------------------------------- #
class Runner(Protocol):
    """Executes a case's seed prompt and returns what the agent produced."""

    def run(self, case: EvalCase, reader) -> EvalContext: ...


class FakeRunner:
    """Deterministic runner for harness tests and offline baselining.

    Returns scripted output per case id (default ``""`` — which is exactly the
    "expected to fail" baseline before any real agent runs).
    """

    provides = frozenset()  # offline echo: no sandbox

    def __init__(self, scripted: dict[str, str] | None = None, default: str = "") -> None:
        self.scripted = scripted or {}
        self.default = default

    def run(self, case: EvalCase, reader) -> EvalContext:
        return EvalContext(
            case_id=case.id,
            output=self.scripted.get(case.id, self.default),
        )


# The real Claude Code runner + judge live in knowledge.evals.claude_code
# (imported at the top). They only touch the `claude` binary when run, so
# importing them is free for --fake runs.


# --------------------------------------------------------------------------- #
# M5 — deterministic check runner
# --------------------------------------------------------------------------- #
def resolve_check(ref: DeterministicCheckRef) -> Callable[..., CheckResult]:
    """Resolve a ``"module.path:function"`` ref to the callable it names."""
    if ":" not in ref.ref:
        raise ValueError(f"check ref must be 'module:function', got {ref.ref!r}")
    module_path, func_name = ref.ref.split(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, func_name)


def run_checks(case: EvalCase, ctx: EvalContext) -> list[CheckResult]:
    results: list[CheckResult] = []
    for ref in case.deterministic_checks:
        func = resolve_check(ref)
        result = func(ctx, **ref.params)
        # Name the result after the ref so duplicate functions stay distinguishable.
        results.append(result.model_copy(update={"name": ref.name}))
    return results


# --------------------------------------------------------------------------- #
# M6 — rubric grader
# --------------------------------------------------------------------------- #
# A judge scores a rubric against the output, returning a JudgeResult.
RubricJudge = Callable[[Rubric, EvalContext], JudgeResult]


def grade_rubric(
    case: EvalCase, ctx: EvalContext, judge: RubricJudge | None
) -> JudgeResult | None:
    """Return the judge result, or ``None`` when there's no rubric/judge."""
    if case.rubric is None or judge is None:
        return None
    return judge(case.rubric, ctx)


# --------------------------------------------------------------------------- #
# Backend capabilities — skip cases a runner structurally can't grade
# --------------------------------------------------------------------------- #
def _embed_model() -> str:
    return os.getenv("OPENROUTER_EMBED_MODEL", openrouter_http.DEFAULT_EMBED_MODEL)


def _slug(model: str) -> str:
    return model.replace("/", "_").replace(":", "_")


def _eval_embedder(case: EvalCase):
    """Resolve the embedder for a case's ``embedder`` axis.

    ``fake`` -> None (VectorGraph's FakeEmbedder default). ``cached`` -> a
    CachedEmbedder over the committed fixture (records misses only with a key).
    ``live`` -> an online OpenRouterEmbedder, or None when no key (the case then
    skips, per ``harness_capabilities``).
    """
    if case.embedder == "fake":
        return None
    model = _embed_model()
    has_key = bool(os.getenv("OPENROUTER_API_KEY"))
    live = OpenRouterEmbedder(model=model) if has_key else None
    if case.embedder == "live":
        return live
    cache = EMBED_CACHE_DIR / f"{_slug(model)}.json"
    return CachedEmbedder(live, cache, model_id=model, allow_compute=has_key)


def harness_capabilities() -> set[str]:
    """Embedding capabilities the harness can satisfy, independent of the runner.

    A committed cache fixture provides ``real_embeddings`` (cached cases replay it
    in CI); a live key provides both ``real_embeddings`` and ``live_embeddings``.
    Component cases ignore the runner, so this is unioned into the provided set.
    """
    caps: set[str] = set()
    if (EMBED_CACHE_DIR / f"{_slug(_embed_model())}.json").exists():
        caps.add("real_embeddings")
    if os.getenv("OPENROUTER_API_KEY"):
        caps |= {"real_embeddings", "live_embeddings"}
    return caps


def case_needs(case: EvalCase) -> set[str]:
    """Runner capabilities a case requires.

    The explicit ``needs`` from YAML, plus an implicit ``sandbox`` for any case
    shipping fixtures (only a runner with a real working dir can mount + grade
    them) and ``code_exec`` for a ``code_task`` (clone + run a test oracle). A
    runner that can't provide these grades the case unfaithfully, so it's skipped.
    """
    needs = set(case.needs)
    has_fixtures = bool(case.fixture_path) or (
        bool(case.source_dir) and (Path(case.source_dir) / "fixtures").is_dir()
    )
    if has_fixtures:
        needs.add("sandbox")
    if case.code_task is not None:
        needs.add("code_exec")
    # A writes_file/modifies_file check reads ctx.artifacts, which only a file-
    # producing runner populates — derive file_io so the case SKIPs (not FAILs) on a
    # text-only backend. file_io is weaker than sandbox: it's satisfied by the
    # structured single-shot runner too, not just a real box.
    if any(ref.ref.endswith((":writes_file", ":modifies_file")) for ref in case.deterministic_checks):
        needs.add("file_io")
    # Real-embedding cases: cached replays a committed fixture (or a key); live
    # needs a key and skips offline. Derived from the embedder axis so the case
    # SKIPs (not mis-runs on FakeEmbedder) where the vectors aren't available.
    if case.embedder == "cached":
        needs.add("real_embeddings")
    elif case.embedder == "live":
        needs.add("live_embeddings")
    return needs


def unmet_needs(case: EvalCase, runner: Runner) -> set[str]:
    """Capabilities the case needs that neither the runner nor the harness provides."""
    provided = set(getattr(runner, "provides", frozenset())) | harness_capabilities()
    return case_needs(case) - provided


def skip_reasons(case: EvalCase, runner: Runner) -> set[str]:
    """Why this runner can't faithfully grade the case (empty => runnable).

    Two sources: capabilities the runner lacks (``needs``), and a pinned ``model``
    this backend can't serve. A runner with no ``serves_model`` serves anything.
    """
    reasons = set(unmet_needs(case, runner))
    model = getattr(case, "model", None)
    serves = getattr(runner, "serves_model", None)
    if model and serves is not None and not serves(model):
        reasons.add(f"model:{model}")
    return reasons


def _skip_reason_text(reason: str, backend: str) -> str:
    """Render a raw skip token into a human sentence for the SKIP line."""
    if reason.startswith("model:"):
        return f"pinned model '{reason[len('model:'):]}' not served by the {backend} backend"
    return f"needs '{reason}', which the {backend} backend does not provide"


def partition_by_capability(
    cases: list[EvalCase], runner: Runner
) -> tuple[list[EvalCase], list[tuple[EvalCase, set[str]]]]:
    """Split cases into (runnable, skipped) for ``runner``.

    Skipped entries carry the reasons (unmet needs and/or an unservable model)
    so the caller can report *why*.
    """
    runnable: list[EvalCase] = []
    skipped: list[tuple[EvalCase, set[str]]] = []
    for case in cases:
        reasons = skip_reasons(case, runner)
        if reasons:
            skipped.append((case, reasons))
        else:
            runnable.append(case)
    return runnable, skipped


# --------------------------------------------------------------------------- #
# M7/M8 — orchestration
# --------------------------------------------------------------------------- #
def _ingest_llm_for(case: EvalCase, llm):
    """Resolve the ingestor's distillation LLM.

    Honors an explicit ``llm`` if given; otherwise, when the case sets
    ``ingest_model``, build a real OpenRouter model so ``PromptIngestor.synthesis``
    actually distills (instead of the passthrough line-split). ``PromptIngestor``
    wants a plain ``str -> str`` callable, so adapt ``OpenRouterLlm.complete`` with
    a one-user-message wrapper. None (no llm, no ingest_model) => passthrough.
    """
    if llm is not None or not case.ingest_model:
        return llm
    from knowledge.llm.llm_def import ChatMessage
    from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm

    model = OpenRouterLlm(model=case.ingest_model)
    return lambda prompt: model.complete([ChatMessage(role="user", content=prompt)])


def _build_trio_for(case: EvalCase, llm=None):
    """Wire the trio honoring the case's reader/embedder/ingest_model axes."""
    llm = _ingest_llm_for(case, llm)
    embedder = _eval_embedder(case)
    graph = None
    if case.substrate == "vector" and case.embedder != "fake":
        # Real-embedder cases seed with a minimal policy: keep redact + dedup (the
        # dedup cases test it), drop the per-write ConflictFlagger LLM call so
        # seeding a large graph stays cheap. Fake vector cases keep the default.
        from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import VectorGraph
        from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor

        graph = VectorGraph(embedder=embedder, policy=[Redactor(), Deduper()])
    return build_trio(
        substrate=case.substrate,
        graph=graph,
        llm=llm,
        reader=case.reader,
        embedder=embedder,
        reader_top_k=case.reader_top_k,
        reader_min_score=case.reader_min_score,
    )


# Optional seed cache: many cases share the exact same seeded knowledge (e.g.
# every matt/applications case ingests the same resume/LinkedIn/degree docs), and
# ingestion can be expensive (real LLM distillation + embeddings). When enabled,
# the seeded reader is cached by a signature of everything that affects the graph,
# so shared-seed cases reuse one ingestion instead of repeating it.
_SEED_CACHE: dict[str, object] = {}
_SEED_CACHE_ENABLED = False


def set_seed_cache(enabled: bool) -> None:
    """Toggle reuse of seeded readers across cases with identical seeds."""
    global _SEED_CACHE_ENABLED
    _SEED_CACHE_ENABLED = enabled


def clear_seed_cache() -> None:
    _SEED_CACHE.clear()


def _seed_signature(case: EvalCase) -> str:
    import hashlib
    import json

    payload = {
        "substrate": case.substrate,
        "embedder": case.embedder,
        "ingest_model": case.ingest_model,
        "reader": case.reader,
        "reader_top_k": case.reader_top_k,
        "reader_min_score": case.reader_min_score,
        "via_ingestor": list(case.seeded_insight.via_ingestor),
        "direct_to_graph": list(case.seeded_insight.direct_to_graph),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _seed_knowledge(case: EvalCase, llm=None):
    """Provision a fresh trio and pre-load the case's seeded insight.

    The graph initializes itself (in-memory for the MVP) — no path, no file.
    """
    if _SEED_CACHE_ENABLED:
        sig = _seed_signature(case)
        cached = _SEED_CACHE.get(sig)
        if cached is not None:
            return cached

    graph, ingestor, reader = _build_trio_for(case, llm=llm)
    # Channel encodes the write intent -> the state the fact lands in:
    #   * direct_to_graph -> "active": simulates a direct user approval.
    #   * via_ingestor    -> "proposed": the system passively distilling raw input.
    for text in case.seeded_insight.direct_to_graph:
        graph.write(text, state="active")
    for text in case.seeded_insight.via_ingestor:
        ingestor.ingest(text, state="proposed")

    if _SEED_CACHE_ENABLED:
        _SEED_CACHE[sig] = reader
    return reader


# --- context producers ------------------------------------------------------
# The eval path has exactly one real fork: deterministic component isolation
# (no agent) vs the full agent pipeline. Both just produce an ``EvalContext``
# that the shared grade/report spine then handles. Each producer below has the
# uniform signature ``(case, runner, llm) -> EvalContext`` and is registered in
# ``CONTEXT_PRODUCERS`` keyed by ``case.component`` (``None`` = full pipeline),
# so the fork is a single dispatch — no branching in the run loop, and a new
# isolation mode is just one more producer. Component producers ignore ``runner``.
def _produce_full(case: EvalCase, runner: Runner, llm=None) -> EvalContext:
    """Full pipeline: seed the trio, then run the agent ``runner``."""
    reader = _seed_knowledge(case, llm=llm)
    return runner.run(case, reader)


def _produce_knowledge_graph(case: EvalCase, runner: Runner, llm=None) -> EvalContext:
    """Component: write the seeded ``direct_to_graph`` lines, read them back."""
    graph, _, _ = _build_trio_for(case, llm=llm)
    for text in case.seeded_insight.direct_to_graph:
        graph.write(text)
    return EvalContext(case_id=case.id, output=graph.read())


def _produce_ingestion(case: EvalCase, runner: Runner, llm=None) -> EvalContext:
    """Component: ingest the seeded ``via_ingestor`` lines, read the graph."""
    graph, ingestor, _ = _build_trio_for(case, llm=llm)
    for text in case.seeded_insight.via_ingestor:
        ingestor.ingest(text)
    return EvalContext(case_id=case.id, output=graph.read())


def _produce_graph_reader(case: EvalCase, runner: Runner, llm=None) -> EvalContext:
    """Component: seed the graph (raw docs via the ingestor and/or pre-distilled
    facts written direct), then retrieve via the reader (``seed_prompt`` as the
    situation/query). Seeding ``via_ingestor`` lets a reader case exercise the
    real *write-intent -> gated-read* path; ``direct_to_graph`` seeds pre-curated
    facts for tests that isolate the reader.
    """
    graph, ingestor, reader = _build_trio_for(case, llm=llm)
    for text in case.seeded_insight.via_ingestor:
        ingestor.ingest(text)
    for text in case.seeded_insight.direct_to_graph:
        graph.write(text)
    return EvalContext(case_id=case.id, output=reader.read(case.seed_prompt))


# case.component (or None for the full agent pipeline) -> context producer.
CONTEXT_PRODUCERS = {
    None: _produce_full,
    "knowledge_graph": _produce_knowledge_graph,
    "ingestion": _produce_ingestion,
    "graph_reader": _produce_graph_reader,
}


def run_component(case: EvalCase, llm=None) -> EvalContext:
    """Exercise a single component in isolation (no agent) and return its output.

    Thin public shim over the component context producers (used by the component
    eval tests); the same producers drive :func:`run_case_full`.
    """
    producer = CONTEXT_PRODUCERS.get(case.component)
    if case.component is None or producer is None:  # pragma: no cover - schema-guarded
        raise ValueError(f"unknown component: {case.component!r}")
    return producer(case, None, llm)  # components ignore the runner


def run_case_full(
    case: EvalCase,
    runner: Runner,
    judge: RubricJudge | None = None,
    llm=None,
) -> tuple[EvalContext, JudgeResult | None, CaseResult]:
    """Run + grade a case, returning everything a transcript needs.

    Component-scoped cases run deterministically via ``run_component`` and ignore
    ``runner``; full-pipeline cases seed knowledge and run the agent ``runner``.
    Returns the runner's context, the judge result (``None`` if unjudged), and
    the verdict. :func:`run_case` is the thin verdict-only wrapper over this.
    """
    # One parent span per case groups the run + judge child spans into a single
    # trace, named after the case so Phoenix lists it by eval name.
    with tracing.llm_span(case.id, kind="CHAIN", input_value=case.seed_prompt) as span:
        producer = CONTEXT_PRODUCERS.get(case.component)
        if producer is None:  # pragma: no cover - schema-guarded
            raise ValueError(f"unknown component: {case.component!r}")
        ctx = producer(case, runner, llm)

        checks = run_checks(case, ctx)
        judge_result = grade_rubric(case, ctx, judge)
        rubric_score = None if judge_result is None else judge_result.overall

        checks_ok = bool(checks) and all(c.passed for c in checks)
        if checks:
            passed = checks_ok and (rubric_score is None or rubric_score >= PASS_THRESHOLD)
        else:
            passed = rubric_score is not None and rubric_score >= PASS_THRESHOLD

        result = CaseResult(
            case_id=case.id,
            checks=checks,
            rubric_score=rubric_score,
            passed=passed,
            xfail_reason=case.xfail,
        )
        tracing.record_output(
            span,
            output=ctx.output,
            **{
                "eval.status": status_of(result),
                "eval.checks_passed": sum(c.passed for c in checks),
                "eval.checks_total": len(checks),
                "eval.rubric_score": rubric_score,
            },
        )
    return ctx, judge_result, result


def status_of(result: CaseResult) -> str:
    """Display status: PASS / FAIL / XFAIL (expected red) / XPASS (unexpected green).

    Only ``FAIL`` means a regression. A case marked ``xfail`` is expected to fail
    until its capability lands; an ``XPASS`` is the signal that it has — promote
    the spec to a real assertion.
    """
    if result.xfail_reason:
        return "XPASS" if result.passed else "XFAIL"
    return "PASS" if result.passed else "FAIL"


def run_case(
    case: EvalCase,
    runner: Runner,
    judge: RubricJudge | None = None,
    llm=None,
) -> CaseResult:
    """Run a single case end-to-end and return its graded verdict."""
    _, _, result = run_case_full(case, runner, judge=judge, llm=llm)
    return result


def build_transcript(
    case: EvalCase,
    ctx: EvalContext,
    judge_result: JudgeResult | None,
    verdict: CaseResult,
    run_id: str,
) -> RunTranscript:
    """Assemble the verbose per-case record from a completed run."""
    return RunTranscript(
        run_id=run_id,
        case_id=case.id,
        seed_prompt=case.seed_prompt or "",  # component cases have no seed_prompt
        injected_knowledge=ctx.injected_knowledge or "",
        agent=AgentRun(
            raw_response=ctx.raw_response,
            output=ctx.output,
            output_source=ctx.output_source,
            artifacts=ctx.artifacts,
        ),
        judge=judge_result,
        verdict=verdict,
    )


# --------------------------------------------------------------------------- #
# M4 (loader) + M8 (registry / baseline)
# --------------------------------------------------------------------------- #
def load_case(case_dir: Path) -> EvalCase:
    """Load an ``EvalCase`` from ``<case_dir>/case.yaml``.

    A sibling ``fixture/`` dir (if present) is the case's start state: its
    resolved path is recorded on the case so the runner can copy it into the box.
    """
    data = yaml.safe_load((case_dir / "case.yaml").read_text(encoding="utf-8"))
    case = EvalCase.model_validate(data)
    updates: dict = {"source_dir": str(case_dir)}
    # A sibling ``fixture/`` dir (Monica's convention) is copied into the box wholesale.
    fixture = case_dir / "fixture"
    if fixture.is_dir():
        updates["fixture_path"] = str(fixture.resolve())
    return case.model_copy(update=updates)


def load_cases(cases_dir: Path = CASES_DIR) -> list[EvalCase]:
    """Load every registered case (any ``case.yaml`` under ``cases_dir``).

    Searches recursively, so cases may live at ``cases/<case-id>/case.yaml``.
    """
    if not cases_dir.exists():
        return []
    return [load_case(f.parent) for f in sorted(cases_dir.rglob("case.yaml"))]


def iter_case_dirs(cases_dir: Path = CASES_DIR) -> list[Path]:
    """Every directory containing a ``case.yaml`` (recursive, sorted)."""
    if not cases_dir.exists():
        return []
    return [f.parent for f in sorted(cases_dir.rglob("case.yaml"))]


def write_baseline(results: list[CaseResult], path: Path = BASELINE_PATH) -> None:
    """Append one JSONL row per case result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for result in results:
            f.write(json.dumps(result.model_dump()) + "\n")


def load_env() -> None:
    """Load .env (OPENROUTER_API_KEY etc.) if python-dotenv is installed."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


# Human-readable label per backend, for run banners.
_BACKEND_LABEL = {
    "claude": "real Claude Code (subscription)",
    "fake": "FakeRunner (offline, no credit)",
    "openrouter": "OpenRouter (cheap single-shot LLM)",
    "structured": "OpenRouter structured file output (file_io)",
}


def select_runner(kind: str):
    """Return ``(runner, judge)`` for a backend kind.

    - ``claude``     — real headless Claude Code runner (full fidelity). Grading uses
                       the cheap OpenRouter judge when ``OPENROUTER_API_KEY`` is set
                       (grading is a text task; routing it through the agent harness
                       costs ~100x), falling back to the Claude judge otherwise.
    - ``fake``       — offline FakeRunner, no judge (deterministic checks only).
    - ``openrouter`` — cheap single-shot OpenRouter runner + judge (loads .env).
    - ``structured`` — single-shot OpenRouter via structured file output (file_io):
                       grades file artifacts on the cheap backend (loads .env).
    """
    if kind == "fake":
        return FakeRunner(), None
    if kind == "openrouter":
        load_env()
        from knowledge.evals.openrouter import OpenRouterJudge, OpenRouterRunner

        return OpenRouterRunner(), OpenRouterJudge()
    if kind == "structured":
        load_env()
        from knowledge.evals.openrouter import OpenRouterJudge, StructuredOpenRouterRunner

        return StructuredOpenRouterRunner(), OpenRouterJudge()
    load_env()  # so CLAUDE_CODE_MODEL / OPENROUTER_* (and any .env) is available
    # Decouple grading from the runner: the judge is a text-grading task, so prefer
    # the cheap structured OpenRouter judge; only fall back to the (far pricier)
    # Claude judge when there's no OpenRouter key.
    if os.getenv("OPENROUTER_API_KEY"):
        from knowledge.evals.openrouter import OpenRouterJudge

        return ClaudeCodeRunner(), OpenRouterJudge()
    return ClaudeCodeRunner(), ClaudeCodeJudge()


def write_transcript(transcript: RunTranscript, runs_dir: Path = RUNS_DIR) -> Path:
    """Write one verbose transcript to ``<runs_dir>/<run_id>/<case_id>.json``."""
    out_dir = runs_dir / transcript.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{transcript.case_id}.json"
    path.write_text(json.dumps(transcript.model_dump(), indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="knowledge.evals.run")
    parser.add_argument("case_ids", nargs="*", help="case ids to run (default: all)")
    backend = parser.add_mutually_exclusive_group()
    backend.add_argument(
        "--fake",
        action="store_true",
        help="offline FakeRunner instead of real Claude Code (no credit)",
    )
    backend.add_argument(
        "--openrouter",
        action="store_true",
        help="cheap single-shot OpenRouter LLM backend (needs OPENROUTER_API_KEY in .env)",
    )
    backend.add_argument(
        "--structured",
        action="store_true",
        help="OpenRouter via structured file output — grades file artifacts (file_io) on the cheap backend",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="run N cases concurrently (default 1, serial). Cases are independent; "
        "bound this to respect API rate limits.",
    )
    args = parser.parse_args(argv)

    # Load .env (PHOENIX_*, OPENROUTER_*) and light up tracing if configured.
    load_env()
    from knowledge.observability.tracing import setup_tracing

    setup_tracing()

    cases = load_cases()
    if args.case_ids:
        wanted = set(args.case_ids)
        cases = [c for c in cases if c.id in wanted]

    if not cases:
        print("no cases to run")
        return 0

    kind = (
        "structured" if args.structured
        else "openrouter" if args.openrouter
        else "fake" if args.fake
        else "claude"
    )
    runner, judge = select_runner(kind)

    # Skip cases this backend can't grade faithfully (e.g. a sandbox case on the
    # single-shot OpenRouter runner) so the scoreboard reflects only real signal.
    cases, skipped = partition_by_capability(cases, runner)
    for case, reasons in skipped:
        why = "; ".join(_skip_reason_text(r, kind) for r in sorted(reasons))
        print(f"[SKIP] {case.id}  ({why})")
    if not cases:
        print("no runnable cases for this backend")
        return 0
    print(f"running {len(cases)} case(s) through {_BACKEND_LABEL[kind]}...")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    def _run_one(case: EvalCase) -> CaseResult:
        ctx, judge_result, run_result = run_case_full(case, runner, judge=judge)
        # Transcripts are per-case files, so writing them as each case finishes is
        # safe under concurrency and preserves partial progress if a later case dies.
        write_transcript(build_transcript(case, ctx, judge_result, run_result, run_id))
        return run_result

    if args.workers > 1:
        from concurrent.futures import ThreadPoolExecutor

        # Cases are independent (own sandbox + own in-process graph); they're
        # I/O-bound on the agent/judge/embed calls, so threads give real overlap.
        # pool.map preserves input order, keeping the scoreboard stable.
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(_run_one, cases))
    else:
        results = [_run_one(case) for case in cases]
    write_baseline(results)

    tally: dict[str, int] = {}
    for r in results:
        status = status_of(r)
        tally[status] = tally.get(status, 0) + 1
        score = "" if r.rubric_score is None else f"  rubric={r.rubric_score:.2f}"
        note = f"  ({r.xfail_reason})" if r.xfail_reason else ""
        print(
            f"[{status}] {r.case_id}  "
            f"checks={sum(c.passed for c in r.checks)}/{len(r.checks)}{score}{note}"
        )
    print(f"\nwrote {len(results)} rows -> {BASELINE_PATH}")
    print(f"wrote {len(results)} transcript(s) -> {RUNS_DIR / run_id}")

    # Summary: only FAIL is a regression; XFAIL is expected; XPASS wants a promote.
    summary = "  ".join(f"{k.lower()}={tally[k]}" for k in sorted(tally))
    print(f"summary: {summary}", end="")
    print(f"  skipped={len(skipped)}" if skipped else "")
    if tally.get("XPASS"):
        print(f"note: {tally['XPASS']} xfail case(s) now PASS — promote them to real assertions")
    return 0


if __name__ == "__main__":
    main()
