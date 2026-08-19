---
name: af-build
description: >
  The build entry point: drive this project's incomplete set — the whole prd-<project> build set, or a
  scoped subset — to done. Run the factory build loop (FIND the next incomplete ticket → CLAIM its lease →
  RESOLVE + pin its checks by query → BUILD to the acceptance condition → VERIFY by running EVERY pinned
  validation check on external signals → FINISH only when all checks pass) until no claimable incomplete
  ticket remains, then convene the ce-* cold-eyes WORK-review panel. Verification is intrinsic and
  always-on (the former af-verify), not a separate step. BY DEFAULT it launches an ultracode Workflow that
  fans the dependency-ready frontier out across parallel one-ticket workers (each in its own worktree,
  spawned with the per-ticket worker contract verbatim), looping until the set is done — falling back to a
  single inline agent only for a linear/one-ticket frontier or when Workflow is unavailable; either way
  exactly ONE decision-making agent per ticket, whose only delegation is a disposable read-only retrieval
  sub-agent. All dynamic state lives in Praxis — no JSON status files or locks. The "go work unfinished"
  entry point (not for planning new work).
---

## The methodology — read first, this is the loop af-build OWNS

State lives in ONE place: **Praxis**. There are no JSON status files, no locks on disk, no self-set "done"
flags. A ticket (requirement) and a check are Praxis facts; everything about what is built / claimed /
passed is state **ON THE TICKET'S Praxis node**, read and written live via `hooks/_ticket_state.py` (on
`hooks/_praxis.py`), per `docs/factory-state-contract.md` (METHODOLOGY.md). Conform to that contract
exactly.

**ONE TICKET AT A TIME, END-TO-END.** This is the cardinal rule of the loop. You pop a SINGLE ticket, then
carry it all the way to `finished` — claim → resolve requirements → synthesize covering validations → build
→ validate → release finished — before you so much as read another ticket. No batching, no surveying the
queue, no pre-loading the next ticket's requirements, no holding two tickets in context. The whole-set run
marker + the gate are the *system's* guarantee that the entire scope gets done; your *attention* stays on
exactly one ticket until it has shipped end-to-end. (The one-time scope stamp in step 0 is id-only
bookkeeping — it reads no ticket bodies and is not "working" them.)

**One-ticket-at-a-time is a per-WORKER rule, not a serialization of the run.** By default af-build **fans
the dependency-ready frontier out across parallel one-ticket workers via an ultracode Workflow** (see
*Execution model*, below) — each worker still carries its single ticket end-to-end in an isolated worktree,
and the orchestrator only *schedules* (it computes the ready frontier and dispatches; it never writes code).
That is deterministic scheduling, **not a crew** — a crew is many agents deciding on ONE ticket, which never
happens. The inline single-agent loop (§1→§7) is the fallback for a linear/one-ticket frontier or when the
Workflow tool is unavailable.

To drive the (optionally scoped) build set to done you run **exactly this loop**:

**AT SESSION START**, before step 0: run `af-retro --flags <project>` (or `af-retro --flags` alone for
every project this session touches). This is R24's push-not-pull surfacing — a suspension, parking,
undraftable, or check-defeat event from an earlier run stays on the PENDING list until someone runs
`af-retro ack <flag_id>`, and this is where that list becomes visible again rather than only at the
loop-end notification that first raised it. A non-empty list is not a build blocker — note it in your
report so a human can ack it — but it must never be silently skipped.

> `af-retro` is a real console script (`[project.scripts]` in `agent_factory/pyproject.toml`), but it
> is only on `PATH` where the factory is installed. It named a binary that existed in NO venv until
> that table was added, which is how this instruction sat unexecutable through entire builds. If
> `af-retro` is not found, run the module instead — same code, no install needed:
> `python -m agent_factory.af_retro --flags <project>` under the loop's `PYTHONPATH` (already
> exported for you; see *Do NOT go hunting for the factory's own code*), or
> `uv run --project <factory>/agent_factory af-retro --flags <project>` from anywhere else. Never
> report the step as skipped because the first form was not found.

0. **OPEN THE RUN** — resolve the scope to its in-scope incomplete ticket ids (an **id-only** pass — do
   not read ticket bodies) and **STAMP the whole-set run marker** on every one
   (`_ticket_state.stamp_run(cids, owner, scope_label)`). This persisted, scope-bearing marker is what arms
   the gate for the *whole* run — so it keeps blocking even in the instant between finishing one ticket and
   claiming the next. Without it the gate only holds you to a ticket you currently have claimed.
1. **FIND (one)** — query Praxis for the incomplete set in scope (incomplete = never-built | regressed |
   stale, derived from recorded outcomes — including any a validation just regressed), then **pop the ONE
   next DEPENDENCY-READY ticket** with `next_ready_ticket(incomplete)`: the single front whose every
   `depends_on` prerequisite is already `finished` (it depends on no unfinished or in-progress job). Claim
   that one and ignore the rest — you do not look at another ticket until this one ships. Pass the **BARE**
   project name (e.g. `team-app`); the endpoint adds the `prd-` prefix itself — passing `prd-team-app`
   searches `prd-prd-team-app`, returns EMPTY, and silently hides all work.
2. **CLAIM** — atomically flip the ticket's `meta.build_state` `incomplete → in_progress`, stamping
   `claim_owner` = you + a heartbeat. The claim is a **LEASE, not a lock**: refresh the heartbeat while
   working; a stale lease (`now - claim_heartbeat_at > claim_lease_ttl`) auto-reclaims so a dead agent
   never strands a ticket. Parallel agents never double-work because a live claim is visible to all; a
   rare double-claim is harmless wasted work, not corruption.

   **The heartbeat proves the agent is ALIVE, not that the ticket is ADVANCING.** They are different
   claims and only the first one is made here. A lease refreshed on schedule by an agent that is
   stuck in a loop, re-reading the same files, or running a step that will never finish looks
   exactly like healthy work — the lease cannot distinguish them, and neither can anyone watching
   it. Any step inside a ticket that can run longer than a few minutes must ALSO emit progress
   lines (see below), or the only available answer to "how is this ticket going" is "the lease is
   fresh", which is not an answer.
3. **RESOLVE the validation REQUIREMENTS** — determine which abstract validation *requirements* this
   ticket must satisfy **BY QUERY** (its tag ∪ its surfaces ∪ semantic match against active
   `category="check"` facts). The ticket carries identity only and **NEVER an authored requirement
   list**. Truncate any prior validations and **PIN the resolved requirement ids as the coverage
   contract** (`start_ticket` does claim + resolve + pin-the-contract in one call).
4. **SYNTHESIZE the VALIDATIONS** — convert the retrieved requirements into a **custom list of concrete,
   executable validations that FAITHFULLY COVER every requirement** (each validation declares the
   requirement id(s) it `covers` and a `run` command whose exit code is the signal), then
   `pin_validations(cid, [...])`. A coverage-back-check (`coverage_gap(cid)` must be empty) is part of
   doneness: a requirement with no covering validation means the ticket is **not** verifiable-done.
4b. **READ WHY IT CAME BACK — ALL of it** — before writing a line of code, check the ticket's
   `meta.regression_detail` and `meta.audit_disposition`. A ticket in the incomplete set is either
   never-built or **regressed**, and a regressed one carries the reports from whatever sent it back.
   `regression_detail` is an **accumulating LIST of finding dicts, oldest first** — not a single
   dict. Concurrent writers (post-merge verification, conflict resolution, the ingestion API) each
   append their own entry rather than clobber a sibling's (R16/E3), and answered findings stay in
   the list stamped `resolved: true`. So `regression_detail.reason` reads as nothing, and
   `regression_detail[0]` is the OLDEST finding, usually already resolved — either way the worker
   rebuilds blind, which is the precise failure this list was built to stop (one ticket was
   regressed with a report naming the defect, the evidence and the fix, and closed again TWICE
   without its file being touched).
   **Read it through the helpers, not by indexing.** With the loop's `PYTHONPATH`
   (`<repo>/agent_factory/hooks:<repo>/agent_factory/src`):
   `import _ticket_state as ts` → `ts.open_findings(meta)` returns EVERY finding still owed an
   answer (unresolved, non-empty `reason`), oldest first; `ts.ticket_briefing(cid, meta)` renders
   all of them as ready-to-read text. Both lift a legacy single dict into a one-entry list, so an
   older ticket still reads correctly. Every open finding is binding — **answer all of them**, not
   just the first.
   Each finding carries `source`, `reason` (what failed), `evidence` (the failing test/gate and its
   error text), `required_fix` (what the rebuild must address), plus source-specific fields
   (`round`, `check_id`, `commit_sha`, `branch`). When a finding's `source` is
   `post-merge-verification`, this ticket's own worktree build was GREEN and it failed only once
   merged — so repeating the original approach reproduces the same failure, and the fix is almost
   always integration-level (a registry/manifest/seed row the merged tree needs, or a collision with
   work that landed after) rather than anything wrong inside the ticket's own diff. Treat those
   reports as part of the acceptance condition and rebuild against the CURRENT integrated tree.
   Skipping it is not a shortcut — it re-derives at full cost a diagnosis another agent already paid
   for and wrote down.
   You do **not** close a finding by asserting you fixed it: `resolved` is stamped only when a later
   verification round confirms the ticket survived integration, or a human dismisses it.
5. **BUILD** — do the work to satisfy the ticket's binary acceptance condition.
6. **VERIFY** — run **EVERY** pinned validation; record each pass **ON THE TICKET NODE** (never on the
   requirement fact — requirements are read-only during builds). **External signals only** (exit codes /
   tests / build / type-check / lint); never self-judge. This is intrinsic — the build ALWAYS verifies.
7. **FINISH** — only when coverage is complete **and** every pinned validation passed: record a
   `succeeded` outcome and release the lease with the hard enum `build_state="finished"` (which also
   clears the run marker on that ticket). If any validation fails, record a `failed` outcome — that
   **regresses** the ticket so it re-enters the FIND set and is re-done. A requirement that genuinely
   **cannot** be covered or run (credential-only, unsatisfiable) → `block(cid, owner, reason)`: surfaced
   for owner action, excluded from churn, never a silent forever-deadlock.
8. **LOOP** — repeat FIND→FINISH until the scoped incomplete set is empty, `refresh_run` at each ticket
   boundary so the marker never goes stale mid-run.
9. **REVIEW + CLOSE THE RUN** — at done, convene the ce-* cold-eyes **WORK-review** panel over the whole
   diff, record the panel-ran episode, then `clear_run(cids, owner)` to end the run and let the gate go
   inert.

**Praxis is a HARD dependency.** If `_praxis` raises `PraxisUnreachable`, STOP — never assume a ticket is
done, never proceed past a gate, never invent or cache state. The single Stop hook
**`hooks/build_completeness_gate.py`** enforces this loop: it reads Praxis live, **fails CLOSED**, arms when
**this session owns a live `in_progress` claim OR a non-stale whole-set run marker** scopes work to it,
honors `build_state="finished"` (and excludes/ surfaces `build_state="blocked"`), and blocks the turn from
ending until the **entire scoped set** is finished — not merely the ticket you currently hold. The run
marker is what closes the between-ticket window; that is why step 0 stamps it and step 9 clears it.

**There are NO `.factory/*.json` manifests.** "A build run is active" ≡ *this session owns a live,
unfinished `in_progress` claim*, read from Praxis — never a file flag. Code lives in **git**, not Praxis;
only judgments and learnings go to the graph. Every step is an event-log entry — cite the fact(s) that
grounded each decision.

---

# Factory Build — drive the (optionally scoped) build set to done

The explicit entry point for *"address unfinished work."* This skill consumes a plan already hardened by
**af-plan** (→ `prd-<project>`) and surfaces bound by **af-intake-plan**; it does **not** plan new work or admit
requirements. It runs the loop above per ticket and convenes the holistic panel at completeness.

## Scope (optional — the whole point of the argument)

