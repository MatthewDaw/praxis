---
title: "feat: Shared graded-check candidate pool + per-ticket rubric assembly"
type: feat
status: active
created: 2026-07-21
deepened: 2026-07-22
depth: deep
area: agent_factory
origin: agent_factory/docs/brainstorms/2026-07-21-planning-time-rubric-seeding-requirements.md
companion_plan: agent_factory/docs/plans/2026-07-21-001-feat-graded-rubric-checks-plan.md
---

# feat: Shared graded-check candidate pool + per-ticket rubric assembly

## Summary

Give the agent factory a **shared graded-check candidate pool** that two contexts write to,
and a **per-ticket assembly** step that draws each ticket's rubric from it — built as a thin
extension of the completed companion plan, not a parallel system.

- **Gather (two writers, one pool).** Both contexts author into the project's
  `building-validation` snapshot: `af-intake-plan` (whole-plan B1 findings) and `af-build`'s
  ticket-start search (ticket-local + rules search). A check carries a **`meta.candidate`
  flag**: `candidate:true` = a non-gating suggestion in the pool; `candidate:false`/absent =
  a hard gate exactly as today (the existing binary floor gates and any check a human authors
  directly). **Both writers contribute `candidate:true` entries with a `severity` hint; neither
  decides gating.**
- **Assemble (at the build-time synthesis seam).** When the worker synthesizes validations
  for a ticket (the companion's existing fold step, which calls `pin_validations`), a rubric
  **assembly** function reads the ticket's resolved `candidate:true` set and tiers it:
  **promote** the highest-value candidates to individual gating graded validations under a
  per-ticket budget, and **fold** the rest into ONE **min-of-candidates** advisory-aggregate
  graded validation. Everything rides the companion engine unchanged (`kind:"graded"`,
  min-of-axes, frozen rubric, content-hash cache, loop guards).

**Grounded in the merged companion code.** The companion already ships the non-gating
candidate mechanism (`seeded_candidates` in `agent_factory/src/agent_factory/seeded_checks.py`),
the graded engine (`agent_factory/src/agent_factory/graded_verdict.py`,
`agent_factory/src/agent_factory/rubric.py`), and `_norm_validation`'s graded pass-through
(`agent_factory/hooks/_ticket_state.py:392`). Its own rule — *"promotion to a hard gate is
out-of-band: author the check into Praxis"* — is the basis for the `candidate` flag here. This
plan makes the pool **project-specific and agent-writable** (the seeded library is global +
hand-edited) and adds the tiering step the worker's fold does not yet do.

**Origin & scope.** The origin brainstorm
(`agent_factory/docs/brainstorms/2026-07-21-planning-time-rubric-seeding-requirements.md`)
scoped only the planning-time writer; this plan carries that forward and adds the shared-pool
model, the `candidate` discriminator, and the assembly step, decided during this planning
session. The origin doc is updated to match (U7). The companion plan (`status: completed`)
owns the engine.

---

## Problem Frame

Two forces, both named by the completed companion plan and confirmed against the code:

1. **No whole-plan view at build time.** `af-build` authors checks ticket-locally and cannot
   see the cross-ticket concerns `af-intake-plan`'s B1 sweep computes
   (`skills/af-intake-plan/SKILL.md` §B1) — that view is gone by build time.
2. **No shared, agent-writable candidate pool, and no tiering.** The companion's candidate
   lane is a **global hand-edited file** (`seeded_checks.toml`), not something the two
   contexts write per project; and the worker folds candidates into validations without
   deciding which should *gate* vs be advisory. Coverage is neither accumulated across
   contexts nor deliberately tiered.

The fix: make `building-validation` the shared candidate pool (with a `candidate` flag), and
give the build-time synthesis step an assembly function that composes each ticket's rubric.

---

## Scope Boundaries

**In scope**
- A `meta.candidate` discriminator in RESOLVE so `candidate:true` checks are non-gating and
  queryable, `candidate:false` gate as today (U1 — the one owned engine change).
- `af-intake-build-validation` authors graded checks in both modes (gating and candidate) (U2).
- `af-intake-plan` B5b contributes candidates (and must-gate cross-ticket checks) via the
  sibling (U3).
- `af-build`'s ticket-start search persists discovered candidates via the sibling (U4).
- A rubric-assembly function at the build-time synthesis seam: deterministic promotion under a
  per-ticket budget + one min-of-candidates advisory aggregate (U5).
- Candidate pool scoping + lifecycle (tight scope, staleness/GC) (U6).
- Survival characterization + origin-doc reconciliation + docs (U7).

**Out of scope (kept as-is)**
- The grading engine, seeded-library file/loader, judge, content-hash cache, loop guards
  (companion) — unchanged (R4). The one engine touch is the `candidate` discriminator (U1),
  called out explicitly rather than disclaimed.
- The whole-diff ce-* WORK-review panel (companion scope boundary).
- Provenance *machinery* — unnecessary; one optional `authored_by` debug breadcrumb only.

### Deferred to Follow-Up Work
- Auto-adjustment coverage for pool candidates (companion U7 / Phase 2 edits the seeded file;
  pool candidates are project Praxis facts — see Assumptions).
- Caching the assembled per-ticket rubric across passes (optimize only if it proves costly).
- Dashboard view of the pool / per-ticket composition.

---

## Requirements Traceability

| Req | Advanced by |
|---|---|
| R1 — planning derives candidates from whole-plan audit | U3 |
| R2 — candidates land in the shared `building-validation` pool feeding the pipeline | U2, U3, U4 |
| R3 — ticket-start must not delete pool candidates / gating checks | U7 |
| R4 — reuse the grading engine (one explicit, scoped engine change only) | U1, U2, U5 |
| New — `candidate` gating discriminator | U1 |
| New — af-build search writes to the shared pool | U4 |
| New — per-ticket rubric-assembly at the synthesis seam | U5 |

---

## Key Technical Decisions

1. **`building-validation` is the single shared pool; a `meta.candidate` flag separates
   gating from poolable.** `resolve_validation_requirements` (`hooks/_ticket_state.py:236`)
   today returns every matching `category=check` into `required_validations` (all gate). Add
   one discriminator: `candidate:true` checks are **excluded** from `required_validations` and
   returned by a **deterministic** candidate query (tag/`*`/surface); `candidate:false`/absent
   behave exactly as today. **Rationale:** this is the "non-gating candidate in the shared
   place" the plan needs, grounded in the companion's own rule (*"promotion to a hard gate is
   out-of-band: author into Praxis"*). It is the ONE owned engine change — named, not
   disclaimed under R4. The deterministic query (not the semantic `retrieve_advisory_checks`,
   which is embedding top-k) is what lets the assembler enumerate *all* of a ticket's candidates.

2. **A separate assembler function decides gating at build time — neither writer does.** The
   sequence is: `af-intake-plan` writes candidates to the pool → `af-build` ADDS its ticket-local
   discoveries to the SAME pool → THEN, per ticket at build time, the rubric assembler determines
   what must be gated. Both writers only contribute `candidate:true` entries + a `severity` hint;
   neither authors a `candidate:false` graded gate or makes a mandatory-vs-advisory call. The
   assembler promotes high-severity candidates (severity + budget) to individual gating validations
   and folds the rest into one min-of-candidates advisory aggregate; the aggregate's soft-floor is
   the backstop so an egregious folded concern still surfaces as a gate rather than being silently
   demoted. This keeps the gating decision in the ticket-local context that runs the build, by design.

3. **Assembly runs at the build-time synthesis seam, not `start_ticket`.** The worker already
   folds candidates into synthesized validations and calls `pin_validations` at build time
   (`hooks/_ticket_state.py:430`, invoked from `skills/af-build/SKILL.md`). The assembler
   *extends* that step; it does NOT insert at `start_ticket:861` (which pins the coverage
   contract, not validations). This is the correct seam and makes U5 a true extension, not a
   rewrite.

4. **The advisory aggregate is min-of-candidates, deterministic, one per ticket.** Folded
   candidates are each an axis of ONE aggregate graded validation whose verdict is the **worst**
   folded candidate (min), not a blended average — preserving the companion's anti-masking
   invariant one layer up. Promotion is **deterministic** given the resolved candidate set
   (severity + budget with a stable tie-break), so the gating set cannot change across passes
   on identical code (no thrash; the companion's frozen-rubric/cache freeze each rubric but not
   *which* candidates gate — this closes that gap). Cost is O(1) in candidate count: ≤budget
   promoted judge calls + one aggregate call. *Open decision:* aggregate soft-floor (trips only
   on egregious min) vs purely informational. Default: soft-floor on an egregious min, so a
   genuine over-budget defect can still surface as a gate.

5. **Pool candidates and gating checks both survive ticket-start; re-resolved, not deleted.**
   `pin_requirements` (`hooks/_ticket_state.py:374`) truncates only `pinned_checks` (per-pass
   eval); the pool is re-queried each pass. U7 characterizes this before any behavior is trusted.

6. **One optional debug breadcrumb, not provenance machinery.** `meta.authored_by ∈
   {planning, build, manual}` — passive, never read by any gate/resolver. Skippable.

---

## High-Level Technical Design

*Directional guidance for review, not implementation specification.*

```
GATHER — two writers, one pool (building-validation); NEITHER decides gating
  1. af-intake-plan  B1 whole-plan findings ─► candidate:true (+severity hint)
  2. af-build ticket-start search ──────────► candidate:true (+severity hint; via sibling — U4)
  (all authored THROUGH af-intake-build-validation → single-writer lock holds)
  (candidate:false stays for the existing binary floor gates / human-authored gates only)

RESOLVE (U1 discriminator)
  candidate:false → required_validations (GATES, as today — floor/manual gates)
  candidate:true  → deterministic candidate query (NON-gating), input to assembly

THEN — a separate function determines what gates (per ticket, build time)

ASSEMBLE — at build-time worker synthesis (U5; extends companion fold step)
  read ticket's candidate:true set
  ┌─ deterministic tier (severity + budget, stable tie-break) ───────────┐
  │  promote ≤budget ─► individual GATING graded validations             │
  │  fold the rest ──► ONE min-of-candidates aggregate validation         │ (soft-floor)
  └───────────────────────────────────────────────────────────────────────┘
  pin_validations (rubric copied via _norm_validation)   ← companion engine
  VERIFY graded → judge(code, rubric) → passed → all_validations_passed   ← companion engine
```

---

## Implementation Units

### U1. `meta.candidate` discriminator in RESOLVE

- **Goal:** Make `candidate:true` `building-validation` checks non-gating and queryable while
  `candidate:false`/absent gate exactly as today.
- **Requirements:** R2, R4 (the one owned engine change), New.
- **Dependencies:** none.
- **Files:** `agent_factory/hooks/_ticket_state.py` (`resolve_validation_requirements:236` —
  exclude `candidate:true` from the gating set; add a deterministic `pool_candidates(ticket)`
  query over tag/`*`/surface filtered to `candidate:true`);
  `agent_factory/tests/test_candidate_discriminator.py`.
- **Approach:** In the tag/`*`/surface resolution, split by `meta.candidate`. Gating path is
  byte-identical to today for `candidate:false`/absent. Add `pool_candidates()` returning the
  `candidate:true` matches (with rubric intact) for the assembler — deterministic, NOT the
  semantic `retrieve_advisory_checks`.
- **Patterns to follow:** the existing tag/`*`/surface lanes in `resolve_validation_requirements`;
  `normalize_tag` for tag matching.
- **Test scenarios:**
  - A `candidate:false` graded check resolves into `required_validations` (gates) — unchanged.
  - A `candidate:true` graded check is absent from `required_validations` and present in
    `pool_candidates()` for a tag-matching ticket.
  - `pool_candidates()` is deterministic (returns the full matching set, not a top-k sample).
  - A `candidate:true` surface-bound check never resolves onto a backend-only ticket.
  - Regression: a plan with no `candidate` fields behaves byte-identically to today.

### U2. af-intake-build-validation authors graded checks (gating and candidate)

- **Goal:** Let the sibling author a graded check into `building-validation` in either mode:
  `candidate:false` (gates) or `candidate:true` (pool), `meta.kind="graded"` + `meta.rubric`,
  no `run`.
- **Requirements:** R2, R4.
- **Dependencies:** U1.
- **Files:** `agent_factory/skills/af-intake-build-validation/SKILL.md` (Step 1/2 — graded +
  candidate authoring); `agent_factory/tools/resolve_preview.py` (show `kind` + `candidate`
  status in `--by-check`); `agent_factory/tests/test_graded_candidate_authoring.py`.
- **Approach:** Extend the check write to accept `kind:"graded"`, a `rubric` (reuse
  `rubric_from_dict` from `agent_factory/src/agent_factory/rubric.py`), and a `candidate` bool.
  Reject a graded check with a missing/malformed rubric at author time.
- **Patterns to follow:** binary authoring in `af-intake-build-validation/SKILL.md` Step 2;
  `SeededCheck`/`rubric_from_dict` shapes in `src/agent_factory/seeded_checks.py` + `rubric.py`.
- **Test scenarios:**
  - A `candidate:true` graded check round-trips with `kind`, `rubric`, `candidate:true` intact.
  - A `candidate:false` graded check round-trips and gates (via U1).
  - A graded check with a missing/malformed rubric is rejected at author time.
  - `resolve_preview --by-check` shows the check's `kind` and candidate/gating status.

### U3. af-intake-plan B5b contributes candidates (first pool writer, no gating decision)

- **Goal:** From B1's whole-plan findings, author every quality finding as a `candidate:true` pool
  entry with a `severity` hint — via the sibling, single-writer lock preserved. af-intake-plan makes
  **no** gating decision (no `candidate:false` graded gate); the build-time assembler decides gating.
- **Requirements:** R1, R4.
- **Dependencies:** U2.
- **Files:** `agent_factory/skills/af-intake-plan/SKILL.md` (§B5b-graded — candidate derivation +
  delegation; §B8 records authored ids).
- **Approach:** Extend B5b's derive-then-delegate. Each B1 quality finding → a `candidate:true`
  candidate with a `severity` hint (higher for expensive/invisible cross-ticket misses), the axis(es)
  from the firing lens, a default threshold (from the seeded-library value for that axis; do NOT
  invent aggressive thresholds), and a TIGHT tag/surface scope. Author all through
  `af-intake-build-validation`. The mandatory-vs-advisory call is the assembler's (U5), at build time.
- **Patterns to follow:** existing B5b binary-guard derivation + delegation; axis vocabulary in
  `src/agent_factory/seeded_checks.py`.
- **Test scenarios:**
  - Integration: a high-severity B1 finding produces a `candidate:true` pool entry (non-gating) with
    its `severity` hint, visible in `resolve_preview --by-check` as a candidate.
  - af-intake-plan authors no `candidate:false` graded gate from B1 findings.
  - af-intake-plan never writes `building-validation` directly (delegates to the sibling).

### U4. af-build ticket-start search persists candidates to the pool (via sibling)

- **Goal:** The second writer — `af-build`'s ticket-start search — persists ticket-local
  discovered candidates as `candidate:true` into the SAME pool, THROUGH `af-intake-build-validation`
  (preserving the single-writer lock — resolves the reviewer's second-writer finding).
- **Requirements:** R2, New.
- **Dependencies:** U2.
- **Files:** `agent_factory/skills/af-build/SKILL.md` (RESOLVE/search step — persist via the
  sibling, not a direct write); `agent_factory/tests/test_build_search_pool_write.py`.
- **Approach:** Where af-build's search finds a candidate, author it as `candidate:true` via
  `af-intake-build-validation` with a stable `check_id` (idempotent update) and
  `authored_by:build`. Scope it **tightly** to the originating ticket's tags/surface — never a
  broad domain tag — so it does not bleed onto unrelated future tickets (U6).
- **Patterns to follow:** U2's candidate authoring; `check_id` idempotency from
  `af-intake-build-validation` Step 2.
- **Test scenarios:**
  - A search-discovered candidate is authored via the sibling and appears in `pool_candidates()`.
  - Re-discovery of the same `check_id` updates in place (no duplicate).
  - A planning candidate and a build candidate for the same ticket both appear in
    `pool_candidates()`.
  - The build candidate's scope does not resolve it onto an unrelated ticket lacking its tag.

### U5. Rubric-assembly at the build-time synthesis seam

- **Goal:** Extend the worker's synthesis step to read the ticket's `pool_candidates()`, tier
  them deterministically under a per-ticket budget (promote → individual gating graded
  validations; fold rest → ONE min-of-candidates aggregate), and pin.
- **Requirements:** R4, New.
- **Dependencies:** U1, U2, U3, U4, companion engine.
- **Files:** `agent_factory/src/agent_factory/rubric_assembly.py` (the assembler);
  `agent_factory/skills/af-build/SKILL.md` (synthesis step invokes it before `pin_validations`);
  `agent_factory/hooks/_ticket_state.py` (only if a helper is needed at the synthesis call site);
  `agent_factory/tests/test_rubric_assembly.py`.
- **Approach:** `assemble(pool_candidates, budget) -> [validations]`. **Deterministic** promotion:
  sort by severity then a stable tie-break (e.g. `check_id`), promote the top ≤budget to
  individual graded validations (rubric from the candidate). Compose ONE aggregate graded
  validation whose rubric scores every folded candidate as its own axis and takes the **min**
  (Decision 4); soft-floor on an egregious min. Return for `pin_validations`; frozen rubric per
  validation comes from the candidate via `_norm_validation`. Runs at the synthesis seam (build
  time), never at `start_ticket`.
- **Execution note:** Test-first with a stubbed `Complete`; tiering + budget + min-aggregate +
  determinism invariants covered without a live model.
- **Patterns to follow:** the fold-into-validations behavior the companion's worker already does;
  `graded_verdict.py` verdict rule; `_norm_validation` graded shape.
- **Test scenarios:**
  - Two high-severity + five low-severity candidates, budget 3 → three gating validations + one
    aggregate covering the other four.
  - Exactly ONE aggregate validation regardless of candidate count (O(1) invariant).
  - Aggregate verdict is the MIN over folded candidates (a single bad folded candidate fails the
    aggregate) — not an average (anti-masking).
  - Determinism: the same resolved candidate set yields the same promoted set + aggregate across
    two passes on identical code (no thrash).
  - Budget overflow: more high-severity candidates than budget → overflow folds into the
    aggregate (whose min still surfaces the worst); overflow recorded.
  - Aggregate soft-floor: an egregious min gates; a merely-mediocre min does not.
  - Zero candidates → no graded validations authored; ticket falls back to acceptance floor.

### U6. Candidate pool scoping + lifecycle

- **Goal:** Keep the pool from bleeding across tickets or growing unbounded.
- **Requirements:** New.
- **Dependencies:** U2, U4.
- **Files:** `agent_factory/skills/af-intake-build-validation/SKILL.md` (scoping guidance —
  candidates default to tight tag/surface, never `["*"]`); `agent_factory/hooks/_ticket_state.py`
  or a small tool (staleness/GC); `agent_factory/tests/test_pool_lifecycle.py`.
- **Approach:** Build-discovered candidates scope to the originating ticket's tags/surface by
  default (U4). Add a staleness policy: a candidate not resolved/used within N build passes (or
  tied to a finished-and-unregressed ticket) is pruned or marked inactive, so `pool_candidates()`
  stays bounded. Reuse `resolve_preview --by-check` to make bleed visible before it ships.
- **Patterns to follow:** `af-intake-build-validation` Step 2b `--by-check` fan-out check;
  `applies_to` hygiene guidance.
- **Test scenarios:**
  - A tightly-scoped build candidate resolves only onto its originating ticket's tag, not a
    broad domain set.
  - A stale candidate (unused N passes) is pruned/inactive and no longer in `pool_candidates()`.
  - `resolve_preview --by-check` surfaces a too-broad candidate before it ships.

### U7. Survival characterization, origin reconciliation, and docs

- **Goal:** Prove pool candidates + gating checks survive ticket-start; reconcile the origin doc
  to the expanded scope; document the model.
- **Requirements:** R3, R4.
- **Dependencies:** U1, U5.
- **Files:** `agent_factory/tests/test_pool_candidate_survival.py`;
  `agent_factory/docs/brainstorms/2026-07-21-planning-time-rubric-seeding-requirements.md`
  (update to sanction the shared-pool + assembly scope — resolves the origin-contradiction
  finding); `agent_factory/docs/factory-state-contract.md` (the `candidate` flag, the two
  writers, the assembly seam); `agent_factory/skills/af-build/SKILL.md`;
  `agent_factory/skills/af-intake-plan/SKILL.md`.
- **Approach:** Characterization-first survival test (seed a `candidate:true` and a
  `candidate:false` check, run `start_ticket` twice, assert both re-resolve to their respective
  lanes). Current reading says this holds (truncation clears only `pinned_checks`) → expected
  outcome is a passing characterization with no production change; if a drop appears, apply the
  minimal additive re-resolution fix. Then update the origin doc and state contract.
- **Execution note:** Characterization-first; the survival test doubles as a regression guard.
- **Test scenarios:**
  - A `candidate:true` and a `candidate:false` check both re-resolve to their lanes after two
    consecutive `start_ticket` calls.
  - Regression: binary-only ticket `start_ticket` behavior byte-identical to today.
  - `Test expectation: none` for the doc-update portions (documentation-only).

---

## Dependencies / Sequencing

```
U1 ─┬─ U2 ─┬─ U3
    │       ├─ U4 ─┐
    │       └──────┼─ U5 ─┬─ U6
    └─ U7 ─────────┘      └─ (U7 docs after U5/U6)
   (U1 first — the discriminator; U2 gates on U1; U3/U4 write via U2;
    U5 needs U1+U2+U3+U4; U6 follows U4; U7 survival can start after U1, docs last)
```

- **U1 first** — the `candidate` discriminator everything depends on.
- **U2** (authoring) gates on U1; **U3** (planning writer) and **U4** (build writer) both write
  through U2.
- **U5** (assembly) needs U1–U4 + the companion engine.
- **U6** (lifecycle) follows U4; **U7** (survival + docs) can characterize after U1 and documents
  last.

**Assumptions**
- **Companion code verified merged:** `seeded_checks.py`/`seeded_candidates`, `graded_verdict.py`,
  `rubric.py`, and `_norm_validation` graded pass-through (`_ticket_state.py:392`) all exist.
- **Auto-adjust reachability (future companion U7):** companion U7 edits the seeded *file*; pool
  candidates are project `building-validation` facts, so U7 as designed cannot loosen a
  miscalibrated pool candidate. Carry into that work: reference the originating seeded `check_id`
  via `source_check_id`, or scope companion-U7 to also read project pool candidates.
- **Open decision (Decision 4):** aggregate soft-floor (default) vs purely informational.
- **Assumption (confirm in U7):** `start_ticket`/`pin_requirements` are the only truncation
  sites for pool state; grep confirmed these; U7's characterization is the proof.

---

## Risk Analysis & Mitigation

| Risk | Mitigation |
|---|---|
| Non-gating candidate has no code support | U1 adds the `candidate` discriminator explicitly — the one owned engine change, named not disclaimed |
| Whole-plan concern silently un-gated by ticket-local budget | Must-gate concerns authored `candidate:false` at intake, bypassing the pool entirely (Decision 2) |
| Single-aggregate masking (avg hides a real defect) | Aggregate is **min**-of-candidates, not average; soft-floor lets an egregious min gate (Decision 4) |
| Judge-assisted tiering thrashes the loop across passes | Promotion is **deterministic** (severity + budget + stable tie-break); gating set is stable on identical candidates (Decision 4) |
| Assembler pinned at the wrong seam (`start_ticket`) | Assembly runs at the build-time synthesis step where `pin_validations` is called (Decision 3) — extends, not rewrites |
| Second writer breaks the single-writer lock | af-build's search persists THROUGH `af-intake-build-validation` (U4), same as planning |
| Pool bleed / unbounded growth | Tight per-ticket scoping + staleness/GC + `--by-check` visibility (U6) |
| Pool candidate dropped at ticket start | U7 characterization proves survival before behavior is trusted |

---

## Verification Strategy

- Lands with `all_validations_passed` and binary-only / candidate-free ticket behavior
  byte-identical to today (U1, U7 regression guards).
- End-to-end: a high-severity cross-ticket B1 finding gates directly (`candidate:false`); a
  low-severity finding and an af-build search discovery both land as `candidate:true`; at build
  synthesis the assembler promotes within budget and folds the rest into one min-aggregate; the
  judge grades them; the ticket finishes only when the promoted (and any soft-floor-tripping
  aggregate) checks pass — deterministically across passes on identical code.
- `resolve_preview --by-check` shows gating vs candidate status and surfaces any too-broad
  candidate before it ships.
