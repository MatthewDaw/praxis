---
title: "Planning-time rubric-check seeding in af-intake-plan"
type: feat
status: draft
created: 2026-07-21
area: agent_factory
depth: standard
companion_plan: agent_factory/docs/plans/2026-07-21-001-feat-graded-rubric-checks-plan.md
---

# Planning-time rubric-check seeding in af-intake-plan

## Summary

Give the agent factory **two complementary sources** of graded rubric checks, each
authoring at a different moment with the context it already holds:

1. **Planning-time (this doc).** During `af-intake-plan`, while Claude already has the
   **whole plan** in context (it is intaking every ticket at once), derive graded rubric
   checks that consider a ticket *in light of everything else* — the broad, cross-ticket
   implications. Author them through `af-intake-build-validation` into the
   `building-validation` snapshot.
2. **Execution-time (already built).** At ticket start, `af-build` slows down to a
   **single ticket**, does a **focused rules/memory search** for just that ticket, and
   authors additional checks from that narrow context. This is the graded-checks path in
   the companion plan (now `completed`).

The two are **not merged**. The separation is not protecting an invariant — it simply
falls out of *when* each runs and *what context is cheap to reach at that moment*: the
whole-plan view is in hand during intake and expensive to reconstruct later; the
ticket-focused rules search is what execution does when it commits to one ticket. Because
their contexts differ, they catch different things, so both are worth keeping.

This rides entirely on the completed grading engine (`kind:"graded"`, min-of-axes,
positive-evidence-of-defect, frozen rubric, content-hash cache, loop guards). It adds an
**authoring point**, not a new grading mechanism.

**Origin:** `/ce-brainstorm` session grounding on `github.com/trevormil/TerMinal`. The
transferable TerMinal discipline: *structure only what a downstream machine grades /
resumes / dispatches against, and generate it with whatever context is already loaded.*

---

## Problem Frame

The companion plan names the gap directly:

> *"The check-authoring agent also invents coverage from scratch each ticket, with no
> curated checklist to draw on, so it silently under-covers."*

Execution-time authoring is inherently ticket-local: at ticket start the worker only sees
that one ticket plus its rules search. It **cannot** see cross-ticket implications
(a missing hand-off between two requirements, a security concern that only appears when
you view the whole surface set) — that view is gone by build time. Yet `af-intake-plan`
*already computes exactly that view* in its B1 adversarial audit (`failure-modes`,
`security`, `data-lifecycle`, `rollback`, `who-pays` sweeps across the whole set). Today
those findings close as requirement edits or binary guards; their quality-axis signal is
not turned into graded checks. Seeding them at plan time is the whole-plan complement the
execution path structurally cannot provide.

---

## Requirements

### R1 — Planning derives graded rubric checks from its whole-plan audit
During `af-intake-plan`, extend the **B5b guard-derivation step** (which already derives
build-validation guards and delegates authoring to `af-intake-build-validation`) so that,
in addition to binary guards, it derives **graded rubric checks**: quality axes with
per-axis min thresholds and a judge prompt, drawn from the B1 lens findings and the
cross-ticket view. Each becomes a `kind:"graded"` check.

- **Acceptance:** given an intake whose B1 audit fires a quality-axis finding on a
  requirement, a corresponding graded check is authored into `building-validation` and
  resolves onto that requirement's ticket, observable via `resolve_preview --by-check`.

### R2 — Both planning sources export to ONE place, and it feeds the existing pipeline
The planning-time generation (R1) **and** the manual `/af-intake-build-validation` path
both write to the **same `building-validation` snapshot** — no separate store. That
snapshot is what the completed graded-checks pipeline already reads (RESOLVE → pin →
VERIFY → `all_validations_passed`). Planning-authored graded checks flow through the
identical `pinned_checks` shape and gate as execution-authored ones.

- **Acceptance:** a planning-authored graded check and an execution-authored graded check
  on the same ticket are indistinguishable to VERIFY — each contributes one `passed`
  boolean to `all_validations_passed`; no source-specific code path exists.
- **Note:** routing through `af-intake-build-validation` preserves the single-writer
  section lock on `building-validation` (`af-intake-plan` never writes the check section
  directly).

### R3 — Ticket-start must not delete planning-authored checks (make RESOLVE additive)
Today ticket start truncates the ticket's checks: `pin_requirements`
(`agent_factory/hooks/_ticket_state.py:376`) resets `M_PINNED_CHECKS` to `[]`, and
`pin_validations` (`:430`) "replaces `pinned_checks` wholesale." Under two authoring
sources this would let the execution-time pass **erase** the planning-seeded checks. The
build's focused search must **add to** the resolved set, never replace what planning
contributed.

- **Acceptance:** a ticket carrying a planning-authored graded check still has that check
  in its coverage contract after `af-build` starts it and runs its own RESOLVE/authoring;
  the execution-added checks are unioned in, not substituted.
- **Open decision (defer to planning):** the exact remediation — the cleanest path is
  likely to keep planning checks in `building-validation` and ensure ticket-start RESOLVE
  **re-resolves them by query** (so `required_validations` always re-includes them) while
  only the per-check *pass state* is truncated, not the authored set. Confirm whether the
  fix is "don't clear" vs "clear pass-state only, re-derive contract from
  `building-validation`" against the actual RESOLVE flow. Verify no regression to the
  companion plan's frozen-rubric guard.

### R4 — Reuse the grading engine; do not re-invent it
Planning-authored checks use the completed engine verbatim: `kind:"graded"`, the frozen
`rubric` shape (`_ticket_state.py:_norm_validation`), min-of-axes with per-axis
thresholds, positive-evidence-of-defect-to-fail, content-hash caching, and the loop
guards. Planning contributes **content** (which axes, which thresholds, for which
tickets), not machinery.

- **Acceptance:** no change to `all_validations_passed`, the judge, or the caching path is
  required to land R1–R3.

---

## Scope Boundaries

**In scope**
- A planning-time graded-check derivation step in `af-intake-plan` (extends B5b).
- Routing it through `af-intake-build-validation` into `building-validation`.
- Making ticket-start check handling additive so planning checks survive (R3).

**Out of scope / kept as-is**
- The grading engine, seeded library, judge, caching, and loop guards (companion plan).
- The execution-time focused-search authoring path (companion plan) — unchanged; this
  only stops it from clobbering planning's contribution.
- Any provenance / "which source authored this" machinery — **explicitly unnecessary**.
  The two sources are not isolated for protection; identical `check_id`s are idempotent
  and different contexts naturally yield different checks.
- Merging the two sources into one pipeline.

**Deferred**
- Rubric auto-adjustment (companion plan U7 / Phase 2).

---

## Dependencies / Assumptions

- **Depends on** the completed graded-checks plan (engine, schema, seeded library).
- **Assumption (needs confirmation at planning):** the ticket-start truncation at
  `_ticket_state.py:376`/`:430` is the *only* place planning-authored checks could be
  dropped. Grep confirmed these two truncation sites; verify no other clear/reset path
  (e.g. in `build_completeness_gate.py` or a re-baseline flow) also wipes them.
- **Assumption:** B1's lens findings carry enough located signal to seed a useful rubric
  axis + threshold. If a finding is too vague to become a min-score axis, it should stay a
  requirement edit / binary guard (do not force a graded check).

---

## Success Criteria

- A ticket's completion gate can be strengthened by checks that **only a whole-plan view
  could produce**, authored at intake without the worker reconstructing that context.
- Planning-authored and execution-authored graded checks coexist on a ticket; neither
  erases the other; both gate through the unchanged `all_validations_passed`.
- Zero changes to the grading engine were needed to add the planning authoring point.
