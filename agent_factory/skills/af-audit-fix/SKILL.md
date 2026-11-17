---
name: af-audit-fix
description: Run an evidence-backed audit of a deployed website, record feature and role coverage in Praxis, fix confirmed failures locally, deploy, and re-audit the failures. Supports Codex and Claude, including supervised remote laptop execution.
---

# af-audit-fix

Use the same workflow with Codex or Claude. Engine selection changes the launcher,
never coverage requirements or verdicts. Read [runners.md](references/runners.md)
for installation, engine selection and unattended launch details.

## How work flows

Praxis owns dynamic audit and build state. Local screenshots, traces, generated
tests and exported reports are evidence, not an alternative completion ledger.
If Praxis cannot be reached, preserve evidence and report the outage; never claim
an unrecorded pass. Confirm the explicit project and tenant before writes.
Fix tickets follow FIND → CLAIM → RESOLVE applicable checks by query → BUILD →
VERIFY external signals → FINISH. Do not place authored check lists on tickets.

## Prepare and audit

1. Resolve repository, deployed URL, exact build revision, roles, supported sports
   or product variants, deployment commands, authenticated runner and existing
   jobs. Preserve live work and existing authorization. A request to audit does
   not itself authorize charges, real messages, deleting user data or production
   deployment. Reuse authorization already supplied in the session.
2. Read [browser-workflow.md](references/browser-workflow.md). Combine Anthropic's
   reconnaissance and evidence collection with Playwright's planner, generator
   and healer. Initialize the upstream definitions for the chosen engine and
   installed Playwright version; actually explore and execute the generated tests.
3. Derive stable scenarios from source routes, actions, requirements and permission
   boundaries, then expand them from rendered controls. Include CRUD, persistence
   after reload, validation, permission denial, tenant isolation, uploads/downloads,
   errors, keyboard/mobile behavior and relevant external integrations. Source
   inventories are leads; a route returning 200 is not a feature pass.
4. Create isolated test identities and run-owned data. Keep secrets and browser
   storage private and out of Git, prompts and shared reports. Record ownership
   before mutations; prepare cleanup before starting. Real device, payment or
   authentication blockers remain explicit coverage gaps.
5. Register the run and inventory using `agent_factory.af_audit_fix`; consult its
   `--help` for the installed CLI. Audit the pinned deployment before making fixes.
   Each result identifies case, role, route, build, expected/actual behavior and
   real execution evidence. Preserve console/network findings and cleanup IDs.
   Reconcile every discovered control with a scenario or an explained exclusion.
   A blocked or untested scenario never counts as passed.

## Use each page visit for a batch

Start with pending original inventory IDs. Group cases by route, role, sport,
viewport and fixture state. In one browser context, inspect the page once and
exercise the pending behaviors that share those prerequisites. Save distinct
verdicts and evidence references for each case. Revisit for a named requirement
such as reload persistence, a different permission, or a failure reproduction.
Generate reusable tests from the same session; run them together when needed
rather than replaying the page for each control during discovery.

Use `check --case ID` before executing. Reuse a same-build verdict and inspect
`linked_evidence` to plan only the steps still missing from an original case.
For a new variant, set `parent_id`, a stable `behavior_id` for the action, and the
role, sport (or `n/a`), and viewport. For a genuinely new control, describe it
in `discovered_control`. The ledger rejects duplicate registered tuples. Report
`inventory_counts` alongside the added case count. Partial evidence never closes
its parent. Record a `rerun_reason` when repeating a completed re-audit case on
the same build. One coordinator assigns each case before parallel browser work.

## Fix, deploy and re-audit

Freeze the initial audit report and failed case IDs before fixing. Create located
Praxis fix tickets with reproduction steps and evidence. Repair application defects
locally and add focused regressions using the captured browser behavior. The healer
may repair demonstrated selector or harness errors; it must not skip a case, remove
an assertion, mock the deployed defect away or change the expected result to pass.

Run the project's applicable checks. Deploy only within the authorized environment,
including migrations, then verify serving revision, health, logs and browser behavior.
Deployment command success alone is insufficient. Re-audit the frozen failures and
affected adjacent behavior on the new revision. Preserve original failures alongside
new verdicts; changing a build invalidates evidence that claims to cover that build.
If failures remain, continue the fix/deploy/re-audit cycle within scope.

Use the existing managed remote runtime for unattended Linux/WSL jobs; see
`../../docs/managed-runtime.md`. Do not replace its supervision, resource limits,
credential handling or another project's service. Confirm a real model turn and
browser action after disconnect/reconnect before reporting a running remote audit.

Finish with coverage counts, unresolved failures and blockers, evidence locations,
fix/deployment revisions, failed-case re-audit and cleanup results. Remove run-owned
active fixtures; retain required immutable audit evidence and report that retention.
Never describe partial or blocked coverage as testing every feature successfully.
