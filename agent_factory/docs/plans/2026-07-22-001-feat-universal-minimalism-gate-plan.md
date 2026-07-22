---
title: "feat: Universal always-enforced minimalism/dedup/DRY gate (report-only → gating)"
type: feat
status: active
created: 2026-07-22
depth: standard
area: agent_factory
origin: (ce-ideate round 2 — idea 3; split from the reviewed factory-loop-hardening draft)
companion_plans:
  - agent_factory/docs/plans/2026-07-21-001-feat-graded-rubric-checks-plan.md
  - agent_factory/docs/plans/2026-07-21-002-feat-planning-time-rubric-seeding-plan.md
---

# feat: Universal minimalism/dedup/DRY gate

## Summary

One seeded graded check — `minimalism-dry` — that grades **strict code minimization, deduplication,
and DRY** on every ticket in every project, made reproducible by **literal copy-pasted 3-good / 3-slop
code anchors** injected verbatim into the judge. It reaches every ticket by making the currently-dead
`promote_universal` field inject into the **mandatory** lane. It ships **report-only first** (grades +
logs, does not block), then flips to **gating** once the anchors prove calibrated — because an always-on
subjective judge is exactly what generates the calibration data, so it must not block before that data
exists.

**Grounded against code (verified in review):** `promote_universal` is parsed
(`src/agent_factory/seeded_checks.py:38,69`) and wired to **nothing** — today a `["*"]` seeded graded
check is opt-in, non-gating. `_ticket_state.py:326-335` already pulls `applies_to:["*"]` *Praxis-authored*
checks into the mandatory contract, so universal gating is achievable today by authoring into Praxis;
this plan's increment is **no per-project authoring + a report-only rollout knob + a ticket exemption**.

---

## Problem Frame

The factory has no always-on enforcement of code economy — the thing coding agents most reliably get
wrong (dead/speculative code, copy-paste, parallel re-implementations). Exit codes can't judge it; it
needs the graded judge. But two hazards, both surfaced in review, must be designed for up front:
- **CI blast radius:** once a universal graded check gates, *every* existing test that drives a ticket
  to `finished` needs an LLM `Complete` judge that does not exist offline — those tickets can never reach
  `all_validations_passed`. An offline judge harness is a prerequisite, not an afterthought.
- **Inapplicable tickets:** a one-line config change / vendored / generated ticket has nothing to
  minimize; a subjective fail is content-hash-cached with **no iteration consumed**, so the loop guard
  never escalates and the session blocks forever. Exemptions are mandatory.

---

## Scope Boundaries

**In scope:** the `anchors` rubric field; anchor injection into the judge; wiring `promote_universal`
into the mandatory lane with a **report-only** mode and a **ticket exemption**; the `minimalism-dry`
seeded check; an offline judge harness for tests/CI.

**Out of scope / deferred:** the judge self-check that VOIDS a pass when a known-good anchor grades as
slop (ships after report-only calibration data exists); anchors for any check other than `minimalism-dry`.

---

## Key Technical Decisions

1. **Report-only → gating rollout.** The universal injection carries a `report_only` flag. In report-only
   mode the check is pinned and its verdict computed + recorded (calibration data) but **excluded from
   `all_validations_passed`** — it cannot block. Flipping `report_only=false` (one TOML edit) makes it
   gate. This is the miscalibration guard the deferred self-check would otherwise provide.
2. **Ticket exemption is first-class.** A ticket tagged `vendored` / `generated` / `config` (or carrying
   `meta.universal_exempt=true`) is omitted from the universal set entirely. Without this an unsatisfiable
   subjective gate on inapplicable code deadlocks the session (cached fail, no iteration consumed).
3. **Anchors are literal text, not infrastructure.** `Rubric.anchors = {good:[str], slop:[str]}` copy-pasted
   into the TOML, injected verbatim. No scoring, no versioning. At least three anchors demonstrate strict
   minimization (dead-code/speculative-abstraction slop vs minimal good; copy-paste slop vs DRY good).
