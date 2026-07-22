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
Operate in the **PROJECT-DERIVED org** — `identity.factory_org()` (the `PRAXIS_ORG` pin, else the cached
selection) — **not** a hardcoded `"agent-factory"`. A fresh session simply proceeds in whatever org this
project pins; do NOT `select_org("agent-factory")`. The hard rule is **MCP-tool org == hook-client org**
(both the project's `PRAXIS_ORG`): the write you issue here and the org af-build reads under must be the
same, and the fail-loud `praxis_select_org` guard enforces it — a `select_org` that disagrees with a
`PRAXIS_ORG` pin is refused by name rather than silently splitting your write into the wrong tenant.
The check must land in the snapshot af-build RESOLVE reads, or it is a silent no-op:
`scope="validation"` → **`(space=<project>, snapshot="building-validation")`**. You target it by passing
**both** `space` and `snapshot` on the write itself — `praxis_add_insight(..., space="<project>",
snapshot="building-validation")`. (A bare `praxis_add_insight` with no `space`/`snapshot` writes your
personal WORKING MEMORY, which af-build never reads — that is exactly the silent no-op to avoid;
`praxis_select_space` does NOT make a write target a snapshot.) Writing a validation check into
`prd-<project>` is refused by the server's section invariant; writing it into `planning-validation` makes
it invisible to the build.

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
  space    = "<project>",              # REQUIRED — target the org-shared snapshot,
  snapshot = "building-validation",    # not working memory (on_conflict N/A for checks)
)
```

- **The `space`/`snapshot` pair is what routes the write to the section af-build reads.** Omit it and the
  check lands in working memory (invisible to the build) even though the call returns success + an id.
  VERIFY it landed: `praxis_facts_by(category="check", space="<project>", snapshot="building-validation")`
  should now list it — the SAME `(space, snapshot)` af-build's RESOLVE reads.
- **Identity is `meta.check_id`, NOT the prose.** The server keys check writes on `meta.check_id` and
  NEVER text-dedups or reconciles them (a check is a declarative gate keyed on `check_id` + `run`, not a
  knowledge assertion). Re-admitting the SAME `check_id` UPDATES that one fact in place (no duplicate, the
  new `run` wins); a DIFFERENT `check_id` is ALWAYS a new distinct fact — even if the description reads like
  an existing one. So give every check a stable, DISTINCT `check_id`, and never worry that a similarly-worded
  gate will be swallowed. `on_conflict` does not apply to checks (the write never merges, overwrites, or
  raises a contradiction); `raw`/`auto_resolve`/`surface` are all irrelevant here.

The check takes effect on the **next build run** with no further action: at each ticket's RESOLVE step
`resolve_validation_requirements` picks it up by tag/surface match, `pin_requirements` writes it into that
ticket's coverage contract, and the ticket is FINISHED iff every pinned validation covering it passed.

## Step 2b — Confirm the fan-out with `--by-check`

Right after authoring, make the check's reach VISIBLE instead of invisible:

```
python -m agent_factory.tools.resolve_preview <project> --by-check
```

Find the just-authored `check_id` in the output and CONFIRM it lands **ONLY on the intended concern's
tickets**. Because `applies_to` is a predicate the build resolves fresh, an over-broad tag pins the check
onto tickets it has no business gating — and `--by-check` is where that shows up before the build ever runs:
- a `"secrets"`-tagged env-purge check **bleeding onto a CDK ticket**;
- a `"migration"`-tagged schema check landing on **a runbook AND a federation ticket**.

A check pinning across unrelated tickets is a **TOO-BROAD `applies_to` to tighten** (a narrower tag, or a
surface bind instead of `["*"]`). The judgment stays with you — the tool does not decide; it just makes the
fan-out legible so you tighten a leak deliberately rather than discover it mid-build.

## Step 2c — GRADED checks and CANDIDATE-pool entries

A check need not be a binary `run` command. Two additional shapes flow the same section-locked write:

**Graded rubric check.** Instead of a `run`, author `meta.kind="graded"` + `meta.rubric` — a
subjective, LLM-judged, min-of-axes rubric whose verdict still reduces to one `passed` boolean
(see `agent_factory/src/agent_factory/rubric.py`). The rubric shape:

```
meta = { "check_id": "<slug>", "kind": "graded", "applies_to": ["<tag>", ...],
         "rubric": { "axes": [ {"name": "error-paths", "threshold": 0.9, "guidance": "..."}, ... ],
                     "confidence_floor": 5, "criterion": "<what good looks like>",
                     "judge_prompt": "<how to score>" },
         "candidate": <bool>, "severity": "<P0|P1|P2|P3 | number>" }
