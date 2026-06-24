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
    build_reference,
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
VERDICT_CACHE_DIR = HERE / "fixtures" / "verdicts"  # committed judge-verdict cassettes
INGEST_CACHE_DIR = HERE / "fixtures" / "ingestion"  # committed ingestion replay cassettes
CAPTION_CACHE_DIR = HERE / "fixtures" / "captions"  # committed VLM image-caption cassettes

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
# A judge scores a rubric against the output, returning a JudgeResult. The judge
# also receives the case's seeded reference (None when the seed is empty) so
# grounding/honesty criteria can verify support rather than mere plausibility.
RubricJudge = Callable[[Rubric, EvalContext, "str | None"], JudgeResult]


def grade_rubric(
    case: EvalCase, ctx: EvalContext, judge: RubricJudge | None
) -> JudgeResult | None:
    """Return the judge result, or ``None`` when there's no rubric/judge.

    Builds the seeded reference from the case (the call site already holds it) and
    passes it to the judge; ``EvalContext`` is run provenance and stays unchanged.
    """
    if case.rubric is None or judge is None:
        return None
    return judge(case.rubric, ctx, build_reference(case))


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
    # Merge-judge verdicts: a committed merge cassette replays offline; a live key
    # can compute them. Either way the dedup cases can run faithfully.
    merge_dir = VERDICT_CACHE_DIR / "merge"
    if os.getenv("OPENROUTER_API_KEY") or (merge_dir.exists() and any(merge_dir.glob("*.json"))):
        caps.add("merge_verdicts")
    # Conflict-judge verdicts: same shape as merge — a committed conflict cassette
    # replays offline; a live key can compute them.
    conflict_dir = VERDICT_CACHE_DIR / "conflict"
    if os.getenv("OPENROUTER_API_KEY") or (conflict_dir.exists() and any(conflict_dir.glob("*.json"))):
        caps.add("conflict_verdicts")
    # Claim-extraction replay for the structural contradiction path: a committed
    # claim cassette replays offline; a live key can record it.
    claim_dir = VERDICT_CACHE_DIR / "claim_extract"
    if os.getenv("OPENROUTER_API_KEY") or (claim_dir.exists() and any(claim_dir.glob("*.json"))):
        caps.add("claim_verdicts")
    # Tier-B aspect-tag verdicts: same shape — committed aspect cassette replays
    # offline; a live key can compute them.
    aspect_dir = VERDICT_CACHE_DIR / "aspect"
    if os.getenv("OPENROUTER_API_KEY") or (aspect_dir.exists() and any(aspect_dir.glob("*.json"))):
        caps.add("tag_verdicts")
    # Ingestion replay: a committed ingestion cassette replays the distilled text
    # offline; a live key can record it. Either way an ingest_model case can run
    # faithfully (rather than mis-running on the passthrough line-split).
    if os.getenv("OPENROUTER_API_KEY") or (
        INGEST_CACHE_DIR.exists() and any(INGEST_CACHE_DIR.glob("*.json"))
    ):
        caps.add("ingest_replay")
    # Image captions: a committed caption cassette replays offline; a live key can
    # compute them. Either way image-asset cases can run faithfully.
    if os.getenv("OPENROUTER_API_KEY") or (
        CAPTION_CACHE_DIR.exists() and any(CAPTION_CACHE_DIR.glob("*.json"))
    ):
        caps.add("real_captions")
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
    # Semantic-merge cases need a merge verdict source (committed cassette or a key),
    # else they'd silently fall back to exact-dedup and mis-grade -> SKIP instead.
    if case.merge_model:
        needs.add("merge_verdicts")
    # Conflict cases need claim-extraction replay (the structural detector's front)
    # plus a value-verdict source; without the claim cassette the detector extracts
    # nothing and the case mis-grades -> SKIP instead.
    if case.conflict_model:
        needs.add("claim_verdicts")
        needs.add("conflict_verdicts")
    # Tier-B implicit-contradiction cases need an aspect-tag verdict source too.
    if case.tag_model:
        needs.add("tag_verdicts")
    # Cases that distill via a real ingest model need the ingestion cassette (or a
    # key) to replay deterministically, else they'd silently mis-run on the
    # passthrough line-split -> SKIP instead.
    if case.ingest_model:
        needs.add("ingest_replay")
    # Image-caption cases need a caption source (committed cassette or a key), else
    # they'd silently fall back to deterministic-only cards and mis-grade -> SKIP.
    if case.caption_model:
        needs.add("real_captions")
    # Seeding image assets requires a real working dir to read the mounted fixture.
    if case.seeded_insight.via_image_ingestor:
        needs.add("sandbox")
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
    a one-user-message wrapper, then wrap that in an ``IngestionCassette`` so the
    distilled text replays from the committed fixture offline (parallel to
    ``_eval_embedder``'s ``cached`` branch). None (no llm, no ingest_model) => passthrough.
    """
    if llm is not None or not case.ingest_model:
        return llm
    from knowledge.llm.ingestion_cassette import IngestionCassette

    has_key = bool(os.getenv("OPENROUTER_API_KEY"))
    inner = None
    if has_key:
        from knowledge.llm.llm_def import ChatMessage
        from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm

        model = OpenRouterLlm(model=case.ingest_model)
        inner = lambda prompt: model.complete([ChatMessage(role="user", content=prompt)])
    cache = INGEST_CACHE_DIR / f"{_slug(case.ingest_model)}.json"
    return IngestionCassette(cache, model_id=case.ingest_model, inner=inner, allow_compute=has_key)


def _merge_judge_for(case: EvalCase):
    """Build the dedup ``MergeJudge`` for a case's ``merge_model`` axis (None => none).

    Mirrors the embedder wiring: a live OpenRouter judge when a key is set, plus a
    committed verdict cassette for offline replay. With neither, returns None so the
    ``Deduper`` falls back to exact-dedup only (the case SKIPs via ``merge_verdicts``).
    """
    if not case.merge_model:
        return None
    from knowledge.knowledge_graph.write_policy.write_step_variants import MergeJudge
    from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm
    from knowledge.llm.verdict_cassette import VerdictCassette

    has_key = bool(os.getenv("OPENROUTER_API_KEY"))
    llm = OpenRouterLlm(model=case.merge_model) if has_key else None
    cache = VERDICT_CACHE_DIR / "merge" / f"{_slug(case.merge_model)}.json"
    cassette = (
        VerdictCassette(cache, model_id=case.merge_model, allow_compute=has_key)
        if (cache.exists() or has_key)
        else None
    )
    if llm is None and cassette is None:
        return None
    return MergeJudge(llm=llm, cassette=cassette)


def _claim_extractor_for(case: EvalCase):
    """Build the ``ClaimExtractor`` for a case's ``conflict_model`` axis (None => none).

    The structural contradiction path's front: extracts (subject, attribute, value)
    claims, replayed offline from a committed claim cassette. With neither cassette
    nor key, returns None so the extractor is inert (the case SKIPs via
    ``conflict_verdicts``).
    """
    if not case.conflict_model:
        return None
    from knowledge.knowledge_graph.write_policy.write_step_variants import (
        ClaimExtractionJudge,
        ClaimExtractor,
    )
    from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm
    from knowledge.llm.verdict_cassette import VerdictCassette

    has_key = bool(os.getenv("OPENROUTER_API_KEY"))
    llm = OpenRouterLlm(model=case.conflict_model) if has_key else None
    cache = VERDICT_CACHE_DIR / "claim_extract" / f"{_slug(case.conflict_model)}.json"
    cassette = (
        VerdictCassette(cache, model_id=case.conflict_model, allow_compute=has_key)
        if (cache.exists() or has_key)
        else None
    )
    if llm is None and cassette is None:
        return None
    return ClaimExtractor(judge=ClaimExtractionJudge(llm=llm, cassette=cassette))


def _claim_value_judge_for(case: EvalCase):
    """Build the ``ClaimValueJudge`` for the gray-zone value check (None => none).

    Mirrors ``_claim_extractor_for`` but for the narrow same-slot value-incompatibility
    decision, with its own committed verdict cassette (the ``conflict`` dir).
    """
    if not case.conflict_model:
        return None
    from knowledge.knowledge_graph.write_policy.write_step_variants import ClaimValueJudge
    from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm
    from knowledge.llm.verdict_cassette import VerdictCassette

    has_key = bool(os.getenv("OPENROUTER_API_KEY"))
    llm = OpenRouterLlm(model=case.conflict_model) if has_key else None
    cache = VERDICT_CACHE_DIR / "conflict" / f"{_slug(case.conflict_model)}.json"
    cassette = (
        VerdictCassette(cache, model_id=case.conflict_model, allow_compute=has_key)
        if (cache.exists() or has_key)
        else None
    )
    # The value judge may legitimately be absent (numeric clashes need no LLM);
    # precision-first suppression handles a missing judge.
    return ClaimValueJudge(llm=llm, cassette=cassette)


def _caption_captioner_for(case: EvalCase):
    """Build the image ``Captioner`` for a case's ``caption_model`` axis (None => none).

    Mirrors ``_merge_judge_for``: a live OpenRouter VLM when a key is set, plus a
    committed caption cassette for offline replay. With neither, returns a captioner
    that yields ``None`` (deterministic-only cards) — though such a case SKIPs via
    ``real_captions`` before it runs.
    """
    if not case.caption_model:
        return None
    from knowledge.injestion.image.captioner import PROMPT_VERSION, make_captioner
    from knowledge.llm.caption_cassette import CaptionCassette

    has_key = bool(os.getenv("OPENROUTER_API_KEY"))
    cache = CAPTION_CACHE_DIR / f"{_slug(case.caption_model)}.json"
    cassette = (
        CaptionCassette(
            cache,
            model_id=case.caption_model,
            prompt_version=PROMPT_VERSION,
            allow_compute=has_key,
        )
        if (cache.exists() or has_key)
        else None
    )
    return make_captioner(model=case.caption_model, cassette=cassette, has_key=has_key)


def _image_asset_dirs(case: EvalCase) -> list[Path]:
    """Resolve ``via_image_ingestor`` entries to absolute fixture-relative dirs."""
    if not case.seeded_insight.via_image_ingestor or not case.source_dir:
        return []
    fixture = Path(case.source_dir) / "fixture"
    return [fixture / entry for entry in case.seeded_insight.via_image_ingestor]


def _seed_image_assets(case: EvalCase, graph) -> None:
    """Ingest the case's image-asset folders into ``graph`` as active knowledge."""
    dirs = _image_asset_dirs(case)
    if not dirs:
        return
    from knowledge.injestion.injestor_variants.image_injestor import ImageIngestor

    captioner = _caption_captioner_for(case)
    img_ingestor = ImageIngestor(graph, captioner=captioner)
    for d in dirs:
        img_ingestor.ingest(str(d), state="active")


def _aspect_tagger_for(case: EvalCase):
    """Build the Tier-B ``AspectTagger`` for a case's ``tag_model`` axis (None => none).

    Mirrors ``_conflict_judge_for``: a live OpenRouter judge when a key is set, plus
    a committed aspect verdict cassette for offline replay. With neither, returns
    None so no tagger is wired (the case SKIPs via ``tag_verdicts``).
    """
    if not case.tag_model:
        return None
    from knowledge.knowledge_graph.write_policy.write_step_variants import (
        AspectJudge,
        AspectTagger,
    )
    from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm
    from knowledge.llm.verdict_cassette import VerdictCassette

    has_key = bool(os.getenv("OPENROUTER_API_KEY"))
    llm = OpenRouterLlm(model=case.tag_model) if has_key else None
    cache = VERDICT_CACHE_DIR / "aspect" / f"{_slug(case.tag_model)}.json"
    cassette = (
        VerdictCassette(cache, model_id=case.tag_model, allow_compute=has_key)
        if (cache.exists() or has_key)
        else None
    )
    if llm is None and cassette is None:
        return None
    return AspectTagger(judge=AspectJudge(llm=llm, cassette=cassette))


def _build_trio_for(case: EvalCase, llm=None):
    """Wire the trio honoring reader/embedder/ingest_model/merge/conflict/tag axes."""
    llm = _ingest_llm_for(case, llm)
    embedder = _eval_embedder(case)
    graph = None
    if case.substrate == "vector" and case.embedder != "fake":
        # Real-embedder cases seed with redact + dedup; the Tier-B AspectTagger
        # (before dedup, so tags are assigned ahead of recall) and the ConflictFlagger
        # are added only when the case opts into tag_model / conflict_model (their
        # cassettes replay offline), so seeding stays cheap by default.
        from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import VectorGraph
        from knowledge.knowledge_graph.write_policy.write_step_variants import (
            ClaimConflictDetector,
            Deduper,
            Redactor,
        )

        policy = [Redactor()]
        aspect_tagger = _aspect_tagger_for(case)
        if aspect_tagger is not None:
            policy.append(aspect_tagger)
        policy.append(Deduper(judge=_merge_judge_for(case)))
        # Structural contradiction path (replaces ConflictFlagger): extract claims,
        # then detect same-functional-slot value clashes. Both opt in via conflict_model.
        claim_extractor = _claim_extractor_for(case)
        if claim_extractor is not None:
            policy.append(claim_extractor)
            policy.append(ClaimConflictDetector(judge=_claim_value_judge_for(case)))
        graph = VectorGraph(embedder=embedder, policy=policy)
    return build_trio(
        substrate=case.substrate,
        graph=graph,
        llm=llm,
        reader=case.reader,
        embedder=embedder,
        reader_top_k=case.reader_top_k,
        reader_abs_floor=case.reader_abs_floor,
        reader_rel_ratio=case.reader_rel_ratio,
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
        # Seed state changes what's retrievable, so two cases differing only in
        # ingest_state must not share a cached seeded reader.
        "ingest_state": case.ingest_state,
        "reader": case.reader,
        "reader_top_k": case.reader_top_k,
        "reader_abs_floor": case.reader_abs_floor,
        "reader_rel_ratio": case.reader_rel_ratio,
        # Judge axes change the seeded graph (merge collapses dups; conflict flags),
        # so two cases differing only in a judge model must not share a cached seed.
        "merge_model": case.merge_model,
        "conflict_model": case.conflict_model,
        "tag_model": case.tag_model,
        "caption_model": case.caption_model,
        "via_ingestor": list(case.seeded_insight.via_ingestor),
        "direct_to_graph": list(case.seeded_insight.direct_to_graph),
        "via_image_ingestor": list(case.seeded_insight.via_image_ingestor),
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
    #   * via_ingestor    -> case.ingest_state ("proposed" default): the system
    #     passively distilling raw input. A case whose seeded docs are established
    #     background (e.g. matt/applications) sets ingest_state: active so the
    #     distilled facts are retrievable rather than staged out of the reader.
    for text in case.seeded_insight.direct_to_graph:
        graph.write(text, state="active")
    for text in case.seeded_insight.via_ingestor:
        ingestor.ingest(text, state=case.ingest_state)
    # Image assets: distilled to derived cards and written active (explicit adds).
    _seed_image_assets(case, graph)

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


def _all_facts_text(graph) -> str:
    """All stored fact texts, regardless of lifecycle state.

    Retrieval is gated to ``active`` facts, but the knowledge_graph/ingestion
    component tests inspect what was *stored* (writes land as ``proposed``), so they
    look at every fact rather than the active-gated ``read`` view.
    """
    facts = getattr(graph, "facts", None)
    if facts is not None:
        return "\n\n".join(f.text for f in facts)
    return graph.read()


def _contradictions_summary(graph) -> str:
    """Render the graph's flagged contradictions as greppable lines (empty if none).

    Conflict cases assert the ConflictFlagger fired; the flag lives on a fact, not
    in its text, so surface ``graph.contradictions()`` into the graded output. A
    pure superset: graphs with no flagged pair add nothing.
    """
    pairs = graph.contradictions() if hasattr(graph, "contradictions") else []
    if not pairs:
        return ""
    lines = [
        f"CONTRADICTION: {p.flagged.text!r} contradicts {p.conflicting.text!r}" for p in pairs
    ]
    return "\n\n" + "\n".join(lines)


def _produce_knowledge_graph(case: EvalCase, runner: Runner, llm=None) -> EvalContext:
    """Component: write the seeded ``direct_to_graph`` lines, inspect stored facts."""
    graph, _, _ = _build_trio_for(case, llm=llm)
    for text in case.seeded_insight.direct_to_graph:
        graph.write(text)
    output = _all_facts_text(graph) + _contradictions_summary(graph)
    return EvalContext(case_id=case.id, output=output)


def _produce_ingestion(case: EvalCase, runner: Runner, llm=None) -> EvalContext:
    """Component: ingest the seeded ``via_ingestor`` lines, inspect stored facts."""
    graph, ingestor, _ = _build_trio_for(case, llm=llm)
    for text in case.seeded_insight.via_ingestor:
        ingestor.ingest(text)
    return EvalContext(case_id=case.id, output=_all_facts_text(graph))


def _produce_graph_reader(case: EvalCase, runner: Runner, llm=None) -> EvalContext:
    """Component: seed the graph (raw docs via the ingestor and/or pre-distilled
    facts written direct), then retrieve via the reader (``seed_prompt`` as the
    situation/query). Seeding ``via_ingestor`` lets a reader case exercise the
    real *write-intent -> gated-read* path; ``direct_to_graph`` seeds pre-curated
    facts for tests that isolate the reader.
    """
    graph, ingestor, reader = _build_trio_for(case, llm=llm)
    for text in case.seeded_insight.via_ingestor:
        ingestor.ingest(text)  # staged (proposed) -> gated out of retrieval by design
    for text in case.seeded_insight.direct_to_graph:
        graph.write(text, state="active")  # pre-curated: retrievable, so the cutoff (not gating) filters
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
