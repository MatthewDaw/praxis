# Implementation Plan: Deterministic Ingestion Cassette

**Branch**: `002-deterministic-ingestion-cassette` | **Date**: 2026-06-23 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/002-deterministic-ingestion-cassette/spec.md`

## Summary

Make the application eval suite's **ingestion → embedding → graph-construction** layer
deterministic and cheap by recording the ingestion LLM's `str → str` output into a committed,
model-keyed **replay cassette** and replaying it offline — the exact committed / keyed / loud-miss
contract `CachedEmbedder` already uses, one layer up. With ingested text stable, all
`matt/applications/*` cases that use `ingest_model` flip from the uncached `embedder: live` to the
committed-vector `cached` path, so a graph-construction run on committed fixtures makes zero live
calls. Scope is eval-infrastructure determinism only; the live agent + judge stay nondeterministic.

## Technical Context

**Language/Version**: Python 3.12

**Primary Dependencies**: existing eval harness (`knowledge/evals`), `OpenRouterLlm` /
`OpenRouterEmbedder`, the `CachedEmbedder` keyed-replay pattern (`knowledge/llm/embedder_variants/`)
and the `embed_cache.py` regenerator. No new third-party dependency.

**Storage**: committed JSON fixtures on disk (`knowledge/evals/fixtures/ingestion/<model-slug>.json`),
mirroring `fixtures/embeddings/` and `fixtures/verdicts/`.

**Testing**: `pytest` (offline replay is the default; recording is gated on `OPENROUTER_API_KEY`).

**Target Platform**: developer machines + CI; offline replay with no network.

**Project Type**: single Python package — eval infrastructure (no UI, no service surface).

**Performance Goals**: zero live ingestion/embedding calls for the graph-construction layer on
replay (down from 2–3 live embeds per write under `live`).

**Constraints**: offline-deterministic; model-keyed; **loud miss** on a stale/uncached fixture
when recording is disabled; graceful skip when no cassette and no key; do not change how cases are
authored (the distiller keeps its `str → str` contract).

**Scale/Scope**: the `matt/applications/*` suite (all `ingest_model` cases) plus the new ingestion
cassette + the embedding vectors for the now-stable distilled strings.

**Sequencing**: this branch is **stacked on `001-us3-tier-b-implicit-contradiction`** (the tip of
the 001 stack), so the 001 code it reuses — the embed-once write path, the `VerdictCassette`
keyed-replay sibling, the `CachedEmbedder` pattern, and the `dom/` eval namespace — is already
present and **implementation can proceed now**. It edits `knowledge/evals/run.py`, which 001 also
rewrites, so building on the 001 tip avoids those conflicts. The 002 PR **merges after** the 001
stack lands on `main` (stacked PR; rebase/retarget onto `main` once 001 merges).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` is an unratified template (placeholders only), so this gates
against the project's established de-facto principles (the same set 001's plan used).

| Principle | Status | Evidence |
|-----------|--------|----------|
| **Reuse over invention** | ✅ PASS | `IngestionCassette` is a near-copy of `CachedEmbedder` (same key/loud-miss/record contract); regenerator mirrors `embed_cache.py`; wiring mirrors `_eval_embedder`'s `cached` branch. No new abstraction. |
| **Simplicity / YAGNI** | ✅ PASS | One `str → str` cassette + one regenerator + a wiring wrap. The unified keyed-replay abstraction across the four surfaces is **explicitly deferred** (spec Assumptions; research R5) until three instances exist. |
| **Test-First (TDD)** | ✅ PASS | Cassette replay / record / loud-miss / skip have unit tests written before the implementation (project house style); a stub-LLM record-then-replay test. |
| **Offline determinism** | ✅ PASS | The whole point: committed fixtures replay with zero live calls; loud-miss prevents silent stale passes (SC-002, SC-003). |
| **Surgical, honest scope** | ✅ PASS | Eval-harness only (FR-012); partial determinism is stated, not oversold (FR-011); the second FR-030/SC-013 prerequisite is explicitly out of scope and named as the next follow-up. |

**Result**: PASS — no violations, Complexity Tracking not required.

## Project Structure

### Documentation (this feature)

```text
specs/002-deterministic-ingestion-cassette/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── ingestion-cassette.md
│   ├── regenerator-cli.md
│   └── eval-wiring.md
└── checklists/
    └── requirements.md   # from /speckit-specify
```

### Source Code (repository root)

```text
knowledge/
├── llm/
│   └── ingestion_cassette.py           # NEW — IngestionCassette (str→str keyed replay),
│                                       #   sibling to embedder_variants/cached_embedder.py
│                                       #   and llm/verdict_cassette.py
├── evals/
│   ├── run.py                          # EDIT — wrap the ingest str→str callable in the cassette
│   │                                   #   (parallel to _eval_embedder's `cached` branch)
│   ├── ingestion_cache.py              # NEW — `--refresh` regenerator (mirror embed_cache.py)
│   ├── fixtures/
│   │   └── ingestion/<model-slug>.json # NEW — committed ingestion replay cassette
│   └── cases/matt/applications/**/case.yaml  # EDIT — flip embedder: live → cached
└── tests/ (+ per-package tests/)
    └── test_ingestion_cassette.py      # NEW — replay / record / loud-miss / skip / ordering
```

**Structure Decision**: Single Python package, existing layout. The cassette lands beside the
established keyed-replay siblings (`CachedEmbedder`, `VerdictCassette`); the regenerator and the
fixture dir mirror the embedding cache's; the only edits to shipped code are the wiring wrap in
`run.py` and the `embedder` axis flip in the application case YAMLs.

## Complexity Tracking

> No Constitution Check violations — section intentionally empty.