- **No argument** (`/af-build`) → drive the **WHOLE incomplete set** to done. Default.
- **A scope argument** (`/af-build auth` · "only the unfinished auth tickets") → claim and build **ONLY**
  the incomplete tickets matching that scope; leave every other ticket alone **even if it is also
  incomplete**. Resolve the scope to a requirement set, in this order: a **class tag** (match `meta.tags`),
  explicit **requirement ids**, or a named **area** (semantic/text match — e.g. "auth" → login, signup,
  logout, JWT/session, password reset, authz). **List exactly which tickets you selected** before
  building, and **report the non-scoped incomplete tickets as parked** — surfaced, never silently skipped,
  but not claimed this run. If the scope is ambiguous, list your selection and ask before churning.

**The resolved scope IS the run, and the gate enforces exactly it.** Whatever set you select — all, a tag,
specific ids, or an area — `stamp_run` marks precisely those ticket ids (step 0). The whole-set gate then
blocks until **every marked ticket** is `finished` (or `blocked`), and the parked non-scoped tickets carry
no marker so the gate leaves them alone. Scope is therefore a hard contract, not advisory: you cannot end
the run with a marked ticket unfinished, and you cannot accidentally over-build a parked one.

## Validation source — the project space's `building-validation` snapshot

Validation **checks live in a DEDICATED snapshot inside the project's own space**, separate from the
`prd-<project>` snapshot that holds the tickets and their build state. **A project IS a space** — the
space id is the BARE project name (`team-app`), and inside it live the `prd-<project>` snapshot
(tickets, mutable) and the check snapshots.

**Two snapshots, one space.** At RESOLVE time af-build reads the *checks* from
`(space=<project>, snapshot=building-validation)`, while every bit of ticket STATE — claims, pins,
passes, outcomes, run-markers — is read and written on the **`prd-<project>`** snapshot. The typed
`project_ref` seam in `hooks/_ticket_state.py` (`resolve_validation_requirements` / `start_ticket`)
points ONLY the check reads at `building-validation`; check resolution never touches the state snapshot.
That check snapshot must hold the `category="check"`, `scope="validation"` rules; if it is empty a ticket
resolves **only** its always-present acceptance-condition floor (below) — fewer checks, never a crash.
(Seed it from the plan or save a snapshot into it out-of-band; af-ingest author-check is how new
`building-validation` rules get authored there.)

### How a check pins onto a ticket — the matching model

Every check **owns its own applicability**. The applicability **PREDICATE** is the check's
`meta.applies_to` — a list of tags. The **IDENTITY** it matches against is the ticket's `meta.tags`
(with the ticket's `meta.applies_to` as a lenient fallback for a ticket that carries no `tags`). **A
check pins onto a ticket iff their tag sets intersect** — one shared tag is enough.

Both sides are **normalized on both ends** — at author time (when the check or ticket is written) AND at
resolve time (when af-build runs the query) — by the same rule: `strip` + `casefold`, with the literal
`"*"` preserved verbatim. So `Auth`, `auth`, and ` auth ` are the same tag, and a check is **never
silently dropped** over casing or stray whitespace.

### The lanes that build the contract

RESOLVE unions three **precise, mandatory** lanes, then prepends the floor:

- **tag lane** — checks whose `meta.applies_to` intersects the ticket's (normalized) tags. This is the
  intersection rule above.
- **`"*"` wildcard lane — SEPARATE on purpose.** Universal gates authored with `applies_to: ["*"]`
  (typecheck, build, lint, test) that apply to EVERY ticket. This lane is queried **separately** because
  the per-tag lookup in the tag lane *structurally cannot* surface a `["*"]` check: a ticket's concrete
  tags are things like `auth`, `backend` — they never include the literal `"*"`, so intersecting a
  ticket's tags with `["*"]` is always empty. Pulling wildcards explicitly is the only way a universal
  gate reaches every ticket.
- **surface lane** — checks bound via the `renders` edge to a surface the ticket renders, so a
  frontend/UI check lands ONLY on tickets that render a screen and never on a pure backend ticket.

The **semantic lane is separate and advisory** — retrieved as *inspiration* during synthesis, never
pinned, never gating completion (§3).

### The acceptance floor is always prepended

`contract_with_floor` ALWAYS puts the ticket's own binary acceptance condition (`<cid>::acceptance`) at
the front of the contract, so **the contract is never empty even when zero Praxis checks match**. Every
ticket therefore has at least one thing to prove: its own red→green acceptance test.

### Two worked examples

- **A check-matched ticket.** A backend ticket tagged `[backend, token-verification]` resolves a
  `backend`-tagged typecheck (tag lane: `backend` is in both sides) and a `token-verification` login-e2e
  check (tag lane: `token-verification` is in both) — **PLUS** the always-prepended `<cid>::acceptance`
  floor. Contract = 3 requirements; every one must be covered and pass before FINISH.
- **A zero-declared-check ticket.** A ticket whose tags match nothing in `building-validation` (and which
  renders no bound surface) resolves the floor ALONE — just `<cid>::acceptance`. **This is NOT a defect
  and needs no amend.** You still author the custom red→green eval for the acceptance condition and finish
  normally; a floor-only contract is a complete, honest contract.

### Verify coverage BEFORE a build — the dry-run inspector

`python -m tools.resolve_preview <project>` prints, **read-only**, exactly which checks pin
onto which tickets and by which lane, without claiming or building anything. It is the **formal way to
verify coverage** — run it before a build whenever you want to see the resolution the loop will compute.

> **Renamed from `coding-validation`.** The build-check snapshot is now `building-validation`, and it is
> a per-project snapshot in the project space — NOT a single global `coding-validation` space. Legacy
> global checks are not retro-fitted into per-project spaces (old data carried no reliable project
> association); teams re-seed each project's `building-validation` snapshot via af-ingest author-check.

**Override — slash argument ONLY** (no env seam): `/af-build [scope] --checks-space=<space[:snapshot]>`
points resolution at a different `(space, snapshot)` for this run. Thread it as an `override`
`(space, snapshot)` pair into **every** `start_ticket(...)` call — including the per-ticket worker
contract (§8), so fanned-out workers read the same reference. With no argument the default applies:
`space=<project>`, `snapshot=building-validation`.

## ORG TENANCY — operate in the PROJECT-DERIVED org, never a hardcoded default

Every Praxis read/write this loop makes is tenanted to an **org**, and that org is **project-derived** — the
`PRAXIS_ORG` pin, resolved through `identity.factory_org()`, NOT a hardcoded `"agent-factory"`. A fresh run
proceeds in the project's pinned org and **never selects `"agent-factory"`** (or any literal) just to "get
going". **Hard rule: the af-build hook-client org (`PRAXIS_ORG`) and the MCP-tool org (`praxis_whoami` /
`praxis_select_org`) MUST AGREE** — a fail-loud guard enforces it, so a header-truthful `whoami` that
disagrees with the pinned client org is a STOP, not something to paper over by re-selecting an org. If they
diverge, align them to the one true project org (fix the pin, or fix the selection) **before** claiming a
single ticket; `praxis_select_org` itself refuses a request that fights the `PRAXIS_ORG` pin, naming both
orgs. Never select around the mismatch.

## STATE TENANCY — the whole loop operates on the plan snapshot

Ticket STATE (build_state, claims, pins, run-markers, outcomes) lives on the project's
`prd-<project>` snapshot, NOT working memory. Compute the plan ref ONCE at the top of the run:
`PLAN = _ticket_state.project_ref(project).plan` (== `(project, "prd-<project>")`). Then:
- pass `space=PLAN[0], snapshot=PLAN[1]` to `_praxis.incomplete_requirements(project, ...)` (FIND) and to
  `_praxis.record_outcome(...)`;
- pass `ref=PLAN` to every `_ticket_state` state call — `stamp_run`/`refresh_run`/`clear_run` here, and
  `claim`/`heartbeat`/`release`/`block`/`pin_*`/`record_validation_pass` in the worker (§8).
`start_ticket(cid, owner, project)` derives PLAN from `project` itself. Working memory is only the
dashboard's edit buffer + personal-memory MCP surface; the factory never keeps state there.

## 0. OPEN THE RUN — stamp the whole-set marker

Resolve the scope (§Scope) to its in-scope incomplete ticket ids, **list them for the human**, then
`_ticket_state.stamp_run(cids, owner, scope_label, ref=PLAN)`. This is the single act that makes the gate
enforce the *whole* run rather than just a held claim. `refresh_run(cids, owner, ref=PLAN)` at every ticket
boundary keeps the marker non-stale (it auto-expires after `DEFAULT_RUN_TTL_S` so a dead run never strands
the set), and `clear_run(cids, owner, ref=PLAN)` at the very end (§7) ends the run.

## Execution model — /af-build FANS OUT the ready frontier (mechanism not prescribed; admission is the one sanctioned narrowing)

**The no-narrowing rule: every round dispatches the WHOLE dependency-ready frontier.** Grinding the set
inline one ticket at a time when 2+ are ready, or silently dispatching fewer ids than a round was handed,
is a BUG, not a safe choice — *unless* the ticket being held back is deferred by resource admission (R15,
below), the one sanctioned exception to this rule. Nothing else narrows a round.

**This fan-out contract does not prescribe WHICH mechanism performs the dispatch** — the `Workflow` tool,
`Agent` subagents, or an external driver are all sanctioned, chosen by the guidance below; what is fixed is
that each dispatches the admission-capped frontier in full, one decision-making agent per ticket, never a
crew on one ticket.

**Resource admission (R15) — the one sanctioned narrowing.** A ticket counts against the fixed concurrency
lane its `meta.device` names (`cpu` or `gpu`, the closed set af-intake-plan stamps at planning time; an
absent value defaults to `cpu`) — never a formula derived from the host's CPU core count. Each lane has a
FIXED cap, `max_cpu_parallel` (default **8**) and `max_gpu_parallel` (default **1**), each overridable per
project; `hooks/_ticket_state.py`'s `lane_cap`/`admit_frontier` are the source of truth (see
`tools/check_no_core_derived_cap.py`, which fails the build on any core-count-derived expression anywhere
under `agent_factory/`). A ticket still claimed under a LIVE lease from an earlier round — a campaign still
running — counts against its lane in THIS round's admission too; staying `incomplete` never frees the lane
on its own, only the lease going stale or the ticket finishing does (`live_claims`). Call `admit_frontier`
on the ready frontier before dispatching: it admits up to each lane's remaining headroom and DEFERS the
rest, logged by ticket id — never dropped, never marked blocked. A deferred ticket may sit deferred across
many rounds with no ill effect: admission is a per-round dispatch read, not a ticket-state write, so it
never reads as a dependency stall (that detector runs purely off `depends_on`).

After §0 stamps the run marker, compute the dependency-ready frontier (id-only, no bodies):
`incomplete = _praxis.incomplete_requirements(project, space=PLAN[0], snapshot=PLAN[1])` → filter to the
marked ids → `_ticket_state.ready_tickets(...)` → `_ticket_state.admit_frontier(ready, live=incomplete,
project=project)` to get this round's admitted set — pass the RAW incomplete list as `live`, not a
pre-filtered `live_claims(...)` call: `admit_frontier` filters it to live claims itself (see its
docstring), and it needs the raw set because a ticket still occupying its lane from an earlier round may
already be claimed and absent from `ready`. Then, unconditionally:
- **≥2 admitted → LAUNCH THE WORKFLOW (the script below). ALWAYS — this is the whole point of the
  command.** The lease + the `depends_on` DAG make parallel isolated workers safe, and it is dramatically
  faster than serial. If you choose NOT to fan out, you MUST name which of the two narrow exceptions below
  applies, in your reply — silence is not an option.
- **≤1 admitted** (a strictly-linear DAG, a single remaining ticket, or every other ready ticket deferred by
  admission), **OR the `Workflow` tool is genuinely absent from this session's tools** → and ONLY then → run
  the inline per-ticket loop (§1→§7) yourself. A fleet buys nothing on a one-wide frontier. These two are the
  ONLY sanctioned inline paths.
