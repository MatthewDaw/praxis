---
date: 2026-07-24
topic: af-build-remote-jobs
focus: Trigger, observe, steer, and clean up long-running af-build jobs on the EC2 devbox via Praxis
rigor: Rigorous
decision_mode: Collaborate
reviewed: 2026-07-24 (ce-doc-review, 7 personas, 24 findings applied)
---

# af-build Remote Jobs

## Summary

Add a **job** concept to Praxis — created by an MCP tool, executed on the EC2 devbox as a
`claude --bg` background session, observed through externally-fired signals, and surfaced in both the
MCP surface and the Praxis website. Any agent in any repo can start a multi-hour af-build run without
SSH, be notified when it stalls, read its recent activity, answer its questions, restart it, and have
its session and worktree cleaned up automatically when it ends. af-build itself keeps working locally,
unchanged.

---

## Problem Frame

af-build runs take many hours. Today they are started by hand: SSH to the devbox with a `.pem` key,
create a named `tmux` session, run `claude`, and re-attach later to see what happened. The devbox's
own CDK output documents this as the intended workflow (`infra/lib/dev-box-stack.ts` — the
`PyCharmHint` output tells the operator to run `claude` inside `tmux` so sessions outlive disconnects).

Three costs fall out of that. Every other agent that wants to hand work to the box must also SSH, so
remote execution is available only to a human at a terminal. Observation is a terminal attach, which
means watching several concurrent builds requires several attaches and tells you nothing after a
disconnect. And nothing closes finished sessions, so dead sessions accumulate on a box that is already
running several at once.

The specific moments of pain are the two ways an af-build run stops making progress: it reaches a
question it cannot answer alone, or the Anthropic API becomes unavailable and the process dies
mid-turn. In both cases the run is silent, and silence currently looks identical to a run working hard
on a difficult ticket. The cost is measured in hours — a run that stalls at 11pm is discovered at 9am.

A fourth cost appears once several snapshots are built against one repo. Each run produces its own
branch, so landing a related batch means merging and reviewing several branches by hand, in sequence,
against a base that moved between them. The operator wants one review surface for work that was
conceived as one batch.

---

## Phasing

The requirements below carry a phase tier. **Phase 1 is exactly what closes the stall pain** and must
independently satisfy the "stalled run noticed in minutes" success criterion. Phase 2 is real work the
operator wants, but no Phase 1 requirement depends on it.

- **Phase 1** — job model and dispatch, execution on the box, observation and notification, control,
  worktree discipline and PR delivery, cleanup, local venue.
- **Phase 2** — job groups (R47–R50) and their list rendering.

---

## Actors

- A1. **Operator** (sole user): dispatches jobs, answers questions, resumes stalled jobs, reviews and
  merges the resulting PRs.
- A2. **Dispatching agent**: a Claude Code session that calls the dispatch MCP tool. In practice this
  is almost always a local `/af-build` invocation told to run remotely; the tool is not restricted to
  af-build, but no other caller is a designed-for case.
- A3. **Box service**: a long-lived process on the devbox. Claims queued jobs, launches and reaps
  background sessions, ships observation data to Praxis, integrates finished job branches into the
  repo's main worktree, and performs all pushes and PR creation.
- A4. **Build session**: a `claude --bg` background session on the box running `/af-build` against one
  prd snapshot. Does not know it is remote.
- A5. **Praxis**: the single source of truth for job state, ticket state, and observation data.
- A6. **Claude Code background daemon**: the built-in supervisor (`claude --bg`, `claude agents`) that
  owns background session lifecycle. Not ours, but relied upon — and therefore version-pinned and
  capability-probed (R16, R17).

---

## Key Flows

- F1. **Dispatch a remote job** *(Phase 1)*
  - **Trigger:** A2 calls the dispatch MCP tool.
  - **Steps:** Resolve the target read-only (snapshot exists, frontier computed, operator org
    confirmed, working tree clean) → push current HEAD to a job-derived branch → create a job row
    carrying project, snapshot, scope, origin URL, build-base SHA, intended PR base, and org → return
    the job id.
  - **Outcome:** A queued job exists; the dispatching session's completeness gate is inert and its turn
    ends immediately.
  - **Covered by:** R1–R9, R30, R31

- F2. **Execute a job** *(Phase 1)*
  - **Trigger:** A3 polls Praxis and finds a claimable queued job.
  - **Steps:** Claim the job under a heartbeated lease → preflight box-side identity and capability →
    ensure the per-repo clone and its main worktree exist (clone on first sight) → fetch → create a
    per-job worktree at the build-base SHA → launch a `claude --bg` session running `/af-build`,
    injecting plugin dirs, MCP config, settings, hooks, and permission mode at dispatch → record the
    session id and the job-scoped owner id on the job row.
  - **Outcome:** Job is running; A4 stamps its run marker under the **job-scoped owner id**, not a
    per-session one.
  - **Covered by:** R10–R17, R45, R46

