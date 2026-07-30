---
description: Drive ANY project end-to-end through the full factory pipeline autonomously — af-plan (brainstorm) → af-intake-plan (admit + harden + bless) → af-build (build + verify) — resuming from whatever stage the project has already reached. For work straightforward enough to let the agent make every judgment call. Reviews still run; mechanically-fixable gate blocks are self-resolved, human-mandated surfaces are parked, never overridden.
argument-hint: [project name] [optional scope/instructions, e.g. "backend-only" or "stop after the plan"]
---

The user invoked `/af-super-run`. Chain the factory's three stages for ONE project and keep going
until the project is built or genuinely parked. This is the general pipeline runner — it belongs to
no repo, and takes the project it operates on from the argument or the environment.

## 0. Resolve the project name — refuse rather than guess

Per the blessed requirement R1 (`af-super-run` project), in this order:

1. the **project argument**, used verbatim;
2. else **`FACTORY_PROJECT`** (from the environment or the repo's `.claude/settings.local.json`),
   with any `prd-` prefix stripped;
3. else a **slug derived from the idea** the user described — and you must **record that derived name
   as a Praxis episode BEFORE any other write**, so the name is never invented mid-flight.

**A run that cannot resolve a name refuses to start.** Use the ONE resolved bare name for every
Praxis call in all three stages. **Never pass a `prd-`-prefixed name to the completeness endpoints**
(`praxis_incomplete_requirements` and friends add the prefix themselves; passing it returns EMPTY and
silently hides all work).

## 1. Find the stage — resume, never restart

Read the project's live state before doing anything, and enter at the first unsatisfied stage. A
project that is already blessed must NOT be re-brainstormed.

| Observed state | Enter at |
|---|---|
| No brainstorm/requirements doc for the idea | **Stage A — af-plan** |
| Doc exists, but `prd-<project>` holds no requirements | **Stage B — af-intake-plan** |
| Requirements exist but the plan is not blessed / `plan_gate_check` does not report ADMITTED | **Stage B — the audit + bless gate** |
| Blessed, with claimable incomplete tickets | **Stage C — af-build** |
| Zero claimable tickets | **Done** — report the terminal state and stop |

Useful probes: `praxis_facts_by(category="requirement", space=<project>, snapshot="prd-<project>")`,
`praxis_incomplete_requirements(<project>)` (BARE name), and
`agent_factory/tools/plan_gate_check.py <project>`.

## 2. Run the stages

- **Stage A — `agent-factory:af-plan`.** Explore/research the idea into the messy exhaustive
  requirements doc. Writes no Praxis state.
- **Stage B — `agent-factory:af-intake-plan`.** Admit the doc's requirements as tickets, harden them,
  run the audit, and clear the bless gate. Every ticket it mints must carry `acceptance` and `verify`
  — a ticket lacking either is stamped `under_specified` and is NEVER claimed, so it would stall the
  run while looking merely queued.
- **Stage C — `agent-factory:af-build`.** Drive the incomplete set to done. It owns its own
  completeness Stop gate, which is what forces the build half to finish.

Read each skill's own instructions in full and follow them verbatim; this command supplies only the
project identity, the resume point, and the autonomy contract below.

## 3. The autonomy contract — what "autonomously" may and may not mean

**Both review panels stay ON.** "It still runs through all of the reviews" is a hard requirement; do
not disable the plan-review or work-review panel to make the run smoother.

Then, for every gate block:

- **Mechanically fixable → fix it and continue.** A missing acceptance condition, an absent `verify`
  mode, a dangling `depends_on`, a failing lint/typecheck/test check, a near-duplicate ticket: resolve
  it and proceed without asking.
- **A judgment call → decide, and record a Praxis decision episode** naming the alternatives not
  taken, so the owner can review and override it later.
- **Human-mandated → PARK it. Never override it.** These have no legal autonomous move:
  - a **surfaced contradiction** — you never settle one yourself;
  - a **`verify="manual"` requirement** — its pass needs an external attestation
    (`PRAXIS_ATTESTED_CALLER`) that a build worker structurally cannot produce;
  - a ticket in terminal **`build_state="blocked"`**;
  - the **plan bless gate** where the chain mandates human surfacing.

The honest terminal state is therefore *"done except N parked"*, and you must report it that way.
Never weaken a gate, never self-attest a manual pass, and never mint a requirement whose acceptance
condition cannot be met — a requirement with no achievable binary acceptance makes every downstream
gate either block forever or get quietly weakened to satisfy it.

## 4. Report

State the resolved project name, which stage the run entered at, what each stage produced, and the
terminal state: built, or *done except N parked* with each parked item and why. If the user asked to
stop after a stage (e.g. "stop after the plan"), honor it and say where it stopped.

## Never

- **Never invent a project name mid-flight**, and never pass a `prd-`-prefixed name to the
  completeness endpoints.
- **Never re-run a completed stage** — resume from live Praxis state.
- **Never disable a review panel or weaken a gate** to reach a clean finish.
- **Never override a parked human-mandated surface** (contradiction, manual verify, blocked ticket,
  bless gate) — surface it instead.
- **Never push on the user's behalf.**

> **Scope note.** This command chains the three stages and enforces the autonomy contract. The
> designed cross-phase *run substrate* — a run-identity marker covering af-plan's Praxis-blind window,
> a token budget, digest, and mid-run resume (`agent_factory/docs/brainstorms/2026-07-25-af-super-run-requirements.md`,
> Releases 1–2) — is **not** implemented here; only its Release-0 substrate fixes (S1–S13) have landed.
> A run that dies mid-flight is resumed by invoking this command again, which re-enters at the correct
> stage from Praxis state.

$ARGUMENTS
