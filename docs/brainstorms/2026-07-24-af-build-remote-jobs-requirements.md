---
date: 2026-07-24
topic: af-build-remote-jobs
focus: Trigger, observe, steer, and clean up long-running af-build jobs on the EC2 devbox via Praxis
rigor: Rigorous
decision_mode: Collaborate
---

# af-build Remote Jobs

## Summary

Add a **job** concept to Praxis — created by an MCP tool, executed on the EC2 devbox as a
`claude --bg` background session, observed through externally-fired signals, and surfaced in both the
MCP surface and the Praxis website. Any agent in any repo can start a multi-hour af-build run without
SSH, check whether it is progressing, read its recent activity, answer its questions, restart it, and
have its session cleaned up automatically when it ends. af-build itself keeps working locally,
unchanged.

---

## Problem Frame

af-build runs take many hours. Today they are started by hand: SSH to the devbox with a `.pem` key,
create a named `tmux` session, run `claude`, and re-attach later to see what happened. The devbox's
own CDK output documents this as the intended workflow
(`infra/lib/dev-box-stack.ts` — the `PyCharmHint` output tells the operator to run `claude` inside
`tmux` so sessions outlive disconnects).

Three costs fall out of that. Every other agent that wants to hand work to the box must also SSH,
so remote execution is available only to a human at a terminal. Observation is a terminal attach,
which means watching several concurrent builds requires several attaches and tells you nothing after
a disconnect. And nothing closes finished sessions, so dead sessions accumulate on a box that is
already running several at once.

The specific moments of pain are the two ways an af-build run stops making progress: it reaches a
question it cannot answer alone, or the Anthropic API becomes unavailable and the process dies
mid-turn. In both cases the run is silent, and silence currently looks identical to a run that is
working hard on a difficult ticket. The cost is measured in hours — a run that stalls at 11pm is
discovered at 9am.

---

## Actors

- A1. **Operator** (Matt, sole user): dispatches jobs, answers questions, resumes stalled jobs,
  reviews and merges the resulting PRs.
- A2. **Dispatching agent**: any Claude Code session in any repo that calls the dispatch MCP tool.
  Usually a local `/af-build` invocation that has been told to run remotely; may be any other agent.
- A3. **Box service**: a long-lived process on the devbox. Claims queued jobs, launches and reaps
  background sessions, ships observation data to Praxis, and performs all git and PR work.
- A4. **Build session**: a `claude --bg` background session on the box running `/af-build` against one
  prd snapshot. Does not know it is remote.
- A5. **Praxis**: the single source of truth for job state, ticket state, and observation data.
- A6. **Claude Code background daemon**: the built-in supervisor (`claude --bg`, `claude agents`) that
  owns background session lifecycle. Not ours, but relied upon.

---

## Key Flows

- F1. **Dispatch a remote job**
  - **Trigger:** A2 calls the dispatch MCP tool (typically from `/af-build` after the operator picks
    the box venue).
  - **Steps:** Resolve the target read-only (snapshot exists, frontier computed, org identity
    confirmed) → push current HEAD to a job-derived branch → create a job row carrying project,
    snapshot, scope, origin URL, build-base SHA, intended PR base, and org → return the job id.
  - **Outcome:** A queued job exists; the dispatching session's completeness gate is inert and its
    turn ends immediately.
  - **Covered by:** R1, R2, R3, R4, R5, R6, R26

- F2. **Execute a job**
  - **Trigger:** A3 polls Praxis and finds a claimable queued job.
  - **Steps:** Claim the job → ensure a per-repo bare mirror exists (clone on first sight) → fetch →
    create a per-job worktree at the build-base SHA → launch a `claude --bg` session running
    `/af-build`, injecting plugin dir, MCP config, settings, and permission mode at dispatch → record
    the session id on the job row.
  - **Outcome:** Job is running; A4 stamps its own run marker under its own owner id.
  - **Covered by:** R8, R9, R10, R11, R12, R37, R38