- F3. **Observe a job** *(Phase 1)*
  - **Trigger:** Continuous, plus operator viewing the dashboard or calling an MCP tool.
  - **Steps:** A3 polls `claude agents --json` for existence and state → hooks fired by the session
    append activity, blocked-on-question, and terminal events to a local trail → A3 ships a bounded
    rolling tail and every terminal or blocked event to Praxis → a blocked, failed, or
    silence-threshold transition emits an outbound notification → operator sees live jobs ordered by
    attention needed and, on selecting one, its stored tail plus (when the session is alive) a deeper
    live fetch.
  - **Outcome:** Operator learns a run has stopped progressing without having gone looking.
  - **Covered by:** R18–R25

- F4. **Answer a question / resume a stalled job** *(Phase 1)*
  - **Trigger:** The operator receives a notification, or sees a blocked-on-question event or a stale
    activity timestamp.
  - **Steps:** Operator posts a message to the job's mailbox from the website or MCP, which a
    per-dispatch hook surfaces to A4 at its next ticket boundary; or the operator triggers resume,
    which takes an exclusive job-control lease, cancels any pending reap, verifies no live process
    holds the session, takes over the prior run's ticket claims under the job-scoped owner id, and
    relaunches.
  - **Outcome:** The run continues under the same ownership identity, so the completeness gate arms.
  - **Covered by:** R26–R29

- F5. **Deliver the work** *(Phase 1)*
  - **Trigger:** A job reaches its terminal state with commits present.
  - **Steps:** *(in-session, unchanged af-build behavior)* per-ticket worktrees are integrated onto the
    job worktree and the WORK-review panel runs → *(box service)* reset the repo's main worktree to the
    intended PR base → merge the job branch into the main worktree → delete the job worktree → push
    **from the main worktree only** → open a PR against the intended base → flush the final tail →
    mark terminal → reap the session.
  - **Outcome:** Reviewable PR exists; no job worktree and no session remain; history is queryable.
  - **Covered by:** R32–R38, R39–R43

- F6. **Deliver a batch as one commit** *(Phase 2)*
  - **Trigger:** Every member of a job group has reached a terminal state.
  - **Steps:** Reset the main worktree to the intended PR base → merge each member's branch in
    dispatch order → produce one commit → delete each member's worktree → push from the main worktree
    → open one PR.
  - **Outcome:** One review surface for the batch.
  - **Covered by:** R47–R50

---

## Requirements

Every requirement is Phase 1 unless marked **[P2]**.

**Job model and dispatch**

- R1. A job is a first-class, queryable Praxis entity with a stable id, distinct from the tickets it
  builds. Its lifecycle states are at minimum: queued, claimed, running, awaiting-human, completed,
  failed. "awaiting-human" is a mid-run state the job returns from; it is not terminal, and it is
  named distinctly from the ticket-level `blocked` state so the two never render as the same thing.
- R2. Claiming a job is an atomic compare-and-set that establishes a heartbeated lease. A claimed job
  whose lease goes stale returns to queued for another claim.
- R3. Both queued-age and stale-claim-age are queryable, so a job that nothing picked up and a job
  whose claimant died are each visible rather than silently stuck.
- R4. A job targets exactly one prd snapshot and runs until every in-scope ticket in that snapshot is
  finished or blocked, using af-build's existing completeness semantics. **"Scope"** means af-build's
  established build target — the `mvp` + `automated-verify` set — not a per-job free-form filter.
- R5. Dispatch is a **separate action from building**. The dispatching session must not claim tickets
  and must not stamp a whole-set run marker. (`hooks/build_completeness_gate.py:301` makes the gate
  inert when the session holds neither an unfinished claim nor its own live run marker; a dispatcher
  that stamped would block its own turn against the gate, up to the configured block cap.)
- R6. The dispatch payload is self-contained: project slug, snapshot, origin URL, build-base commit
  SHA, intended PR base, and Praxis org identity. Nothing is read from a file committed in the target
  repo.
- R7. The build base is recorded as a resolved commit SHA. Branch names are provenance only, because a
  branch recorded by name can move between dispatch and execution.
- R8. The origin URL must match a pre-registered allowlist of repos known to the operator's org.
  Dispatch is refused otherwise. Without this, an arbitrary origin can be cloned and autonomously
  built on a host holding administrative credentials.
- R9. Dispatch fails loud when the working tree or index is dirty, naming the uncommitted paths, so the
  operator never receives a PR built without changes they could see on screen when they dispatched.

**Execution on the box**

