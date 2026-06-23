# Implementation Plan: Grounding-aware rubric judge

**Branch**: `004-grounding-aware-rubric-judge` | **Date**: 2026-06-23 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/004-grounding-aware-rubric-judge/spec.md`

## Summary

The rubric judge grades each criterion from `RUBRIC + the answer only` — it never sees the case's seeded reference, so grounding/honesty criteria score plausibility instead of support (a documented hallucination scored ~0.85 on `grounded`). The fix: thread the case's **full raw seed** into the judge as a neutrally-labeled reference and let each criterion's own text drive grading. No classification mechanism, no blanket "must match the reference" rule (that would mis-penalize the safety/override and conflict cases). Apply identically to the OpenRouter and Claude judges. Separately, **widen** (don't remove) the brittle literal-keyword deterministic checks so correct answers aren't failed on phrasing.

Technical approach: `grade_rubric(case, ctx)` already has the `case` in scope, so it builds the reference from `case.seeded_insight` and passes it to the judge via a new optional `reference` parameter; the judges add the labeled REFERENCE block to their prompt (omitted when the seed is empty → today's behavior, no regression). Deterministic-check widening is per-case edits to the brittle check params (plus, if needed, a synonym-tolerant check helper).

## Technical Context

**Language/Version**: Python ≥3.12

**Primary Dependencies**: stdlib only for the judge HTTP path (`urllib`); pydantic v2 for the case/result models; the Claude judge shells out to the Claude CLI with `--json-schema`. OpenRouter chat-completions for the OpenRouter judge.

**Storage**: N/A — eval cases are YAML on disk under `knowledge/evals/cases/`; results are JSON/JSONL under `knowledge/evals/results/`. No database.

**Testing**: pytest. Judge HTTP is injected (`post` seam in `OpenRouterClient`) so judge tests run fully offline with canned responses.

**Target Platform**: Local / CI eval harness (developer machines + CI). Not a runtime/product surface.

**Project Type**: Single Python project (eval infrastructure inside `knowledge/evals/`). No frontend.

**Performance Goals**: Not latency-sensitive. The one cost consideration is judge input size: passing the full raw seed grows judge prompt tokens (~4–5k input/case for the matt cases) — accepted as the price of correctness (spec Assumptions).

**Constraints**: No edits to rubric criteria; no change to agent/runner behavior; no regression on the 18 reference-free cases; the reference must be the raw seed (never the retrieved subset, never distilled facts) and labeled neutrally (never as authoritative truth the answer must obey).

**Scale/Scope**: 34 rubric cases, 16 affected. Two judges. ~3 source files for the reference path + per-case widening of a handful of brittle checks.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

The project constitution (`.specify/memory/constitution.md`) is an **unpopulated template** — no ratified principles. Applying default engineering gates (simplicity/YAGNI, test-first, surgical changes):

- **Simplicity**: PASS — threads a reference through an existing call site, adds one optional parameter to two judges, and reuses the existing `seeded_insight` data and `rubric_score_schema`. No new abstractions, no classification layer (explicitly rejected during clarify).
- **Test-First & Determinism**: PASS (planned) — offline judge tests (via the `post` seam) assert the REFERENCE block is present/absent and that prompts defer to criterion text; widened-check tests assert synonym-pass + wrong-answer-fail. The grounding/honesty/safety **score** criteria are made reproducible offline via a **judge-verdict cassette** (extending `knowledge/llm/verdict_cassette.py`) replayed over authored fixed control answers — so the headline SCs satisfy Principle II's deterministic/offline mandate rather than relying only on live runs.
- **Surgical changes**: PASS — touches the judge prompt path, the `grade_rubric` reference build, and the specific brittle checks; leaves runner/agent and rubric text untouched (FR-011).

No violations → Complexity Tracking empty.

## Project Structure

### Documentation (this feature)

```text
specs/004-grounding-aware-rubric-judge/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   └── judge-prompt.md  # Phase 1 output — the judge prompt/interface contract
└── checklists/
    └── requirements.md  # from /speckit-specify
```

### Source Code (repository root)

```text
knowledge/evals/
├── run.py                         # grade_rubric(case, ctx): build reference from case.seeded_insight,
│                                  #   pass to judge; RubricJudge type alias gains optional reference
├── openrouter.py                  # OpenRouterJudge.__call__: accept reference, add labeled REFERENCE block
├── claude_code.py                 # ClaudeCodeJudge.__call__: same change for parity
├── eval_def.py                    # (likely) a small build_reference(case) helper; models unchanged
├── deterministic_checks/
│   └── text.py                    # widen brittle checks; possibly add a synonym-tolerant helper
├── verdict_cache.py               # extend the --refresh recorder to the rubric judge
├── ../llm/verdict_cassette.py     # extend VerdictCassette to key rubric-judge verdicts
├── cases/
│   ├── matt/applications/**/case.yaml              # 14 grounded/honest cases (affected)
│   ├── matt/volta_video/** (matt_volta_video_mock) # factual_grounding (affected)
│   └── **/safety_user_overrides_graph**            # ignores_graph_rule (affected; seed the rule into reference)
└── tests/
    ├── test_openrouter.py         # judge prompt: REFERENCE present w/ seed, absent w/o (offline)
    ├── test_claude_code.py        # parity assertions for the Claude judge
    ├── test_run.py                # grade_rubric threads reference from the case
    └── test_text_checks.py        # widened checks: synonym passes, wrong answer fails
```

**Structure Decision**: Single-project eval-infra change confined to `knowledge/evals/`. No new modules or directories; the work is a prompt/interface change plus per-case check widening.

## Key Design Decisions (see research.md for detail)

1. **Thread the reference at the call site, not via `EvalContext`.** `grade_rubric(case, ctx)` already holds the `case`; build the reference there and pass it as a new optional `reference` parameter on the judge. `EvalContext` (run provenance for the transcript) stays unchanged.
2. **Reference = `"\n\n".join([*seeded_insight.via_ingestor, *seeded_insight.direct_to_graph])`** — the full raw seed. Empty → `None` → omit the REFERENCE block (no-regression path, FR-005).
3. **No classification; criterion text drives grading.** The prompt adds a neutrally-labeled REFERENCE block and instructs the judge to grade each criterion per its own text using the reference as context — explicitly NOT a blanket "any claim not in the reference fails" rule (clarify decision; protects the safety/override + conflict cases).
4. **Both judges, identical change.** `OpenRouterJudge` and `ClaudeCodeJudge` get the same parameter and prompt block; keep the per-rubric `rubric_score_schema` structured output (FR-009).
5. **Widen, don't retire, deterministic checks.** Broaden the brittle `regex_matches`/`requires_all_substrings` params on affected cases (or add a synonym-tolerant check helper); keep the checks as a reproducible backstop (FR-010/010a/010b).
6. **Judge-verdict cassette + authored control answers (FR-012/012a).** Extend the existing `VerdictCassette` to key the rubric judge's per-item scores by `(judge_model, prompt)`; plumb a `cassette` seam into both judges and the rubric-judge construction in `run.py`; extend the `verdict_cache.py --refresh` recorder. Deterministic SC validation feeds **authored fixed control answers** (grounded + fabricated/rule-obeying) as committed fixtures and replays the cassette — no live runner output needed. Live runs remain only to record the cassette and for broad empirical tuning. Provisional thresholds: ungrounded ≤ 0.3, grounded ≥ 0.7, separation ≥ 0.4.

## Complexity Tracking

> No constitution violations — section intentionally empty.
