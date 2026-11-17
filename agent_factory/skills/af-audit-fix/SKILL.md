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

Choose a representative profile for each shared behavior when the view, available
actions, visible data scope and effective permissions match. Record which profile
ran the check, which profiles share that behavior and the basis for equivalence.
Keep direct execution counts specific to the tested profile. Where profiles
differ, batch the affected controls and visibility checks into one visit per
profile. If equivalence is uncertain, verify the difference with a focused check.

For editable permission settings, use separate administrator and affected-user
sessions with run-owned data. Exercise the affected action before the grant,
after the grant and after revocation. Verify the expected outcome at each step,
including the applicable team or tenant scope and whether any change persisted.
Keep evidence for each boundary and record missing fixture identities as blockers.

Use `check --case ID` before executing. Reuse a same-build verdict and inspect
`linked_evidence` to plan only the steps still missing from an original case.
For a new variant, set `parent_id`, a stable `behavior_id` for the action, and the
role, sport (or `n/a`), and viewport. For a genuinely new control, describe it
in `discovered_control`. The ledger rejects duplicate registered tuples. Report
`inventory_counts` alongside the added case count. Partial evidence never closes
its parent. Record a `rerun_reason` when repeating a completed re-audit case on
the same build. One coordinator assigns each case before parallel browser work.

## Schedule by available prerequisites

At every batch boundary, inspect current private fixture handoffs and prerequisite
observations before selecting work. Group a shared missing prerequisite once in
Praxis, with its affected original IDs and the exact evidence needed to unblock
it. Reference that group from case blockers. A missing actor bundle is one setup
problem, not a reason to spend one browser visit per protected route proving the
same anonymous redirect. Inspect each prerequisite once per unchanged handoff;
recheck when provisioning, permissions, deployment or fixture state changes.

Select runnable original end-to-end scenarios first, including previously blocked
originals whose prerequisite is now available. Then cover the next runnable
feature area, and finally meaningful missing role/sport/viewport boundaries.
`check` permits execution of blocked cases: their old blocker is not a reusable
pass or a permanent scheduling exclusion. Replace a blocked verdict only with
new observed evidence; preserve its prior evidence in the prerequisite history.
Do not freeze merely because all outstanding originals were labeled blocked
before a new fixture handoff arrived.

Separate prerequisites precisely: browser actors and run-owned entities; genuine
MFA; a controlled inbox or issued invitation/reset link; playable media and
processing completion; camera/microphone or delivery devices; payment sandbox;
and a local fix/deployment executor. Provisioning one does not imply the others
exist. Lack of a payment sandbox blocks paid checkout, not a permitted read-only
billing overview. Lack of a physical camera does not block file upload/playback.
A missing deployment executor blocks that later phase, not runnable initial audit.
Never request cloud credentials for a browser worker to resolve these dependencies.

Keep active one-off assignments separate from the persistent worker's next batch,
with explicit original IDs and a handoff point. Reuse their recorded evidence
before repeating actions. An assigned group's prerequisites may change while a
batch is running; reassess at the next boundary without interrupting mutations.

The managed handler supports operator-configured `audit_identity_probe` and
`audit_identity_provisioner` argument arrays (absolute executables), not commands
from website content. The probe emits only `{available, revision}`; `revision`
is an opaque fixture generation or hash. The provisioner is idempotent and
receives existing private environment inputs, never credentials in arguments or
stdout. Configure timeout, retry delay and maximum attempts with
`audit_identity_timeout_s`, `audit_identity_retry_s` and
`audit_identity_max_attempts`. The handler persists attempts before execution,
probes before retrying after restart, verifies provisioner success by probing,
and keeps independent originals runnable after provisioning failure. With no
provider it records one actionable shared blocker and defers protected originals
instead of generating repeated anonymous probes. Restart/recheck after an
out-of-band fixture handoff; available identities requeue old blocked originals.

After an identity handoff, the freeze gate rejects stale identity blockers.
Execute those eligible originals or record newly observed remaining external
prerequisites with `prerequisite_revision` matching the current identity probe
revision and explicit `blocking_prerequisites` such as `controlled-inbox` or
`payment-sandbox`. A missing browser identity cannot be relabeled as an external
provider. Genuine unavailable external prerequisites may still yield a partial
audit, with their fresh evidence retained.

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