- R10. The box keeps one clone per repo **with a checked-out main worktree**, created on first sight of
  a job for that repo. The main worktree is the repo's single integration and push point (R32–R34).
- R11. Each job builds in its own worktree created at the job's build-base SHA. Concurrent jobs never
  share a working tree.
- R12. Job worktrees have no network remote. Their only remote is the box's local clone.
- R13. Background sessions are launched via `claude --bg`, and their lifecycle is owned by the built-in
  Claude Code background daemon rather than by hand-managed `tmux` sessions.
- R14. Per-dispatch flags supply the agent_factory plugin directory **and its required
  compound-engineering plugin**, the Praxis MCP configuration, the settings needed for the completeness
  gate to be enforceable, the observation hooks, and the permission mode — so no machine-level or
  repo-level configuration step is required to make a new repo work.
- R15. The box service preflights, at claim time and before launching a session, that the hook-client
  org and the MCP-tool org agree for the box principal. af-build treats divergence as a fail-loud stop;
  catching it at claim time costs seconds instead of hours.
- R16. The box service pins the Claude Code CLI version it was validated against.
- R17. On startup the box service probes each relied-upon capability — background launch, the session
  listing's fields and state vocabulary, per-dispatch hook injection, the terminal event, and resume —
  and **refuses to claim jobs** when a probe fails, rather than degrading silently. The document's own
  reason for rejecting transcript parsing (an internal surface that changed and broke the existing Go
  tailer) applies with equal force to this surface.
- R18. Steps that contend on non-file resources — the project's deploy step and any test fixture bound
  to a fixed host port or shared database — are serialized by a host-level advisory lock wrapping the
  contending command itself, keyed per repo and taken per invocation. The lock cannot live in the job
  scheduler or in af-build's instructions: the contending commands run inside the session, which the
  box service never mediates, and af-build's own parallel per-ticket workers contend on the same fixed
  port within a single job. (Verified concrete case: this repo's suite requires Postgres on host port
  5433 per `docker-compose.yml`.)
- R19. The build session runs under a permission mode with an explicit allowlist rather than
  interactive approval, because an unattended session cannot answer a permission prompt. The allowlist
  must exclude tools capable of reaching cloud credential endpoints (see R37).

**Observation**

- R20. Job observation must not depend on the build session's cooperation. Every signal used to
  determine whether a job is progressing is either fired by the harness or observed externally.
- R21. Session existence and state are polled externally via `claude agents --json`, which reports per
  session an id, working directory, kind, start time, name, and state without requiring a TTY.
- R22. A last-activity timestamp is maintained from harness-fired hook events, since the external poll
  payload carries a start time but no activity time.
- R23. **"Blocked on a question" is a first-class af-build behavior with its own harness-emitted
  event.** It is not inferred from a permission prompt: R19's allowlist mode means no permission prompt
  can occur, and the pain this work exists to solve — af-build reaching a question it cannot answer —
  is the agent producing *text*, which fires no permission hook at all. Without a purpose-built event,
  the awaiting-human state collapses into elapsed silence, which the document itself concedes is
  indistinguishable from a hard ticket, and the headline success criterion goes unmet.
- R24. A job's terminal moment is captured as a discrete event rather than inferred from a poll
  interval, and the job's recorded terminal state distinguishes completed from failed. Session exit
  alone does not mean the work finished — af-build's gate blocks session end until tickets pass, so exit
  must be reconciled against ticket completeness.
- R25. A bounded rolling tail of recent activity is stored in Praxis so recent messages remain readable
  after the session is gone, the box is unreachable, or the process died. A deeper live fetch is
  available while the session still exists.
- R26. The operator can query, from MCP and from the website: which jobs are live, their states, and a
  per-job view of recent activity. The website's top level lists live jobs, **ordered so that jobs
  needing attention — awaiting-human, failed, or past the silence threshold — sort above jobs
  progressing normally.** Selecting one shows its recent activity.
- R27. When a job enters awaiting-human or failed, or crosses the silence threshold, the system
  delivers an **unsolicited notification** to the operator over a channel readable without opening the
  dashboard, carrying the job id, project, and which condition fired. Every other observation path is
  pull-based; without this, a run that stalls at 11pm is still discovered at 9am and the motivating
  cost is unchanged.

**Control**

- R28. The operator can post a message to a **remote** job, from the website and from MCP. Delivery is
  implemented by a per-dispatch injected hook that surfaces pending messages at the ticket boundary, so
  af-build's own instructions are unchanged and the capability is absent by construction on local runs
  rather than by a venue conditional.
- R29. The operator can trigger a resume action for a **remote** job that did not finish, from the
  website and from MCP.
- R30. Resume and reap are mutually exclusive per job through a single serialized job-control path.
  Resume atomically cancels any pending reap and takes an exclusive job-control lease before launching;
  the reaper refuses to act on a job holding that lease. A liveness check alone is insufficient — it is
  read-then-act against a poll and stale by construction.