- F3. **Observe a job**
  - **Trigger:** Continuous, plus operator viewing the dashboard or calling an MCP tool.
  - **Steps:** A3 polls `claude agents --json` for existence and state → hooks fired by the session
    append activity, permission-request, and terminal events to a local trail → A3 ships a bounded
    rolling tail and terminal events to Praxis → operator sees live jobs at the top level and, on
    selecting one, its stored tail plus (when the session is alive) a deeper live fetch.
  - **Outcome:** Operator can distinguish working, waiting-on-a-question, and dead without attaching.
  - **Covered by:** R14, R15, R16, R17, R18, R19

- F4. **Answer a question / resume a stalled job**
  - **Trigger:** Operator sees a permission-request event or a stale activity timestamp.
  - **Steps:** Operator posts a message to the job's mailbox, which A4 reads at its next ticket
    boundary; or, if A4 is not reading, the operator triggers the resume action, which verifies no
    live process holds the session before relaunching against it.
  - **Outcome:** The run continues, or a new session continues the same conversation.
  - **Covered by:** R20, R21, R22

- F5. **Deliver the work**
  - **Trigger:** A job (or a group) reaches its terminal state with commits present.
  - **Steps:** Merge per-ticket worktrees → run the WORK-review panel → push the job branch → open a
    PR against the intended base → flush the final tail → mark terminal → reap the session.
  - **Outcome:** Reviewable PR exists; no session remains; history is queryable.
  - **Covered by:** R23, R24, R25, R27, R28, R29, R32, R33

---

## Requirements

**Job model and dispatch**

- R1. A job is a first-class, queryable Praxis entity with a stable id, distinct from the tickets it
  builds. Its lifecycle states are at minimum: queued, claimed, running, blocked, completed, failed.
- R2. A job targets exactly one prd snapshot and runs until every in-scope ticket in that snapshot is
  finished or blocked, using af-build's existing completeness semantics.
- R3. Dispatch is a **separate action from building**. The dispatching session must not claim tickets
  and must not stamp a whole-set run marker. (`hooks/build_completeness_gate.py:301` makes the gate
  inert when the session holds neither an unfinished claim nor its own live run marker; a dispatcher
  that stamped would block its own turn against the gate, up to the configured block cap.)
- R4. The dispatch payload is self-contained: project slug, snapshot, scope, origin URL, build-base
  commit SHA, intended PR base, and Praxis org identity. Nothing is read from a file committed in the
  target repo.
- R5. The build base is recorded as a resolved commit SHA. Branch names are provenance only, because
  a branch recorded by name can move between dispatch and execution.
- R6. Dispatch verifies org identity before enqueueing, so a mismatch fails at dispatch rather than
  hours into the run. (af-build treats hook-client org vs MCP-tool org divergence as a fail-loud stop.)
- R7. Multiple jobs may exist concurrently for one repo and across repos, distinguished by job id and
  working directory.

**Execution on the box**

- R8. Background sessions are launched via `claude --bg` and their lifecycle is owned by the built-in
  Claude Code background daemon rather than by hand-managed `tmux` sessions.
- R9. Per-dispatch flags supply the agent_factory plugin directory, the Praxis MCP configuration,
  the settings needed for the completeness gate to be enforceable, and the permission mode — so no
  machine-level or repo-level configuration step is required to make a new repo work.
- R10. The box keeps one bare mirror per repo and creates it on first sight of a job for that repo.
- R11. Each job builds in its own worktree created at the job's build-base SHA. Concurrent jobs never
  share a working tree.
- R12. Steps that contend on non-file resources — the project's deploy step and any test fixture
  bound to a fixed host port or shared database — are serialized per repo. (Verified concrete case:
  this repo's suite requires Postgres on host port 5433 per `docker-compose.yml`, so two concurrent
  same-repo jobs would otherwise collide.)
