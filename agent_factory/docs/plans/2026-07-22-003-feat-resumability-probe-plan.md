---
title: "feat: Falsifiable fresh-worker resumability probe"
type: feat
status: active
created: 2026-07-22
depth: standard
area: agent_factory
origin: (ce-ideate round 2 — idea 5; split from the reviewed factory-loop-hardening draft)
companion_plans:
  - agent_factory/docs/plans/2026-07-21-001-feat-graded-rubric-checks-plan.md
---

# feat: Falsifiable fresh-worker resumability probe

## Summary

A pure, offline probe that verifies a ticket's "done" is reconstructable from Praxis state alone, wired
at claim time so an under-specified ticket routes back to intake instead of into a lease. Makes the
"Praxis = sole state" invariant testable per-ticket rather than an untested belief the parallel-worktree
workers bet on.

**Grounded against code (verified in review):** `contract_with_floor` (`hooks/_ticket_state.py`) prepends
the acceptance floor **only when acceptance is non-empty**, and a ticket covered purely by declared
`required_validations` (no acceptance text) is a legitimate, buildable state today. So the probe must NOT
require acceptance unconditionally — it must accept `acceptance OR resolved required_validations`, or it
starves exactly the terminal/backend tickets the acceptance-floor already makes coverable (adversarial
finding). `start_ticket`→`claim` (`:892/:591`) is the real insertion point.

---

## Scope Boundaries

**In scope:** a pure structural resumability predicate over a ticket's Praxis meta; a claim-time guard that
routes non-resumable tickets to an under-specified state.

**Out of scope / deferred:** the cold-worker LLM "deep" probe (a fresh agent reconstructs the ticket from
state and is graded) — sits behind an injected `Complete` seam, a follow-up once the structural probe is
in and shown insufficient.

---

## Key Technical Decisions

1. **Resumable = coverable-from-state, not acceptance-required.** A ticket is resumable iff its Praxis rows
   let a cold worker know what "done" means: `(non-empty acceptance) OR (non-empty resolved
   required_validations)`, AND a `verify` mode is set, AND every `depends_on` names a real requirement id.
   This mirrors `contract_with_floor`'s own coverability rule so the probe never starves a check-covered
   ticket (the false-positive the adversarial review caught).
2. **Structural first, LLM-deep later.** The shipped probe is pure over the meta dict — deterministic,
   offline, CI-safe. No model call. The deep probe is deferred behind an injected `Complete`.
3. **Route, don't silently drop.** A non-resumable ticket is marked with an explicit
   `under_specified: [missing fields]` state that surfaces to intake (and, once plan 002's planning hook
   lands, to its escalation) — never a silent skip.

---

## Implementation Units

### U1. Structural resumability probe
- **Goal:** `resumability_report(ticket_meta, resolved_required) -> {resumable: bool, missing: [...]}`.
- **Dependencies:** none.
- **Files:** `agent_factory/src/agent_factory/resumability.py`; `agent_factory/tests/test_resumability.py`.
- **Approach:** Pure predicate. `resumable` iff `(acceptance non-empty OR resolved_required non-empty)` AND
  `verify` set AND `depends_on` all name plan requirement ids. `missing` lists exactly which condition
  failed. No Praxis calls — the caller passes the resolved required set (from
  `resolve_validation_requirements`).
- **Patterns to follow:** the meta-key contract in `docs/factory-state-contract.md`; `contract_with_floor`'s
  coverability logic (mirror it, don't contradict it).
- **Test scenarios:**
  - Fully-specified ticket → `resumable: True, missing: []`.
  - **Acceptance-less but check-covered** (non-empty resolved required) → `resumable: True` (the regression
    the review flagged — must NOT route back).
  - No acceptance AND no resolved checks → `resumable: False, missing:["contract"]`.
  - `verify` unset → not resumable.
  - `depends_on` naming an absent requirement → not resumable (dangling), surfaced in `missing`.
  - `verify=manual` with human-sign-off acceptance → resumable.

### U2. Claim-time resumability guard
- **Goal:** Before leasing, a failed probe routes the ticket to `under_specified` instead of claiming it.
- **Dependencies:** U1.
- **Files:** `agent_factory/hooks/_ticket_state.py` (`start_ticket`/`claim` path — probe using the already-
  resolved required set, then route); `agent_factory/skills/af-build/SKILL.md` (document the pre-claim
  guard); `agent_factory/tests/test_resumability_gate.py`.
- **Approach:** In `start_ticket`, after `resolve_validation_requirements` (the resolved set is already
  computed there), run `resumability_report`; on failure, do NOT claim — set an explicit
  `under_specified:[missing]` state that surfaces to intake. A resumable ticket claims and proceeds
  unchanged.
- **Patterns to follow:** `start_ticket` claim/return flow; the `block()` path for unprogressable tickets.
- **Test scenarios:**
  - A non-resumable ticket is not leased; surfaces `under_specified` with the missing fields.
  - A resumable ticket (incl. acceptance-less-but-check-covered) claims and proceeds byte-identically to
    today (regression).
  - Integration: a ticket missing both acceptance and checks never enters the build set; adding either clears it.

---

## Dependencies / Sequencing

```
U1 ─ U2
```

Independent of plans 001/002; can land anytime.

**Assumptions**
- `start_ticket` already has the resolved required set in hand at the claim point — verified
  (`resolve_validation_requirements` is called there), so the probe adds no extra Praxis round-trip.

---

## Risk Analysis & Mitigation

| Risk | Mitigation |
|---|---|
| Probe false-positive starves check-covered tickets | Resumable = acceptance OR resolved required (KTD1), mirroring `contract_with_floor`; explicit regression test |
| Silent drop of a routed ticket | Explicit `under_specified:[missing]` state that surfaces to intake (KTD3) |
| Over-strict probe blocks the build set | Structural + conservative — only flags genuinely missing contract/verify/dangling deps; `missing` is explicit and fixable |

---

## Verification Strategy

- Suite green; a resumable ticket claims byte-identically to today (U2 regression), including the
  acceptance-less-but-check-covered case.
- End-to-end: a ticket missing both acceptance and declared checks is refused a lease and surfaces
  under-specified; adding an acceptance condition OR a declared check makes it claimable.