- R31. A job's ticket claims and run marker are stamped under a **job-scoped owner id recorded on the
  job row**, not a per-session id, and resume takes over the prior run's claims before relaunching.
  Otherwise a resumed session owns neither the prior claims nor the prior run marker, the completeness
  gate goes inert, and the job ends immediately having built nothing while recording itself failed —
  worst exactly when the operator resumes promptly.

**Integration, worktree discipline, and code delivery**

- R32. Integration is two-level and the levels are owned by different actors. Per-ticket worktrees are
  integrated onto the job worktree **inside the session** by af-build, followed by its WORK-review panel
  — unchanged existing behavior. The box service never re-does that merge; it integrates the finished
  job branch one level up.
- R33. **All work merges into the repo's main worktree, and pushes happen only from the main
  worktree.** The box service resets the main worktree to the intended PR base, merges the job branch,
  and pushes from there. No push originates from a job worktree.
- R34. When the merge into the main worktree conflicts, the integration fails, the job branch is
  preserved, and the job records a needs-attention terminal state. The main worktree is never left in a
  partially-merged state.
- R35. Work reaches the intended base branch only through a pull request, opened against the branch the
  operator dispatched from. There is no bypass — no per-dispatch opt-in, no configuration flag, and no
  conditional refspec. An opt-in whose authorizing surface is the dispatch payload can be set by a
  confused agent, which would put branch safety back into instructions and make R36's refspec
  restriction conditional on a model-supplied field.
- R36. Only the box service holds a credential capable of pushing, and it refuses to push any ref
  outside the job-branch pattern. The restriction lives in deterministic code, not in instructions given
  to a model.
- R37. The push credential must not be reachable from a build session. File-permission separation
  through a distinct operating-system user is necessary but **not sufficient**: the credential is
  fetched using the instance's ambient administrative role, which any local process can assume through
  the instance metadata service regardless of which user it runs as. Build-session processes must be
  denied access to the metadata service, or the credential fetched under a scoped-down assumed role held
  only in the service's memory.
- R38. The credential is account-wide and provisioned once, so onboarding a new repo requires no grant,
  no key, and no repo-level configuration.

**Cleanup**

- R39. A session that has reached a terminal state is closed automatically. The operator never
  accumulates dead sessions.
- R40. **A job worktree is deleted once its work has been merged into the main worktree and its tail
  persisted.** Per-repo clones and their main worktrees persist across jobs. Session reaping never
  deletes a worktree, and worktree deletion never precedes integration — otherwise the accumulation
  problem reappears as disk rather than sessions, or a batch's already-finished members lose the trees
  their integration needs.
- R41. The final activity tail and terminal event are persisted **before** teardown, so the evidence for
  a failed job outlives the session that produced it.
- R42. The job row and its observation history outlive the session and the worktree. Cleanup destroys
  execution artifacts, never history.
- R43. On box-service restart, live sessions are reconciled against open job rows and adopted rather
  than orphaned or duplicated.

**Local venue**

- R44. af-build continues to work locally with no behavioral change. Venue is a field on a job, not a
  branch in af-build's instructions.
- R45. A local run is surfaced as a **derived** job — projected at read time from the per-ticket run
  markers af-build already writes, with an id derived deterministically from the run owner. Only remote
  jobs get a persisted job row. This is what lets local runs appear at no additional write cost while
  keeping venue a property of the projection.
- R46. A local job's terminal state is derived from run-marker and claim staleness past their TTL,
  reconciled against the snapshot's in-scope tickets. Without this a killed local run shows as running
  forever, reintroducing silence-looks-like-work for the venue the operator uses most.
- R47. Local runs deliberately do not provide an activity tail, question detection, message delivery,
  resume, or liveness detection finer than TTL staleness. The dashboard labels this as a deliberate
  distinction rather than presenting a local job as a degraded remote one.

**Job groups — [P2]**

- R48. **[P2]** Several jobs on one repo may be dispatched as a group with a barrier: the group's
  integration runs only once every member has reached a terminal state. Members hold **separate prd
  snapshots** — their ticket sets are disjoint, which is what keeps per-project completeness queries
  attributable.
- R49. **[P2]** A group's integration merges every member's branch into the repo's main worktree in
  dispatch order, produces a single commit, and opens a single PR — rather than one PR per member.
- R50. **[P2]** Group membership is explicit on the job, so group integration can identify which
  finished work belongs to the batch.
- R51. **[P2]** Grouped jobs are visually associated in the job list, so the operator can tell that
  three rows are one batch awaiting a shared barrier rather than three unrelated jobs.

---

## Acceptance Examples