- R13. The build session runs under a permission mode with an explicit allowlist rather than
  interactive approval, because an unattended session cannot answer a permission prompt.

**Observation**

- R14. Job observation must not depend on the build session's cooperation. Every signal used to
  determine whether a job is progressing is either fired by the harness or observed externally.
- R15. Session existence and state are polled externally via `claude agents --json`, which reports
  per-session id, working directory, kind, start time, name, and state without requiring a TTY.
- R16. A last-activity timestamp is maintained from harness-fired hook events, since the external
  poll payload carries a start time but no activity time.
- R17. An event indicating the session is requesting permission or otherwise awaiting a human is
  captured and surfaced, so a question-blocked run is distinguishable from a working run.
- R18. A job's terminal moment is captured as a discrete event rather than inferred from a poll
  interval, and the job's recorded terminal state distinguishes completed from failed from blocked.
  Session exit alone does not mean the work finished — af-build's gate blocks session end until
  tickets pass, so exit must be reconciled against ticket completeness.
- R19. A bounded rolling tail of recent activity is stored in Praxis so that recent messages remain
  readable after the session is gone, the box is unreachable, or the process died. A deeper live
  fetch is available while the session still exists.
- R20. The operator can query, from MCP and from the website: which jobs are live, their states, and
  a per-job view of recent activity. The website's top level lists live jobs; selecting one shows its
  recent activity.

**Control**

- R21. The operator can post a message to a job. The build session consumes messages at ticket
  boundaries. This is the cooperative path and is not relied on for jobs that have stopped reading.
- R22. The operator can trigger a resume action for a job that did not finish. Resume must verify no
  live process still holds the session before relaunching, because two processes sharing one session
  interleave and corrupt its transcript.

**Job groups**

- R23. Several jobs on one project may be dispatched as a group with a barrier: the group's
  integration step runs only once every member has reached a terminal state.
- R24. A group's integration merges its members' work on the box and produces a single commit and a
  single PR, rather than one PR per member.
- R25. Group membership is explicit on the job, so group integration can identify which finished work
  belongs to the batch.

**Code delivery and branch safety**

- R26. Dispatch pushes the operator's current HEAD to a job-derived branch. It never pushes to the
  branch the operator is standing on, so dispatching from main leaves main untouched.
- R27. Work reaches the intended base branch only through a pull request, opened against the branch
  the operator dispatched from.
- R28. Landing on the base branch without a PR is an explicit per-dispatch opt-in, never a
  configuration setting that persists across runs.
- R29. Only the box service holds a credential capable of pushing. The build session holds none, and
  its worktree's remote is the box's local mirror rather than a network remote — so a confused agent's
  push reaches a local bare repo and nothing else.
- R30. The box service refuses to push any ref outside the job-branch pattern. The refspec restriction
  lives in deterministic code, not in instructions given to a model.
- R31. The credential is account-wide and provisioned once, so onboarding a new repo requires no
  grant, no key, and no repo-level configuration.

**Cleanup**

- R32. A session that has reached a terminal state is closed automatically. The operator never
  accumulates dead sessions.
- R33. The final activity tail and terminal event are persisted **before** teardown, so the evidence
  for a failed job outlives the session that produced it.
- R34. The job row and its observation history outlive the session. Cleanup destroys the session, never
  the history.
- R35. On box-service restart, live sessions are reconciled against open job rows and adopted rather
  than orphaned or duplicated.

**Local venue**

- R36. af-build continues to work locally with no behavioral change. Venue is a field on a job, not a
  branch in af-build's instructions.
- R37. A local run appears in the job list with its state and ticket progress, at no additional
  complexity cost beyond writes af-build already makes.
- R38. Local runs deliberately do not provide an activity tail, question-detection, message delivery,
  or resume. The dashboard labels the difference rather than presenting a local job as a degraded
  remote one.

---

## Acceptance Examples

- AE1. **Covers R3.** Given a local session that dispatches a remote job, when it finishes
  dispatching, its turn ends without the completeness gate blocking, because it holds no claim and no
  run marker of its own.
