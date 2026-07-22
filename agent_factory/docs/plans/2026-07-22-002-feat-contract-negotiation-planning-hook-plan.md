---
title: "feat: Adversarial contract negotiation + planning Stop-hook"
type: feat
status: completed
created: 2026-07-22
depth: deep
area: agent_factory
origin: (ce-ideate round 2 — ideas 1 + 2; split from the reviewed factory-loop-hardening draft)
companion_plans:
  - agent_factory/docs/plans/2026-07-21-001-feat-graded-rubric-checks-plan.md
---

# feat: Adversarial contract negotiation + planning Stop-hook

## Summary

Two coupled improvements (the hook's bless predicate includes the contract):

- **Contract negotiation (idea 1).** Intake hardening becomes a planner draft + a SEPARATE evaluator
  role that adversarially rewrites/cuts/adds testable assertions and **signs** the result — recording a
  `contract-signed` episode carrying the assertion count AND the evaluator's cut/merge/add actions.
- **Planning Stop-hook (idea 2).** A `plan_completeness` Stop hook (second entry in `hooks/hooks.json`)
  that keeps a planning session from ending until the plan mechanically blesses; the human is summoned
  only on a failing predicate, with a **bounded terminal escalation** so an unresolvable predicate never
  loops forever.

**Scoping the rigor claim honestly (product review):** auto-bless raises *structural* rigor (adds the
signed-contract + contradiction-ran predicates); it does NOT preserve the qualitative human plan review
today's manual gate provides — that review becomes **sampled only on a failing predicate**. This plan
mitigates the gap with evidence-based predicates (below), not by claiming rigor is unchanged.

**Grounded against code (verified in review):** `evaluate_plan` (`src/agent_factory/plan_gate.py:156`)
is a **pure** function — no `src/agent_factory` module has a Praxis client. `praxis_record_episode` /
`praxis_get_contradictions` exist **only** as MCP-server tools (`knowledge/mcp/server.py:684,745`);
`hooks/_praxis.py` exposes neither. `plan_gate` **delegates** contradiction detection to Praxis and does
not compute it, so "contradictions empty" can mean detection never ran (the raw-bulk path skips it).
`_ticket_state.stamp_run` (`:711`) is the marker to mirror. `hooks/hooks.json`'s `Stop` is an array.

---

## Scope Boundaries

**In scope:** a Praxis-client prerequisite (episode/contradiction wrappers in `hooks/_praxis.py`); the
planner/evaluator split + signed-contract episode; the `R-CONTRACT-SIGNED` gate rule (keeping
`evaluate_plan` pure); a `contradictions-checked` evidence marker; the planning arming marker; the
`plan_completeness` hook with bounded escalation and a scoped escape.

**Out of scope:** the universal minimalism gate (its own plan 001); resumability (plan 003); a full
qualitative auto-reviewer to replace the human (the predicate is deliberately structural — the human
still handles the sampled failures).

---

## Key Technical Decisions

1. **`evaluate_plan` stays pure; Praxis I/O lives in the caller.** `R-CONTRACT-SIGNED` gates on a
   `contract: {signed: bool, actions_recorded: bool}` input **threaded into** `evaluate_plan` from
   `tools/plan_gate_check.py` (which has `_praxis`). The rule never reads Praxis itself; the eval-case
   harness supplies the field. (Feasibility finding — the read cannot happen inside the pure function.)
2. **Praxis-client prerequisite.** Add `record_episode` / `get_episodes` / `get_contradictions` wrappers to
   `hooks/_praxis.py` over the same REST endpoints the MCP tools call, so the hook and gate-check can read
   them. The episode **write** stays a SKILL MCP-tool call (`praxis_record_episode`), not a src helper —
   `src/agent_factory` has no client and must not grow one.
3. **Gate on evaluator ACTIONS, not a padded count (anti-Goodhart).** `R-CONTRACT-SIGNED` requires a
   signed episode whose meta records real evaluator actions (cuts/merges/additions) — a raw
   `n_assertions >= floor` count alone is a Goodhart target an evaluator clears by padding. The count is
   recorded and the sub-floor case FLAGS for evaluator attention (not a hard reject), reconciling B's own
   "flags, not rejects" intent. The hard bless predicate is "signed + actions recorded", not "count ≥ N".
4. **Auto-bless requires contradiction detection to have RUN.** Because `plan_gate` delegates and the
   raw-bulk path skips detection, "empty" is not evidence of consistency. The hook requires a
   `contradictions-checked` marker/episode for the pinned snapshot (positive evidence detection ran) in
   addition to the queue being empty. (Adversarial finding.)
5. **Bounded terminal escalation + scoped escape (no infinite loop).** After K failed bless attempts on an
   **unchanged** plan snapshot, `plan_completeness` emits a terminal `plan_blocked` state (mirroring
   build's `blocked` churn-exclusion) that ALLOWS the stop and surfaces "human required" — so an
   unresolvable contradiction / `R-NO-VAGUE` term never re-blocks forever in an autonomous run. The hook
   also honors its own scoped escape, not only the global `FACTORY_GATE_DISABLED` (which would disable
   build enforcement too). (Adversarial finding.)

---

## Implementation Units

### U1. Praxis-client prerequisite — episode + contradiction wrappers in `hooks/_praxis.py`
- **Goal:** The hook layer and gate-check can `record_episode` / `get_episodes` / `get_contradictions`.
- **Dependencies:** none. **Blocks U3/U4/U6.**
- **Files:** `agent_factory/hooks/_praxis.py`; `agent_factory/tests/test_praxis_episode_wrappers.py`.
- **Approach:** Add thin wrappers over the REST endpoints the MCP tools (`knowledge/mcp/server.py:684,745`)
  call, matching the existing `_praxis` client style (get_fact/facts_by/patch_meta/context). Fail-closed on
  `PraxisUnreachable`, like the rest of the client.
- **Patterns to follow:** existing `hooks/_praxis.py` method shapes; the MCP tool request bodies.
- **Test scenarios:** each wrapper issues the right request and parses the response (stubbed transport);
  `PraxisUnreachable` propagates; empty results return `[]`/None cleanly.

### U2. Planner/evaluator split + signed-contract episode (skill)
- **Goal:** A SEPARATE evaluator role adversarially rewrites/cuts/adds assertions and records a
  `contract-signed` episode (count + actions); a sub-floor requirement FLAGS for evaluator attention.
- **Dependencies:** none (skill + MCP tool call).
- **Files:** `agent_factory/skills/af-intake-plan/SKILL.md` (Step 1-3 / B1 — the negotiation + signing step).
- **Approach:** After extraction, dispatch the read-only evaluator sub-agent whose only job is to falsify /
  cut / merge / tighten the candidate assertions; the planner never grades its own contract. Record via the
  `praxis_record_episode` MCP tool: `kind="contract-signed"`, `n_assertions`, `actions:{cut,merged,added}`,
  `signer`. A requirement below ~10 concrete assertions is FLAGGED for the evaluator, not hard-rejected.
- **Patterns to follow:** existing B1 adversarial dispatch + `praxis_record_episode` MCP usage; the
  read-only retrieval sub-agent contract.
- **Test scenarios:** `Test expectation: none` (prose skill change) — validated by U4's gate integration.

### U3. `contract_signature.py` — PURE payload/validation helpers
- **Goal:** Pure helpers to build/validate the signed-contract episode payload and evaluate the floor —
  no I/O.
- **Dependencies:** U1 (for the read path used by U4, not by this module).
- **Files:** `agent_factory/src/agent_factory/contract_signature.py`; `agent_factory/tests/test_contract_signature.py`.
- **Approach:** `build_signed_payload(n, actions, signer) -> dict`, `is_signed(episode) -> bool`,
  `actions_recorded(episode) -> bool`, `below_floor(n, floor) -> bool`. Pure over dicts; the actual
  episode read/write happens via `_praxis` (U1) / the skill MCP call, injected — not embedded (feasibility
  finding: src has no Praxis client).
- **Test scenarios:** `is_signed`/`actions_recorded` true on a well-formed episode, false on a bare count;
  `below_floor` boundary; malformed episode → not signed.

### U4. `R-CONTRACT-SIGNED` rule (evaluate_plan stays pure)
- **Goal:** A blessed plan requires a signed contract with recorded evaluator actions.
- **Dependencies:** U1, U3.
- **Files:** `agent_factory/src/agent_factory/plan_gate.py` (add the rule + a `contract` input field to
  `evaluate_plan`); `agent_factory/tools/plan_gate_check.py` (read the episode via `_praxis` U1, pass
  `contract` in); `agent_factory/tests/test_plan_gate_contract.py` + an `evals/cases/plan_gate/` case.
- **Approach:** `evaluate_plan(..., contract: {signed, actions_recorded} | None)` — reject when not signed
  or actions not recorded; the count is informational. `plan_gate_check.py` reads the `contract-signed`
  episode via the U1 wrapper and threads the field. Reason printed verbatim; non-zero exit blocks the bless.
- **Patterns to follow:** existing `R-HAS-SOURCE` / `R-NO-DANGLING-DEP` rules; `evals/cases/plan_gate/` format.
- **Test scenarios:** signed+actions → passes; unsigned → `R-CONTRACT-SIGNED` reject; signed-but-no-actions
  (padded count) → reject; `evaluate_plan` remains pure (no Praxis import); eval-case with the new field.

### U5. Planning-session arming marker
- **Goal:** `stamp_planning` / `clear_planning` / `planning_active(project)` — the signal U6 arms on.
- **Dependencies:** none.
- **Files:** `agent_factory/hooks/_ticket_state.py`; `agent_factory/skills/af-intake-plan/SKILL.md` (stamp at
  intake start, clear at bless); `agent_factory/tests/test_planning_marker.py`.
- **Approach:** Mirror `stamp_run`: a session-owned, heartbeated planning marker on the `prd-<project>`
  snapshot; `planning_active` True while a non-stale marker is present and the plan is unblessed.
- **Patterns to follow:** `_ticket_state.stamp_run` + the run-marker arming in `build_completeness_gate.py`.
- **Test scenarios:** stamp→active; clear→inactive; stale marker (past TTL)→inactive; no marker→inactive.

### U6. `plan_completeness` Stop hook (with bounded escalation + scoped escape)
- **Goal:** Block the planning Stop until the plan blesses; auto-bless when the predicate holds; never loop
  forever; fail closed on Praxis-unreachable.
- **Dependencies:** U1, U4, U5.
- **Files:** `agent_factory/hooks/plan_completeness_gate.py` (new); `agent_factory/hooks/hooks.json` (append
  a 2nd `Stop` entry — verified array); `agent_factory/tests/test_plan_completeness_gate.py`.
- **Approach:** Copy the arm/enforce/fail-closed skeleton of `build_completeness_gate.py`. ARM on
  `planning_active` (U5). ENFORCE: `plan_gate_check` exit 0 (incl. U4), a `contradictions-checked` marker
  present AND `get_contradictions` (U1) empty, planning-validation lens coverage complete. BLOCK with the
  specific failing predicate — the human-escalation moment. AUTO-BLESS (ALLOW) when all hold. **Bounded
  escalation:** track attempts per snapshot hash; after K failures on an unchanged snapshot, ALLOW the stop
  with a terminal `plan_blocked` "human required" state (Key Decision 5). Own scoped escape env var
  (distinct from `FACTORY_GATE_DISABLED`, which disables build too). FAIL CLOSED loudly on `PraxisUnreachable`.
- **Patterns to follow:** `hooks/build_completeness_gate.py` arm/enforce/fail-closed + `blocked` churn
  exclusion; `plan_gate_check.py` invocation.
- **Test scenarios:**
  - Inactive planning session → ALLOWS (inert, byte-identical to no hook).
  - All predicates pass → ALLOWS (auto-bless).
  - plan_gate non-zero / missing contract / no `contradictions-checked` marker / non-empty contradictions /
    lens gap → BLOCKS with the specific reason.
  - K failed attempts on an unchanged snapshot → terminal `plan_blocked` ALLOWS the stop (no infinite loop).
  - `PraxisUnreachable` → BLOCKS loudly; the scoped escape stands down planning only (build enforcement intact).

---

## Dependencies / Sequencing

```
U1 ─┬─ U3 ─ U4 ─┐
    │           ├─ U6
U5 ─┴───────────┘        (U2 skill anytime; U6 needs U1 + U4 + U5)
```

- **U1 first** (Praxis-client prerequisite) — U3/U4/U6 depend on it.
- **U4 depends on U3** (pure helpers) + U1; **U6 depends on U4 + U5 + U1**.
- U2 (skill) is independent prose; land it with U4 so the gate has an episode to read.

**Assumptions**
- The REST endpoints behind `praxis_record_episode` / `praxis_get_contradictions` are reachable from the
  hook client's transport — verify at U1.
- A `contradictions-checked` marker exists or can be stamped by intake when it runs detection — if intake
  doesn't currently mark this, U2/U5 add the stamp (raw-bulk path must stamp "checked=false" honestly).

---

## Risk Analysis & Mitigation

| Risk | Mitigation |
|---|---|
| Praxis I/O planned where no client exists | U1 prerequisite adds the wrappers; `evaluate_plan` stays pure; episode write is a skill MCP call |
| Auto-bless ships a contradictory/padded plan | Gate on evaluator ACTIONS not a count (KTD3); require `contradictions-checked` evidence, not just empty (KTD4) |
| Rigor claim overstated | Scoped honestly: structural rigor rises; qualitative review is sampled on failure (Summary) |
| Planning session loops forever on an unresolvable predicate | Bounded terminal escalation after K unchanged-snapshot failures → `plan_blocked` ALLOWS the stop (KTD5) |
| Standing down planning disables build enforcement | The hook uses its own scoped escape, distinct from `FACTORY_GATE_DISABLED` (KTD5) |

---

## Verification Strategy

- Suite green; inactive-planning behavior byte-identical to today (U6 regression).
- End-to-end: an intake with no signed contract cannot bless — `plan_completeness` blocks with the
  `R-CONTRACT-SIGNED` reason; a padded-count-but-no-actions contract also blocks; signing (evaluator, actions
  recorded) + a `contradictions-checked` clean snapshot auto-blesses with no human.
- Deadlock guard: a plan with an unresolvable contradiction hits the K-attempt cap and terminates with
  `plan_blocked`, not an infinite re-block.