4. **The injected universal validation carries its serialized rubric.** When injected at
   `contract_with_floor`, the required entry carries `kind="graded"` + the seeded rubric dict + a stable id,
   so a worker-synthesized validation covers it and `verify_graded_check` grades it exactly like a pool
   graded check (the feasibility residual-risk note, made explicit).

---

## Implementation Units

### U1. `anchors` on the rubric type
- **Goal:** `Rubric` gains optional `anchors={good:[str],slop:[str]}`; `rubric_from_dict` parses/validates.
- **Dependencies:** none.
- **Files:** `agent_factory/src/agent_factory/rubric.py`; `agent_factory/tests/test_rubric_anchors.py`.
- **Approach:** Optional frozen `anchors`; absent → None, byte-compatible with every existing rubric. No
  scoring semantics.
- **Patterns to follow:** the `Axis`/`Rubric` dataclasses + `rubric_from_dict` validation.
- **Test scenarios:** parses good/slop lists; absent anchors byte-identical; malformed (non-list) → ValueError.

### U2. Inject anchors into the judge prompt + a behavioral anchor eval
- **Goal:** `build_judge_prompt` embeds good/slop anchors verbatim; an eval confirms anchors actually move
  the verdict (not just appear in the string).
- **Dependencies:** U1.
- **Files:** `agent_factory/src/agent_factory/graded_verdict.py`; `agent_factory/tests/test_graded_verdict.py`;
  `agent_factory/evals/` (an anchor-calibration case).
- **Approach:** With anchors present, append a `CALIBRATION` block (good then slop, verbatim). Absent → prompt
  byte-identical. Add an eval that runs a known-good and a known-slop snippet through the real judge (or a
  scripted `Complete`) and asserts the expected pass/fail — the reproducibility claim gets a behavioral check
  (scope-guardian finding).
- **Test scenarios:** prompt contains each snippet verbatim under the calibration heading; no-anchor prompt
  unchanged; anchor eval: known-slop fails, known-good passes.

### U3. Wire `promote_universal` into the mandatory lane — report-only + exemption
- **Goal:** A `promote_universal=true` seeded check injects as a mandatory graded validation on every
  NON-exempt ticket; in `report_only` mode it records a verdict but does not gate.
- **Dependencies:** U1.
- **Files:** `agent_factory/hooks/_ticket_state.py` (`contract_with_floor`/`start_ticket` inject;
  `all_validations_passed` skips `report_only` entries); `agent_factory/src/agent_factory/seeded_checks.py`
  (`universal_seeded_checks()`); `agent_factory/tests/test_promote_universal_gating.py`.
- **Approach:** `universal_seeded_checks()` returns `promote_universal` entries. In the mandatory-contract
  assembly, for each non-exempt ticket append them as required graded validations (kind=graded, serialized
  rubric, stable id, covers the ticket) — deterministic, deduped by check_id, tag-independent. Exempt tickets
  (`vendored`/`generated`/`config` tag or `meta.universal_exempt`) get none. `report_only` entries are pinned
  and graded but excluded from `all_validations_passed`.
- **Patterns to follow:** the `["*"]` wildcard lane (`_ticket_state.py:326-335`); `_norm_validation` graded
  shape; `rubric_assembly` pinning.
- **Test scenarios:**
  - A `promote_universal` check appears on every non-exempt ticket incl. tag-less backend.
  - `report_only=true` → pinned + verdict recorded but `all_validations_passed` ignores it (no block).
  - `report_only=false` → it gates (`all_validations_passed` False until it passes).
  - An exempt ticket (`generated` tag / `universal_exempt`) gets NO universal check.
  - `promote_universal=false`/absent → does not inject (regression, byte-identical to today).
  - Deterministic + deduped across two RESOLVE passes.

### U4. Offline judge harness for tests/CI
- **Goal:** Existing tests that drive a ticket to `finished` still pass once a universal graded check exists —
  provide a stub `Complete` so CI has a judge.
