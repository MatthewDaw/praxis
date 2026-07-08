---
name: af-intake-plan-validation
description: >
  Add ONE planning-validation lens to a project's `planning-validation` snapshot — the section-locked
  sibling of af-intake-plan (the plan) and af-intake-build-validation (the build gates). A planning lens
  is a declarative "how to plan" consideration the af-intake-plan AUDIT (Part B) must close for every
  requirement it bears on (e.g. "any app with user accounts needs a credential-recovery flow", "every
  destructive action needs an undo/confirm"). A lens OWNS its own applicability predicate
  (`meta.applies_to` tags / `["*"]`) and is GLOBAL/`applies_when`-bound, not ticket-bound. This command
  writes ONLY into `(space=<project>, snapshot=planning-validation)` and touches nothing else — no plan
  requirements, no build gates. It also re-arms the audit so the plan is no longer blessable until the new
  lens is closed. To add a BUILD gate use af-intake-build-validation; to add a requirement use af-intake-plan.
---

## What this command does (and does NOT)

This is ONE of three section-locked intake commands, each the SOLE writer of one canonical snapshot in
the project space (`space == the bare project name`):

| command | writes into | contents |
| --- | --- | --- |
| `af-intake-plan` | `prd-<project>` | the plan: requirement tickets, `renders` bindings, deps |
| `af-intake-build-validation` | `building-validation` | validation checks af-build reads |
| **`af-intake-plan-validation`** (this) | **`planning-validation`** | planning lenses the audit reads |

**This command writes EXACTLY ONE `category="check"`, `scope="planning"` fact into the
`planning-validation` snapshot and nothing else** (plus one re-arm episode, Step 3). It never touches the
plan (`prd-<project>`), the build gates, or ticket state. That single-section lock is the point: the
server enforces it too (a `scope="planning"` check is the only kind the `planning-validation` snapshot
admits, and a check can never land in a `prd-*` plan — the write-time section invariant), so you cannot
silently co-mingle a planning lens with the plan or a build gate. If you meant to add a *build* gate or a
*requirement*, STOP and use the right command above.

All Praxis access follows **`docs/af-memory-policy.md`** (tenancy §0, `insight` vs `ingest`, snapshot
targeting). Praxis is a HARD dependency: if the write cannot reach Praxis, **fail closed** (error and
stop) — never fall back to a file.

## Step 0 — Tenancy + target the section

Confirm tenancy per `docs/af-memory-policy.md` §0. The lens must land in the snapshot the audit reads, or
it is a silent no-op: `scope="planning"` → **`(space=<project>, snapshot="planning-validation")`**. You
target it by passing **both** `space` and `snapshot` on the write itself —
`praxis_add_insight(..., space="<project>", snapshot="planning-validation")`. (A bare `praxis_add_insight`
with no `space`/`snapshot` writes your personal WORKING MEMORY, which the audit never reads — the silent
no-op to avoid; `praxis_select_space` does NOT make a write target a snapshot.) Writing a planning lens
into `prd-<project>` is refused by the server's section invariant; writing it into `building-validation`
makes it invisible to the audit.

> **Per-project, not global.** `planning-validation` is a SNAPSHOT in THIS project's own space — the
> planning checklist is no longer a single global library. A lens authored here governs only this
> project's plan.

## Step 1 — Infer the lens from the request

A planning lens is a consideration the audit applies while validating the plan. From the one-liner infer:
- **criterion** — the consideration (e.g. "any app with user accounts needs a credential-recovery
  (password reset) flow");
- **angle** — a short lens label (`auth`, `states`, `security`, `data-lifecycle`, `rollback`, `privacy`);
- **applies_to** — `["*"]` for always, else the gating tags.

A lens is GLOBAL by design — it is `applies_when`-bound (a consideration the whole plan must satisfy),
NOT tag/surface-bound to individual tickets the way a build gate is. Prefer `["*"]` unless the lens
genuinely only applies to a class of plan.

## Step 2 — Write the lens into `planning-validation`

```
praxis_add_insight(
  insight  = "<criterion>",
  source   = "planning-checklist",
  category = "check",
  scope    = "planning",
  meta     = { "check_id": "<stable-slug>", "applies_to": ["<tag>", ...] | ["*"], "angle": "<lens-label>" },
  space    = "<project>",              # REQUIRED — target the org-shared snapshot,
  snapshot = "planning-validation",    # not working memory (on_conflict N/A for lenses)
)
```

- Keep **`source="planning-checklist"`** (the lens-library identity) but the `space`/`snapshot` pair is
  what LANDS the fact in THIS project's `planning-validation` snapshot — the source string is provenance,
  the `(space, snapshot)` is where it lives. Omit the pair and it goes to working memory (the audit never
  reads it). VERIFY: `praxis_facts_by(category="check", scope="planning", space="<project>",
  snapshot="planning-validation")` should now list it.
- **Identity is `meta.check_id`, NOT the prose.** The server keys lens writes on `meta.check_id` and NEVER
  text-dedups or reconciles them: re-admitting the SAME `check_id` UPDATES that one fact in place; a
  DIFFERENT `check_id` is ALWAYS a new distinct lens, even if the criterion reads like an existing one. Give
  each lens a stable, DISTINCT `check_id`. `on_conflict` does not apply (no merge, no contradiction).

The active `scope="planning"` checks in that snapshot ARE the planning checklist af-intake-plan's audit
(Part B3) pulls. The lens takes effect on the **next plan** for this project: the audit queries the
project's active planning checks and must close every lens whose `applies_to` matches, for every
requirement it bears on.

## Step 3 — Re-arm the audit (REQUIRED — this is not optional for a planning lens)

Adding a lens makes the latest **panel-ran episode** STALE — it covered a checklist that no longer
includes this lens. Record a re-arm episode so the audit cannot be treated as still-passed:

```
praxis_record_episode(
  text="Re-armed prd-<project> plan audit: planning checklist extended with check <check_id> (<angle>); prior panel-ran is stale and the audit must reconvene to close the new lens.",
  outcome="pending",
)
```

The human's planning gate (af-intake-plan Part B9) is satisfied only by a panel-ran episode covering the
CURRENT active checklist; because the new lens post-dates the last panel-ran assertion, the plan is no
longer blessable until af-intake-plan's audit reconvenes and closes the new lens for every requirement it
bears on. (Unlike a build gate, a planning lens has no per-ticket "regress the work" step — it re-arms
the whole-plan audit, not individual tickets.)

## Report

Report the lens you wrote (id, `angle`, `applies_to`, criterion, and that it landed in
`planning-validation`), and that the audit is re-armed (the plan is no longer blessable until Part B
reconvenes). If any Praxis call failed, report the failure — never claim success.

## Never

- **Never** write into any snapshot other than `planning-validation` — not the plan, not the build gates.
  To add a build gate use af-intake-build-validation; a requirement, af-intake-plan.
- **Never** skip the Step-3 re-arm — a new lens with a still-fresh panel-ran episode would let a plan be
  blessed without the new lens closed.
- **Never** write or read a `.factory/*.json` file, and **never** proceed if Praxis is unreachable (fail
  closed).
