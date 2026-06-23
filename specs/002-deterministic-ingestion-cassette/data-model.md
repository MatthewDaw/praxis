# Phase 1 Data Model: Deterministic Ingestion Cassette

New and touched entities. The feature adds one persisted artifact (the ingestion cassette) and one
in-process wrapper; everything else is existing infrastructure it composes with.

## `IngestionCassette` (**NEW**)

A keyed-replay store for the ingestion splitter's `str → str` output. The fourth keyed-replay
surface alongside `CachedEmbedder`, the merge `VerdictCassette`, and the conflict `VerdictCassette`.

| Field | Type | Notes |
|-------|------|-------|
| `path` | Path | committed fixture, `knowledge/evals/fixtures/ingestion/<model-slug>.json` |
| `model_id` | str | the `ingest_model`; part of the key — a model swap is a clean miss |
| `inner` | `Callable[[str], str] \| None` | the live ingest callable; `None` in replay-only mode |
| `allow_compute` | bool | record on miss only when a key (and `inner`) is present |
| `_cache` | dict[str, str] | replayed map: `key → output_text` |

**Key**: `sha256(f"{model_id}\n{raw_input}")` — identical to `CachedEmbedder._key`, over the raw
ingestion input rather than the embedded text.

**Value**: the distilled output text verbatim (the atomic-insight string block the splitter emits).
JSON map `key → text`, sorted keys for stable diffs.

**Behavior** (mirrors `CachedEmbedder`):

| Condition | Outcome |
|-----------|---------|
| key in cache | replay `output_text` (deterministic, offline, no call) |
| miss + `allow_compute` + `inner` | call live, record under key, save, return |
| miss + recording disabled | **loud `RuntimeError`** with a refresh instruction (no silent stale/empty) |
| no cassette + no key (caller-level) | the case is **skipped** (graceful degradation) |

**Invariants**:
- Replay makes **zero** live ingestion calls.
- The same `(model_id, raw_input)` always yields the same `output_text` within a committed fixture.
- A changed seeded input or `ingest_model` → a new key → a miss → loud (never a stale reuse).

## `CachedEmbedder` (existing) — downstream consumer

`(model, text) → vector` committed cache. Today the application suite can't use it because the
embedded `text` drifts. Once the ingestion cassette stabilizes the distilled strings, the embedded
text is stable, so application cases flip `embedder: live → cached` and this cache replays for them.
**Refresh order**: ingestion cassette first (fix the text), embedding cache second (embed the
stable text).

## Application eval case (existing, `EvalCase`) — touched

The primary beneficiary. No schema change; one field value changes per case.

| Field | Change |
|-------|--------|
| `embedder` | `live → cached` for every `matt/applications/*` case that sets `ingest_model` |
| `ingest_model` | unchanged — its presence is what scopes a case onto the ingestion cassette |

A case requiring cassetted ingestion is **skipped** when neither a committed ingestion cassette nor
a key is available (a new `ingest_verdicts`-style capability gate in the harness, parallel to the
embedding/verdict capabilities).

## Relationships

```text
raw seeded input ──▶ IngestionCassette ──▶ distilled text ──▶ CachedEmbedder ──▶ vector ──▶ graph
   (case YAML)        (NEW: stabilize)       (now stable)       (now usable)              (reproducible)
```

The cassette sits one layer above the embedding cache; making its output deterministic is the
precondition that makes the embedding cache usable for the application suite.
