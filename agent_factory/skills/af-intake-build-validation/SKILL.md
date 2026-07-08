---
name: af-intake-build-validation
description: >
  Add ONE build-time validation check to a project's `building-validation` snapshot — the
  section-locked sibling of af-intake-plan (the plan) and af-intake-plan-validation (the planning
  lenses). A validation check is a declarative "must pass before a ticket is done" rule that OWNS its
  own applicability predicate (`meta.applies_to` tags / `["*"]` wildcard / surface bind) and a `run`
  command whose non-zero exit is failure; it NEVER names tickets and no ticket carries a check list —
  af-build's per-ticket RESOLVE query (tag ∪ "*" ∪ surface) picks it up. This command writes ONLY into
  `(space=<project>, snapshot=building-validation)` and touches nothing else — no plan requirements, no
  planning lenses. Use to graft a new coding/build gate onto an already-hardened plan (e.g. "every auth
  ticket must pass the login e2e", "typecheck must be green on every ticket"). To add a PLANNING lens use
  af-intake-plan-validation; to add a missing requirement/ticket use af-intake-plan.
---

## What this command does (and does NOT)

This is ONE of three section-locked intake commands, each the SOLE writer of one canonical snapshot in
the project space (`space == the bare project name`):

| command | writes into | contents |
| --- | --- | --- |
| `af-intake-plan` | `prd-<project>` | the plan: requirement tickets, `renders` bindings, deps |
| **`af-intake-build-validation`** (this) | **`building-validation`** | validation checks af-build reads |
| `af-intake-plan-validation` | `planning-validation` | planning lenses af-intake-plan's audit reads |

**This command writes EXACTLY ONE `category="check"`, `scope="validation"` fact into the
`building-validation` snapshot and nothing else.** It never touches the plan (`prd-<project>`), the
planning lenses, or ticket state. That single-section lock is the point: the server enforces it too (a
`scope="validation"` check is the only kind the `building-validation` snapshot admits, and a check can
never land in a `prd-*` plan — the write-time section invariant), so you cannot silently co-mingle a
build gate with the plan. If you meant to add a *planning* lens or a *requirement*, STOP and use the
right command above.

All Praxis access follows **`docs/af-memory-policy.md`** (tenancy §0, `insight` vs `ingest`, snapshot
targeting). Praxis is a HARD dependency: if the write cannot reach Praxis, **fail closed** (error and
stop) — never fall back to a file. This is a single decision-making agent; it may dispatch the read-only
retrieval sub-agent for bulk reading, never a crew that writes.

## Step 0 — Tenancy + target the section

Confirm tenancy per `docs/af-memory-policy.md` §0 (the right org, this project's per-project MCP cache).
The check must land in the snapshot af-build RESOLVE reads, or it is a silent no-op:
`scope="validation"` → **`(space=<project>, snapshot=building-validation)`**. Target it via the
snapshot-bound write path (`praxis_select_space("<project>")` sets the client's space default, then the
write targets the `building-validation` snapshot). Writing a validation check into `prd-<project>` (the
plan) instead is refused by the section invariant; writing it into `planning-validation` makes it invisible
to the build.

> **Per-project, not global.** `building-validation` is a SNAPSHOT in THIS project's own space (it was
> renamed from the retired global `coding-validation` space). A check authored here governs only this
> project.

## Step 1 — Infer the check from the request

A validation check is a build-time gate. From the one-liner infer:
- **criterion** — the fact text, the thing that must be true (e.g. "login works end-to-end against the
  live service");
- **run** — the command that PROVES it, non-zero exit = fail. Discover the repo's REAL command (the e2e
  runner, the typecheck, the suite); never assume a placeholder;
- **applies_to** — an ARRAY of requirement-class tags; `["*"]` = every ticket;
- optional **applies_when** / **surfaces**.

**`applies_to` hygiene — this is what makes frontend/backend separation AUTOMATIC (mechanical, not LLM
judgment):**
- **Universal gate** (typecheck, build, lint, test — must run on EVERY ticket) → `applies_to: ["*"]`.
  The wildcard lane resolves it onto every ticket, including tag-less/backend ones.
- **Context gate** (a rule tied to a domain class — auth, notifications, seed) → a **specific tag**
  (`["auth"]`), so it lands ONLY on tickets carrying that tag.
- **Frontend/UI gate** (Playwright E2E, visual-render, axe a11y, no-console-errors) → **surface-bind it**
  (`meta.surfaces` + the `renders` edge), NOT `["*"]`. A backend-only ticket renders no surface, so a
  surface-bound UI check **can never resolve onto it** — the guarantee is structural, not the build
  agent's N/A judgment. Do **not** author a UI check as `["*"]`+`applies_when` and lean on the agent to
  skip it.

## Step 2 — Write the check into `building-validation`

```
praxis_add_insight(
  insight  = "<criterion>",
  source   = "prd-<project>",
  category = "check",
  scope    = "validation",
  meta     = { "check_id": "<stable-slug>", "applies_to": ["<class-tag>", ...],
               "applies_when": "<condition | empty>", "surfaces": ["<screen-id>", ...],
               "run": "<command>" },
  on_conflict = "surface",
)
```

- **Idempotent on `meta.check_id`**: if one already exists, `praxis_edit_fact` it rather than duplicating.
- **`on_conflict="surface"`**, never `raw=True`/`auto_resolve`: a near-duplicate surfaces as a
  contradiction (`praxis_get_contradictions`) instead of silently minting a twin; settle it with
  `praxis_resolve_contradiction`.
- If it binds to surfaces, also create the `renders` edge (`praxis_bind_surface(check_id, screen_id, ...)`)
  so the surface lane of RESOLVE finds it.

The check takes effect on the **next build run** with no further action: at each ticket's RESOLVE step
`resolve_validation_requirements` picks it up by tag/surface match, `pin_requirements` writes it into that
ticket's coverage contract, and the ticket is FINISHED iff every pinned validation covering it passed.

## Step 3 — (optional) Re-enter matching work NOW, for immediacy

The check **automatically** re-enters matching tickets — completion is gated on checks resolved by query,
so there is nothing to re-author. For **immediacy** (so the next build run picks the affected tickets up
at once instead of on their next natural RESOLVE), regress the matched set by STATE only, using the SAME
query af-build's RESOLVE uses — never by writing anything onto the check or hand-listing checks on tickets:

- **tag match** — requirements whose `meta.tags` intersect the check's `applies_to`
  (`praxis_facts_by(category="requirement", meta=...)`);
- **surface match** — `praxis_requirements_for_surface` / `praxis_checks_for_surface`;
- any **explicit ids** the user named.

If a target requirement lacks the class tag, add it to its `meta.tags` via `praxis_edit_fact` (ticket
**identity**, not a check list; preserve all existing meta). Then regress each matched ticket by STATE
only against the plan snapshot: set `meta.build_state="incomplete"` (merge; preserve everything else) AND
`praxis_record_outcome(fact_id, success=False)` so it re-enters `incomplete_requirements`. A never-built
ticket is already incomplete — leave it. **Do NOT touch `meta.pinned_checks`, the claim lease
(`claim_owner`/`claim_at`/`claim_heartbeat_at`/`claim_lease_ttl`), or the check fact** — af-build's
RESOLVE/PIN steps re-pin the fresh check set at the next ticket start. (These are STATE writes against
`prd-<project>`; the ticket graph is snapshot-bound — pass the plan `(space=<project>, snapshot=prd-<project>)`.)

Confirm with `praxis_incomplete_requirements(<project>)` (BARE name).

## Report

Report the check you wrote (id, `applies_to`, `run`, criterion, and that it landed in
`building-validation`), and the tickets regressed (id + text) that now show incomplete — or that no
immediacy regression was requested. If any Praxis call failed, report the failure — never claim success.

## Never

- **Never** write into any snapshot other than `building-validation` — not the plan, not the planning
  lenses. To add a requirement use af-intake-plan; a planning lens, af-intake-plan-validation.
- **Never** author a check that names specific tickets or hand-lists checks onto a ticket — a check owns a
  predicate; WHICH tickets it governs is the build's fresh RESOLVE query.
- **Never** write or read a `.factory/*.json` file, and **never** proceed if Praxis is unreachable (fail
  closed).
- **Never** edit an existing requirement's content here — that is a re-baseline via af-intake-plan.
