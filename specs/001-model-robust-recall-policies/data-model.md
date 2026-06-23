# Phase 1 Data Model: Model-Robust Recall Policies

Entities the feature touches or adds. Existing types are extended in place; new types are marked **NEW**.

## Read path

### Cutoff policy config (on `RetrievingReader`)
The reader's system contract; production defaults.
| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `top_k` | int | 8 | volume cap (backstop) |
| `abs_floor` | float | ~0.30 | existence floor; below it → dropped. `0` disables (isolation) |
| `rel_ratio` | float | ~0.75 | keep hits `>= rel_ratio * top_score`. `0` disables (isolation) |

Removed: `min_score` (subsumed by `abs_floor`).
**Validation**: `0 ≤ abs_floor`, `0 ≤ rel_ratio ≤ 1`, `top_k ≥ 1`. Apply order is fixed: floor → relative → cap.

### `SearchHit` (existing, unchanged)
`{ fact: Fact, score: float }` — similarity, higher = better. The cutoff operates only on these scores (no model call).

## Write path

### `Fact` (existing)
`id, text, embedding, flags[], observation_count, confidence, scope, metadata`. **Tier B adds** `tags: list[str]` (controlled-vocabulary aspect labels). Merge bumps `observation_count` and keeps `text` verbatim.

### `WriteDecision` (existing, extended)
| Field | Type | Change |
|-------|------|--------|
| `text` | str | — |
| `embedding` | Vector \| None | **NEW** — incoming text embedded once in `write`, shared by recall pass + judges + persist (FR-015) |
| `action` | "add" \| "update" | — |
| `update_target_id` | str \| None | merge survivor id |
| `dropped` | bool | — |
| `flags` | list[str] | conflict flags appended here |

### Recall candidate set (**NEW**, transient)
Result of the single per-write `most_similar` pass: `list[SearchHit]` filtered by one shared `recall_floor`. Consumed by both `MergeJudge` and `ConflictFlagger`. **Tier B (conflict only):** unioned with `same-tag` candidates.

## Judges & verdicts

### `MergeVerdict` (**NEW**)
`{ same_lesson: bool, keep_id: str | None }`. `same_lesson=true` → `decision.action="update"`, `update_target_id=keep_id`. Keyed by `sha256(judge_model + textA + textB)`.

### `ConflictVerdict` (**NEW**, replaces free-text parse)
`{ contradicts: bool, target_id: str | None }`. `contradicts=true` → append `contradiction:<target_id>` flag. Keyed by `sha256(judge_model + existing + new)`. Replaces today's `answer.startswith("yes")`.

### `VerdictCassette` (**NEW**)
| Field | Type | Notes |
|-------|------|-------|
| `path` | Path | `knowledge/evals/fixtures/verdicts/<kind>/<model-slug>.json` |
| `model_id` | str | part of the key; model swap → clean miss |
| `allow_compute` | bool | record on miss only when a key is present |
| `_cache` | dict[str, verdict] | replayed map |

**Behavior** (mirrors `CachedEmbedder`): hit → replay; miss + `allow_compute` → call live judge, record, save (merge-on-disk under lock); miss + not allowed → **loud error**; no cassette + no key → **skip** (caller degrades gracefully).

### Aspect tag (**NEW, Tier B, gated**)
Controlled-vocabulary label (`code-quality-tradeoff`, `deploy-policy`, …) assigned to a `Fact` at write time by the policy LLM. Used only as a second recall key on the conflict path. Governed by a seed/normalize step to avoid fragmentation.

## Eval schema (`EvalCase`)

| Field | Change |
|-------|--------|
| `reader_min_score` | **removed** (subsumed by `reader_abs_floor`) |
| `reader_abs_floor` | **NEW** — per-case override (isolation: set `0` to disable floor) |
| `reader_rel_ratio` | **NEW** — per-case override (isolation: set `0` to disable relative cutoff) |
| `reader_top_k` | existing |

These overrides are **mechanism-isolation knobs only** (neutralize one mechanism to test another), never to tune a pass.

## Eval case states (reconciliation)
`PASS` (asserts real behavior) · `XFAIL` (control / honest can't-yet) · `XPASS` (flip-and-promote) · **provisional** (undecided, e.g. near-only `scattered_multifact`, Tier-B implicit-contradiction cases pending the gate).
