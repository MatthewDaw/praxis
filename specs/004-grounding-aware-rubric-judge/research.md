# Phase 0 Research: Grounding-aware rubric judge

This feature is an integration with the existing eval harness, so "research" here grounds decisions in the current code. The two `/speckit-clarify` answers (criterion-text-driven grading + neutral label; widen-don't-retire) settled the load-bearing questions; no `NEEDS CLARIFICATION` remained.

## Decision 1 — Where and how the reference is threaded

**Decision**: Build the reference in `grade_rubric` and pass it to the judge as a new optional `reference: str | None` parameter; do not add a field to `EvalContext`.

**Rationale**: `grade_rubric(case, ctx)` already receives the full `case` (`run.py:120-126`) and calls `judge(case.rubric, ctx)`. The `case` carries `seeded_insight.via_ingestor` / `direct_to_graph` (`eval_def.py:99-103, 142`). `EvalContext` (`eval_def.py:182-196`) is explicitly run *provenance* for the transcript and carries no seed — adding a seed field there would overload its purpose and thread data through the runner unnecessarily. Passing `reference` at the existing call site is the minimal, local change. The `RubricJudge` type alias (`run.py:117`) widens to accept the optional reference.

**Alternatives considered**: Add `ground_truth` to `EvalContext` (rejected — wrong layer; the runner would have to populate it though it isn't run output). Re-derive the seed inside the judge (rejected — the judge doesn't have the case).

## Decision 2 — What the reference contains

**Decision**: `reference = "\n\n".join([*case.seeded_insight.via_ingestor, *case.seeded_insight.direct_to_graph])`. Empty string → `None` → omit the REFERENCE block.

**Rationale**: Matches the proposal exactly. `via_ingestor` + `direct_to_graph` are the **raw seeded source** (`SeededInsight`, `eval_def.py:99-103`), the truest reference — not the distilled graph facts (distillation can drop/rephrase) and not `ctx.injected_knowledge` (the reader-retrieved subset, which would false-flag true claims outside the retrieved set). When both lists are empty (the 18 reference-free cases), there is no block and the prompt is byte-identical to today (FR-005 / SC-004 no-regression).

**Alternatives considered**: `ctx.injected_knowledge` (rejected — reintroduces the §2 false-positive bug). Distilled graph facts (rejected — lossy).

## Decision 3 — Prompt change: neutral label, criterion-text-driven, no blanket rule

**Decision**: Add a REFERENCE block to the judge prompt, labeled neutrally as the seeded background/source material the scenario was built from, and instruct the judge to grade each criterion per its own text using the reference as context. Do **not** add a blanket "any claim not supported by the reference is a failure" instruction.

**Rationale**: This is the core clarify outcome. Today the prompt is `"...RUBRIC:\n{items}\n\nARTIFACT:\n{ctx.output}\n"` (`openrouter.py:340-345`; identical in `claude_code.py:318-321`). A blanket "must match the reference" frame would mis-penalize the safety/override case (the agent is *supposed* to override a seeded rule) and the conflict cases (ignore a low-confidence rumor, follow active over retired). The criterion text already states the policy (`grounded`, `honest`, `ignores_graph_rule`, "flags the conflict"), so supplying the reference + deferring to the criterion is sufficient and safe. The label must not assert authority, or the override case backslides.

**Alternatives considered**: Global strictness instruction scoped to grounding criteria by name (rejected during clarify — requires classification; criterion text already carries the policy). Per-criterion `kind` tag in rubrics (rejected — would edit rubric data, violating FR-011).

## Decision 4 — Both judges, structured output preserved

**Decision**: Apply the identical `reference` parameter + prompt block to `OpenRouterJudge.__call__` (`openrouter.py:338`) and `ClaudeCodeJudge.__call__` (`claude_code.py:314`). Keep the per-rubric `rubric_score_schema` structured output on both.

**Rationale**: FR-009 — results must not depend on which judge ran. Both already build the same `RUBRIC + ARTIFACT` prompt and both use `rubric_score_schema` (OpenRouter `response_format`; Claude `--json-schema`), so the change is symmetric. The schema is unaffected (scores are still keyed by rubric item id).

## Decision 5 — Widen, not retire, deterministic checks

**Decision**: Broaden the brittle literal-keyword checks on affected cases — they are `regex_matches` / `requires_all_substrings` predicates (`deterministic_checks/text.py:33-101`) invoked from case YAML with literal params (e.g. a `(?i)rag` pattern). Widen by broadening the pattern/substring set to accept synonyms/paraphrases, or by adding a small synonym-tolerant check helper to `text.py`. Keep the checks in place.

**Rationale**: Clarify decision (widen + keep as a reproducible backstop; retirement is a conditional follow-up, FR-010b). The checks are generic predicates over `ctx.output`, so widening is mostly per-case param edits; a reusable `mentions_any` / synonym helper is optional if several cases share the need. Determinism is a virtue here (more stable run-to-run than the LLM judge), so they stay.

**Alternatives considered**: Remove redundant checks now (rejected by clarify — premature; the grounding judge isn't yet validated as a replacement).

## Risks & validation (from the proposal §5)

- **Judge model**: decoupled via `OPENROUTER_JUDGE_MODEL` (`openrouter.py:334-336`), falling back to `OPENROUTER_MODEL` / `gpt-4o-mini`. A stronger judge (e.g. `gpt-4.1`) is defensible for long-reference claim verification; pick empirically by whether it catches the seeded-hallucination control. Not fixed by this feature (spec Assumptions).
- **Cost**: full-seed references grow judge input (~4–5k tokens/case for matt); accepted. If trimming is needed, drop obvious boilerplate from the raw docs — never substitute the retrieved subset.
- **Nondeterminism**: the judge is a live model; reference-aware grading is more stable than token-matching but not deterministic. A verdict cassette over the judge (like the merge/conflict cassettes) is a possible later refinement, out of scope here.
- **Validation gate**: re-run the 16 affected cases; confirm (a) a deliberately-ungrounded control scores low on `grounded`/`honest` and the `safety_user_overrides_graph` rule-ignore is now actually checked, and (b) widened deterministic checks pass synonyms while still failing wrong answers, before considering any retirement.

## Open items deferred to `/speckit-tasks` / implementation

- Exact wording of the neutral REFERENCE label + criterion-deference instruction (validate empirically against the hallucination control).
- Whether a shared `mentions_any`/synonym helper is added vs. per-case param widening only.
- Numeric thresholds for "scores low" / "clear separation" used in the validation assertions (set empirically; spec leaves them as relative outcomes).
- Judge-model selection for the validation run (`gpt-4.1` vs `gpt-4o`/`-mini`).
