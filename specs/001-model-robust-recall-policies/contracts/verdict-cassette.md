# Contract: verdict cassette (P2 + P3)

A committed, model-keyed replay store for nondeterministic judge calls — the `CachedEmbedder` pattern applied to merge/conflict verdicts. One implementation parameterized by `kind` (`merge` | `conflict`) and verdict codec.

**Path**: `knowledge/evals/fixtures/verdicts/<kind>/<model-slug>.json`
**Key**: `sha256(judge_model_id + "\n" + payload)` where payload is the ordered note pair text.
**On-disk**: JSON `{ key -> verdict }`, sorted keys for stable diffs.

## Behavior (mirrors CachedEmbedder)
| Situation | Action |
|-----------|--------|
| key in cassette | replay the recorded verdict (offline, deterministic) |
| miss + `allow_compute` (key present) | call the live judge, record, `save()` |
| miss + recording disabled | **loud error** (stale fixture / changed text or model) |
| no cassette + no key | **skip** — caller degrades (exact dedup only / no conflict flag) |

## Invariants
- **Model-keyed**: swapping the judge model is a clean miss, never silent staleness.
- **Concurrency-safe save**: `save()` merges with on-disk state under a process lock (parallel `--workers` cases sharing a cassette can't clobber each other) — same fix already applied to `CachedEmbedder`.
- **Regeneration**: a `--refresh` regenerator (mirrors `embed_cache.py`) rebuilds the cassette from the live judge with a key, then commit.
- **CI**: with cassettes committed, dedup/conflict evals run with zero live calls and a stale fixture is surfaced 100% of the time. *(SC-008)*

## Ordering with embeddings (note)
For application/full-pipeline cases the cassette inputs depend on ingested text; deterministic ingestion is a prerequisite there (research R11) — irrelevant for component cases that seed verbatim text.