```

A graded check carries **no `run`**. A missing/malformed `rubric` is rejected at author time
(`rubric_from_dict` raises) — never written as a silent floor-only check. Do NOT re-grade anything
already exit-codeable (typecheck/build/lint/test) — the rubric is for the residue exit codes can't judge.

**`meta.candidate` — gating vs pool.** This is the discriminator RESOLVE reads (U1):
- **`candidate:false` / absent → a HARD GATE.** Resolves into `required_validations` exactly as a
  binary check does; completion is gated on it. Author your must-hold concerns here.
- **`candidate:true` → a NON-GATING POOL ENTRY.** Excluded from `required_validations`; returned only
  by the deterministic `pool_candidates(ticket)` query, which the build-time **rubric assembler**
  (`agent_factory/src/agent_factory/rubric_assembly.py`) tiers into promoted gating validations + one
  advisory aggregate. Use candidate entries for "ideas worth checking" that should not each be a
  standalone permanent gate.

**Scoping a candidate (pool hygiene — U6).** A pool candidate resolves by the SAME tag/`*`/surface
lanes as a gate, so scope it **tightly** to the concern it came from — a specific tag or a surface
bind, **never `["*"]`** for a build-discovered candidate. A candidate whose `applies_to` matches no
live ticket is an **orphan** (`agent_factory/src/agent_factory/pool_lifecycle.orphaned_candidate_ids`)
and should be `reject`ed so the pool tracks the live plan rather than accumulating forever. Confirm a
candidate's fan-out with `resolve_preview --by-check` (it prints each check's `kind` + gating/candidate
status and flags a too-broad predicate) before finishing.

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
**identity**, not a check list; preserve all existing meta). Then regress the matched set by STATE only
with **one bulk call** — `praxis_regress_requirements(<project>, [<id>, ...])` — which records a failure
outcome AND stamps `meta.build_state="incomplete"` on every id in a single write, so each re-enters
`incomplete_requirements`. Use the bulk tool, NOT a per-ticket loop of `praxis_record_outcome` +
`praxis_edit_fact`: that fired ~two calls per ticket and timed out on a real plan. A never-built ticket is
already incomplete — you may still include it (regress is idempotent). **Do NOT touch `meta.pinned_checks`,
the claim lease (`claim_owner`/`claim_at`/`claim_heartbeat_at`/`claim_lease_ttl`), or the check fact** —
af-build's RESOLVE/PIN steps re-pin the fresh check set at the next ticket start. (`praxis_regress_requirements`
targets the canonical `prd-<project>` plan snapshot automatically — the graph completeness derives from — so
you pass only the bare project name and ids, no `(space, snapshot)`.)

Confirm with `praxis_incomplete_requirements(<project>)` (BARE name).

## Step 4 — Verify coverage (the closing gate)

This is the CLOSING step of the command, and it is an **ENFORCED GATE, not advisory**. After
authoring the check(s), RUN:

```
python -m agent_factory.tools.resolve_preview <project> --require-coverage
```

**This command MUST exit ZERO before you report success. A non-zero exit BLOCKS completion** — the intake
is NOT done, and you may not report it done. On a non-zero exit you either author the missing check(s) or
mark the offending ticket `meta.verify="manual"`, then **re-run until it exits zero**. There is no path
that finishes over a non-zero `--require-coverage`; treating its failure as a warning to note-and-proceed
is exactly the unguarded-ticket state this gate exists to prevent.

- If it exits **non-zero**, the plan is **NOT fully covered**: the command lists the `verify=automated`
  requirement_ids that resolve **ZERO declared checks** (only their acceptance floor). Author the missing
  building-validation check(s) for those tickets' tags (or, for a legitimately manual ticket, confirm it
  is `meta.verify="manual"` — manual tickets are exempt because their floor is human sign-off) and re-run
  until it exits **zero**.
- **A floor-only AUTOMATED ticket is a COVERAGE DEFECT, not an acceptable state.** The acceptance floor is
  a backstop, not a declared check — an automated ticket whose ONLY resolved lane is the floor has no gate
  a build agent actually runs. This command adds ONE check at a time and nothing else guarantees every
  ticket ends up covered, so this closing run is what makes af-build runnable with **no manual
  post-fixes**: a clean (zero-exit) `--require-coverage` is the contract that every automated ticket has a
  real check behind it.

## Report

Report the check you wrote (id, `applies_to`, `run`, criterion, and that it landed in
`building-validation`), and the tickets regressed (id + text) that now show incomplete — or that no
immediacy regression was requested. If any Praxis call failed, report the failure — never claim success.

## Never

- **Never** write into any snapshot other than `building-validation` — not the plan, not the planning
  lenses. To add a requirement use af-intake-plan; a planning lens, af-intake-plan-validation.
- **Never** author a check that names specific tickets or hand-lists checks onto a ticket — a check owns a
  predicate; WHICH tickets it governs is the build's fresh RESOLVE query.
- **Never** leave a check whose `--by-check` fan-out pins it onto unrelated tickets (a `"secrets"` purge
  check on a CDK ticket, a `"migration"` schema check on a runbook or federation ticket) — that is a
  too-broad `applies_to`; tighten the tag or surface-bind it before finishing.
- **Never** write or read a `.factory/*.json` file, and **never** proceed if Praxis is unreachable (fail
  closed).
- **Never** edit an existing requirement's content here — that is a re-baseline via af-intake-plan.
- **Never** end the command with a `verify=automated` ticket resolving only its acceptance floor — that is
  an unguarded ticket. Author its check or mark it `meta.verify="manual"`, and re-run
  `resolve_preview <project> --require-coverage` until it exits zero.
