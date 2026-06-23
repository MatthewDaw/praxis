# Quickstart: Model-Robust Recall Policies

How to build, calibrate, and verify the feature. All commands POSIX (Git Bash).

## Prerequisites
- `OPENROUTER_API_KEY` in `.env` (only for calibrating defaults and regenerating cassettes; CI/offline runs don't need it).
- `uv` for running (`uv run pytest`, `uv run python -m ...`).

## Build order (matches the three slices)
1. **P1 — reader cutoff**: `RetrievingReader` floor→relative→cap; add `reader_abs_floor`/`reader_rel_ratio` to `EvalCase` (remove `reader_min_score`); thread through `wiring.build_trio` + `run._build_trio_for`.
2. **P2 — dedup**: `verdict_cassette.py`; `merge_judge.py`; `Deduper` recall gate (`threshold`→`recall_floor`) + judge; merge cassette.
3. **P3 — unify + conflict**: vector-aware `VectorGraph.write` (embed once, one recall pass, shared floor, merge-before-conflict); structured `ConflictFlagger` + conflict cassette. Then **Tier B (gated)**: `aspect_tagger.py` + same-tag recall union + implicit-contradiction eval set + gate measurement.

## Verify (TDD order)

**Mechanism-isolation tests first (red), per the spec's isolation matrix:**
```bash
# Reader: relative-drop (floor off), relative-keep-all (floor off), floor-empties (ratio off), integration
uv run pytest knowledge/graph_reader -q
# Write policy: stub-judge merge true/false + keep_id + distinct-preserved; recall-gate→judge ordering;
# embed-once-per-write; cassette replay / loud-miss / record; skip-when-no-key
uv run pytest knowledge/knowledge_graph knowledge/llm -q
```

**Calibrate defaults against the committed cache** (proposal §2 grounding): set `abs_floor≈0.30`, `rel_ratio≈0.75`, `recall_floor≈0.45`; document the model on the reader/steps. Confirm `lost_in_middle_reader` (with `reader_abs_floor: 0`) drops CloudFront/X-Ray/SES and `scattered_multifact` keeps all relevant.

**Regenerate cassettes** (local, with key), then commit:
```bash
uv run python -m knowledge.evals.embed_cache --refresh        # if seeded texts changed
uv run python -m knowledge.evals.verdict_cache --refresh      # NEW: merge + conflict verdicts
```

**Run the reconciled cluster offline (deterministic):**
```bash
uv run python -m knowledge.evals.run --structured             # component + reader + dedup/conflict cases
```
Expect: `ingestion_merge_near_dupes` + `skills_merge_dedup` flip XFAIL→PASS; `lost_in_middle_reader` PASS (no PROVISIONAL); `reader_returns_all` → `_before` XFAIL control (+ `after` PASS unless redundant); `scattered_multifact` far-only PASS, near-only provisional; negative-control/no-leak pass as floor tests.

## Success signals (from spec Success Criteria)
- Reader: 100% relevant / 0% irrelevant on a relevant query; empty on no-match; split holds across a model swap. *(SC-001..003)*
- Dedup: one verbatim survivor, 0% over-merge; the two XFAILs pass. *(SC-004..006)*
- Write: exactly one embedding per write; merged dup → zero conflict checks; offline determinism with loud stale-miss. *(SC-007, SC-008)*
- Tier B: gate measured and a keep/kill decision recorded. *(SC-010)*

## Out of scope / prerequisites
- Application-suite validation (FR-030/SC-013) is gated on the **deterministic-ingestion cassette** (separate proposal). Until then, verify on component-level cases.
- Wiring `RetrievingReader` into the serve path is a separate decision.
