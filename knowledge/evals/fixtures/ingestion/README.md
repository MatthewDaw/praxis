# `fixtures/ingestion/` — committed ingestion replay cassettes

One JSON file per ingest model, `<model-slug>.json` (e.g. `openai_gpt-4o-mini.json`),
mapping `sha256(model_id + "\n" + raw_input) -> distilled_text`. It lets the eval
harness replay the ingestion splitter's `raw input -> distilled text` step offline and
deterministically, with **zero** live calls — the same committed / model-keyed /
loud-miss contract the embedding cache (`fixtures/embeddings/`) and the verdict
cassettes (`fixtures/verdicts/`) use, one layer up. See
[`knowledge/llm/ingestion_cassette.py`](../../../llm/ingestion_cassette.py).

A miss under replay-only (no key) is a **loud `RuntimeError`**, never a silent stale
pass: a changed seeded input or `ingest_model` is a new key, so it fails until refreshed.

## Refresh order (record locally, with a key) — order matters

The ingestion cassette must be refreshed **before** the embedding cache, because the
embedding key includes the distilled text: fix the text first, then embed the stable
strings. Reversing the order caches vectors for soon-to-be-stale text.

```bash
# 1. Fix the distilled text.
OPENROUTER_API_KEY=… uv run python -m knowledge.evals.ingestion_cache --refresh
# 2. Embed the now-stable strings.
OPENROUTER_API_KEY=… uv run python -m knowledge.evals.embed_cache --refresh
# commit fixtures/ingestion/* and fixtures/embeddings/*
```
