# Implementation Plan: Model-Robust Recall Policies for the Knowledge Graph

**Branch**: `001-model-robust-recall-policies` | **Date**: 2026-06-22 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/001-model-robust-recall-policies/spec.md`

## Summary

Replace three brittle, model-pinned cosine constants in the knowledge graph with model-robust recall policies, and reconcile the eval cluster that bets on them. Three independently-shippable slices:

1. **Read path (P1):** `RetrievingReader` gains a layered cutoff — absolute floor → relative-to-best ratio → volume cap — replacing the single `min_score`, embedding-model-robust and able to return nothing.
2. **Write-path dedup (P2):** `Deduper` becomes a loose recall gate + an LLM `MergeJudge` (keep the verbatim survivor), replayed offline from a committed merge-verdict cassette.
3. **Write-path unification & conflict (P3):** `Deduper` + `ConflictFlagger` share one candidate-recall pass (embed once per write), `ConflictFlagger` emits structured output replayed from a conflict-verdict cassette, plus a **gated** Tier-B experiment (aspect tags for implicit-contradiction recall) and a documented Tier-C residual.

Technical approach reuses existing praxis patterns verbatim: the LLM-in-write-policy precedent (`ConflictFlagger(llm=...)`), the committed model-keyed cassette pattern (`CachedEmbedder` / `embed_cache.py`), and the per-case eval axis pattern (`reader_top_k`).

## Technical Context

**Language/Version**: Python 3.12

**Primary Dependencies**: stdlib + `pydantic` (eval/case models), `pyyaml` (cases), OpenRouter HTTP seam (`knowledge/llm/openrouter_http.py`) for live embeddings + LLM; no new third-party deps anticipated.

**Storage**: In-process `VectorGraph` (list of facts) per eval-case lifecycle; committed JSON fixtures under `knowledge/evals/fixtures/` (embeddings today; merge/conflict verdicts added by this feature).

**Testing**: `pytest` (`uv run pytest`); offline determinism via injected fakes (`FakeEmbedder`, `FakeLlm`) and committed cassettes.

**Target Platform**: Local + CI (offline, no live model calls); developer machines with `OPENROUTER_API_KEY` for fixture regeneration.

**Project Type**: Single Python package (`knowledge/`) — library + eval harness. No web/mobile/frontend.

**Performance Goals**: Write path embeds the incoming text **exactly once** per write (FR-015/SC-007, down from 2–3×); eval suite runs deterministically offline with zero live calls when fixtures are present.

**Constraints**: Model-robustness (survive an embedding-model swap without re-tuning a precise separating value); honest evals (assert real shipped behavior, no per-case-tuned constants); graceful degradation when no key/cassette (skip, never mis-run).

**Scale/Scope**: ~90 eval cases; write path is per-insight (many per ingest). Numeric defaults calibrated against the committed `text-embedding-3-small` cache.

**Dependency / sequencing note**: FR-030/SC-013 (application-suite validation) require **deterministic ingestion**, tracked separately in [`docs/proposals/completed/2026-06-22-deterministic-ingestion-cassette.md`](../../docs/proposals/completed/2026-06-22-deterministic-ingestion-cassette.md). That cassette is a **prerequisite** for relying on the application suite as a measurement instrument but is **out of scope** for this plan. The component-level cases (reader-isolation, dedup, conflict) are deterministic today via seeded text + committed cassettes and are the primary verification surface here.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

The project constitution (`.specify/memory/constitution.md`) is an unfilled template — no ratified principles to gate against. Falling back to the project's engineering guidelines (`CLAUDE.md` — Karpathy principles):

| Principle | Status | Notes |
|-----------|--------|-------|
| **Simplicity first** | ✅ PASS | Reuses existing patterns (cassette, LLM-write-step, eval axis); no new abstractions beyond `MergeJudge` (a sibling of `ConflictFlagger`). |
| **Surgical changes** | ✅ PASS | Touches `RetrievingReader`, `Deduper`, `ConflictFlagger`, `VectorGraph.write`, `EvalCase`, `build_trio`, and the affected cases — each traceable to a requirement. |
| **Test-first (TDD)** | ✅ PASS (planned) | Mechanism-isolation tests (FR-027) authored red before implementation; cassette replay/loud-miss tests per cassette. |
| **Reuse over invention** | ✅ PASS | Merge/conflict cassettes mirror `CachedEmbedder`; recall gate reuses `most_similar`. |
| **Observability (LangChain)** | ✅ RESOLVED | Global guideline prefers LangChain for LLM observability; praxis standardizes on Phoenix via `knowledge/observability/tracing.py`. The new judges route through the existing `OpenRouterLlm` seam, inheriting that tracing. **Owner decision (2026-06-22): praxis uses Phoenix; LangChain is not adopted.** No new observability stack introduced. |

**One architectural change worth flagging (see Complexity Tracking):** embed-once-per-write (FR-015) requires the candidate-recall vector to be reused for both judges *and* persistence — which makes the write path vector-aware rather than re-deriving the embedding in each step.

**Gate result: PASS** — no unjustified violations.

## Project Structure

### Documentation (this feature)

```text
specs/001-model-robust-recall-policies/
├── plan.md              # This file
├── spec.md              # Feature spec (+ clarifications, known gaps)
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output (interface contracts)
│   ├── reader-cutoff.md
│   ├── write-policy-recall.md
│   ├── judge-schemas.md
│   └── verdict-cassette.md
├── checklists/
│   └── requirements.md  # spec quality checklist (existing)
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
knowledge/
├── graph_reader/
│   ├── grapher_reader_variants/
│   │   └── retrieving_reader.py        # P1: floor → relative → cap
│   └── graph_reader_def.py
├── knowledge_graph/
│   ├── knowledge_graph_variants/
│   │   └── vector_graph.py             # P3: one candidate-recall pass, embed-once
│   └── write_policy/
│       └── write_step_variants/
│           ├── deduper.py              # P2: recall gate (rename threshold→recall_floor)
│           ├── merge_judge.py          # P2: NEW — LLM same-lesson judge
│           ├── conflict_flagger.py     # P3: structured output, shared recall, cassette
│           └── aspect_tagger.py        # P3 Tier B (gated): NEW — write-time tags
├── llm/
│   ├── llm_variants/openrouter_llm.py  # reused judge backend
│   └── verdict_cassette.py             # P2/P3: NEW — text/struct keyed replay (mirrors CachedEmbedder)
├── wiring.py                           # P1: thread reader_abs_floor / reader_rel_ratio
└── evals/
    ├── eval_def.py                     # P1: add reader_abs_floor/reader_rel_ratio (subsume reader_min_score)
    ├── run.py                          # thread new axes; verdict cassette wiring
    ├── fixtures/
    │   ├── embeddings/                 # existing
    │   └── verdicts/                   # NEW — merge/conflict cassettes (model-keyed)
    └── cases/                          # reconcile reader + dedup + conflict cluster
```

**Structure Decision**: Single existing Python package (`knowledge/`). The feature extends established modules in place and adds two new files (`merge_judge.py`, `verdict_cassette.py`) plus one gated file (`aspect_tagger.py`). No new top-level structure.

## Complexity Tracking

> Only one item rises above "extend an existing pattern."

| Decision | Why Needed | Simpler Alternative Rejected Because |
|----------|------------|-------------------------------------|
| Make the write path **vector-aware** (compute the incoming embedding once in `VectorGraph.write`, thread it through the shared recall pass and `_add`) | FR-015/SC-007 require embedding the text exactly once per write; today `Deduper`, `ConflictFlagger`, and `_add` each re-embed via `search(text)` | Keeping `most_similar(text)` and memoizing per-call was considered, but it leaves the store-time re-embed and two search embeds unattributable; threading the vector is the only way to guarantee "exactly once" and is the natural shape for the shared candidate-recall pass (FR-015 unification). |
| New `MergeJudge` write step (vs folding into `Deduper`) | Keeps the precision decision (LLM) cleanly separable from the recall gate; mirrors `ConflictFlagger` | Inlining the LLM call into `Deduper` couples recall and precision and blocks the FR-015 shared-judge surface. |
