# Contract: judge schemas (P2 + P3)

Both judges are structured LLM calls over the existing `OpenRouterLlm` seam, replayed offline from a verdict cassette. No cosine threshold decides either outcome.

## MergeJudge (P2)
**Question**: do these two notes record the SAME lesson, just phrased differently? If yes, which EXISTING note survives **verbatim**? Do not rewrite either.

**Schema**:
```json
{ "same_lesson": "boolean", "keep_id": "string | null" }
```
- `same_lesson=true` → `WriteDecision.action="update"`, `update_target_id=keep_id`.
- `same_lesson=false` → add as new fact.
- `keep_id` MUST be an existing candidate fact id (the verbatim survivor), never new text. *(FR-010, FR-011)*

## ConflictFlagger (P3)
**Question**: does the NEW note contradict the EXISTING note?

**Schema**:
```json
{ "contradicts": "boolean", "target_id": "string | null" }
```
- `contradicts=true` → append `contradiction:<target_id>` flag (note kept, flagged for review).
- Replaces the prior free-text `answer.startswith("yes")` parse. *(FR-018, FR-020)*

## Common contract
- **Determinism**: every call goes through the verdict cassette (see verdict-cassette.md). Key includes the judge model id.
- **Graceful skip**: no key + no cassette → judge returns "no decision"; caller falls back (exact dedup only / no conflict flag). *(FR-014, FR-019)*
- **Backend**: `OpenRouterLlm` (LLM judge is the default). A cross-encoder is a documented cost-driven fallback only — it cannot pick the verbatim survivor or explain itself, so it is **not** part of this feature.