- AE1. **Covers R5.** Given a local session that dispatches a remote job, when it finishes dispatching,
  its turn ends without the completeness gate blocking, because it holds no claim and no run marker.
- AE2. **Covers R7.** Given a job dispatched from a branch, when a new commit lands on that branch
  before the box claims the job, the box still builds the SHA recorded at dispatch.
- AE3. **Covers R9.** Given uncommitted changes in the working tree, when the operator dispatches,
  dispatch fails and names the uncommitted paths.
- AE4. **Covers R11, R18.** Given two jobs dispatched for different snapshots of the same repo, when
  both run concurrently, each builds in its own worktree, and neither their deploy steps nor their
  port-bound test commands execute simultaneously.
- AE5. **Covers R2, R3.** Given a box service that claims a job and then dies before launching a
  session, when its lease goes stale, the job returns to queued and is claimable again.
- AE6. **Covers R20, R22, R23.** Given a job whose session is awaiting a human, when the operator checks
  it, they see a blocked-on-question event and a stale activity timestamp — without the build session
  having reported anything itself.
- AE7. **Covers R27.** Given a job that enters awaiting-human while the operator's laptop is closed,
  when the state transition occurs, a notification reaches the operator without the dashboard being
  open.
- AE8. **Covers R20, R25, R41.** Given a job whose process died because the API was unavailable, when
  the operator checks it afterward, they can still read the last recorded activity, because it was
  persisted before teardown and does not require the session to exist.
- AE9. **Covers R24.** Given a session that exits while tickets remain incomplete, when the job's
  terminal state is recorded, it is failed rather than completed.
- AE10. **Covers R30, R31.** Given a job the operator resumes inside its reap grace window, when resume
  runs, the pending reap is cancelled, the prior run's claims are taken over under the job-scoped owner
  id, and the relaunched session's completeness gate arms rather than going inert.
- AE11. **Covers R32, R33.** Given a job whose tickets finished, when delivery runs, af-build has
  already integrated the per-ticket worktrees in-session, and the box service merges the job branch into
  the main worktree and pushes from there — never from the job worktree.
- AE12. **Covers R34.** Given a main worktree whose base moved such that the job branch conflicts, when
  integration runs, it fails with the job branch preserved and the main worktree not partially merged.
- AE13. **Covers R35, R36, R37.** Given a build session that attempts to push to the base branch, when
  the push runs, it reaches the box's local clone; and given that same session attempting to read the
  push credential from cloud metadata, the attempt is denied.
- AE14. **Covers R39, R40, R42.** Given a job that completed, when the operator looks at the box, no
  session and no job worktree for that job remain, while the repo's main worktree persists and the job's
  history in Praxis is intact.
- AE15. **Covers R45, R46.** Given a local af-build run whose process is killed, when its run markers
  pass their TTL, the derived local job stops showing as running.
- AE16. **Covers R48, R49.** Given three jobs dispatched as one group, when two have finished and one is
  still running, no integration or PR has occurred; when the third reaches a terminal state, exactly one
  merge into the main worktree, one commit, and one PR are produced.

---

## Success Criteria

- The operator can start a multi-hour build from any repo without SSH, close the laptop, and be
  **notified** when a job stalls, fails, or needs an answer — rather than having to go look.
- A stalled run reaches the operator's attention in minutes rather than at the next morning's check,
  and Phase 1 alone satisfies this.
- The three states that matter — working, awaiting a human, dead — are distinguishable from outside the
  session, without the session's cooperation.
- No dead sessions and no orphaned worktrees accumulate on the box without the operator acting.
- Every push originates from a repo's main worktree, and work reaches a base branch only through a PR.
- Onboarding a brand-new repo requires no configuration in GitHub, on the box, or in the repo.
- af-build's local behavior is unchanged, and its instructions contain no venue conditionals.
- A downstream planner can implement each requirement without inventing job states, terminal-state
  semantics, integration levels, branch-safety rules, or the local-vs-remote fidelity boundary.

---

## Edge States and Failure Classes

**Empty and boundary states**
- No queued jobs; no live jobs (dashboard empty state). Covered by R26.
- A job whose snapshot has zero incomplete tickets at claim time — completes immediately, produces no
  PR. Covered by R4, R24.
- A group with one member. Covered by R48.
- A job whose build produced no commits — terminal without integration or a PR. Covered by R35.

**Failure classes**
- **Silent partial failure:** session exits with tickets incomplete but the job records completed.
  Closed by R24.
- **Resumed job builds nothing:** new session owns neither prior claims nor run marker, gate goes inert.
  Closed by R31.