- **An EXPLICIT id list from an external driver** (`af-ticket-loop.sh` submits a round as
  `/af-build <project> ID,ID,...`) → fan out with **`Agent` subagents, ALL spawned in ONE message**, NOT the
  Workflow tool. This is a THIRD sanctioned path, and on a small box it is the only one that delivers the
  fan-out this section demands: `Workflow` derives its OWN internal concurrency from the machine's CPU
  count, so on a small box routing an N-ticket round through it silently serializes that round into
  sequential clumps while reporting success — the exact core-derived narrowing this contract forbids, and
  distinct from R15's fixed, project-overridable lane caps above. The driver has already computed the
  frontier and proven the ids are mutually independent, so the Workflow tool's scheduling buys nothing
  here — its only effect is its cap. `Agent` subagents carry no core-derived cap, which is why a batch of N
  genuinely runs N-wide (admission-capped, as above). Everything else is unchanged: one decision-making
  agent per ticket, each with `isolation: "worktree"`, each handed the §8 worker contract verbatim. Fanning
  out narrower than the admitted id list is a BUG, exactly as it is above.

**§1–§7 below ARE the per-ticket worker contract** — the exact loop each parallel Workflow worker runs (the
§8 block hands it to them verbatim, one worker per ready ticket). Read them as *what the workers do*, not as
*what you do sequentially*. You run §1–§7 inline ONLY under the narrow exception above. Either way YOU own
the run marker and the gate: `build_completeness_gate` armed on YOUR session in §0 and BLOCKS your turn from
ending until the whole marked set is `finished` — that is the hook that forcibly keeps the build rolling.

**What the workflow does** (deterministic scheduling — each ticket still has exactly ONE decision-making
worker; never a crew):
1. Compute the current dependency-ready frontier (a cheap read-only dispatcher agent runs the hooks).
2. Fan out **one worker per ready ticket**, each in its **own git worktree** (`isolation:'worktree'`) so
   parallel file edits never clobber, each spawned with the **§8 per-ticket worker contract VERBATIM** —
   that block is what makes each worker EVAL-FIRST (red→green) and lease-safe. Do not paraphrase it.
3. As workers finish, their tickets flip to `finished` in Praxis, which **unlocks dependents** — loop back
   to (1) and dispatch the newly-ready frontier (loop-until-dry). Repeat until the frontier is empty.
4. A round with **`ready:[]` while work remains** is a **dependency stall** (a cycle, or a chain rooted on a
   `blocked` ticket) — break it exactly as §1 says (unblock the root, fix a bad `depends_on`, or `block()`
   the unsatisfiable dependents). Do not spin.

**Worktree isolation is DISK-EXPENSIVE — share the dependency cache and guard the disk.** Each worktree is a
full checkout, and a worker that bootstraps its own environment (`pip install`, `npm install`) materializes a
FULL dependency tree per worktree. That multiplies: a real run put 29 worktrees on one box, each installing a
~5GB CUDA torch build that a CPU host never needed, and filled a 98GB volume to 100%. A full disk does not
fail loudly — it corrupts writes mid-build and strands the run. Three rules, in order of leverage:

- **Point every package manager at ONE shared cache, outside the worktrees**, and prefer the tools that
  materialize from that cache by HARDLINK rather than by copy — `uv` for Python and `pnpm` for Node. With
  hardlinks, the Nth environment costs ~0 additional bytes; with `pip`/`npm` it costs a full copy every time.
  Export these into the workers' environment (a stable path on the same filesystem as the worktrees, or the
  hardlinks silently degrade to copies):

  ```bash
  export UV_CACHE_DIR=/workspace/.uv-cache        # uv: hardlinks into each venv
  export PIP_CACHE_DIR=/workspace/.pip-cache      # pip fallback: saves re-download, NOT disk
  export npm_config_cache=/workspace/.npm-cache
  ```

- **Preflight the disk before every fan-out round, and STOP — do not degrade — when it is low.** Free space
  is a precondition for a correct build, so treat it like Praxis being unreachable: fail closed.
- **Reap each worktree once its ticket is integrated.** Worktrees are per-ticket scratch space, not run
  artifacts; the 29 that stranded the run outlived their agents by hours. Only their BRANCHES need to
  survive integration (`git worktree remove --force <path>` keeps the branch).

**You own the run marker and the gate.** You stamped it in §0, so `build_completeness_gate` arms on YOUR
session and blocks your turn from ending until the whole marked set is `finished` — even though the workers
build. **Await the workflow** (`run_in_background:false`); its completion is the whole job. `refresh_run`
the marker across a long run.

**Integrate, then review.** Worktree workers leave changes in per-ticket worktrees. After the workflow
returns, **integrate the finished tickets' worktrees onto the run's working tree** (merge each; resolve the
rare conflict when two same-round tickets touched one file — dependency-independent is not file-disjoint),
then run the **WORK-review panel (§7)** over the integrated diff and `clear_run`.

> **A finished ticket MUST arrive as commits on its own branch — never as a dirty worktree.** §8 step 7
> makes the worker commit before it releases, so integration is a pure `git merge`. If you nevertheless find
> a *finished* ticket whose worktree is dirty or whose branch holds no new commit, that is a **contract
> violation, not a packaging chore**: its work never passed the evals it claims to have passed, because the
> evals ran against a tree the worker then failed to preserve. **Fail closed** — `record_outcome(...,
> success=False)` / `release(TICKET, OWNER, state="incomplete", ref=PLAN)` to regress that ticket so it is
> rebuilt properly, and say so in the run report. **NEVER sweep stray worktree changes into a catch-all
> `wip:`/`salvage` commit on the run branch.** Such a commit launders unverified edits — possibly from a
> worker that died mid-BUILD, between CONFIRM RED and CONFIRM GREEN — into the branch under cover of a green
> run, which is exactly the silent-partial-failure class the gates exist to prevent.

**Canonical build-churn workflow — author it inline (substitute PROJECT / SCOPE / OWNER):**

```javascript
export const meta = {
  name: 'af-build-churn',
  description: 'Drive the scoped incomplete set to done: fan out the dependency-ready frontier as isolated per-ticket workers, loop until dry.',
  phases: [{ title: 'Build' }],
}
const project = args.project            // BARE name (no prd- prefix)
const scope = args.scope || 'ALL'
const owner = args.owner

const FRONTIER = { type: 'object', required: ['ready', 'remaining'], additionalProperties: false,
  properties: { ready: { type: 'array', items: { type: 'string' } }, remaining: { type: 'integer' } } }

// The §8 per-ticket worker contract, VERBATIM, with only PROJECT/TICKET/OWNER/CHECKS_SNAPSHOT substituted.
// Space is always the project; snapshot defaults to building-validation (or the run's --checks-space override).
const WORKER = (cid) => `<<< the full §8 block, TICKET=${cid}, PROJECT=${project}, OWNER=${owner}:${cid}, CHECKS_SNAPSHOT=building-validation >>>`

const DISK = { type: 'object', required: ['freeGb'], additionalProperties: false,
  properties: { freeGb: { type: 'number' } } }
const MIN_FREE_GB = 15   // a fan-out round of worktrees + their dep trees must fit, with headroom

let guard = 0
while (guard++ < 200) {                  // runaway backstop, far above any real frontier depth
  // DISK PREFLIGHT — worktree isolation is disk-expensive and a full volume corrupts builds
  // silently rather than failing. Fail CLOSED, exactly like an unreachable Praxis.
  const d = await agent(
    `Read-only. Report free space on the filesystem holding the repo: run \`df -BG --output=avail .\` ` +
    `(or \`df -g .\` on macOS) and return {freeGb:<integer gigabytes available>}.`,
    { phase: 'Build', label: 'disk-preflight', schema: DISK, effort: 'low' })
  if (d && d.freeGb < MIN_FREE_GB) {
    log(`STOPPING: only ${d.freeGb}GB free (<${MIN_FREE_GB}GB). Worktrees + per-worktree dependency ` +
        `trees will exhaust the disk and corrupt the run. Reclaim space, then resume.`)
    return { done: false, stalled: 'disk', freeGb: d.freeGb }
  }

  const f = await agent(
    `Read-only — issue NO claims/edits/writes. For PROJECT="${project}" (BARE), scope="${scope}": run ` +
    `_praxis.incomplete_requirements(project), filter to the scope's marked ids, then ` +
    `_ticket_state.ready_tickets(...) (every depends_on finished; exclude live leases). ` +
    `Return {ready:[cid,...], remaining:<in-scope incomplete not-yet-finished count>}.`,
    { phase: 'Build', label: 'frontier', schema: FRONTIER, effort: 'low' })
  if (!f || !(f.ready || []).length) break            // empty frontier -> done (or a stall to surface)
  await parallel(f.ready.map(cid => () =>
    agent(WORKER(cid), { phase: 'Build', label: `ticket:${cid}`, isolation: 'worktree' })))
  // finished tickets unlock dependents; the next iteration re-queries the frontier
}
return { done: true }
```

> **Reap the round's worktrees before looping.** After integrating a round, remove its worktrees
> (`git worktree remove --force <path>`, which KEEPS the branch) and `git worktree prune`. Left alone they
> accumulate across every round — one run stranded 29 of them, each holding a full dependency tree.

## Progress logging — a heartbeat is not progress

**A heartbeat proves the process is alive. It cannot tell you how far along it is, whether it will
finish this hour, or whether what it is producing is getting worse.** Those are the questions
actually asked while a build runs, and answering them by waiting for it to end is the same as not
answering them.

Measured on a real run: one step ran **28 minutes emitting nothing**. Its per-unit scores were
0.6183 / 0.6273 / 0.4123 / 0.0491 — degrading from the third unit onward — and it was
*simultaneously* being truncated by a wall-clock budget. Both facts existed inside the process the
whole time and were only discoverable after it exited, by which point a meaningless result had
been recorded as a verdict. Every check while it ran returned "394% CPU", which was true right up
to the end and told nobody anything.

Any step that can run longer than a few minutes MUST emit one line per unit of work:

```python
import sys; sys.path.insert(0, "<praxis>/agent_factory/scripts")
from progress import Progress

p = Progress("migrate call sites", total=len(sites))   # total is what makes an ETA possible
for site in sites:
    ...
    p.step()                                            # p.step(score=x) adds a degradation warning