- AE2. **Covers R5.** Given a job dispatched from a branch, when a new commit lands on that branch
  before the box claims the job, the box still builds the SHA recorded at dispatch.
- AE3. **Covers R11, R12.** Given two jobs dispatched for different snapshots of the same repo, when
  both run concurrently, each builds in its own worktree, and their deploy and fixture-bound test
  steps do not overlap in time.
- AE4. **Covers R14, R16, R17.** Given a job whose session is waiting on a question, when the operator
  checks the job, they see a permission-request event and a stale activity timestamp — without the
  build session having reported anything itself.
- AE5. **Covers R14, R19, R33.** Given a job whose process died because the API was unavailable, when
  the operator checks the job afterward, they can still read the last recorded activity, because it
  was persisted before teardown and does not require the session to exist.
- AE6. **Covers R18.** Given a session that exits while tickets remain incomplete, when the job's
  terminal state is recorded, it is failed or blocked rather than completed.
- AE7. **Covers R22.** Given a job the operator wants to restart, when a live process still holds its
  session, the resume action refuses rather than launching a second process against it.
- AE8. **Covers R23, R24.** Given three jobs dispatched as one group, when two have finished and one
  is still running, no integration or PR has occurred; when the third reaches a terminal state,
  exactly one merge, one commit, and one PR are produced.
- AE9. **Covers R26.** Given the operator dispatches while on main with local commits, when dispatch
  completes, main has not been pushed and the commits exist on a job-derived branch.
- AE10. **Covers R29, R30.** Given a build session that attempts to push to the base branch, when the
  push runs, it reaches the box's local mirror and no network remote is contacted.
- AE11. **Covers R32, R34.** Given a job that completed, when the operator looks at the box, no session
  for that job remains; when they look at the job in Praxis, its history is intact.

---

## Success Criteria

- The operator can start a multi-hour build from any repo without SSH, close the laptop, and later
  determine — from a phone — whether each running job is working, waiting on a question, or dead.
- A stalled run is noticed in minutes rather than at the next morning's check.
- No dead sessions accumulate on the box without the operator doing anything.
- Onboarding a brand-new repo requires no per-repo configuration in GitHub, on the box, or in the repo.
- af-build's local behavior is unchanged, and its instructions contain no venue conditionals.
- A downstream planner can implement each requirement without inventing job states, terminal-state
  semantics, branch-safety rules, or the local-vs-remote fidelity boundary.

---

## Edge States and Failure Classes

Enumerated per Step 2c. Each is either covered by a requirement or explicitly out of scope.

**Empty and boundary states**
- No queued jobs; no live jobs (dashboard empty state). Covered by R20.
- A job whose snapshot has zero incomplete tickets at claim time — completes immediately, produces no
  PR. Covered by R2, R18.
- A group with one member. Covered by R23.
- A job whose build produced no commits — terminal without a PR. Covered by R27 (no PR to open).

**Failure classes**
- **Silent partial failure:** session exits with tickets incomplete but the job is recorded as
  completed. Closed by R18.
- **Lost evidence:** session reaped before its tail is persisted. Closed by R33.
- **Duplicate execution:** two box services, or a restarted service, launching two sessions for one
  job. Closed by R35 plus job claiming (R1).
- **Transcript corruption:** two processes on one session. Closed by R22.
- **Cross-job merge conflict:** two same-repo jobs produce branches that never saw each other; both
  can be green and still conflict at merge time. af-build resolves conflicts only *within* a job.
  Accepted, not closed — see Scope Boundaries.
- **Resource contention:** concurrent deploys, or concurrent suites bound to one host port. Closed by
  R12.
- **Wedged-but-alive:** session neither progressing nor exiting. Detected by R16; recovery is the
  manual resume path (R22).
- **Unauthorized action aborts the run:** an allowlist-driven permission mode terminates on an
  unapproved tool. Mitigated by R13; residual risk recorded under Open Decisions.
