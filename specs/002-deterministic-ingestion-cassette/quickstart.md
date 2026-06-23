# Quickstart: Deterministic Ingestion Cassette

How to build, record, and verify the feature. Commands are POSIX (Git Bash).

## Prerequisites

- This branch is **stacked on `001-us3-tier-b-implicit-contradiction`** (the 001 stack tip), so the
  reused 001 code (embed-once write path, `VerdictCassette` sibling, `CachedEmbedder`, the `dom/`
  eval namespace) is already present — implementation can proceed now. The 002 PR merges after the
  001 stack lands on `main`.
- `OPENROUTER_API_KEY` in `.env` — only for recording fixtures; CI / offline runs don't need it.
- `uv` for running.

## Build order (TDD)

1. **Red tests first** — `knowledge/tests/test_ingestion_cassette.py`: replay hit (no live call),
   record-on-miss (with stub `inner`), loud-miss when recording disabled, skip-when-no-source,
   record-then-replay round-trip, model-id-in-key.
2. **`IngestionCassette`** (`knowledge/llm/ingestion_cassette.py`) — near-copy of `CachedEmbedder`
   with a `str → str` value codec; make the red tests pass.
3. **Wire** into `run._ingest_llm_for` (wrap the ingest callable, parallel to `_eval_embedder`'s
   `cached` branch) + add the `ingest_replay` capability/skip gate.
4. **Regenerator** `knowledge/evals/ingestion_cache.py --refresh` (mirror `embed_cache.py`).
5. **Flip** every `matt/applications/*` `ingest_model` case `embedder: live → cached`.

## Record the fixtures (local, with key) — order matters

```bash
# 1. Fix the distilled text.
OPENROUTER_API_KEY=… PHOENIX_COLLECTOR_ENDPOINT= uv run python -m knowledge.evals.ingestion_cache --refresh
# 2. Embed the now-stable strings.
OPENROUTER_API_KEY=… PHOENIX_COLLECTOR_ENDPOINT= uv run python -m knowledge.evals.embed_cache --refresh
# commit fixtures/ingestion/* and fixtures/embeddings/*
```

## Verify offline (zero live calls)

```bash
# Replay-only: empty key so dotenv won't override -> loud miss on any gap.
OPENROUTER_API_KEY= PHOENIX_COLLECTOR_ENDPOINT= uv run pytest knowledge/tests/test_ingestion_cassette.py -q
# Run an application case twice and assert identical graph facts both times, no live calls.
OPENROUTER_API_KEY= PHOENIX_COLLECTOR_ENDPOINT= uv run python -m knowledge.evals.run --fake <application_case_id>
```

## Success signals (from spec Success Criteria)

- Re-running an `ingest_model` case's ingestion twice offline → identical distilled facts. *(SC-001)*
- Offline application run on committed fixtures → zero live ingestion/embedding calls for graph
  construction. *(SC-002, SC-005)*
- A changed seeded input or `ingest_model` → loud miss, never a silent stale pass. *(SC-003)*
- Every `ingest_model` application case runs on `cached` deterministically. *(SC-004)*
- A deterministic component case seeded from cassetted real distillation replays offline. *(SC-006)*

## Out of scope / follow-ups

- **Second FR-030/SC-013 prerequisite** — active-fact retrievability (`ingest_state: active`) — is
  the explicit next follow-up, its own unit; 002 alone does not fully unblock FR-030/SC-013.
- **Unified keyed-replay abstraction** across the four cassette surfaces — deferred until the
  shared shape is extracted as its own refactor.
- Application cases still run a live agent + judge — not made deterministic end-to-end here.