p.done()
```

```
[progress] migrate call sites 34/120 28% elapsed 4m10s eta 10m32s
[progress][WARN] fit arm: last=0.0491 is 3.2 sigma below the mean of the previous 7 (0.5881)
```

Three properties are load-bearing:

- **`total` gives an ETA**, which is what turns "it is still running" into a decision. The unit
  count is almost always known up front — files to migrate, tickets in a set, folds × seeds.
- **`score` gives a degradation warning** *while there is still time to act*. It fires at 3 sigma
  over at least 4 prior samples, so it stays rare enough to be read.
- **Lines are flushed and prefixed `[progress]`**, so a supervisor can `grep` them out of an
  interleaved log. A buffered progress line is not a progress line.

**The consumer half is where this actually breaks.** A correct producer is useless behind a
reader that buffers, and the obvious reader is wrong in a way that looks right:

```python
for line in proc.stdout:          # WRONG -- hidden read-ahead buffer withholds lines
```

Iterating a pipe in text mode fills an internal buffer before yielding anything. Measured: an arm
emitted its first progress line at 1m31s and then nothing for 30 minutes, while the child sat at
373% CPU working normally. The producer was correct and `PYTHONUNBUFFERED` was set — the
supervisor's own reader was holding the lines. Use the helper:

```python
from progress import stream_progress
out = stream_progress(proc)       # echoes [progress] live, returns full stdout
```

It also drains stdout continuously, which matters independently: a child that fills a pipe nobody
is reading BLOCKS, so collecting output only after `wait()` deadlocks anything chatty enough to
fill 64KB.

Non-Python steps follow the same convention by hand: one flushed line per unit, prefixed
`[progress]`, carrying `n/total`, `elapsed`, and a metric where one exists.

## 1. FIND — pop the ONE next dependency-ready ticket

**Work exactly one ticket at a time, end-to-end.** FIND pops a SINGLE ticket; you then carry it all the way
to `finished` (§2→§6) before you look at, read, or claim any other. Do not survey the queue, pre-read other
tickets' requirements, or hold a batch in mind — one ticket is the entire working set until it ships.

Call `_praxis.incomplete_requirements(project, space=PLAN[0], snapshot=PLAN[1])` with the **BARE** project
name (PLAN binds it to the plan snapshot — §State tenancy). The server derives this view from outcomes +
staleness + lease state, so a validation that just regressed a ticket already shows up here — no local sync,
no manifest. To skip tickets another live session already holds, pass `exclude_leased=True`.
Filter to the **marked scope** (the ids you stamped in §0), then **pop the single front** with
`_ticket_state.next_ready_ticket(incomplete)` — the one ticket that is not finished, not blocked, and
depends on **no unfinished or in-progress job**. Claim that one; ignore the rest.

- **Readiness is computed over the WHOLE incomplete set**, not just your scope, so a cross-scope
  prerequisite still gates correctly. (`ready_tickets`/`pending_deps` exist for the gate's report and for
  choosing among equally-ready candidates — not for batching work.)
- **`next_ready_ticket` returns None but work remains** → a **dependency stall** (a cycle, or a chain rooted
  on a `blocked` ticket). Do not spin: fix/unblock the root prerequisite (af-intake-plan amend / accept), correct
  a wrong `depends_on` edge, or `block()` the unsatisfiable dependents. The gate detects + surfaces this too.
- **`next_ready_ticket` returns None and nothing is waiting** (only `finished` + `blocked` remain) → the
  scoped set is done; go to the WORK-review panel (§7).

**ML research ticket routing (R21) — checked BEFORE claim, right after FIND pops the ticket.** A ticket
whose `meta.experiment_id` names a registered `knowledge.ml_registry` MODEL (R5's cross-project link, the
same field name a model was registered with) is a RESEARCH ticket: it is dispatched to the af-ml-supervise
loop (`knowledge.ml_registry.supervisor.supervise_campaign`) and **never** to a generic build worker; a
ticket carrying no `experiment_id` is never routed to the supervisor. `hooks/_ticket_state.py`'s
`resolve_research_route(ticket, models)` is the source of truth — call it with the ticket just popped and
the candidate model facts (a live registry readback):

- **`route == "generic"`** (no `experiment_id`) — proceed to §2 exactly as below; nothing here applies.
- **`route == "supervisor"`** — dispatch a worker that runs `supervise_campaign` against `model_id`, never a
  generic per-ticket worker. `live_campaign` tells the dispatcher whether it is **ATTACHING** to a campaign
  already under way (resume — never register a second model for the same `experiment_id`, never start a
  second supervisor session) or starting a fresh one. Before claiming, also call
  `research_claim_guard(ticket, models, other_claims)` (`other_claims` = the raw candidate ticket set, same
  convention `admit_frontier`'s `live` uses) — a non-`None` return **refuses this claim**: a DIFFERENT
  ticket already holds a LIVE lease naming the SAME `experiment_id`, because `supervise_campaign` dispatches
  trials SERIALLY by construction and two concurrent supervisor sessions against one model are never safe.
  A refused ticket is left `incomplete` (not `blocked` — it is a timing collision, not a defect) and skipped
  this round; `next_ready_ticket` will offer it again once the live campaign's lease frees.
- **`route == "refused"`** — `experiment_id` names NO registered model: `block(cid, owner, route["reason"],
  ref=PLAN)` naming the missing model, rather than silently building it as an ordinary ticket.

The research-target check (`agent_factory/scripts/checks/af_ml_research_target.py`, R19) resolves onto
every `experiment_id`-carrying ticket **by query**, not by a Praxis-authored check fact: `contract_with_floor`
(§2 below) appends `research_target_requirement(cid, experiment_id)` — a synthetic `<cid>::research-target`
requirement, the same pattern as the `<cid>::acceptance` floor — whenever the ticket's own meta carries
`experiment_id`, so it always appears in that ticket's pinned check set with no authoring step required.

## 2. CLAIM + RESOLVE REQUIREMENTS — one transaction per ticket

For the next claimable ticket call `_ticket_state.start_ticket(cid, owner, project)` (BARE project name;
pass an `override=(space, snapshot)` pair too when this run overrides the default — the project space's
`building-validation` snapshot — §Validation source).

**Pre-claim resumability guard (falsifiable "Praxis = sole state").** BEFORE it leases, `start_ticket`
resolves the requirement set and runs the pure structural resumability probe
(`agent_factory.resumability.resumability_report`) over the ticket's meta: is a cold worker able to
reconstruct what "done" means from Praxis state ALONE? A ticket is resumable iff it is
**coverable-from-state** (`non-empty acceptance` **OR** `non-empty resolved required_validations` — the
same rule `contract_with_floor` uses, so a check-covered but acceptance-less backend/terminal ticket is
NOT starved) **AND** carries a `verify` mode. If the probe FAILS, `start_ticket` does **not** claim: it
stamps `meta.under_specified = [missing fields]` (a planning defect surfaced to intake, never a silent
skip) and returns `None`. Fix the gap at intake (add an acceptance condition OR a declared check); the
next `start_ticket` then clears the marker and claims. A resumable ticket claims and proceeds unchanged.

On a resumable ticket, `start_ticket` does three things atomically:

1. **Claim the lease.** `incomplete → in_progress`, stamping `meta.claim_owner`, `meta.claim_at`,
   `meta.claim_heartbeat_at`, `meta.claim_lease_ttl` (default `DEFAULT_LEASE_TTL_S = 900`) via
   `POST /requirements/{cid}/claim`, whose grant is atomic. (NOT `patch_meta`: a blessed
   `prd-<project>` plan refuses candidate edits, so that route could not claim a ticket at all.)
   Returns `None` if a live lease already holds it (or the ticket is `blocked`) — skip it.
2. **Resolve the MANDATORY (precise) requirements — a fresh QUERY, never a list authored on the ticket.**
   The ticket carries identity only (its tags/surfaces); the requirement set is computed live from the
   `category="check"` facts in `(space=<project>, snapshot=building-validation)` — read there while ticket
   state stays on `prd-<project>`. `resolve_validation_requirements` returns the de-duplicated union of
   three **precise** lanes (the full matching model is in §Validation source):
   - **tag match** — a check pins iff its `meta.applies_to` (the applicability PREDICATE) intersects the
     ticket's `meta.tags` (the IDENTITY; the ticket's `meta.applies_to` is the lenient fallback). Both
     sides are normalized `strip`+`casefold` with `"*"` preserved, on BOTH the author side and the
     resolve side — so `Auth` vs `auth` never silently drops a check.
   - **`"*"` wildcard** — universal gates (typecheck/build/lint/test) authored `applies_to: ["*"]`, pulled
     as a SEPARATE lane because a per-tag query can never match a `["*"]` check: a ticket's concrete tags
     never include the literal `"*"`.
   - **surface match** — requirements bound via the `renders` edge to a surface the ticket renders, so a
     frontend/UI check lands ONLY on tickets that render a screen and never on a backend ticket.

   These are abstract *"what must be proven"* facts — declarative and read-only during a build — and they
   are **mandatory**: the coverage contract (§3) forces every one to be covered. (The fuzzy **semantic**
   lane is separate and ADVISORY — §3.)
3. **Pin the contract = resolved checks PLUS the acceptance-condition FLOOR.** `start_ticket` calls
   `contract_with_floor` then `pin_requirements`: it always prepends the ticket's **own binary acceptance
   condition** (`<cid>::acceptance`) as a requirement, so the contract is **never empty even when zero
   Praxis checks match**. This is what makes "the validation agent generated no evals" impossible to wedge
   on: there is always at least one thing to validate — the red→green acceptance test. It **TRUNCATES** any
   prior validations and writes `meta.required_validations` with an empty `meta.pinned_checks`; synthesis
   (§3) fills that in. A ticket with no checks AND no acceptance condition is an empty contract — but the
   pre-claim resumability guard (above) now catches that case FIRST, routing it to `under_specified`
   (returns `None`, never claimed) instead of letting it reach an empty pin. So `start_ticket` returns
   `None` for two reasons — a live lease already holds the ticket, **or** it was routed under-specified
   (check `meta.under_specified`); neither is a claim, so skip it and (for the latter) surface it to intake.

## 3. SYNTHESIZE the validations — convert requirements into a custom covering set

**First, pull the ADVISORY candidates (the semantic lane) as inspiration.** Before authoring, call
`retrieve_advisory_checks(cid, project, scope="validation")` (same `(space=<project>, snapshot=building-validation)` seam) — a hybrid
retrieval of `category="check"` facts semantically close to THIS ticket's text. They are **inspiration,
NOT the contract**: fold the genuinely-relevant ones into the validations you author, and **ignore the
rest** — an irrelevant retrieval is harmless precisely because it never gets pinned and never gates
completion. This is the "search the DB for candidate checks, then let the LLM curate" step; the hard
guarantee stays on the mandatory precise set (§2.2), the recall boost comes from here.

**Then consider the SEEDED generic candidates (the deterministic lane).** `agent_factory/seeded_checks.toml`
is a hand-curated library of generic reusable checks (correctness, security, error-paths, maintainability —
each a binary command or a graded rubric) offered to EVERY ticket via `seeded_candidates(ticket_tags)`. Unlike
the semantic lane these are surfaced deterministically (not embedding-dependent), but they are equally
**opt-in and non-gating**: fold in the ones genuinely relevant to this ticket as authored validations, ignore
the rest. A graded seeded candidate becomes a `kind:"graded"` validation carrying its rubric (see §5 VERIFY).
`python -m tools.resolve_preview <project>` lists the seeded candidates offered per ticket.

**Then GATHER + ASSEMBLE the graded candidate pool (the shared pool; the gating function).** Beyond the
semantic lane above, the `building-validation` pool holds `candidate:true` graded checks contributed by
BOTH `af-intake-plan` (whole-plan B1 findings) and your own ticket-local search — two writers, one pool.
The mandatory-vs-advisory decision is made HERE, by a function, not by either writer:

1. **ADD your discoveries to the pool (U4).** If your rules/memory search surfaces a ticket-specific
   quality concern worth grading, PERSIST it as a `candidate:true` graded check via
   **`af-ingest author-check`** (never a direct write — preserves the single-writer lock), scoped
   TIGHTLY to this ticket's tags/surface (never `["*"]`), `authored_by:"build"`, with a `severity` hint.
   Idempotent on `check_id`, so re-discovery updates in place. The literal command:
   `af-ingest author-check "<criterion>" --project <project> --applies-to <this ticket's tags> --rubric '<json>'`
   — or `python -m agent_factory.ingestion_api author-check …` where the console script is not on
   `PATH` (it is a `[project.scripts]` entry, so it exists only where the factory is installed).
2. **READ the pool for this ticket.** `pool_candidates(cid, project, scope="validation")` (hooks/) — the
   DETERMINISTIC set of every `candidate:true` check resolving onto this ticket (NON-gating; the full
   set, unlike the semantic `retrieve_advisory_checks` sample).
3. **ASSEMBLE the per-ticket rubric — the function that determines what gates (U5).**
   `from rubric_assembly import assemble` (src/), then
   `graded = assemble(pool_candidates(...), budget=<N>, covers=[<this ticket's requirement_id>])`. It
   promotes the highest-`severity` candidates (up to `budget`) to individual GATING graded validations
   and folds the rest into ONE min-of-candidates advisory aggregate. Deterministic — the gating set is
   stable across passes on the same pool (no thrash). Include `graded` in the list you `pin_validations`.

The promoted graded validations gate via `all_validations_passed` like any pinned validation; the
aggregate is soft-floored (advisory unless a folded concern is egregious). Neither `af-intake-plan` nor
you chose which candidates gate — `assemble` did, at build time, from the pooled severity hints. All
graded validations are judged in **§5 VERIFY** (`verify_graded_check`, fresh-context judge), never run.

This is the heart of the two-tier model. The retrieved requirements say *what* must be proven; **you author
the concrete validations that prove it for THIS ticket**, faithfully covering every **mandatory**
requirement (advisory candidates you chose to honor become validations too, but coverage is only enforced
on the mandatory set). For each
requirement decide the executable signal (a specific test command, a type-check, a build, a lint, an AST
parse, a script) and emit a validation entry `{validation_id, covers: [requirement_id, ...], run: "<cmd>"}`.
One validation may cover several requirements and several may cover one — what matters is that the **union of
`covers` equals the full requirement set**. Then `pin_validations(cid, [...])`.

