# `dom/` — Dominic's eval cases (feature 001: model-robust recall policies)

Eval cases authored/reconciled for [`specs/001-model-robust-recall-policies`](../../../../specs/001-model-robust-recall-policies/spec.md),
grouped by user story. Case **ids** are unchanged by the move — the runner
discovers cases via a recursive `case.yaml` glob and keys everything off the `id:`
field, not the path (`knowledge/evals/run.py::load_cases`). Mirrors the existing
per-owner `matt/` namespace.

| Subfolder | Story | What it pins |
|-----------|-------|--------------|
| `reader_cutoff/` | US1 | `RetrievingReader` floor→relative→cap: relevant kept, distractors dropped, no-match empty (`lost_in_middle_reader` + `_before` control, `scattered_multifact` + `_near`, `reader_returns_all_before`, `negative_control_irrelevant`, `context_budget_overload`) |
| `semantic_dedup/` | US2 | paraphrase merge into one verbatim survivor via the recall gate + `MergeJudge`, replayed from the committed merge cassette (`ingestion_merge_near_dupes`, `skills_merge_dedup`) |
| `contradiction/conflict/` | US3 Tier A | structured `ConflictFlagger` flags a negation contradiction from the conflict cassette (`conflict_should_flag`) |
| `contradiction/implicit/` | US3 Tier B (kept) | implicit (disjoint-vocab, below-floor) contradictions surfaced via `AspectTagger` same-tag recall — 7 PASS + 1 documented XFAIL residual (`implicit_conflict_*`) |

Tier-B gate metrics: `uv run python -m knowledge.evals.tier_b_metrics`.

Not moved here (pre-existing benchmark, not feature-authored): `ingestion_dedup`
(exact-match arm), `lost_in_middle` / `_before` (full-pipeline agent versions),
`contradiction_should_flag`, `scoped_conflict`.
