# Contract: RetrievingReader cutoff (P1)

**Interface**: `RetrievingReader(graph, *, top_k=8, abs_floor=0.30, rel_ratio=0.75).read(context) -> str`

## Behavior
Given retrieved `SearchHit`s for the query, apply in fixed order:
1. **Floor (existence):** drop hits with `score < abs_floor`.
2. **Relative (shape):** if any remain, let `top = max score`; keep hits with `score >= rel_ratio * top`.
3. **Cap (volume):** keep at most `top_k`.

Return the surviving fact texts concatenated. Operates only on existing `SearchHit.score`s — **no second model call**.

## Invariants
- **Empty on no-match:** if the best hit `< abs_floor`, return empty (nothing injected downstream). *(FR-002, SC-002)*
- **Keep-all-relevant:** weaker-but-relevant facts within `rel_ratio` of the best survive. *(FR-003, SC-001)*
- **Drop-irrelevant-present:** retrieved hits well below the best are dropped. *(FR-004)*
- **Model-robust:** swapping the embedding model preserves the relevant/irrelevant split without changing a precise separating value (only coarse values may change). *(FR-001, SC-003)*
- **Idempotent on scores:** same hits → same output.

## Overrides (isolation only)
`reader_abs_floor=0` disables the floor (forces the relative cutoff to do the work — e.g. `lost_in_middle_reader`). `reader_rel_ratio=0` disables the relative cutoff (forces the floor alone — negative-control/existence cases). Production defaults exercise both (integration). Overrides MUST NOT be used to tune a pass. *(FR-005, FR-006)*

## Out of scope
Wiring `RetrievingReader` into the production serve path (separate decision). Global enablement on the application suite requires the relative cutoff + check recalibration (see research R3) and deterministic ingestion (R11).