**Author each `run` as the NARROWEST command that faithfully proves its requirement.** For a ticket-local
requirement that means the specific test file/pattern (`npx vitest run src/foo/bar.test.ts`,
`pytest tests/test_x.py::test_y`) — not the repo-wide suite. Reserve whole-repo commands for the wildcard
gates that genuinely mean "the whole repo", because those are the ones that cost minutes and therefore run
once, at the end (§5 *Test-run budget*). A narrow requirement pinned to a broad command silently converts
every correction cycle into a full-suite pass.

**The contract always includes the `<cid>::acceptance` floor, which is ALWAYS coverable** — it is the
ticket's own binary acceptance condition, so you author the red→green acceptance test for it (write the
failing test, watch it fail, make the change, watch it pass). That single validation alone lets the ticket
finish, so a ticket is never stuck "no evals were generated." Cover the acceptance floor first, then any
additional resolved checks.

`coverage_gap(cid)` must return `[]` before the ticket can finish: a requirement with no covering validation
is an **uncovered contract**, not a pass. If an *additional* requirement genuinely cannot be turned into any
runnable signal (it needs a credential/secret only the owner can supply, or it is unsatisfiable as written),
do **not** fake a covering validation — `block(cid, owner, reason)` the ticket so it is surfaced for owner
action. Never stub or fake a validation to escape coverage. (The acceptance floor itself is unsatisfiable
only if the acceptance condition is — that is a planning defect to `block()`, not to paper over.)

There is **no preflight manifest and no separate env-readiness step.** Environment readiness is just another
requirement you cover with a validation: a missing env var / unauthenticated CLI / unreachable service is a
**failing validation**, and the ticket can't finish until it passes (or is `block`ed if only the owner can
fix it).

**Pin knowledge at kickoff.** Record the run's `as_of` timestamp so every retrieval this run sees one
stable plan even as write-backs land, and **mount read-only** the conventions pool + the project's
`prd-<project>` snapshot. The live graph is this run's scratch; the plan + conventions are mounted, not
copied in.

## 4. BUILD — one decision-making agent

**a. Assemble hermetic context (declare it; don't free-query mid-task).** Up front, pull exactly: the
ticket's requirement + its **binary acceptance condition**, the conventions/invariants it touches, and any
ticket-specific facts — via declared queries (scope + top_k + `as_of`). Budget it (hot constitution always
in; warm/cold to a ceiling well below the context-rot threshold). The agent works from this sealed bundle;
a new need is a new declared pull, logged — never unbounded mid-task querying. For a **screen-scoped
ticket**, pull the governing behavior with `praxis_requirements_for_surface(project, screen_id)` (the
active requirement facts bound to that wireframe screen via `renders`, per af-intake-plan) and take the layout
from the wireframe HTML in git.

**Read-only retrieval sub-agent (the ONE permitted delegation).** When the bundle needs reading many files
or large surfaces, dispatch a *disposable, single-shot* sub-agent to read and return a compact digest — so
the parent window never absorbs raw noise. Hard constraints, or it degrades into a crew:
- **Read-only tools only** (Read/Grep/Glob/LS). It never edits, runs state-changing commands, writes to
  Praxis, or commits.
- **One shot, no dialogue.** It returns once; you never converse with it or chain it into a decision.
- **Cheap model, fixed compact schema.** Output is a curator's digest (*file → role*, the specific
  facts/patterns asked for, constraints/gotchas, what's *still unknown*) — filter ruthlessly, it is a
  curator of insights, not a summarizer.
- You remain the **only** agent that decides, edits, writes to Praxis, or commits. This is context hygiene,
  not orchestration. **Read-fully guard:** any file the human or plan names *explicitly* is read fully in
  your own context first (no limit/offset); only exploratory/bulk reading is delegated.

**b. Re-anchor the goal.** Restate the ticket's acceptance condition at the start of each cycle (and after
any context compaction). Goal drift comes from semantic accumulation, not token count — re-injecting the
objective is the cheap, proven defense.

**c. Act.** The single agent does the work with real tools in the repo (edit, run, search). Make the change
that satisfies the acceptance condition — nothing broader (resist scope creep into adjacent tickets).
`heartbeat(cid, owner)` across long stretches so the lease stays live and isn't reclaimed out from under
you.

**d. Iterate on TARGETED tests — the whole-repo suite is NOT your feedback loop.** While building and while
correcting, run **only the specific test file(s)/pattern covering the slice you are changing** —
`npx vitest run src/foo/bar.test.ts`, `pytest tests/test_x.py::test_y`, `go test ./pkg/thing/...`. Derive
that target ONCE from the files your diff touches, then reuse it every cycle. The whole-repo gates (full
suite, build, repo-wide typecheck/lint) run **at most once per ticket, at the END** — see *Test-run budget*
in §5. Why the rule exists: on a repo whose suite is ~1835 tests and ~2 minutes per pass, a single ticket
invoked the FULL suite three separate times — from the repo root, then from a sub-package, then again with
`--reporter=json` — and ran past 20 minutes without finishing, while tickets on a comparable repo that
iterated on targeted tests finished in 10–14. Targeted tests return the same red→green signal in seconds.

## 5. VERIFY — intrinsic, always-on, external signals only

A ticket is **not done because the agent believes it is** — it is done when an external signal says so.
Intrinsic self-correction (the model reviewing its own work) *degrades* coding quality; only signals the
agent cannot fake count. The build **ALWAYS** runs this — it is not optional and not a separate skill.

**Run EVERY pinned validation — exit code is the verdict.** For every entry in `meta.pinned_checks` (your
synthesized validations), run its `run` command and take its **exit code** (0 = pass) / raw output as the
verdict — not the agent's reading of it. Record the result on the ticket:

```
record_validation_pass(cid, validation_id, passed=(exit_code == 0), ran_at=now)
```

This MERGES into the ticket's `pinned_checks` entry via the sanctioned build-state route
(`POST /requirements/{cid}/build-state`) — **never onto the requirement fact**, and never through
`patch_meta`, which a blessed plan refuses.

**SCOPE the run to what the ticket actually touched.** A monorepo's universal `npm --prefix backend test`
gate makes a frontend-only ticket run the whole backend suite to prove nothing about its own change. Before
running, split the pinned set against the ticket's diff:

```
from _ticket_state import scope_checks_to_changes   # hooks/
changed = git_diff_name_only(INTEGRATION_REF)       # the ticket's own changed paths
to_run, skipped = scope_checks_to_changes(pinned, changed)
```

Each check's module comes from its own command (`--prefix` / `cd` / `-C`), or from an authored
`meta.when_changed` glob list when the command is not self-describing. Record every entry in `skipped` as a
SKIP with its `meta.skipped_reason` — a skipped check is **never** recorded as a pass, and the completion
gate must see the distinction. The function fails SAFE in all three ambiguous cases (a check that names no
module, an unknown diff, or a change outside every known module root all run **everything**), so widening
the blast radius is always the default when the evidence is thin.

Resolution already folds byte-identical commands together (`collapse_duplicate_runs`), so a plan carrying
both a universal gate and an older lane-scoped check with the same `run` executes it **once**; the survivor
lists the ids it stands in for in `meta.collapsed_duplicates`. Neither mechanism ever drops a distinct
command — if two checks run different things, both still run.

**GRADED validations (`kind:"graded"`) — subjective judgment, still one boolean.** A validation the worker
synthesized from a seeded rubric candidate (see §3 / `agent_factory/seeded_checks.toml`) has no exit-code
command; its verdict is a min-of-axes rubric judgment. Run it through the graded harness instead of a shell
command:

```
from _graded_verify import verify_graded_check   # hooks/
r = verify_graded_check(cid, validation_id, code_diff, complete, ref=PLAN)  # complete = fresh-context judge
if r.should_block:
    block(cid, owner, r.block_reason, ref=PLAN)   # cap / non-convergence → HITL, never incomplete-forever
```

It grades the ticket's diff with a **fresh-context judge** (never the builder's context), records the same
`passed` boolean the gate reads, and **caches the verdict by code-state hash** so identical code is never
re-graded (this is what stops a nondeterministic judge from thrashing the forcibly-continue loop). A graded
check only *fails* on a below-threshold axis or a located, above-confidence-floor defect — vague
dissatisfaction with no located defect passes. The rubric is the copy **frozen** onto the pinned validation
at synthesis time, so editing the seeded library never moves the target mid-ticket. `verify_graded_check`
returns `should_block=True` once the graded iteration cap is hit or the defect set stops shrinking; route
that to `block()` (the existing HITL escalation tier), never an endless retry.

