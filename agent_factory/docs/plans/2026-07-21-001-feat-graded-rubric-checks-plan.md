---
title: "feat: Graded rubric checks for the agent factory verification loop"
type: feat
status: completed
created: 2026-07-21
depth: deep
area: agent_factory
---

# feat: Graded rubric checks for the agent factory verification loop

## Summary

Add a second **kind** of validation check to the agent factory: alongside today's
binary exit-code checks, a **graded rubric check** whose pass/fail verdict is a
subjective, LLM-judged, min-of-axes rubric evaluation. The subjectivity is
*encapsulated inside the check* — the verdict still resolves to a single `passed`
boolean on the pinned validation, so it flows through the existing
`all_validations_passed` coverage gate untouched. The per-ticket loop (clear →
search → author → pin → forcibly continue) is unchanged in shape.

Two supporting pieces:
1. A **file-backed seeded library** of generic reusable checks (TerMinal-derived
   quality axes) that RESOLVE always offers to the check-authoring agent as
   opt-in candidates. The file is the single, easy-to-extend source of truth.
2. **Loop-termination guards** so a nondeterministic subjective verdict cannot
   thrash the forcibly-continue loop.

Auto-adjustment of rubrics (weights/thresholds evolving from review signal) is
in scope as **Phase 2**, gated behind the static rubric proving out in Phase 1.

**Origin:** Interactive planning session (no upstream brainstorm doc). Idea
sourced from an investigation of `github.com/trevormil/TerMinal`'s six-axis
`min`-verdict code review, adapted to Praxis's objective-gate design.

---

## Problem Frame

