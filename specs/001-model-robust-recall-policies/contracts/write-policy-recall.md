# Contract: write-policy candidate recall (P2 + P3)

**Interface**: `VectorGraph.write(content) -> None`, running `Redactor → Deduper → ConflictFlagger` then persist.

## Behavior (after this feature)
1. **Embed once.** `write` computes the incoming text's embedding a single time and carries it on `WriteDecision.embedding`. *(FR-015, SC-007)*
2. **Exact-match short-circuit.** Byte-identical existing fact → merge (bump `observation_count`), done. *(FR-008)*
3. **One candidate-recall pass.** A single `most_similar` over the shared vector, filtered by one shared `recall_floor` (loose, high-recall). The resulting candidate set feeds **both** judges. *(FR-009, FR-015, FR-016)*
4. **Merge decision (Deduper + MergeJudge).** For candidates, `MergeJudge` decides `{same_lesson, keep_id}`. `same_lesson` → `action="update"`, merge into the verbatim survivor. *(FR-010, FR-011)*
5. **Conflict decision (ConflictFlagger).** Skipped if `action=="update"` (merge precedence). Otherwise structured `{contradicts, target_id}` per candidate; `contradicts` → append `contradiction:<id>` flag. *(FR-017, FR-018)*
6. **Persist** reusing `WriteDecision.embedding` (no re-embed at store time). *(FR-015)*

## Invariants
- **Exactly one embedding of the incoming text per write**, shared by merge, conflict, and persistence. Empty-graph first write embeds at most once and issues no candidate search it knows returns nothing. *(SC-007)*
- **Single shared recall floor** replaces the prior inconsistent `0.95` (dedup) / `0.6` (conflict). *(FR-016)*
- **Merge before conflict;** a merged dup triggers zero conflict checks. *(FR-017, SC-007)*
- **Verbatim survivor:** merge never rewrites text. *(FR-011)*
- **No over-merge:** distinct ideas kept; a distinct-ideas guard catches regressions. *(FR-012, SC-005)*
- **Graceful degradation:** no judge available (no key, no cassette) → semantic merge/conflict skipped, exact dedup still applies, affected eval SKIPs. *(FR-014)*

## Tier B (gated, conflict path only)
Conflict candidates = `cosine-kNN ∪ same-tag`. Bounded (cap same-tag candidates per write). Kept only if the kill/keep gate clears. *(FR-021, FR-022, FR-023)*
