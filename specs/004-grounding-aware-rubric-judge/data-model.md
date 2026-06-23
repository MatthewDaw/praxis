# Phase 1 Data Model: Grounding-aware rubric judge

No persistent data and no schema changes. The "model" here is the existing eval-case/judge data shapes and the new derived **reference** that flows into the judge. All types already exist in `knowledge/evals/eval_def.py`.

## Existing shapes (unchanged)

- **EvalCase** (`eval_def.py:112`): the case. Relevant fields: `rubric: Rubric | None`, `seeded_insight: SeededInsight`, `deterministic_checks: list[DeterministicCheckRef]`. No new fields.
- **SeededInsight** (`eval_def.py:99`): `via_ingestor: list[str]`, `direct_to_graph: list[str]` — the raw seeded source. **This is the reference source.**
- **Rubric / RubricItem** (`eval_def.py:31-39`): `RubricItem{ id, criterion, weight }`. The `criterion` text is the grading policy — no `kind`/classification field is added (clarify decision).
- **EvalContext** (`eval_def.py:182`): run provenance — `output`, `injected_knowledge` (retrieved subset), `artifacts`, etc. **Unchanged**; not used as the reference (`injected_knowledge` is explicitly the wrong source).
- **JudgeResult** (`eval_def.py:209`): `overall`, `per_item`, `raw_response`. Unchanged — scores stay keyed by rubric item id.

## New derived value: the judge reference

- **reference: str | None** — computed, not stored. Definition:
  `reference = "\n\n".join([*case.seeded_insight.via_ingestor, *case.seeded_insight.direct_to_graph])`, normalized to `None` when empty.
- **Producer**: `grade_rubric(case, ctx)` in `run.py` (a `build_reference(case)` helper, likely in `eval_def.py` or `run.py`).
- **Consumer**: `RubricJudge.__call__(rubric, ctx, reference=None)` — both `OpenRouterJudge` and `ClaudeCodeJudge`.
- **Validation rules**:
  - When the seed is empty → `reference is None` → judge omits the REFERENCE block (FR-005, no regression).
  - The reference is always the raw seed — never `ctx.injected_knowledge`, never distilled facts (FR-002).
  - The reference is rendered into the prompt under a **neutral** label (background/source material), never as "authoritative truth the answer must obey" (FR-002, FR-007).

## Interface change: the RubricJudge protocol

- **Before**: `RubricJudge = Callable[[Rubric, EvalContext], JudgeResult]` (`run.py:117`); judges called as `judge(case.rubric, ctx)`.
- **After**: judges accept an optional `reference: str | None = None`; called as `judge(case.rubric, ctx, reference=build_reference(case))`. The type alias widens accordingly. Default `None` preserves any caller that doesn't pass a reference (back-compatible).

## Affected cases (data, not schema)

The 16 cases whose criteria need the reference (spec §Scope): the 14 `matt/applications/*` (`grounded` + `honest`), `matt_volta_video_mock` (`factual_grounding`), and `safety_user_overrides_graph` (`ignores_graph_rule` — the seeded rule must appear in the reference so the judge can confirm it was correctly overridden). The other 18 rubric cases have empty/irrelevant seeds → no REFERENCE block.

Deterministic-check widening touches the brittle `regex_matches` / `requires_all_substrings` params on the affected cases (per-case YAML), and optionally adds one synonym-tolerant helper in `deterministic_checks/text.py`. No model/schema change.