Today verification is uniformly **binary exit-code, coverage-gated** (per the
research: `agent_factory/hooks/_ticket_state.py:480` `all_validations_passed`,
`agent_factory/docs/factory-state-contract.md`). This is deliberate — "the model
never self-judges." But it leaves an entire class of quality concerns unverified:
anything that cannot be reduced to an exit code (architectural soundness,
error-path completeness, security posture, "was the fix actually correct vs.
merely green"). The check-authoring agent also invents coverage from scratch each
ticket, with no curated checklist to draw on, so it silently under-covers.

We want to grade that residue **without** abandoning the objective gate, and
without creating an infinite iteration loop.

---

## Scope Boundaries

**In scope**
- A graded-rubric check kind on the **per-ticket check** path only.
- A hardcoded, easy-to-extend seeded-check library file + loader.
- Deterministic surfacing of the library as opt-in RESOLVE candidates.
- A fresh-context LLM judge producing per-axis scores + located defects.
- `min`-of-axes verdict with per-axis thresholds, confidence floor, and
  positive-evidence-of-defect-to-fail.
- Content-hash-cached verdicts + loop-termination guards.
- Phase 2: human-gated auto-adjustment of rubrics.

**Out of scope (kept as-is)**
- The whole-diff ce-* WORK-review panel (`agent_factory/skills/af-build/SKILL.md`
  §7) — it keeps emitting findings/severities, ungraded. The rubric does **not**
  run there.
- The objective wildcard checks (typecheck/build/lint/test) — the rubric never
  re-judges anything already exit-codeable.
- The offline self-benchmark harness (`agent_factory/evals/build_repro/`,
  `plan_repro/`) — reused as a *library* for the judge, not modified as an eval.

### Deferred to Follow-Up Work
- Rubric versioning/history UI in the dashboard.
- Cross-project rubric sharing.

---

## Key Technical Decisions

1. **The rubric enters the gate only as a check's pass-determination.** No parallel
   scoring system. A graded check computes `passed: bool` exactly like a binary
   check does; `all_validations_passed` (`_ticket_state.py:480`) is unchanged.
   *(see conversation: "subjective scoring can enter the gate in how we determine
   if the assigned checks pass or not")*

2. **`min`-of-axes, not weighted average.** `min` is the load-bearing anti-masking
   property. Weights are incompatible with `min` (min ignores them and re-opens
   the masking problem), so axis importance is expressed as a **higher per-axis
   threshold**, never a weight.

3. **Fail requires a located, actionable defect** (positive-evidence-of-defect).
   A graded check may only *fail* on `file:line + what's wrong + what would
   satisfy it`. No concrete defect → pass. This is the primary anti-loop lever:
   it converts vague dissatisfaction (unsatisfiable) into a convergent fix list.

4. **Fresh context ≠ builder.** The judge is always a fresh evaluator, never the
   context that wrote the code ("code is never graded by the context that wrote
   it"). Load-bearing because the verdict is now subjective. Reuse the injected
   `Complete = (prompt) -> text` pattern from
   `agent_factory/evals/build_repro/score.py` / `evals/plan_repro/llm_evaluator.py`
   (`claude_cli`) — runs on the subscription, no API key, offline-testable.

5. **Verdict is a pure function of code-state; cache by content hash.** Grade a
   given tree/diff SHA once and cache the verdict. Identical code → identical
   verdict → no re-grade. Eliminates flapping. Mirrors the `/check` early-exit
   (`skip if HEAD == last-run SHA`).

6. **Rubric frozen for the duration of a ticket's build.** Axes/thresholds/prompt
   are pinned at `start_ticket` (`_ticket_state.py:840`) and cannot change
   mid-loop, so the target never moves under the worker. Auto-adjust (Phase 2)
   may never mutate a rubric pinned to an in-progress ticket.

7. **Seeded library is a single hardcoded file, append-friendly.** One entry per
   generic check; adding a check = adding one record, no code change.

---

## High-Level Technical Design

*Directional guidance for review, not implementation specification.*

```
check-authoring agent (per ticket)
   │  RESOLVE
   ├─ tag / "*" / surface lanes ......... gating, auto-pinned  (unchanged)
   ├─ semantic advisory lane ............ inspiration          (unchanged)
   └─ SEEDED GENERIC LIBRARY ............ always offered, opt-in   ← new (U1,U3)
          (file-backed, each entry = binary OR graded rubric)

worker SYNTHESIZE → pin_validations(...)   (unchanged shape)

VERIFY, per pinned validation:
   kind == "binary"  → run cmd, passed = (exit==0)            (unchanged)
   kind == "graded"  → verdict = judge(code_state, rubric)    ← new (U4,U5)
                         cached by content hash
                         passed = min(axis_i) ≥ threshold_i
                                  ∧ (no located defect above confidence floor)
   record_validation_pass(cid, vid, passed, ...)              (unchanged sink)

all_validations_passed  →  finish gate                        (unchanged)

loop guards (U6): iteration cap · defect-count monotonicity ·
                  frozen rubric · cap → HITL blocked
```

---

## Implementation Units

### U1. Seeded generic-check library (file + loader)

- **Goal:** A single hardcoded, append-friendly file defining the generic reusable
  checks (correctness, security, error-paths, etc.), each as a binary or graded
  rubric check, plus a loader.
- **Dependencies:** none.
- **Files:** `agent_factory/src/agent_factory/seeded_checks.py` (or a
  `seeded_checks.toml`/`.json` data file + thin loader — pick the format that makes
  adding one entry trivial); `agent_factory/tests/test_seeded_checks.py`.
- **Approach:** Each entry carries `check_id`, `applies_to` tags (default candidate,
  never `["*"]` unless a project promotes it), `kind`, and for graded entries the
  rubric block (axes with per-axis thresholds, confidence floor, judge prompt).
  Loader validates schema and rejects duplicate `check_id`s. Ship a starter set
  derived from TerMinal's six axes as the default general-quality rubric.
- **Patterns to follow:** the check `meta` shape in
  `agent_factory/skills/af-intake-build-validation/SKILL.md` Step 2.
- **Test scenarios:**
  - Loader parses a valid file into check records (happy path).
  - Duplicate `check_id` → load error naming the id.
  - A graded entry missing `axes`/`threshold`/`judge_prompt` → validation error.
  - A binary entry with a `run` command loads with `kind="binary"`.
  - Adding one new entry requires touching only the file (assert by loading a
    fixture with an extra record).

### U2. Graded-check schema on the validation model

- **Goal:** Extend the check/validation representation so a validation can declare
  `kind: "graded"` with a rubric (axes, per-axis thresholds, confidence floor,
  judge prompt) — binary remains the default.
- **Dependencies:** U1.
- **Files:** `agent_factory/src/agent_factory/validation_target.py`;
  `agent_factory/docs/factory-state-contract.md` (document the extended
  `pinned_checks` entry shape); `agent_factory/tests/test_validation_target.py`.
- **Approach:** Extend the pinned-check entry (`{validation_id, covers, run,
  passed, ran_at, source}`) with optional `kind` (default `"binary"`), `rubric`,
  and `verdict` (per-axis scores + defects + code-hash) for graded checks. Binary
  entries are byte-compatible with today. Keep `passed` the single gate signal.
- **Patterns to follow:** existing `pinned_checks` contract in
  `agent_factory/docs/factory-state-contract.md`.
- **Test scenarios:**
  - Binary validation round-trips unchanged (no `kind`/`rubric` fields).
  - Graded validation serializes/deserializes with rubric + verdict intact.
  - `coverage_gap` (`_ticket_state.py:463`) treats graded checks identically to
    binary for coverage math (a graded check covers its `req_id`s).

### U3. Surface the seeded library as deterministic RESOLVE candidates

- **Goal:** During RESOLVE the seeded library is always offered to the authoring
  agent as opt-in candidates — reliably, independent of embedding similarity — and
  never auto-pinned (unless a project explicitly promotes an entry to `["*"]`).
- **Dependencies:** U1.
- **Files:** `agent_factory/hooks/_ticket_state.py` (RESOLVE / `contract_with_floor`
  path — add a candidate lane, non-gating); `agent_factory/tools/resolve_preview.py`
  (show seeded candidates); `agent_factory/tests/test_check_resolution_lanes.py`;
  `agent_factory/tests/test_resolve_preview_coverage.py`.
- **Approach:** Add a **candidate lane** distinct from the three gating lanes and the
  semantic advisory lane. Seeded entries appear in the candidate set every ticket;
  the worker opts in per item (identical selection semantics to today). Promotion
  to universal is per-project config marking an entry `["*"]`, which then flows the
  existing wildcard-gating path — no new gating code.
- **Patterns to follow:** the tag/`"*"`/surface/advisory lane structure documented
  in `agent_factory/skills/af-build/SKILL.md` and implemented around
  `contract_with_floor`.
- **Test scenarios:**
  - Seeded candidates appear in RESOLVE output for a ticket regardless of tags.
  - Candidates are **not** auto-pinned (absent from the gating contract until the
    worker selects them).
  - A promoted (`["*"]`) seeded entry auto-pins to every ticket via the existing
    wildcard lane.
  - `resolve_preview --by-check` lists seeded candidates under their own lane.

### U4. Graded-verdict evaluator (fresh-context judge)

- **Goal:** Given a code-state and a rubric, produce per-axis scores (0–1),
  located defects with confidence, and a `passed` verdict.
- **Dependencies:** U2.
- **Files:** `agent_factory/src/agent_factory/graded_verdict.py`;
  `agent_factory/tests/test_graded_verdict.py`.
- **Approach:** `evaluate(code_state, rubric, complete: Complete) -> Verdict`.
  Verdict rule: `passed = all(axis_i >= threshold_i) AND no defect with
  confidence >= floor`. A defect must be `{file, line, problem, remedy,
  confidence}`; defects below the confidence floor are dropped (not failed on).
  Approval requires the judge to cite **positive evidence of safety**, not mere
  absence. Inject `Complete = (prompt) -> text` so it is offline-testable with a
  stub — do **not** call the model directly.
- **Execution note:** Implement test-first with a stubbed `Complete`; the verdict
  math must be fully covered without a live model.
- **Patterns to follow:** the injected-judge pattern in
  `agent_factory/evals/build_repro/score.py` and
  `agent_factory/evals/plan_repro/llm_evaluator.py` (`claude_cli`).
- **Test scenarios:**
  - All axes above thresholds, no defects → `passed=True`.
  - One axis below its threshold → `passed=False` (min-of-axes; strong axes do
    not mask it).
  - A high-confidence located defect → `passed=False` with the defect surfaced.
  - A defect below the confidence floor → dropped, does **not** fail the check.
  - Judge returns dissatisfaction but **no located defect** → `passed=True`
    (positive-evidence-of-defect rule; the key anti-loop case).
  - Per-axis threshold override: a stricter threshold on one axis fails a score
    that a uniform threshold would pass.
  - Malformed judge output → surfaced as an evaluator error, never a silent pass.

### U5. Wire the graded verdict into VERIFY with content-hash caching

- **Goal:** VERIFY runs graded checks through the judge, caches the verdict by
  code-state hash, and records the resulting `passed` boolean via the existing
  sink so the gate is unchanged.
- **Dependencies:** U3, U4.
- **Files:** `agent_factory/hooks/_ticket_state.py`
  (`record_validation_pass:429` call site / VERIFY path);
  `agent_factory/skills/af-build/SKILL.md` (§ VERIFY + § worker contract — document
  the graded path); `agent_factory/tests/test_graded_verify.py`.
- **Approach:** For `kind=="graded"`, compute the code-state hash (tree/diff SHA
  over the ticket's touched paths, reusing the `git_diff` helper pattern in
  `evals/build_repro/score.py`). If a cached verdict exists for that hash, reuse
  it; else evaluate (U4) and cache on the pinned-check entry. Then
  `record_validation_pass(cid, vid, passed, ran_at, source="graded-judge")`.
  `all_validations_passed` needs no change.
- **Test scenarios:**
  - Graded check pass → `record_validation_pass(..., passed=True)`; ticket can
    finish when coverage is complete.
  - Graded check fail → `passed=False`; `all_validations_passed` returns False;
    ticket regresses to FIND (integration).
  - Identical code-state hash on re-verify → cached verdict reused, judge **not**
    re-invoked (assert stubbed `Complete` call count).
  - Changed code-state → verdict recomputed.
  - Mixed binary + graded pinned checks → gate is the AND of all `passed` (min
    over the pinned set), no special-casing.

### U6. Loop-termination guards

- **Goal:** Guarantee the forcibly-continue loop terminates when a graded check
  keeps failing.
- **Dependencies:** U5.
- **Files:** `agent_factory/hooks/_ticket_state.py` (iteration counter,
  frozen-rubric-at-`start_ticket:840`, monotonicity check);
  `agent_factory/hooks/build_completeness_gate.py` (HITL escalation on cap);
  `agent_factory/skills/af-build/SKILL.md` (§5 correction loop — document the
  graded cap tier); `agent_factory/tests/test_graded_loop_guards.py`.
- **Approach:**
  - **Dedicated iteration cap** for graded checks, lower than the objective cap;
    on cap → route to existing tiered escalation as `blocked` (never
    `incomplete`-forever).
  - **Defect-count monotonicity:** track outstanding defects across iterations; a
    pass that fails to reduce them (or raises the structural-erosion delta) trips
    the breaker early. Extend the existing complexity-delta erosion check.
  - **Frozen rubric:** snapshot the rubric onto the ticket at `start_ticket`;
    VERIFY reads the frozen copy, not the live library.
- **Test scenarios:**
  - Repeated failing verdicts on **identical** code → caching prevents re-grade;
    no iteration is consumed on unchanged code (no flapping loop).
  - N failing iterations with changing code → cap trips → ticket `blocked` + HITL
    item filed (integration).
  - Iteration that does not reduce the defect set → breaker trips before the cap.
  - Rubric edited in the library mid-build → the in-progress ticket keeps its
    frozen rubric (verdict uses the snapshot, not the edit).

### U7. Auto-adjustment of rubrics (Phase 2)

- **Goal:** Rubrics evolve from review signal — human-gated, never applied to
  in-flight tickets, and biased toward *loosening/clarifying* miscalibrated checks.
- **Dependencies:** U6 (needs the loop-signal + frozen-rubric invariant in place).
- **Files:** `agent_factory/src/agent_factory/rubric_adjust.py`;
  `agent_factory/tools/rubric_adjust_review.py` (human-gated apply);
  `agent_factory/tests/test_rubric_adjust.py`; update `seeded_checks` file on apply.
- **Approach:** Capture signal — recurring located defects across sets (tighten /
  add axis) and non-convergence events from U6 (loosen / clarify a check whose bar
  is too high or too vague). Adjustments are **proposals** written for human review;
  applying edits the seeded file. Hard constraint: never mutate a rubric currently
  pinned to an in-progress ticket (enforced via U6's frozen snapshot).
- **Execution note:** Build the signal-capture + proposal path first; the apply
  step stays human-gated for the whole phase.
- **Test scenarios:**
  - Recurring high-confidence defect on an axis → proposal to strengthen that axis.
  - Repeated non-convergence on a check → proposal to loosen/clarify, not tighten.
  - Proposal targeting a rubric pinned to an in-progress ticket → refused/deferred
    until that ticket releases.
  - Apply is inert without explicit human confirmation (no silent mutation).

---

## Dependencies / Sequencing

```
U1 ─┬─ U2 ── U4 ─┐
    └─ U3 ───────┼─ U5 ── U6 ── U7 (Phase 2)
                 │
   (U3 needs U1; U5 needs U3+U4)
```

Phase 1 = U1–U6 (self-contained, shippable). Phase 2 = U7.

---

## Risk Analysis & Mitigation

| Risk | Mitigation |
|---|---|
| Subjective verdict thrashes the loop | Content-hash cache (U5), defect-evidence-to-fail (U4), iteration cap + monotonicity (U6) |
| Judge overrides objective signals | Rubric never grades exit-codeable concerns; objective wildcard checks stay separate (scope boundary) |
| Non-determinism erodes trust in the gate | Verdict is a pure function of code-state; frozen rubric; fresh-context judge |
| Auto-adjust tightens into an unsatisfiable bar | Human-gated apply; loosen-on-non-convergence bias; never mutate in-flight rubric (U7) |
| Cost of an LLM judge per ticket | Graded checks are opt-in candidates, not universal; cache avoids re-grading unchanged code |

---

## Verification Strategy

- Phase 1 lands with every unit's tests green and no change to
  `all_validations_passed` behavior for binary-only tickets (regression guard).
- An end-to-end test drives a ticket with one graded check through fail →
  fix → pass → finish, and a second through fail → cap → blocked + HITL.
- `resolve_preview` shows seeded candidates as a distinct, non-gating lane.
