# Contract: eval-harness wiring

How the cassette plugs into `knowledge/evals/run.py`, mirroring the embedder wiring.

## Ingest callable wrap (parallel to `_eval_embedder`)

`_ingest_llm_for(case, llm)` already returns a `str → str` callable when `case.ingest_model` is set.
Wrap it so ingestion replays from the committed cassette:

```text
_ingest_llm_for(case):
  if not case.ingest_model: return passthrough            # unchanged
  has_key  = bool(OPENROUTER_API_KEY)
  inner    = live OpenRouter str→str callable if has_key else None
  cassette = INGEST_CACHE_DIR / f"{slug(ingest_model)}.json"
  return IngestionCassette(cassette, model_id=ingest_model,
                           inner=inner, allow_compute=has_key)   # replay / record / loud-miss
```

- Mirrors `_eval_embedder`'s `cached` branch exactly (`CachedEmbedder(live, cache, model_id, allow_compute)`).
- `PromptIngestor` is unchanged — still handed a `str → str` callable (FR-006).

## Capability gate / skip (parallel to embeddings + verdicts)

A new harness capability — e.g. `ingest_replay` — is provided when a committed ingestion cassette
exists for the model **or** a key is present; a case that sets `ingest_model` requires it. With
neither, the case is **skipped** (graceful), exactly like `real_embeddings` / `merge_verdicts` /
`conflict_verdicts`. (FR-005.)

## Application-case axis flip

Every `matt/applications/*` case that sets `ingest_model` flips `embedder: live → cached` (FR-008,
SC-004). No other case-schema change; `ingest_model` stays as-is.

## Invariants

- An offline application run on committed fixtures makes zero live ingestion **or** embedding calls
  for graph construction (SC-002, FR-010).
- The change is confined to the eval harness; no production ingestion/serve path is touched (FR-012).