- **Lost evidence:** session reaped before its tail is persisted. Closed by R41.
- **Orphaned worktrees:** the accumulation problem in disk form. Closed by R40.
- **Premature worktree deletion:** integration loses the tree it needed. Closed by R40.
- **Partially-merged main worktree.** Closed by R34.
- **Duplicate execution:** two box services, or a restarted service, launching two sessions for one job.
  Closed by R2 (atomic claim) plus R43.
- **Stuck claim:** claimant dies after claiming, before launching. Closed by R2, R3.
- **Reap/resume race:** the system producing the very corruption R29 exists to prevent. Closed by R30.
- **Transcript corruption:** two processes on one session. Closed by R29, R30.
- **Silent capability regression:** a CLI upgrade degrades liveness, resume, or reaping with no signal.
  Closed by R16, R17.
- **Unattributable question state:** no harness signal for awaiting-human. Closed by R23.
- **Arbitrary code built on a privileged host:** unbounded dispatch origin. Closed by R8.
- **Credential exfiltration via cloud metadata.** Closed by R19, R37.
- **Irreversible action:** a push to the base branch. Closed by R33, R35, R36 — with no opt-in that an
  agent could set, and explicitly *not* closed by instructions alone.
- **Cross-job merge conflict:** two same-repo jobs produce branches that never saw each other. Surfaced
  by R34 as a failed integration rather than silently resolved. af-build's conflict handling remains
  within-job only.
- **Wedged-but-alive:** session neither progressing nor exiting. Detected by R22, notified by R27,
  recovered by R29.
- **Praxis unreachable from the box:** job state cannot be updated. Not closed — see Open Decisions.
- **Box unreachable:** dispatch succeeds, nothing claims the job. Closed by R3 (queued-age).

**Lifecycle**
- Create: dispatch (F1). Recover: resume (R29). Destroy: sessions and job worktrees only (R39, R40, R42).
- Cancelling a queued or running job is **not** specified — see Open Decisions.
- Retention and rotation of stored tails is **not** specified — see Open Decisions.

**Gap lenses — explicit fire-or-pass**
- *Failure modes:* **fires** — see above.
- *Security:* **fires** — R8 (origin allowlist), R19 + R37 (credential reachability), R33–R36 (branch
  safety), with residual privilege concentration recorded under Dependencies.
- *Data lifecycle:* **fires, partially unresolved** — tail retention and rotation remain open.
- *Rollback:* **fires** — PR-only delivery is the rollback story for delivered work; R34 keeps the main
  worktree clean on a failed integration; a half-merged group remains open.
- *Who pays the tradeoff:* **fires** — the operator pays for local runs having lower observability
  fidelity (R47), and for cross-job conflicts being resolved by hand in the PR.

---

## Key Decisions

- **Pull, not push.** The MCP tool writes a job row; the box claims it. Praxis cannot reach into the
  box. The rejected alternative — Praxis calling AWS SSM to run a command — would give the public API
  arbitrary shell on a box whose instance role is administrative.
- **Wrap the built-in background daemon rather than hand-rolling a supervisor** — but pin its version
  and probe its capabilities (R16, R17). Wrapping removed most of the originally-planned daemon and
  eliminated the accumulating-dead-session failure mode by construction; the pin-and-probe is what keeps
  that from becoming a silent dependency on an unstable surface.
- **Observation is external, never self-reported.** Self-reporting fails in exactly the two cases that
  matter — a run awaiting a question is not writing, and a crashed run cannot report its own death.
- **A question is a first-class event, not an inferred state** (R23). This is the one place the design
  requires new af-build behavior rather than only new infrastructure.
- **Do not parse the session transcript JSONL.** Its location is stable but its entry format is
  documented as internal and subject to change between releases; the existing Go tailer in
  `session-capture/` has already been broken once by that layout.
- **Venue is a field, not a code path**, and local jobs are a read-time projection rather than a second
  write path (R45).
- **One integration point and one push point per repo** (R33). Two-level integration keeps af-build's
  in-session merge unchanged while giving the box service a single, auditable place where work becomes
  pushable.
- **Branch safety lives in code and topology, not in instructions**, with no bypass anywhere (R35).
- **No repo-level configuration anywhere** — not in GitHub settings, not in a committed file, not as a
  per-repo grant.
- **No automatic resume.** The operator resumes explicitly; the system's job is to make a stalled job
  obvious, notified, and restartable.
- **Store a bounded tail, fetch depth on demand.** Storing everything is unnecessary for knowing when a
  job ended (one terminal event suffices); storing nothing fails the crash case that motivated the work.
- **Phase 1 is defined by the stall pain, not by separability** — so a split cannot ship the plumbing
  and leave the motivating problem open.

---

## Defaults Taken (flagged for override)

- D1. **Stuck is reported as an observation, not a verdict.** Raw signals plus one conservative
  elapsed-silence threshold, phrased as elapsed silence rather than as "stuck". Rationale: at short
  timescales a hard ticket is indistinguishable from a hang. Note this is now a *supplement* to R23's
  purpose-built event, not the primary signal.