- **Irreversible action:** a push to the base branch. Closed by R27, R29, R30 — and explicitly *not*
  closed by instructions alone.
- **Credential exposure:** a token readable by an autonomous agent on the box. Mitigated by R29;
  residual risk recorded under Dependencies.
- **Praxis unreachable from the box:** job state cannot be updated. Not closed — see Open Decisions.
- **Box unreachable:** dispatch succeeds, nothing claims the job. Detectable via queued-age; no
  requirement yet — see Open Decisions.

**Lifecycle (create / edit / delete / recover)**
- Create: dispatch (F1). Recover: resume (R22). Delete: cleanup destroys sessions only (R34).
- Cancelling a queued or running job is **not** specified — see Open Decisions.
- Retention and rotation of stored tails is **not** specified — see Open Decisions.

**Gap lenses — explicit fire-or-pass**
- *Failure modes:* **fires** — see above.
- *Security:* **fires** — branch safety (R26–R31), credential isolation (R29), residual token risk.
- *Data lifecycle:* **fires, partially unresolved** — tail retention and rotation are open.
- *Rollback:* **fires** — PR-only delivery (R27) is the rollback story for delivered work; a
  half-merged group is not yet specified (open).
- *Who pays the tradeoff:* **fires** — the operator pays for local runs having lower observability
  fidelity (R38), and pays for cross-job conflicts being resolved by hand.

---

## Implied Features (surfaced during ideation, accepted)

- Job-derived branch naming making PR creation idempotent, so a restarted terminal step finds the
  existing PR instead of opening a second one.
- Dispatch-time preflight as the place where identity and config problems surface, chosen over
  discovering them hours into a run.
- A PR body that reports the job: tickets completed, tickets blocked, and check results — the artifact
  doubling as the report.
- Draft-versus-ready PR state as a free signal of whether the set completed or partially blocked.
- Reusing the deployed website's existing tab mechanism rather than introducing routing, since the
  frontend currently selects views by state rather than by route.

---

## Key Decisions

- **Pull, not push.** The MCP tool writes a job row; the box claims it. Praxis cannot reach into the
  box. The rejected alternative — Praxis calling AWS SSM to run a command — would give the public API
  arbitrary shell on a box whose instance role is `AdministratorAccess`.
- **Wrap the built-in background daemon rather than hand-rolling a supervisor.** `claude --bg` and
  `claude agents --json` provide launch, liveness, state, restart survival, and per-dispatch injection
  of plugin/MCP/settings/permission configuration. This removed most of the originally-planned daemon,
  and eliminated the accumulating-dead-`tmux`-sessions failure mode by construction.
- **Observation is external, never self-reported.** Self-reporting fails in exactly the two cases that
  matter — a run waiting on a question is not writing, and a crashed run cannot report its own death.
- **Do not parse the session transcript JSONL.** Its location is stable but its entry format is
  documented as internal and subject to change between releases; the existing Go tailer in
  `session-capture/` has already been broken once by that layout.
- **Venue is a field, not a code path.** Conditionals in a skill's instructions are the expensive kind
  of complexity; identical behavior with a differing launcher is the cheap kind.
- **Branch safety lives in code and topology, not in instructions.** No credential in the agent, a
  local mirror as its only remote, and a hardcoded refspec in the service.
- **No repo-level configuration anywhere** — not in GitHub settings, not in a committed file, not as a
  per-repo grant. Onboarding cost for a new repo must be zero.
- **No automatic resume.** The operator resumes explicitly; the system's job is to make a stalled job
  obvious and restartable.
- **Store a bounded tail, fetch depth on demand.** Storing everything is unnecessary for knowing when
  a job ended (one terminal event suffices); storing nothing fails the crash case that motivated the
  work.

---

## Defaults Taken (flagged for override)