- **Dependencies:** U3.
- **Files:** `agent_factory/tests/conftest.py` or a test helper (a scripted `Complete` returning a pass
  verdict); audit + patch of existing ticket-to-finished tests; `agent_factory/tests/test_universal_ci_harness.py`.
- **Approach:** Provide a deterministic stub judge for tests; enumerate the existing tests/integration that
  drive a ticket to `finished` and inject the stub (or assert the universal check is injected-but-report-only
  so it can't block). State explicitly that "no-universal byte-identical" ceases once U5 ships gating.
- **Execution note:** Run the FULL suite after U3/U4 and fix every ticket-to-finished test the universal
  injection touches — this is the adversarial CI-break finding; do not defer it.
- **Test scenarios:** a stubbed-judge ticket reaches `finished`; the suite is green with the universal check
  injected in report-only mode.

### U5. Author `minimalism-dry` + flip to gating
- **Goal:** The seeded `minimalism-dry` graded check (axes minimalism/deduplication/dry + 3-good/3-slop
  anchors), shipped `report_only=true`, with a documented flip to gating.
- **Dependencies:** U1, U2, U3, U4.
- **Files:** `agent_factory/seeded_checks.toml`; `agent_factory/tests/test_seeded_checks.py`.
- **Approach:** Append `check_id="minimalism-dry"`, `kind="graded"`, `applies_to=["*"]`,
  `promote_universal=true`, `report_only=true`, high `confidence_floor`, three axes with strict thresholds +
  pointed guidance, and `[check.anchors]` with 3 good / 3 slop literal snippets (≥3 demonstrating strict
  minimization), sourced from the repo's clean-code conventions. Document the one-line `report_only=false`
  flip and the owner/signal for it (blocked-ticket rate).
- **Patterns to follow:** the `correctness-review`/`security-review` graded blocks in `seeded_checks.toml`.
- **Test scenarios:** loader parses it with axes + anchors; it injects as report-only on an arbitrary ticket;
  its judge prompt contains the strict-minimization anchors verbatim; flipping `report_only=false` makes it gate.

---

## Dependencies / Sequencing

```
U1 ─ U2
U1 ─ U3 ─ U4 ─ U5   (U5 needs anchors U1/U2 + gating U3 + CI harness U4)
```

Land U1→U4 (report-only, no block, CI green), then U5 ships the check report-only; flip to gating only
after calibration data confirms strictness.

**Assumptions**
- The clean-code anchors match house style (see the `clean-code` / `simplify` skills), not an external opinion.
- `all_validations_passed` (`_ticket_state.py:501`) can cheaply skip `report_only` pinned entries — verify.

---

## Risk Analysis & Mitigation

| Risk | Mitigation |
|---|---|
| Miscalibrated judge false-blocks every ticket | Report-only rollout (U3/U5) gathers calibration data before it can block; anchors pin taste; threshold tunable in one TOML line with a named owner/signal |
| Inapplicable ticket (config/vendored/generated) deadlocks | First-class exemption (U3, Key Decision 2) — exempt tickets get no universal check |
| Universal gate breaks CI | Offline judge harness (U4) is a prerequisite unit, run against the full suite before gating |
| Cached fail on unchangeable code never escalates | Exemption removes the class; report-only can't block; if gating later, a config/vendored ticket is exempt by construction |
| Adds a judge call to every ticket | ONE check; content-hash cache skips unchanged code; exemptions prune inapplicable tickets |

---

## Verification Strategy

- Full suite green with the universal check injected in report-only mode (U4 regression).
- End-to-end (gating mode): a ticket with duplicated/dead code fails `minimalism-dry` and cannot finish
  until consolidated; an exempt (`generated`) ticket finishes unaffected; the judge prompt shows the
  strict-minimization anchors.
- Anchor eval (U2): a known-slop snippet fails and a known-good passes through the real judge.