- D2. **Successful jobs reap immediately; failed jobs hold their session for a grace window, then a
  backstop reaps them.** Rationale: immediate reaping of failures destroys the crash scene; no reaping
  is the accumulation problem. Interacts with R30 — the grace window is cancellable by resume.
- D3. **Contention is handled by a host-level advisory lock wrapping the contending command**, not by
  restricting how jobs are queued (R18).
- D4. **Secrets Manager** for the box credential, matching the only existing secret-fetch pattern in the
  codebase (`knowledge/serve/db.py`; there is no SSM Parameter Store usage anywhere). Subject to R37 —
  the store is not the boundary.
- D5. **No per-job ticket tagging.** Concurrent same-repo jobs — including group members (R48) — hold
  different snapshots, so their ticket sets are disjoint and project-level completeness queries
  attribute progress correctly.

---

## Open Decisions

### Resolve before planning

- [Affects R36, R38][User decision] **Credential mechanism: account-wide PAT versus GitHub App
  installation.** Both satisfy zero-per-repo onboarding. A GitHub App gives short-lived tokens and
  bot-attributed PR authorship; a PAT is faster to stand up. Checked: GitHub scopes tokens by repo and
  permission category, never by ref, so neither can be restricted to non-base branches — which is why
  R33–R37 place the boundary in topology, code, and credential reachability instead.
- [Affects R25, R42][User decision] **Where the tail lives, then how big.** Nothing in Praxis currently
  stores log-like text; existing storage is small structured facts, the closest precedent being recorded
  episodes, and request bodies are capped at 128KB. Decide whether the tail belongs in Praxis at all
  versus an object store referenced by the job row — R25's after-the-box-is-unreachable guarantee
  depends on that answer — and only then set truncation, rotation, and retention.
- [Affects R48, R49][User decision, P2] **A partially-failed group.** If two of three members complete
  and one fails, does integration proceed with the successful members, wait for a resume, or abort? This
  determines whether group integration is resumable, and the group barrier currently has no liveness
  bound — one wedged member withholds the others' work indefinitely.

### Deferred to planning

- [Affects R22, R23, R24][Technical] Exact hook event names and payload fields, verified against the
  pinned CLI version — specifically which event can carry R23's blocked-on-question signal, and whether
  a terminal event fires on abnormal termination as well as clean exit.
- [Affects R21][Technical] The state vocabulary reported by the session listing, and whether it
  distinguishes awaiting-input from otherwise blocked.
- [Affects R29][Technical] Whether resume composes with background launch and continues the same
  session rather than forking a new one.
- [Affects R19][Technical] Whether an unapproved tool call in an unattended session terminates the run
  or hangs awaiting approval. D2's grace-window logic differs by answer.
- [Affects R25][Technical] Chunking strategy for shipping the tail under the 128KB body cap.
- [Affects R1, R25][Technical] Storage shape for jobs — a new table versus metadata on existing rows.
  The existing lease/heartbeat pattern uses JSONB metadata rather than dedicated columns, so both have
  precedent.
- [Affects R13, R39][Technical] Whether the background daemon can bound a session's lifetime, or whether
  the backstop reaper must terminate processes itself.
- [Affects R19][Needs research] Whether the allowlist can be derived from the checks af-build resolves
  at run start, so every command the run will execute is pre-authorized and only genuinely novel actions
  can abort it.
- [Affects R5][Technical] How the venue choice is presented at af-build's entry point such that the
  remote path cannot accidentally claim or stamp.
- [Affects box liveness][Technical] How the box service itself stays running and is restarted — the
  service that watches builds currently has nothing watching it.
- [Affects R28][Technical] Mailbox ordering, acknowledgement, and what happens to an unread message when
  a job terminates.
- [Affects R25][Technical] Whether the stored tail needs secret scrubbing before it is written.
- [Affects cancellation][User decision, deferred] Cancelling a queued or running job is unspecified.
- [Affects R26][Technical] Whether the website exposes a finished-jobs history view, or the delivered PR
  is treated as the record of completed work.
- [Affects R26][Technical] Phone legibility of the live-jobs list, given the success criterion names a
  phone as the triage device.

---

## Scope Boundaries

- Planning commands (`af-plan`, `af-intake-plan`, `af-intake-*`) always run locally. Only af-build has a
  venue.
- Local runs get status and ticket progress only, with liveness no finer than TTL staleness. Reaching
  parity would require running the observation machinery on the laptop.
- Automatic resume is out. Resume is an explicit operator action.
- Capturing a complete event timeline is out. A bounded tail plus discrete terminal and blocked events
  is the requirement.
