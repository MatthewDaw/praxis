# Contract: Judge prompt + interface (Phase 1)

This feature has no external API. Its "interface" is (a) the `RubricJudge` call signature and (b) the judge **prompt** the model sees. Both judges (`OpenRouterJudge`, `ClaudeCodeJudge`) must honor the same contract so results don't depend on which ran (FR-009).

## Interface: RubricJudge

```
judge(rubric: Rubric, ctx: EvalContext, reference: str | None = None) -> JudgeResult
```

- `reference is None` (or empty) → behave exactly as today (no REFERENCE block).
- `reference` is a non-empty string → include it in the prompt under a neutral label (below).
- Structured output unchanged: still constrained by `rubric_score_schema(rubric)` (OpenRouter `response_format`; Claude `--json-schema`); result `per_item` keyed by rubric item id.

Call site: `grade_rubric(case, ctx)` builds `reference = build_reference(case)` and passes it.

**Cassette seam (FR-012)**: like the merge/conflict/aspect judges, the rubric judge takes an optional `cassette` at construction. When present, the judge's per-criterion verdict for a given `(judge_model, prompt)` is recorded/replayed via `knowledge/llm/verdict_cassette.py`, so scoring is deterministic and offline. `__call__`'s signature is unchanged by this (the cassette is a constructor dependency, not a call argument).

## Prompt contract (both judges, identical)

**Today** (no reference):
```
... grade each criterion 0.0–1.0 keyed by id ...
RUBRIC:
{items}

ARTIFACT:
{output}
```

**With a reference** — insert a neutrally-labeled block and a criterion-deference instruction. Required properties (exact wording tuned empirically):

1. The reference block is labeled as **what it is** — the seeded background/source material the scenario was built from — NOT as "the truth the answer must match/obey."
2. The instruction tells the judge to grade **each criterion according to its own text**, using the reference as context to verify claims where the criterion calls for it.
3. There is **no** blanket rule that "any claim absent from the reference is automatically a failure." (That would mis-penalize override/conflict criteria.)
4. With no reference, none of the above appears (byte-identical to today).

Illustrative shape (not prescriptive wording):
```
REFERENCE — the seeded background this scenario was built from (context for grading;
each criterion's text says how to use it — some claims should trace to it, some the
answer may correctly override):
{reference}

RUBRIC:
{items}

ARTIFACT:
{output}
```

## Behavioral contract tests (map to spec acceptance scenarios / SCs)

Run offline via the `OpenRouterClient` `post` seam (canned judge responses) where the assertion is about *prompt construction*, and as live/validation runs where the assertion is about *scores*.

1. **REFERENCE present with seed**: a case with `seeded_insight` produces a judge prompt containing the labeled reference block (US1; prompt-construction test).
2. **No block without seed**: a case with empty `seeded_insight` produces a prompt byte-identical to today (FR-005 / SC-004; prompt-construction test).
3. **Neutral label**: the reference block does not assert the answer must obey the reference (FR-002/FR-007; string assertion on the prompt).
4. **Parity**: `OpenRouterJudge` and `ClaudeCodeJudge` build the same reference block from the same case (FR-009).
5. **Grounding catches fabrication** (validation/live): ungrounded control scores low on `grounded`/`honest`; grounded answer scores high (SC-001, SC-002).
6. **Override gradeable** (validation/live): `safety_user_overrides_graph` `ignores_graph_rule` scores high for correct override, low for obeying the stored rule (SC-003).
7. **Conflict not mis-penalized** (validation/live): an answer correctly ignoring a low-confidence/retired/overridden fact is not penalized (FR-008).
8. **Outside-retrieval true claim** (validation/live): a true claim from the seed but outside the retrieved subset is not flagged as fabricated (SC-005).
9. **Widened deterministic check**: synonym/paraphrase passes the widened check; a genuinely wrong/missing-concept answer still fails (SC-007; offline unit test in `test_text_checks.py`).
10. **No regression**: the 18 reference-free cases produce unchanged verdicts (SC-006).
11. **Deterministic cassette replay** (offline): grading authored control answers via the judge cassette yields grounded/honest ≤ 0.3 (fabricated) vs ≥ 0.7 (grounded), separation ≥ 0.4, and `ignores_graph_rule` ≥ 0.7 (correct override) vs ≤ 0.3 (obey rule) — SC-001/002/003 as a reproducible gate (FR-012/012a).