- D1. **Stuck is reported as an observation, not a verdict.** Raw signals plus one conservative
  threshold flag phrased as elapsed silence. Rationale: at short timescales a hard ticket is
  indistinguishable from a hang, so a tighter threshold would cry wolf. Override by choosing a
  threshold or removing the flag.
- D2. **Successful jobs reap immediately; failed jobs hold their session for a grace window, then are
  reaped by a backstop.** Rationale: immediate reaping of failures destroys the crash scene, and no
  reaping at all is the accumulation problem. Override by changing the window.
- D3. **Deploy and fixture contention is handled by a per-repo lock on those steps**, not by
  restricting how jobs are queued. Rationale: a narrow lock beats a constraint on the operator.
- D4. **Secrets Manager** for the box credential, matching the only existing secret-fetch pattern in
  the codebase (`knowledge/serve/db.py` reads a DB secret this way; there is no SSM Parameter Store
  usage anywhere). Override if a different store is preferred.
- D5. **The job-id tagging layer is optional.** Since concurrent same-repo jobs target *different*
  snapshots, their ticket sets are disjoint and project-level completeness queries attribute progress
  correctly. Group integration (R25) is the one case that needs explicit membership.

---

## Open Decisions

### Resolve before planning

- [Affects R31, R29][User decision] **Credential mechanism: account-wide PAT versus GitHub App
  installation.** Both satisfy zero-per-repo onboarding. A GitHub App gives short-lived tokens and
  bot-attributed PR authorship; a PAT is faster to stand up. Checked: GitHub scopes tokens by repo and
  permission category, never by ref, so neither can be restricted to non-base branches — which is why
  R29/R30 place the boundary in topology and code instead.
- [Affects R23, R24][User decision] **A partially-failed group.** If two of three members complete and
  one fails, does the group integrate the successful members, wait for a resume, or abort? Not yet
  specified, and it determines whether group integration is resumable.
- [Affects R19, R34][User decision] **Tail size and retention.** Nothing in Praxis currently stores
  log-like text; existing storage is small structured facts, the closest precedent being recorded
  episodes, and request bodies are capped at 128KB. Truncation, rotation, and retention need numbers.

### Deferred to planning

- [Affects R16, R17, R18][Technical] **Exact hook event names and payload fields** must be verified
  against the installed Claude Code version rather than trusted from research notes. Specifically:
  which event fires when a session awaits a human, and whether a terminal event fires on abnormal
  termination as well as clean exit.
- [Affects R15][Technical] **The meaning of each `state` value** reported by the external poll, and
  whether it distinguishes waiting-on-input from otherwise blocked.
- [Affects R19][Technical] Whether the deeper on-demand fetch uses the documented export path, and
  what it returns for a session that has ended.
- [Affects R1, R19][Technical] **Storage shape for jobs**: a new table versus metadata on existing
  rows. Note the existing lease/heartbeat pattern uses JSONB metadata rather than dedicated columns,
  so both have precedent.
- [Affects R8, R9][Technical] Whether the background daemon can bound a session's lifetime, or whether
  the backstop reaper must terminate sessions itself.
- [Affects R13][Needs research] Whether the allowlist can be derived from the checks af-build resolves
  at run start, so that every command the run will execute is pre-authorized and only genuinely novel
  actions can abort it.
- [Affects R3][Technical] How the venue choice is presented at af-build's entry point such that the
  remote path cannot accidentally claim or stamp.
- [Affects box liveness][Technical] How the box service itself stays running and is restarted, and
  how a job queued while the box is down becomes visible as stuck.
- [Affects R21][Technical] Mailbox delivery semantics: ordering, acknowledgement, and what happens to
  an unread message when a job terminates.
- [Affects cancellation][User decision, deferred] Cancelling a queued or running job is unspecified.

---

## Scope Boundaries

- Planning commands (`af-plan`, `af-intake-plan`, `af-intake-*`) always run locally. Only af-build has
  a venue.
- Local runs get status and ticket progress only — no activity tail, question detection, message
  delivery, or resume. Reaching parity would require running the observation machinery on the laptop.
