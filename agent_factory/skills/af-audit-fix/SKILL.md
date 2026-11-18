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
2. Read [browser-workflow.md](references/browser-workflow.md). Initialize the
   upstream definitions for the chosen engine and installed Playwright version.
   Perform reconnaissance and execute generated tests after source preparation.
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

## Prepare the remaining scenarios before browser execution

For a mapped initial audit, prepare a source-based plan for every outstanding
original scenario before opening the browser. Split independent preparation by
scenario ID and run the groups in parallel. Use the registered inventory,
local product source, existing tests and fixture notes. Reuse existing plans,
scoped browser evidence and linked verdicts. Exclude originals with a final
verdict. For partially tested cases, write only the remaining checks and link
the prior evidence. Preparation does not execute product tests or record verdicts.

Put the route, roles, fixture, concrete steps, expected outcome, cleanup and
reuse references in each plan. Group related scenarios by page, role, sport,
viewport and fixture. Resolve unknown selectors using a small browser sample
after preparation. In execution, read the assigned plans and any checkpoints
recorded since preparation. Close compatible checks in one visit.

The managed supervisor persists the run- and build-scoped plans under
`runtime/state/audit-preparation-<run>.json` and resumes incomplete preparation
by scenario ID. It runs up to three preparation groups at once and uses the
same preparation workflow for Codex and Claude.
For unmanaged runs, use the same preparation and reuse steps before execution.

## Functional smoke pass

A managed audit with `audit_smoke_pass: true` checks the primary behavior of
each outstanding scenario using representative roles and owned fixtures.
Verify persistence when a write is the primary behavior. Exercise actual role
differences when permissions are central. Reuse matching evidence, reproduce
a product failure once, and record missing prerequisites as blocked. Leave
extra device, history and equivalent-profile checks for the full audit.

Submit smoke results with `record-smoke --input` using the same input shape as
`record-batch`. Include both `console` and `network` arrays on every
pass or failure. Use empty arrays only when the browser observations were
checked and clean. Attach retained evidence with its URI and SHA-256.
See `queue` for counts. Smoke outcomes never populate full
verdicts or advance the audit phase. Completed full verdicts count as existing
coverage in the smoke queue.

## Use each page visit for a batch

When the managed supervisor attaches execution packets, use their scenario
plans, scoped checkpoints and linked verdicts as your ledger read. Execute
compatible checks together and close each original whose contract has a
verdict. Put verified outcomes in one `record-batch --input` call using an
array of objects with `case_id` and the normal `result` fields. A partial
check remains sweep evidence. Move on when a fixture is missing and record
the concrete dependency. Return immediately after recording the batch.

Start with pending original inventory IDs. Group cases by route, role, sport,
viewport and fixture state. In one browser context, inspect the page once and
exercise the pending behaviors that share those prerequisites. Save distinct
verdicts and evidence references for each case. Revisit for a named requirement
such as reload persistence, a different permission, or a failure reproduction.
For routine controls, open the page in a prepared profile, enumerate visible
actions once, click each applicable control and observe its specific effect
(dialog, navigation, filter, validation or saved state). Exercise controls
revealed by those clicks. Keep one compact interaction log with the observed
outcome per action and shared evidence references. Preserve failures, console
errors and meaningful coverage gaps. Continue to another page when a deeper
workflow needs a separate fixture. Use the full planner/generator/healer loop for
reproducible failures or complex workflows. Reuse the established browser setup
and session for routine actions without generating or replaying a test for each.
Cover several reachable pages per worker turn. Batch known clicks and assertions
in one browser script. If a workflow needs more fixture setup, keep its exact
remaining steps in the queue and use the current session to cover the other
assigned pages. Keep distinct verdicts and complete the deeper workflows in
follow-up batches. Summarize unique pages, actions exercised and open workflows
in the batch report.

Use the structured page-sweep helper during initial audit. Supply the scoped
ledger command so the runner records all assertions once at sweep end. Set
remaining checks against the complete original contract, including other roles.
Finish close_first items while the fixture is open. Inspect case_focus for
prior assertions and their exact scope. Directly execute every differing
permission. Record the original verdict when its acceptance contract is decided.
Compare end-to-end, planning, browser and recording times in sweep receipts,
original verdicts per hour, and scoped page revisits in the queue.

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

Check all selected IDs together with `check --case ID1 ID2` before executing. Reuse a same-build verdict and inspect
`linked_evidence` to plan only the steps still missing from an original case.
For a new variant, set `parent_id`, a stable `behavior_id` for the action, and the
role, sport (or `n/a`), and viewport. For a genuinely new control, describe it
in `discovered_control`. The ledger rejects duplicate registered tuples. Report
`inventory_counts` alongside the added case count. Partial evidence never closes
its parent. Record a `rerun_reason` when repeating a completed re-audit case on
the same build. One coordinator assigns each case before parallel browser work.

## Schedule by available prerequisites

Enable `audit_smoke_local_recovery: true` when setting up a managed smoke
run against a disposable loopback instance. Give either runner access to the
run-owned fixture tools and private setup notes. When a local prerequisite is
missing, reuse or provision the required account, record, or emulator state in
that instance. Verify fixture readiness and continue the same scenario. If
the setup would hold up the batch, record the blocker and attempted remedy so
the supervisor can requeue that original alongside other work.

Use the blocker prefix `Local browser interrupted:` for a browser crash or
resource exhaustion in a disposable local run. The supervisor checks a fresh
browser session before requeuing these cases. Continue from retained evidence.
If the interruption recurs, a local smoke profile can supply
`audit_browser_fallback`. Verify the affected UI on the alternate loopback
origin with the same pinned source and disposable API. Record that origin and
resume the remaining checks.

Check whether the application can consume the requested fixture before
creating it. Record demonstrated missing behavior as a product failure.
If the scenario allows a scoped redirect or unavailable state, directly
exercise that branch with an owned entity and verify the settled destination
or visible state. Record which branch was verified.
When the scenario makes an action conditional on the controls that are
present, record whether the action exists and continue its independent checks,
including any required role comparison.
Keep setup probes as prerequisite evidence and use observed application
behavior for the scenario verdict. Retain any remaining blocker and the
specific recovery already attempted.

When a picker omits an expected actor or record, check that fixture's eligibility
using the application's supported lookup or setup workflow. Verify the
organization category, assignment scope, and any required lifecycle state that
the picker uses. Sign-in alone establishes identity. If the fixture is
ineligible, repair it or provision a separate eligible fixture, then retry the
original action. Record the exact missing prerequisite as blocked if supported
local setup cannot supply it.

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

## Map and report the initial audit queue

Before the first feature test, enumerate the deployed pages and actions, meaningful
role differences, required fixtures and expected outcomes. Register that inventory
with `init`, then run `queue` to inspect the mapped batches. Each batch shares a
route, role context and other recorded prerequisites. Use `primary_route` when a
scenario lists multiple routes. Select eligible batches in order, keeping separate
evidence for each action. Record a reason when a dependency requires another order.
Newly discovered controls enter the ledger separately so the original denominator
stays comparable. Resume from the same run after interruption.

Use `record --receipt` to save a verified verdict without retrieving the whole inventory again. The worker combines eligible retries and pending checks on the same page and favors pages with less recorded evidence.

Use `queue` to report completed originals, remaining work, held dependencies and
the next batch. A completed original has a verified pass or failure. A fresh
prerequisite can move an earlier blocked verdict into the retry queue. The ETA
uses up to 20 recent timed original verdicts and needs at least four spread over
five minutes. Report an unknown full-audit ETA while dependency waits are open.
Estimates cover the initial audit only.
