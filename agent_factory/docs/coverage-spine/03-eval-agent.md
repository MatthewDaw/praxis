# The Eval Agent (plan-reproduction / coverage eval)

> Companion to [`00-overview.md`](00-overview.md). The eval that proves the planner has no holes.
> Lives in `evals/plan_repro/` (see its `README.md`).

## What it proves
Given the raw product docs in `docs/inspiration/`, the planning process must produce a plan
whose **feature set has no holes** vs. the hand-refined golden (`prd-team-app`). Archetypal
hole: the raw spec has signup/login/logout but **no password reset** — the refined plan does.
A reproduced plan that omits it (or the consent gate, minor flow, loading/empty states, …)
**fails**. Real job: the **regression net** for de-hardcoding the planning checklist (see
`02-planner.md`) — it proves the generalized, checklist-driven planner stays hole-free.

## Why a new lane (not a `case.yaml`)
The deterministic suite (`evals/cases/<component>/case.yaml`) feeds fixed input to a registered
`Gate.evaluate()` and its loader **rejects non-deterministic input**. This eval runs an LLM
planning process + a fuzzy coverage judge, so it gets its own lane: `evals/plan_repro/`.

## Inputs & target
- **Inputs:** `docs/inspiration/*.txt` (the raw PRD set).
- **Golden (target):** `evals/plan_repro/team-app/golden-features.yaml` — 78 features in 15
  epics, extracted from the live `prd-team-app` graph on 2026-06-26. `derived: true` flags the
  11 features the raw PRD never stated (the eval's teeth).

## Scoring (coverage / no-holes)
- Each golden feature must be **covered by ≥1 candidate feature**, judged **semantically** —
  *variant wording passes*; this is "is this represented at all," not text-equivalence.
- **PASS = zero holes**, especially zero missed `derived` features.
- Report per golden feature → `covered | missing | variant` + the matched candidate text.
  Extra candidate features (not in golden) are reported but do **not** fail (over-coverage is
  allowed; holes are not). Coverage can be scored MVP-only or full (post-MVP features tagged).

## `coverage.py` — build it as the SHARED engine
Do **not** build a planning-only checker. Build a general coverage engine, because the
validation gate is the same spine (see `00-overview.md`):

```
coverage(set, target, item_evaluator) -> per-item report -> zero-hole pass/fail
```
- **Instantiation #1 (now):** planning feature-coverage — set = golden features, target =
  reproduced plan, evaluator = semantic match (lexical pre-filter narrows, LLM judge confirms).
- **Instantiation #2 (later):** validation — set = Praxis validation checks, target = code,
  evaluator = run-test / agent-eval.
- Remediation stays in each skill (planner: ask/expand; coder: test/fix).

## Two halves
1. **Coverage checker** *(buildable now)* — load golden + a candidate feature list → report +
   pass/fail. The fuzzy match is the pluggable judge. Can score a *recorded* candidate today.
2. **Plan-production run** *(follow-up)* — orchestrate `af-plan` → `af-intake`
   (which now owns admission + planning validation) over `docs/inspiration/` to generate the
   candidate, making it end-to-end.

## Eval-agent loop (when run end-to-end)
1. Pull the golden.
2. Obtain a candidate plan (recorded, or produced by a real planning run).
3. For each golden feature, judge `covered | variant | missing` against the candidate.
4. Report holes; **remediation mirrors the planner's** — ask the user or expand the plan —
   because the eval *is* the offline form of the planning coverage gate.

## Ties
- This is the D13 "lessons → evals" proof layer for the planning surface (`00-overview.md`).
- The golden's derived features seed the planning checklist (`02-planner.md`).
- Regenerate the golden from `prd-team-app` when the golden plan changes.