- Automatic resume is out. Resume is an explicit operator action.
- Capturing a complete event timeline is out. A bounded tail plus a discrete terminal event is the
  requirement.
- Cross-job merge conflicts are resolved by the operator in the PR. af-build's conflict handling is
  within-job only and is not being extended.
- No GitHub repository configuration — no branch protection, rulesets, per-repo grants, or deploy
  keys. Accepted consequence: branch safety is a strong mitigation via topology and code, **not** an
  enforced boundary. A determined or sufficiently confused agent with box access is not cryptographically
  prevented from reaching the base branch; it is prevented from having any credential or network remote
  with which to try.
- Streaming live output to the website is out. Recent activity plus on-demand fetch replaces it.
- Rebuilding af-build on the Agent SDK is out. af-build is a Claude Code session with an armed Stop
  hook; re-hosting it is a rewrite of the factory, not a deployment change.
- Stopping and starting the instance to save idle cost is out of scope for this work.

---

## Dependencies / Assumptions

- The devbox already exists and is provisioned by `infra/lib/dev-box-stack.ts` with a retained
  workspace volume, a static address, and the Claude Code CLI, `uv`, git, and Docker installed at boot.
- Praxis is reachable from the box over HTTPS, and long-lived org-scoped API keys already exist as an
  auth mode alongside bearer tokens, so the box can authenticate without an interactive login.
- The background-session feature requires an interactive login on the box rather than an API key. This
  is a one-time setup step and does not scale per repo. **Assumption to verify:** that this login
  persists across box restarts.
- Hooks can be supplied per-dispatch via settings rather than requiring machine-level configuration.
  **Assumption to verify** at planning time.
- The website currently selects views by component state rather than by route, so a jobs view is an
  additional tab rather than new routing infrastructure.
- Migrations are the single source of truth for schema and are applied automatically on merge to the
  default branch.
- **Residual risk, accepted:** the box's instance role is `AdministratorAccess`, and a git credential
  will live on that box. Running the service under a separate operating-system user makes the token
  unreadable to build sessions, but does not eliminate the concentration of privilege.

---

## Rigor Mode

- **Rigor:** Rigorous. Decision mode: **Collaborate** — forks were brought to the operator rather than
  defaulted, except the items recorded under Defaults Taken.
- `ce-ideate` ran ahead of this document: six parallel grounded-ideation agents plus two grounding
  agents, 32 raw candidates, 7 survivors after adversarial filtering, with rejections recorded.
- `ce-brainstorm` ran as the dialogue that produced these requirements, including the constraint
  additions (session cleanup, zero per-repo friction, job groups) surfaced mid-conversation.
- External research pass ran on remote-agent triggering patterns and on Claude Code's actual session
  IO, lifecycle, hook, permission, and background-session mechanics. Two conclusions from that pass
  overturned earlier design assumptions and are recorded under Key Decisions.
- Repo grounding pass ran against the API, migrations, MCP surface, frontend, config, and test
  conventions, and against the completeness gate and ticket-state hooks. Cited inline.
- Adversarial pass: each behavior challenged for missing actors, unbounded conditions, and unhandled
  failures; results recorded under Edge States and Open Decisions rather than resolved away.
- **`ce-doc-review` pass:** pending at time of writing — findings to be integrated before hand-off to
  af-intake-plan.
- **Size note:** this document carries 38 requirements, above the range where a single implementation
  phase is advisable. Branch safety and code delivery (R26–R31) and job groups (R23–R25) are the two
  clusters most separable from a first phase; the planner should expect to split.

---

## Hand-off

af-plan wrote nothing to Praxis. **af-intake-plan** owns admission and validation: it admits each
settled requirement as a `source="prd-<project>"` fact, runs the cold-eyes planning audit, forces each
Open Decision above to a resolution, reconciles contradictions, clears the done-gate, and calls
`save_snapshot`.
