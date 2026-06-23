# Quickstart: Grounding-aware rubric judge

How to build and verify this feature. It is eval-infra only — no product/runtime surface.

## Prerequisites

- `uv` (Python ≥3.12). Judge unit tests run fully offline via the injected `post` seam — no API key needed.
- For the validation runs (scoring real cases), an `OPENROUTER_API_KEY` and optionally `OPENROUTER_JUDGE_MODEL` (e.g. `openai/gpt-4.1`) to pick the judge model. The Claude judge needs the Claude CLI.

## Where the change lives

- `knowledge/evals/run.py` — `grade_rubric(case, ctx)` builds the reference from `case.seeded_insight` and passes it; `RubricJudge` type alias gains the optional `reference`.
- `knowledge/evals/openrouter.py` — `OpenRouterJudge.__call__` accepts `reference`, adds the labeled REFERENCE block.
- `knowledge/evals/claude_code.py` — `ClaudeCodeJudge.__call__` — same change (parity).
- `knowledge/evals/deterministic_checks/text.py` (+ affected `cases/**/case.yaml`) — widen the brittle keyword checks.

## TDD order (write failing tests first)

1. **Prompt construction (offline)** — in `test_openrouter.py` / `test_claude_code.py`, feed a fake `post`/CLI and assert: with a seeded case the prompt contains the neutrally-labeled reference block; with an empty seed the prompt is unchanged; the label never says the answer must obey the reference.
2. **Reference threading** — in `test_run.py`, assert `grade_rubric` builds `reference` from `via_ingestor` + `direct_to_graph` and passes it to the judge (and passes `None` when empty).
3. **Widened checks** — in `test_text_checks.py`, assert a synonym/paraphrase passes the widened check and a wrong/missing-concept answer still fails.

Then implement until green.

## Run the tests

```bash
uv run pytest knowledge/evals -q
# Targeted:
uv run pytest knowledge/evals/tests/test_openrouter.py knowledge/evals/tests/test_run.py -q
uv run pytest knowledge/evals/tests/test_text_checks.py -q
```

## Validation runs (the real proof — needs a key)

Re-run the 16 affected cases and confirm the grounding signal is real:

```bash
# Pick a judge model for the run (empirical choice):
export OPENROUTER_JUDGE_MODEL=openai/gpt-4.1   # or gpt-4o / gpt-4o-mini to compare
uv run python -m knowledge.evals.run --scope matt/applications   # 14 grounded/honest cases
# plus matt_volta_video_mock and safety_user_overrides_graph
```

Confirm:
- A deliberately ungrounded control answer scores **low** on `grounded`/`honest`; a grounded answer scores high (SC-001, SC-002).
- `safety_user_overrides_graph` `ignores_graph_rule` now scores high for correct override, low for obeying the stored rule (SC-003).
- An answer correctly ignoring a low-confidence/retired/overridden fact is not penalized (FR-008).
- A true claim from the seed but outside the retrieved subset is not flagged as fabricated (SC-005).
- The 18 reference-free cases are unchanged (SC-006 / SC-004).
- At least one widened deterministic check passes a synonym while still failing a wrong answer (SC-007).

## Done / acceptance

- `uv run pytest knowledge/evals -q` green, including the new prompt-construction, threading, and widened-check tests.
- The validation runs above show clear grounded-vs-fabricated separation and a gradeable safety-override criterion, with no regression on reference-free cases.
- No rubric criteria edited; no agent/runner behavior changed (FR-011); no deterministic checks removed (FR-010b).