Alongside the pinned validations, run the project's real external gates so the acceptance condition is
actually observable (discover the commands; don't assume):

| Gate | Signal | When |
|---|---|---|
| **Pre-flight** | schema / type-check / lint / AST parse, scoped to the touched paths | before trusting an edit |
| **Targeted tests** | the task's acceptance test(s), run by file/pattern | the primary oracle, every cycle |
| **Build** | compile / bundle succeeds | for anything that must build |
| **Whole-repo gates** | the FULL suite + repo-wide typecheck/lint/build | **once**, at the end of the ticket |

- **The acceptance test must exist and must have failed before the change** (red→green). A test written to
  match the implementation proves nothing — if the acceptance condition has no test, write the failing test
  first, watch it fail, then verify the change makes it pass. **Confirm red with the TARGETED run of that
  test alone** — a whole-repo suite is not part of the red check (a wildcard suite/typecheck gate is not
  expected to be red before your change, so running it there buys nothing and costs a full pass).
- **Nothing about *what* must be proven lives in this skill or any file** — the validation *requirements*
  are resolved by query. This skill says only *how* to synthesize covering validations, run them, and
  record each pass. **The build NEVER waits on af-intake-plan to author per-ticket eval requirements**, and
  af-intake-plan must NOT be asked to author them. The contract a ticket resolves is: its **own acceptance
  condition** (the always-present `<cid>::acceptance` floor — every ticket has one) ∪ any **STANDING
  general validation lenses** already in Praxis (wildcard `applies_to:"*"` / tag-matched conventions, e.g.
  a universal typecheck+build+lint gate). A ticket that resolves *only* the floor is **not** a defect and
  needs **no** amend — you still author a custom eval for its acceptance condition and proceed. (af-intake-plan
  *amend* exists to add a NEW general lens when one is discovered — a compounding improvement — never as a
  prerequisite for building an existing ticket.)

**Test-run budget — targeted every cycle, the whole-repo suite ONCE per ticket, at the END.** Verification of
a ticket has two phases and you never mix them:

| Phase | What you run | How often |
|---|---|---|
| **Iterate** (§4c–d, and every correction-loop pass) | ONLY the targeted evals — the specific test file(s)/pattern covering your slice, plus typecheck/lint scoped to the touched paths | every cycle, freely |
| **Final gate** (immediately before FINISH, §6) | the whole-repo pinned gates — full suite, repo build, repo-wide typecheck/lint | **ONCE**, after every targeted eval is already green |

So: sequence your pinned validations. Run the narrow ones first and drive them green with targeted commands;
run the wildcard whole-repo ones **last, together, in a single pass**, and only when you believe the ticket
is done. `record_validation_pass` for each as usual — the budget changes *when* a gate runs, never *whether*
it runs, and every pinned validation still has to pass before FINISH.

**Never use the whole-repo suite as your iteration signal, and never re-run it to "compare against a
baseline."** Why the rule exists: a ~1835-test suite costs ~2 minutes per pass, and one observed ticket paid
that three times over — root, sub-package, then `--reporter=json` — mostly to work out whether some failures
predated it. That ticket ran past 20 minutes without finishing. The provenance question it was answering is
one you are forbidden to ask (next paragraph), so the re-runs bought nothing at all. A red final gate is
re-run only **after you have actually changed something**, as part of the correction loop's bounded cycle —
that is a fix cycle, not a fresh budget.

**Whole-repo gates pin on EVERY ticket — leave the repo green with ONLY your slice.** The universal
`applies_to:["*"]` gates (`backend-build`, `backend-vitest`, typecheck, lint, the suite) resolve onto
**every** matching ticket through the wildcard lane, so each isolated per-ticket worker is responsible for
leaving the **whole repo** compiling and its tests green using ONLY its own slice. That per-ticket
greenness invariant is **not** relaxed by the test-run budget above: the budget moves the whole-repo pass to
the end of the ticket and caps it at one clean pass, it does **not** defer it past the ticket. Make your
slice **self-consistent** — stub or adjust the callers your change touches so the shared build/test stays
green even though a sibling ticket's half has not landed yet — or, if you genuinely cannot go green without a
sibling's change, `block(cid, owner, reason)` and surface it. **NEVER weaken, skip, or scope-down a
whole-repo gate to get your ticket green** — a red shared build is the gate doing its job, not an obstacle to
route around. (Deferring the whole-repo gate to **end-of-SCOPE** — one integration gate for the entire run
instead of one per ticket — is a genuinely different and weaker thing than the end-of-TICKET budget above,
and is NOT what that budget authorizes. FLAG its tradeoff if anyone asks for it: intermediate tickets can
merge non-green, so the repo is not guaranteed buildable between tickets. Present it as an option, never the
default.)

**FIX EVERYTHING THE GATES SURFACE — "my slice didn't cause it" is not a disposition.** When the end-of-ticket
whole-repo pass (or any gate, at any point) comes back red, you **fix every failure it reports**, whether or
not your change caused it. Do **not** spend a cycle establishing provenance: no re-running the suite on a
stashed or reverted tree, no diffing against a "known baseline" of pre-existing failures, no reasoning about
whether the breakage predates your claim. That triage is the single most expensive habit this section
deletes — each provenance re-run is another full-suite pass (~2 minutes on the repo above), and the answer
cannot change what you do next, because the disposition is identical either way: **fix it.** The ticket
finishes only with the whole repo green, so an inherited failure is yours now. Fixing one is in scope by
definition and is not scope creep (§4c's "nothing broader" governs *features*, not *repo greenness*).

**Skipping is NEVER the resolution.** Deleting a failing test, `.skip()`/`xfail`/`it.todo`-ing it, narrowing
its assertions, or excluding its path from the suite / lint / typecheck config is a **faked pass** — the same
prohibition as weakening a whole-repo gate (above) and faking a validation (§6), and it is worse than an
honest red because it hides the signal from every later ticket.

**A discovered failure you genuinely cannot close from here ESCALATES — bounded, never silent.** Some
inherited breakage is truly out of reach: it needs a credential or live infrastructure, an upstream/vendor
fix, or a change large enough to be its own ticket. Route it through the tiers already defined below, in
this order and no other:
1. **Fix it** — the default, and the answer for the large majority. Attempt it under the correction loop's
   max-attempts cap, driven by the captured failing signal.
2. **Replan the fix once** (Strategy tier) after the cap trips — a different approach to the same failure.
3. **Still red → `block(cid, owner, reason)`** naming the failing test(s), the captured signal, and why this
   ticket's context cannot close it. That surfaces it for owner action and **excludes it from churn**, so the
   run completes AROUND it (§6) rather than looping forever or shipping red under a green claim. When the
   remainder is real work rather than an owner-only blocker, **also emit it as an `incomplete` ticket** the
   way the WORK-review panel emits findings (§7), so it gets scheduled instead of forgotten.
There is no fourth option: silently passing over a red test and grinding on it past the cap are both
contract violations.

**Correction loop — fires ONLY on an external signal.** On a failing gate or pinned validation, re-enter BUILD
(§4c) with the **captured failing signal** as context. Never let "the model decided to revise" be a
transition. Four tiers with explicit trip conditions:
1. **Execute** — one attempt.
2. **Correction** — retry with the failing signal attached. Bounded (a max-attempts cap).
3. **Strategy** — after **N identical failures** (degeneration), stop retrying and replan the ticket.
4. **Human escalation** — after **M replans** without progress, or any low-confidence / irreversible step,
   escalate. Don't loop forever.

A **circuit breaker** trips on repeated identical output or identical errors — that's degeneration, not
progress; escalate rather than burn iterations.

**Failures you did not cause run on the SAME tiers and the SAME budget.** They are neither exempt from being
fixed nor entitled to a fresh attempt count — a ticket that burns its whole correction budget on inherited
breakage escalates exactly like one that burns it on its own, ending at `block()` with the failing signal
named. **Re-run only the failure you just worked on** while cycling; the whole-repo pass is re-taken once,
after the cycle believes it is green — never once per attempt.

**Structural-erosion check.** Passing tests are necessary, not sufficient: long iterative runs erode
structure (complexity, duplication, file-spread) even while green. Track a per-iteration complexity-delta
(cyclomatic / churn / new-symbol fan-out — wire an existing tool like `radon`/`ruff`/`git diff --stat`,
don't build one) and **halt/escalate** if the delta per unit of verified progress exceeds the task's
budget.

**Separate evaluator / non-coding fallback.** For anything needing judgement rather than a deterministic
signal (rare in coding, common for soft outputs), the evaluator is a **different model from the
generator** — used only for the residue with no deterministic oracle, and only as escalation triage
(proceed vs. park), never as the success verdict for coding. A task type with no deterministic oracle
(form-filling, video) verifies by **human confirmation**: in an unattended run a low-confidence non-coding
step **parks** a checkpoint for batch review; high-confidence steps proceed. For any acceptance criterion
tagged **manual** (af-plan), in an attended run pause and hand it off for human confirmation; in an
unattended run record it as a deferred owned decision and proceed.

**NEVER emit a question and wait for a reply.** An unattended session has no one to answer it: the
prompt sits on screen forever, the ticket never finishes, and the watchdog cannot even reap it because
the spinner keeps animating so the pane never looks frozen. Observed 2026-07-28 — an appeal_engine
session sat 24 minutes at 83% context on the literal text *"Do you confirm this satisfies the
acceptance condition?"*, blocking its whole project until it was killed by hand. This applies to EVERY
phrasing of it (asking for confirmation, approval, a preference, or "should I proceed?"), not just
`verify="manual"` requirements. If you genuinely need a human, you have exactly two legitimate moves:
record the deferred decision and PROCEED, or `block(cid, owner, reason)` and move to the next ticket.
Stopping to ask is never one of them. **A `verify="manual"` requirement's
pass counts only when it carries a human signal** — record it with `record_validation_pass(cid, vid,
passed=True, source="human", ref=PLAN)`, never the default `source="worker"`. `all_validations_passed`
refuses a manual requirement that was only worker-self-certified, so a worker-sourced self-pass leaves the
ticket unfinished by design until a human confirmation lands.

## 6. FINISH — doneness is THE EVAL, recorded as a hard enum (never a count)

The ticket is **finished IFF `all_validations_passed(cid)`** — there is a coverage contract (≥1 required
requirement), **`coverage_gap(cid)` is empty** (every requirement covered), there is ≥1 pinned validation,
and **every** pinned validation `passed == True` (coverage + the synthesized validations ARE the eval).
Then, and only then:

- `_ticket_state.release(cid, owner, state="finished")` — flips `build_state` to the hard enum `finished`,
  NULLs the lease keys, and clears the run marker on this ticket. The single authoritative "done" signal.
- `praxis_record_outcome(cid, success=True)` — recorded too, but it is a **trust/utility signal only** (it
  weights retrieval); it is **NEVER** the completion criterion. A bare success count must never be read as
  "done".

If any pinned validation failed/is unrun, or a requirement is uncovered, the ticket does **not** pass:
- `praxis_record_outcome(cid, success=False)` — **regresses** the ticket so it re-enters the FIND set (the
  fail → regress → re-pick loop; the compounding mechanism).
- `release(cid, owner, state="incomplete")` — yields the lease cleanly so the build loop re-picks it. The
  run marker is **kept**, so the whole-set gate keeps the ticket in scope and forces it to be re-done — a
  clean yield does **not** end the run.

**Yield cleanly** (handing back): `release(cid, owner, state="incomplete")` and say why. **A blocker only
the owner can pass** (a credential/secret, an unsatisfiable requirement): `block(cid, owner, reason)` — this
sets `build_state="blocked"`, surfaces it for owner action, and removes it from the churn set so the run can
complete around it rather than wedging forever. **Never fake a validation pass to escape the loop** —
completeness is outcome-grounded, so the only honest finish is to actually build and pass every covering
validation. Only an externally-confirmed pass is eligible to **write a learning back**: stamp `source` and
`category="learning"`; never write speculative facts and **never block the loop on a write** — queue it and
proceed.

**Infra-dependent verification → `block`, NEVER fake.** A requirement that can only truly verify against
**live infrastructure** — Cognito-token verification against a real pool, the e2e login, a backfill against a
real DB, a federated relink — whose check or acceptance CANNOT honestly go green locally must be
`block(cid, owner, reason)`, surfaced for owner action. It is **never** a stubbed, weakened, or faked-green
validation. Blocking one infra-gated ticket never wedges the run: the single `build_completeness` gate
**completes AROUND blocked tickets** (they are excluded from the churn set and surfaced, not counted as
finished), so the rest of the scope finishes while the infra-gated ticket waits for the owner.

## 7. LOOP, then convene the WORK-review panel

This is the **inline sequential loop** — the fallback when the ready frontier is linear/one-ticket or the
Workflow tool is unavailable (default is the ultracode Workflow fan-out — see *Execution model*). Only
**after the current ticket has shipped end-to-end** (`finished`) do you look at the next: re-query
`incomplete_requirements(project)` (filtered to the marked scope), `refresh_run` the marker, and `FIND` the
**one** next ready ticket (§1), repeating §1→§6 until `next_ready_ticket` returns None and nothing is
waiting (only `finished` + `blocked` left). One ticket fully done, then the next — never two in flight in
*this* agent's context. **By default, though, independent ready tickets ARE fanned out in parallel** — the
lease + DAG make that safe, and the *Execution model* section makes it the default, not a "MAY". **A
fanned-out worker is a GENERIC sub-agent that does NOT read this skill** — it follows only the prompt it is
handed, so the EVAL-FIRST / red→green ordering survives fan-out only if it travels IN that prompt. Therefore:
**spawn EVERY parallel worker (whether via the workflow script or by hand) with the canonical
[per-ticket worker contract](#8-the-per-ticket-worker-contract-spawn-every-fanned-out-worker-with-this) (§8)
verbatim**, one ticket per worker, each in its own worktree. Do NOT paraphrase the loop into a bespoke worker
prompt — copy the contract block. (A worker that builds first and tests after is the exact drift this closes;
it has happened.)
*"Are we done?"* is **not** a counter you maintain: the one `build_completeness_gate` answers it live against
Praxis, blocking until the whole marked set is finished.
After the panel (below), `clear_run(cids, owner)` to end the run; any ticket left `blocked` is surfaced to
the human as needing owner action, never silently dropped.

When the scoped set is empty, convene the holistic **cold-eyes WORK-review panel** over the whole
artifact — the emergent, cross-cutting defects (a source/scope contract inconsistency, an unsatisfiable
target) that per-item checks structurally can't see. **A model judging its own output inflates its own pass
rate**, so the panel is **independent sub-agents** spawned via the Agent tool — never the agent that wrote
the code grading itself.

**compound-engineering is a HARD required dependency** and its ce-* reviewers ARE the default panel — not a
"use if installed" preference. **PRESENCE CHECK first:** verify the ce reviewer agents resolve via the
Agent tool / `/code-review`. If **absent** (compound-engineering not installed/enabled), **do NOT proceed
and do NOT record a panel-ran episode** — surface the remediation
(`claude plugin install compound-engineering@compound-engineering-plugin` / `/reload-plugins`); a missing
panel is a **blocked review**, never a silent pass.

**Surface:** the full diff for the build (`git diff` against the build's base) + the touched modules in
context. **Lenses (≥1 independent reviewer each):**

| Lens | ce subagent type |
|---|---|
| architecture / strategy | `ce-architecture-strategist` |
| correctness | `ce-correctness-reviewer` |
| security | `ce-security-reviewer` |
| maintainability | `ce-maintainability-reviewer` |
| performance | `ce-performance-oracle` |
| testing | `ce-testing-reviewer` |

Don't reinvent these — `/code-review` already merges/dedups their tiered output; either drive it or spawn
the subagents directly. **Dedupe** (merge multiple angles into one finding per distinct defect, carry the
strongest severity) BEFORE emitting. **Emit each finding as an `incomplete` Praxis ticket/check** bound to
the touched area: a defect demanding a fix → a **ticket** (the build loop re-opens via FIND and the
completeness gate stays blocked until it is `finished`); a recurring "this must be proven" rule → a
**check** (af-ingest author-check, which also regresses the matching finished tickets). That is the
entire enforcement mechanism — no second gate, no advisory-only suggestions. **Closing a finding** = its
ticket/check reaching `build_state="finished"`: **resolved** (built + checks pass) or **accepted** (a
conscious owned trade-off, recorded as a Praxis episode before the ticket is released `finished` — never
silently dropped).

**Panel-ran assertion — the only residue.** After the panel runs, record exactly **one**
`praxis_record_episode` (phase `work`, the project, the panel composition, the count of findings emitted) —
an assertion that reviewing happened, so it can never be silently skipped.

**SKIPPABLE — explicit policy, never silent.** Compute a size/risk signal: `small` = changed lines under
threshold (~400) **AND no high-risk area touched** (auth/authz, payments, secrets/config,
migrations/data-lifecycle, deploy/CI — any of these forces non-small). **small + attended** → propose skip;
human confirms → record a skip episode. **small + unattended** → auto-skip → record a skip episode
(`"auto-skip: small/low-risk, unattended"`). **NOT small** → review is mandatory (a human MAY force-skip
only with an explicit recorded reason). A skip is the *absence* of a panel-ran episode plus the *presence*
of a skip episode; never fabricate a panel-ran assertion and never edit config to get past the panel.

## 8. The per-ticket worker contract (spawn every fanned-out worker with THIS)

### First: read why this ticket came back (state this to every worker)

Before reading a file or writing a line, read your ticket's `meta.regression_detail` and
`meta.audit_disposition`. An incomplete ticket is either never-built or **regressed**, and a regressed
one carries the reports from whatever sent it back — `reason`, `evidence` (the failing test/gate and its
error text), `required_fix`.

`regression_detail` is an **accumulating LIST of finding dicts, oldest first**, not one dict.
Post-merge verification, conflict resolution and the ingestion API each append their own entry instead
of overwriting a sibling's (R16/E3), and answered findings remain in the list stamped `resolved: true`.
`regression_detail.reason` therefore reads as nothing and `regression_detail[0]` is the oldest entry —
usually one already answered. Either way you rebuild blind, which is exactly the failure this list
exists to prevent: a ticket was regressed with a precise report and closed again twice without its file
being touched. Read it with the helpers instead — under the loop's `PYTHONPATH`
(`<repo>/agent_factory/hooks:<repo>/agent_factory/src`), `import _ticket_state as ts` gives
`ts.open_findings(meta)` (every finding still owed an answer, oldest first) and
`ts.ticket_briefing(cid, meta)` (all of them rendered as text). Both lift a legacy single dict into a
one-entry list, so an older ticket still reads correctly.

**Every open finding is binding — answer all of them**, not just the first. And you do not close one
by saying you did: `resolved` is stamped only when a later verification round confirms the ticket
survived integration, or a human dismisses it. That is the self-certification this guard exists to stop.

If any open finding's `source` is `post-merge-verification`, that attempt's worktree build was **green**
and it failed only after merging: repeating that approach reproduces the failure exactly, because the
defect is integration-level (a registry/manifest/seed the merged tree needs, or a collision with work
that landed afterwards), not inside the ticket's own diff. Treat the reports as part of your acceptance
condition and build against the CURRENT integrated tree. An empty list (or no such field) means this is
a first build and there is nothing to read.

### You are one of N — share the box (state this to every worker)

A fanned-out worker is **not alone on the machine**. Its own thinking is API-bound and costs almost nothing
locally, so a wide fan-out is cheap — right up until every worker starts a test runner at the same moment,
at which point N workers contend for the same few cores and the round collapses. Measured on a 4-core box: a
suite that ran in two minutes alone took **twenty** under sibling load, and one worker burned 26 minutes and
259k tokens without producing a commit. Three rules keep a wide round cheap:

- **Run test runners SINGLE-THREADED.** Modern runners default to one worker per core, so N sibling workers
  each spawning a full pool oversubscribes the box by N× — 8 workers on 4 cores becomes 32 processes fighting
  for 4. Pass the runner's concurrency flag explicitly (`vitest --maxWorkers=1`, `jest -w=1`,
  `pytest -p no:xdist`, `go test -p 1`). One thread per worker, N workers, N threads total. **Verify the flag
  against the installed version before relying on it** — a wrong flag is not ignored, it aborts the run
  (`vitest --poolOptions.threads.maxThreads=1` is valid config-file syntax but dies on the v4 CLI with
  "Unknown option", so a worker "running its tests single-threaded" would actually be running none).
- **Namespace any SHARED test infrastructure by checkout.** One database server usually serves every worker,
  and a harness that names its scratch state with a constant (`__test_template__`, a fixed schema, a fixed
  redis db index, a fixed port) turns that into shared mutable state across workers: one worker's setup
  drops or rebuilds what another is mid-way through using. It surfaces as flaky tests, never as an honest
  resource error. Derive such names from the checkout path (or a per-worker env var) so concurrent runs
  cannot see each other, and make sure teardown drops what setup created.
- **Run only the tests your change implicates.** Your gate is the tests covering what you edited plus their
  callers — not the whole repo. Most runners take a changed-files form (`vitest related <files>`,
  `jest --findRelatedTests <files>`); use it. The repo-wide sweep runs ONCE, later, on the merged tree.
- **Seed dependencies, do not reinstall them — and seed EVERY workspace.** A fresh worktree has no
  `node_modules`/`venv`, and N concurrent installs is both the disk and the CPU spike. Hardlink them from
  the integration checkout instead — `cp -al <checkout>/node_modules ./node_modules` is near-instant and
  shares blocks rather than duplicating GBs. Do it for **every** workspace that has one — repo root AND
  each package (`backend/`, `frontend/`, `cdk/`, …), enumerated rather than assumed. Seeding only the
  obvious two is a trap: the gap never surfaces as a missing directory, it surfaces much later as a check
  that is inexplicably slow or broken when it finally reaches that package (observed: a worker 35 minutes
  into a ticket, discovering `cdk/node_modules` was absent only when an infrastructure test tried to
  synthesize). One loop costs seconds:

  ```
  for d in $(cd <checkout> && ls -d node_modules */node_modules 2>/dev/null); do
    [ -e "./$d" ] || cp -al "<checkout>/$d" "./$d"
  done
  ```

  Reinstall only if the ticket actually changes a manifest.
- **Do NOT go hunting for the factory's own code.** Your launcher already exported `PYTHONPATH` (the hooks
  and `src` dirs) and named the interpreter to use: import `_ticket_state` / `_praxis` and call them.
  Searching for them burns minutes and can wedge the box — `find / -maxdepth 6 -iname "_ticket_state.py"`
  walks every mounted filesystem, including other projects' worktrees and their dependency trees, while N
  siblings do the same. If an import genuinely fails, report it as a blocker: a stale copy found by
  searching is worse than a clean failure.

None of this narrows WHICH failures gate the ticket — the same tests must pass. It changes only how many
cores each worker takes while proving it.

### FIRST: check whether the ticket is already satisfied (before reading anything)

A ticket can arrive already built — its work landed in an earlier run, a sibling ticket implemented it, or
the state was regressed for re-verification while the code stayed in the tree. Treating that case as a
fresh build is the single most expensive mistake a worker makes: observed 36 minutes and 164k tokens on a
ticket whose implementation was already on the integration branch, 17 of those minutes spent reading code
to decide how to write something that existed.

So the FIRST thing you do, before exploring the codebase, is ask whether the acceptance condition already
holds on the integration ref:

1. Look for the ticket's own acceptance artifacts by id — a test named for it, a script, the module its
   acceptance names. `git log --oneline --fixed-strings --grep="(TICKET)"` on the integration branch
   answers this in seconds.
2. If they exist, RUN them. Green means the ticket is satisfied.
3. **Then verify, do not rebuild.** Run its pinned validations against the existing tree and FINISH it.
   Record in the completion note that the work was pre-existing and what proved it.

This is not a licence to self-certify: the acceptance evidence still has to be produced and pass, exactly
as for built work. What changes is that you STOP once it does, instead of writing a second implementation
of something that already works. If the evidence does NOT pass, you are in the normal build path — proceed
red-to-green as usual.

A corollary for the eval-first ordering below: when a ticket is already satisfied, its acceptance test
cannot be made to fail, and "confirm red" is unachievable by construction. Do not thrash trying to force a
red — record that the condition already holds, with the passing evidence, and finish.

This is the **single canonical, verbatim** statement of the per-ticket loop — the same EVAL-FIRST ordering
§1–§6 walk, condensed into one self-contained prompt that **travels with a spawned worker**. The §1–§6 prose
is for *you* (the orchestrator, who read this skill); this block is for the **generic sub-agent** you fan a
ticket out to, which has not. When fanning out (§7), **spawn each worker with the block below copied
verbatim**, substituting only `PROJECT` / `TICKET` / `OWNER`. Do not paraphrase it.

The lifecycle calls below are **code-enforced** in `hooks/_ticket_state.py` (per
`docs/factory-state-contract.md`): `start_ticket` truncates prior evals + pins the resolved requirement
contract (incl. the acceptance-condition floor), and `release(state="finished")` is **refused** unless
`all_validations_passed`. The worker **calls** them — it never reinvents or works around them, and never
fakes a pass.

```text
You are an af-build per-ticket worker. Build EXACTLY ONE ticket, EVAL-FIRST (red→green). You own only
this ticket — never look at, claim, or build another. Inputs: PROJECT=<bare name>, TICKET=<cid>,
OWNER=<your session id>, CHECKS_SNAPSHOT=<building-validation | the run's --checks-space override
snapshot>. Checks resolve from (space=PROJECT, snapshot=CHECKS_SNAPSHOT). Run helpers from
hooks/_ticket_state.py (contract: docs/factory-state-contract.md). af-intake-plan is NOT in this path — it
does not author eval requirements at build time; never wait on it.

TICKET STATE lives ON THE PLAN SNAPSHOT, never working memory: let PLAN=(PROJECT, "prd-"+PROJECT) and
pass ref=PLAN to EVERY _ticket_state state call below (pin/coverage/heartbeat/record/all_/release/block).
start_ticket takes PROJECT and derives PLAN itself, so it needs no ref. Missing the ref on any one call
splits that write into working memory and the ticket never reads back as done — always pass ref=PLAN.

1. CLAIM + RESOLVE + TRUNCATE  — start_ticket(TICKET, OWNER, PROJECT, override=(PROJECT, CHECKS_SNAPSHOT)).
   This atomically claims the lease (on PLAN), resolves the eval REQUIREMENTS (tag ∪ surface from the project
   space's CHECKS_SNAPSHOT ∪ the ticket's own acceptance-condition floor), TRUNCATES any prior evals, and pins
   the fresh requirement contract. If it returns None → the ticket is taken/blocked,
   stop. If it returns an EMPTY list (no checks AND no acceptance condition) → block(TICKET, OWNER, reason, ref=PLAN)
   and stop; there is nothing to prove.
2. READ THE CODE  — read the ticket's acceptance condition and the specific files/surfaces it touches, so
   your evals fit THIS code case (real paths, real commands) — not generic placeholders. Do not edit yet.
3. AUTHOR + PIN EVALS  — first pull INSPIRATION: retrieve_advisory_checks(TICKET, PROJECT, scope="validation")
   — semantically-related candidate checks; fold in the relevant ones, ignore the rest (they never gate).
   Then write CUSTOM executable validations that COVER every MANDATORY resolved requirement (each declares
   covers:[req_id] and a `run` command whose exit code is the verdict). pin_validations(TICKET, [...], ref=PLAN).
   coverage_gap(TICKET, ref=PLAN) MUST be empty before you continue (coverage is enforced on the mandatory set only).
4. CONFIRM RED  — run your TARGETED evals NOW, BEFORE writing any implementation: the acceptance test and any
   other eval scoped to the files you are about to touch, invoked by file/pattern
   (`npx vitest run path/to/x.test.ts`, `pytest tests/test_x.py::test_y`). The acceptance test MUST FAIL (red)
   for the right reason. An eval that passes before you write code proves nothing — fix it until it genuinely
   fails. Do NOT run the whole-repo evals here (full suite / repo build / repo-wide typecheck+lint): they are
   not expected to be red, and each pass is expensive. Do NOT write implementation in this step.
5. BUILD  — only now make the change that satisfies the acceptance condition; nothing broader.
   heartbeat(TICKET, OWNER, ref=PLAN) across long stretches so the lease stays live. While iterating, re-run
   ONLY the targeted evals — never the whole-repo suite; that is the per-edit feedback loop.
6. CONFIRM GREEN + RECORD  — with every targeted eval green, run the whole-repo evals + the project's real
   external gates (typecheck / build / lint / full suite) ONE time, as the final gate.
   record_validation_pass(TICKET, vid, passed=(exit_code==0), ran_at=now, ref=PLAN) per eval.
   On a failure, RE-ENTER step 5 with the captured failing signal as context — never revise from self-doubt
   alone, never weaken an eval to pass — then re-take the whole-repo pass only once you believe it is green
   again. Budget: at most ONE whole-repo pass per fix cycle, never one per attempt and never as an iteration
   signal (a ~1835-test suite costs ~2 minutes a pass; one ticket that ran it three times blew past 20
   minutes without finishing).
6b. FIX EVERY FAILURE THE GATES REPORT — including ones your slice did not cause. Do NOT investigate whether
   a failure is pre-existing: do not re-run the suite on a stashed/reverted tree, do not compare against a
   "known baseline". The disposition is the same either way — fix it — so the investigation is pure waste.
   NEVER skip, delete, .skip()/xfail, loosen, or config-exclude a failing test to get green. If a failure is
   genuinely unfixable from this ticket's context (needs a credential / live infra / an upstream fix / a
   change big enough to be its own ticket), attempt it under the same bounded retry budget as any other
   failure, then block(TICKET, OWNER, reason, ref=PLAN) naming the failing test(s) and the captured signal —
   never silently pass over it, never loop on it forever.
7. COMMIT  — commit AS YOU GO on YOUR OWN worktree branch, not once at the end: `git add -A` then
   `git commit -m "<type>(<scope>): <what the ticket delivered> (<TICKET requirement_id>)"`. Then assert the
   tree is CLEAN — `git status --porcelain` must print NOTHING. You build in an ISOLATED worktree, and the
   orchestrator integrates finished tickets by MERGING your branch: work you leave uncommitted is invisible to
   that merge, so an uncommitted "finished" ticket either loses its work or gets swept into an unreviewed WIP
   commit that never passed your evals. If you have nothing to commit because the ticket needed no code change,
   say so explicitly in your final report — do not leave an ambiguous dirty tree.

   **The trailing `(TICKET-ID)` is load-bearing, not decoration.** Integration extracts the id with a
   regex anchored to the END of the commit subject. A conventional-commit SCOPE does not count, and the
   match is case- and punctuation-exact against the ticket's own `requirement_id`:

   | subject | id extracted |
   |---|---|
   | `feat(ocr): stand up the sidecar (COV-1B)` | `COV-1B` ✅ |
   | `feat(cov1b): stand up the sidecar` | none ❌ — scope position, wrong case, missing hyphen |
   | `feat: stand up the sidecar (cov-1b)` | none ❌ — case-exact |

   A branch whose commits yield no id is NOT merged: the orchestrator cannot establish provenance and
   leaves it stranded. Observed 2026-08-06 on appeal_engine COV-1B — 42 files and ~1800 insertions of
   finished, passing work sat unmerged behind `feat(cov1b):` subjects, and a relaunch would have rebuilt
   all of it from scratch because workers branch from `origin/main`.

   **Commit after every coherent step, not once at the end.** The orchestrator purges each round's
   worktree unconditionally — the tree is scratch, the branch is the artifact — so anything uncommitted
   when a round is killed, times out, or halts on a billing failure is DESTROYED, not paused. A run that
   held ~2.5h of work in one uncommitted tree lost all of it; the next run over the same ticket survived
   an identical halt with three commits banked. WIP commits are expected and cheap; a later step can amend
   or rebase them. Prefer many small commits over one clean one — a tidy history is worth nothing if the
   round dies before it exists.
8. FINISH  — when all_validations_passed(TICKET, ref=PLAN) is True AND step 7 left the tree clean:
   release(TICKET, OWNER, state="finished", ref=PLAN).
   The release is REFUSED while any eval is unrun or red — that refusal is the contract, not an error to route
   around. NEVER release "finished" with uncommitted changes in your worktree: finished MUST imply committed.
   To yield without finishing: release(TICKET, OWNER, state="incomplete", ref=PLAN). Credential-only /
   unsatisfiable: block(TICKET, OWNER, reason, ref=PLAN).

NEVER build-first / test-after. NEVER fake, delete, or weaken an eval to get green. NEVER finish without
all_validations_passed. NEVER release "finished" leaving uncommitted changes. NEVER ask af-intake-plan to
author the eval requirements. NEVER run the whole-repo suite as your iteration signal or to establish a
failure's provenance — targeted every cycle, whole-repo once at the end. NEVER leave a failing test unfixed
because your ticket did not cause it; fix it, or block() it with the signal named.
```

## Long-horizon control (so the run survives length)

- **Disposable agent:** keep durable state in Praxis (the ticket node) + the event log, not the context
  window. If compacted or re-spawned, reconstruct the working set from the pinned `as_of` view + the
  ticket's `meta.pinned_checks` + the log — losing the window should lose nothing.
- **Compact early, don't drop:** at **~50–60%** context fill, summarize old turns into a fixed compaction
  artifact: (1) end goal; (2) current approach; (3) steps completed; (4) **dead-ends tried and why they
  failed**; (5) key file locations + roles; (6) next step + its binary acceptance condition. Drop raw tool
  output, keep its conclusions.
- **Heartbeat across the gap:** before any long-running step, `heartbeat` the lease so the ticket doesn't
  go stale and get reclaimed mid-build.

## Decisions are episodes (the why, not just the what)

When the loop makes a non-obvious choice (picked library X; defaulted Y because the plan was silent),
record it with `praxis_record_episode` — `text` = decision + rationale, `alternatives` = options not taken.
Episodes are store-only and excluded from semantic recall by default, so the "why" compounds without
polluting task-grounding retrieval. Flip `outcome` later via `praxis_record_outcome` when the decision
proves out or fails.

## Deploy hard-gate

If the project declares a deploy/release step, it is a **hard gate, not advice**: deploy only after the
scoped build reaches completeness (every ticket `finished`) AND the WORK-review panel is satisfied (or
explicitly, recordedly skipped). A deploy whose preconditions are validation requirements covers them like
any other — external signal, recorded on the ticket, fail-closed.

## Never

- **Never** write or read any `.factory/*.json` manifest, build-status file, lock, or "awaiting subagents"
  flag — dynamic state lives ONLY on the Praxis ticket node; JSON is static config. Reaching for a JSON
  state file reintroduces the deleted bug.
- **Never** proceed when `_praxis` raises `PraxisUnreachable`, or cache/invent state to keep going. Fail
  closed: stop, surface the error.
- **Never** query the incomplete endpoint with the `prd-` prefix — pass the BARE project name, or it
  searches `prd-prd-<project>`, returns EMPTY, and fakes completeness.
- **Never** work more than one ticket at a time — pop ONE via `next_ready_ticket`, ship it end-to-end to
  `finished`, and only then look at the next. No batching, no surveying the queue, no pre-reading another
  ticket's requirements, no two tickets in context at once.
- **Never** claim a ticket that is not dependency-READY — its every `depends_on` prerequisite must be
  `finished`; a ticket waiting on an unfinished/in-progress job stays parked. If nothing is ready but work
  remains, that's a dependency stall — break it, don't spin.
- **Never** author or pre-bind a ticket's requirement list — which validation *requirements* apply is the
  fresh `resolve_validation_requirements` query at ticket start (truncate + re-derive); requirements are
  read-only during a build. You DO author the concrete *validations* that cover them — that is the point.
- **Never** pin a validation that does not faithfully cover a real requirement, and never finish with a
  non-empty `coverage_gap(cid)` — every retrieved requirement must be covered by a runnable validation.
- **Never** record a validation pass on the requirement fact — passes go on the TICKET NODE via
  `record_validation_pass`.
- **Never** skip verification — the build ALWAYS runs every pinned validation; verification is intrinsic.
- **Never** use the whole-repo suite as the iteration signal, and never re-run it to work out whether a
  failure is pre-existing — targeted tests every cycle, the whole-repo gates ONCE per ticket at the end, and
  re-taken only after an actual fix. Repeated multi-minute suite passes and baseline-comparison triage are
  the largest measured time sink in a ticket (§5 *Test-run budget*).
- **Never** leave a failure a gate surfaced unfixed because your slice did not cause it — fix everything the
  gates report, and if it is genuinely unfixable from this ticket, `block(cid, owner, reason)` it with the
  failing signal named (and emit a follow-up ticket when it is real work). Skipping, deleting, `.skip()`-ing,
  loosening, or config-excluding a failing test is a faked pass, never a resolution.
- **Never** mark a ticket finished without `all_validations_passed(cid)` true (coverage complete + every
  validation green on an external signal, or human confirmation for non-coding); never fake a pass to escape.
- **Never** let an uncoverable/credential-only requirement wedge the run — `block(cid, owner, reason)` it so
  it is surfaced for owner action; a blocked ticket is excluded from churn, never silently passed or dropped.
- **Never** fake, stub, or weaken a pass to escape an **infra-dependent** requirement (Cognito against a
  real pool, the e2e login, a real-DB backfill, a federated relink) that cannot honestly go green locally —
  `block(cid, owner, reason)` it instead; the `build_completeness` gate completes AROUND the blocked ticket,
  so blocking never wedges the run.
- **Never** operate in a hardcoded `"agent-factory"` org or select around an org mismatch — run in the
  project-derived org (`PRAXIS_ORG` / `identity.factory_org()`); the hook-client org and the MCP-tool org
  (`whoami`/`select_org`) MUST agree, and a divergence is a fail-loud STOP to align, not to re-select past.
- **Never** stamp/clear the run marker for a scope you were not asked to build — the marked set IS the
  enforced run; ending it early (`clear_run`) with a marked ticket unfinished is an explicit abort, reported.
- **Never** trigger a correction from self-doubt alone — corrections require a failing signal; never use
  the generator's own model as the success judge; never accept an acceptance test written green.
- **Never** loop past the iteration cap / circuit breaker — escalate.
- **Never** run a crew **on a single ticket** — exactly ONE decision-making agent builds a given ticket.
  (Fanning the dependency-ready frontier out as parallel one-ticket workers via the ultracode Workflow is
  NOT a crew — it is deterministic scheduling, one decider per ticket; see *Execution model*.) Within a
  single ticket's build the only delegation is the disposable read-only retrieval sub-agent (it reads and
  digests, never decides/edits/writes/commits).
- **Never** start a new plan or add requirements here — af-build only finishes existing tickets (planning
  is af-plan; intake is af-intake-plan).
- **Never** build a ticket outside the requested scope; the parked non-scoped incomplete tickets MUST
  appear in the report — scoping is explicit, never a silent under-build.
- **Never** make the WORK-review advisory-only or self-reviewed; never pass on a missing ce panel (record
  no panel-ran episode, surface remediation); never skip the panel silently — every skip records a reason
  as a Praxis episode.