- Cross-job merge conflicts are resolved by the operator in the PR. af-build's conflict handling is
  within-job only and is not being extended.
- No GitHub repository configuration — no branch protection, rulesets, per-repo grants, or deploy keys.
  Accepted consequence: branch safety is a strong mitigation via topology, code, and credential
  reachability, **not** a cryptographically enforced boundary. An agent with box access is prevented
  from having any credential or network remote with which to reach a base branch; it is not
  mathematically prevented from trying.
- Streaming live output to the website is out. Recent activity plus on-demand fetch replaces it.
- Rebuilding af-build on the Agent SDK is out. af-build is a Claude Code session with an armed Stop
  hook; re-hosting it is a rewrite of the factory, not a deployment change.
- Stopping and starting the instance to save idle cost is out of scope.

---

## Dependencies / Assumptions

- The devbox already exists and is provisioned by `infra/lib/dev-box-stack.ts` with a retained workspace
  volume, a static address, and the Claude Code CLI, `uv`, git, and Docker installed at boot.
- Praxis is reachable from the box over HTTPS, and long-lived org-scoped API keys already exist as an
  auth mode alongside bearer tokens.
- **Both plugin sources must be available to dispatched sessions** — the agent_factory plugin directory
  and its hard-required compound-engineering dependency, which af-build refuses to proceed without.
  One-time box setup, not per-repo.
- The background-session feature requires an interactive login on the box rather than an API key. This is
  one-time setup and does not scale per repo. **Assumption to verify:** that the login persists across
  box restarts. Note this login is itself a durable high-value credential on a privileged host.
- Hooks can be supplied per-dispatch via settings rather than requiring machine-level configuration.
  **Assumption to verify** at planning time; R17's probe is the enforcement.
- The website currently selects views by component state rather than by route, so a jobs view is an
  additional tab rather than new routing infrastructure.
- Migrations are the single source of truth for schema and are applied automatically on merge to the
  default branch.
- **Residual risk, accepted:** the box's instance role is administrative, and a push credential lives on
  that box. R37 reduces reachability; it does not eliminate the concentration of privilege.
- **Residual risk, accepted:** the cross-job conflict tax grows with concurrency and is bounded nowhere.
- **Minority position, recorded:** a smaller increment — hooks plus notification on today's
  hand-started sessions — would close the stall-discovery latency without a box service, per-repo
  clones, job worktrees, a push credential, or a two-tier local/remote story. It was rejected because it
  leaves dispatch-without-SSH, cleanup, and batch delivery unsolved, but the standing maintenance cost of
  the larger footprint is accepted deliberately rather than by omission.

---

## Rigor Mode

- **Rigor:** Rigorous. Decision mode: **Collaborate** — forks were brought to the operator rather than
  defaulted, except the items recorded under Defaults Taken.
- `ce-ideate` ran ahead of this document: six parallel grounded-ideation agents plus two grounding
  agents, 32 raw candidates, 7 survivors after adversarial filtering, with rejections recorded.
- `ce-brainstorm` ran as the dialogue that produced these requirements, including the constraint
  additions surfaced mid-conversation: session cleanup, zero per-repo friction, job groups, and the
  merge-into-main-worktree / push-only-from-main-worktree discipline.
- External research pass ran on remote-agent triggering patterns and on Claude Code's session IO,
  lifecycle, hook, permission, and background-session mechanics. Two conclusions overturned earlier
  design assumptions and are recorded under Key Decisions.
- Repo grounding pass ran against the API, migrations, MCP surface, frontend, config, and test
  conventions, and against the completeness gate and ticket-state hooks. Cited inline.
- Adversarial pass: each behavior challenged for missing actors, unbounded conditions, and unhandled
  failures.
- **`ce-doc-review` pass ran** (7 personas: coherence, feasibility, product-lens, design-lens,
  security-lens, scope-guardian, adversarial). 38 raw findings → 5 cross-persona merges → 24 actionable
  and 11 advisory. **All 24 were applied into this revision**; nothing was skipped. The advisory set and
  the reviewers' residual concerns were folded into Open Decisions and Dependencies. Findings that
  changed the design most: the question-signal gap (R23), the resume-ownership defect (R31), the
  credential-reachability defect (R37), the removal of the direct-push opt-in (R35), and the absence of
  any notification path (R27).

---

## Hand-off

af-plan wrote nothing to Praxis. **af-intake-plan** owns admission and validation: it admits each settled
requirement as a `source="prd-<project>"` fact, runs the cold-eyes planning audit, forces each Open
Decision above to a resolution, reconciles contradictions, clears the done-gate, and calls
`save_snapshot`. Phase 2 requirements (R48–R51) should be admitted with a `post-mvp` scope tier so the
build target excludes them until Phase 1 is delivered.
