# Contract: ingestion-cassette regenerator

`knowledge/evals/ingestion_cache.py --refresh` — mirrors `embed_cache.py --refresh`.

## Command

```bash
# Local, with a key. Records the ingestion cassette for every ingest_model case.
OPENROUTER_API_KEY=… uv run python -m knowledge.evals.ingestion_cache --refresh
```

## Behavior

1. Require `OPENROUTER_API_KEY`; without it, exit non-zero with a clear message (no silent no-op).
2. **Delete** the model's cassette file first, so orphaned keys (from edited/removed seeded inputs)
   are dropped — start-from-empty, same as `embed_cache.py`.
3. Re-run every case with `ingest_model` set, driving its ingestion so the recording cassette
   captures exactly the inputs those cases distill.
4. Persist the cassette to `knowledge/evals/fixtures/ingestion/<model-slug>.json` for commit.

## Two-step refresh ordering (FR-009)

The application suite needs both the ingestion cassette and the embedding cache regenerated, in
this order:

```bash
# 1. Fix the distilled text first.
OPENROUTER_API_KEY=… uv run python -m knowledge.evals.ingestion_cache --refresh
# 2. Then embed the now-stable strings.
OPENROUTER_API_KEY=… uv run python -m knowledge.evals.embed_cache --refresh
# commit knowledge/evals/fixtures/ingestion/* and knowledge/evals/fixtures/embeddings/*
```

Reversing the order caches vectors for soon-to-be-stale text. Following the order yields a
self-consistent fixture pair (SC, FR-009).

## Validation

- After a refresh, an offline run (`OPENROUTER_API_KEY=` unset) of the `ingest_model` cases makes
  zero live ingestion/embedding calls (SC-002) and reproduces identical graphs (SC-001).
