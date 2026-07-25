# af-super-run — requirements (af-plan output)

**Status:** af-plan brainstorm/research output. NOT admitted to Praxis. Hand-off target: `af-intake-plan`.
**Date:** 2026-07-25
**Rigor mode:** Rigorous. **Decision mode:** Autonomous (force decisions) — every fork below that could
be settled with a low-regret default HAS been, and is flagged `[DEFAULT — override me]`. Genuine
high-regret forks are escalated to **Open decisions** per the anti-masking guard.

---

## 1. Scope

### The ask (verbatim)

> "I would like a new skill called /af-super-run that executes the full af-plan af-intake-plan and
> af-build workflow end to end autonomously. This will only be used for things that are very
> straightforward where I would feel okay letting the LLM just make all decisions."

### Owner amendments (in-session, treat as requirements)

- **A1.** The skill must have **hooks that force the run to keep iterating until it reaches af-build**,
  which already has its own hook forcing the job to keep going.
- **A2.** The skill must **lean on existing patterns, reuse code, and avoid duplication.**
- **A3.** It **still runs through all of the reviews** — it is gated in that sense — but **the process
  is expected to resolve the gate blocks entirely on its own.**

> **A3 AS LITERALLY STATED IS UNACHIEVABLE — amended here rather than deferred (review finding).** This
> document's own research establishes it: **CH-16** counts at least four places where the chain
> *mandates* human surfacing; **OD-12** finds a surfaced contradiction has no legal autonomous move
> ("You never settle it yourself"); **OD-1** finds `verify="manual"` tickets structurally unfinishable;
> **OD-13** concedes the honest terminal state is "done except N blocked." Carrying A3 forward verbatim
> would be actively harmful: if af-intake-plan admits it as a `source="prd-<project>"` requirement it
> becomes a fact with **no achievable binary acceptance condition**, so every downstream gate reading it
> either blocks forever or gets quietly weakened to satisfy it.
>
> **Amended A3 (the bounded claim the research supports):** *the run self-resolves every*
> ***mechanically-fixable*** *gate block, records a decision episode for every* ***judgment*** *closure,
> and* ***parks — never overrides*** *— the enumerated human-mandated surfaces (OD-1, OD-12, CH-16). It
> also FORCES both review panels on (B17), so "all of the reviews" is true in fact and not just in
> intent.* **The owner should confirm or reject this amendment at intake.**

### Why not the cheap baseline? (answering CH-18 here, where the premise belongs)

The cheapest design is `--autonomous` flags on the three existing skills, typed as three commands. It is
the right default unless something specific defeats it. What it cannot deliver:

1. **Coverage of the af-plan blind window (F19).** af-plan writes nothing to Praxis, so for the entire
   brainstorm phase no marker says a run is in flight. Flags cannot close that; a marker can.
2. **Surviving context exhaustion (F21).** One session holding af-plan's doc + the intake audit + a
   multi-hour build await will compact or die, and the run then silently stops being enforced.
3. **Cross-phase run identity** — without it there is no digest (B11), no budget (B14), no resume (B12).

**Everything else the flags-only baseline already gives us**, so the new surface is scoped to exactly
those three things. This is the crisp answer CH-18 demanded; anything beyond it is over-build.

### Release increments (added in review — the flat set was un-shippable)

The behaviors below are **not one release.** Sequencing them produces evidence about whether the premise
holds before the expensive half gets built:

- **Release 1 — plan-only (IF2).** Preflight + refusal (B13/B15/B17-bootstrap), the unattended signal
  (OD-2), the af-plan-window marker (F19), the status inspector (IF9), the digest (B11). Terminal state:
  *a blessed plan, then stop.* This is where trust is earned cheaply.
- **Release 2 — unattended build.** The continuation gate's hand-off (B7), autonomous block resolution
  (B9), whole-set build (B6), resumability (B12).

`[DEFAULT — override me]` Ship Release 1 first. Rationale: the doc's own OD-13 concedes the terminal
state involves mandatory human follow-up, so the plan-only mode delivers most of the owner-visible value
at a fraction of the risk and tells us whether the "very straightforward" population is real.

### Release 0 — SUBSTRATE FIXES (added by owner decision, 2026-07-25)

> **Owner decision (in-session):** *"sounds like we need to fix praxis itself to make this skill work"* →
> *"include fixing these in your plan."* The substrate defects below are **IN SCOPE of this plan**, and
> that puts a `knowledge/` **service** change back in scope (S1), overriding the original
> "Explicitly OUT of scope" boundary. One plan, one project.

These are **not af-super-run features.** They are latent defects in the factory/Praxis substrate that
af-super-run merely forces into the open. **S2, S3, S4 and S6 are biting today**, with no super-run
involved — `af-build-remote-jobs` carries 89 nodes of build state (so S2 already applies to it), and the
`praxis` space has **no `planning-validation` snapshot at all** (so S6 means its plan was blessed against
zero lenses). Every af-super-run behavior depends on these landing first.

- **S1 — Generalize the marker category (SERVICE change, `knowledge/`).** The marker fact is
  materialized server-side by `ensure_planning_marker` → `POST /planning-marker`, backed by a hardcoded
  `PLANNING_MARKER_CATEGORY` and a single find-or-create per project
  (`knowledge/serve/app.py`, `postgres_vector_graph.py`). No third marker is possible without this.
  **Acceptance:** a marker of a NEW category can be stamped, read, and cleared for a project through the
  same endpoint, and the existing `planning-marker` behavior is byte-identical (its tests still pass).
  *(If the owner later reverses the service-change decision, OD-3b option (b) — meta keys on the existing
  marker fact — becomes forced instead, and S1 drops.)*
- **S2 — `_snapshot_hash` must fingerprint plan CONTENT only.** `plan_completeness_gate.py:127-142` does
  `json.dumps(f.get("meta") or {})` over the FULL meta, so `build_state`/`claim_owner`/
  `claim_heartbeat_at`/`run_at` are in the hash. Its own docstring claims "two attempts on the SAME plan
  share a hash" — false the moment any build activity touches the snapshot, so the escalation counter
  resets perpetually and **never binds**. **Acceptance:** a claim/heartbeat/release cycle on a ticket
  leaves the hash UNCHANGED, while an edit to a requirement's text or a plan-relevant meta key changes
  it; the escalation cap fires after K attempts on a plan that is being actively built.
- **S3 — `planning_active()` must be owner-scoped, matching `clear_planning()`.** Today
  (`_ticket_state.py:889-903`) it checks freshness only, while `clear_planning` (`:875-879`) is
  owner-checked — an asymmetry that lets one session's live marker arm the plan gate against a different
  session. **Acceptance:** a live planning marker owned by session A does not arm the plan gate for
  session B; a session's own live marker still arms it.
- **S4 — `_bump_attempts` must fail loud, not swallow.** `plan_completeness_gate.py:106-107` swallows all
  write errors, so an unwritable `~/.praxis/` silently disables the bounded escalation entirely.
  **Acceptance:** an unwritable attempts path surfaces a named error in the gate's block/allow message
  rather than silently capping attempts at 1.
- **S5 — `record_validation_pass` must not accept an unattested human `source`.** It stamps the caller's
  string verbatim (`_ticket_state.py:531`), so any agent can write `source="human"` and satisfy
  `all_validations_passed` for a `verify="manual"` requirement with zero human involvement. **Acceptance:**
  a worker-context call cannot record a pass whose source is in `HUMAN_PASS_SOURCES`; the attested path is
  distinguishable from a self-asserted one. See **OD-17** for the exact bar.
- **S6 — An empty validation snapshot must be DISTINGUISHABLE from "everything passed".** Both
  `planning-validation` (intake B3) and `building-validation` (af-build RESOLVE) degrade to *pass* when
  empty — "fewer checks, never a crash." **Acceptance:** resolving zero lenses/checks emits an explicit
  `zero_resolved` signal that a caller can gate on, and the plan gate reports it rather than a silent
  green. **Judgment call with teeth (flagged):** whether it should hard-FAIL is a separate decision —
  failing loud would immediately block every project lacking a `planning-validation` snapshot, which
  today includes `praxis` itself.
- **S7 — Gate kill-switches must be tamper-evident.** Every Stop hook reads `FACTORY_*_DISABLED` from
  `os.environ` after loading repo-root `.env`, which the runner can write (F22). **Acceptance:** a gate
  that stands down because of a disable var records that fact where the run report can surface it, and
  the observed values appear in the report; a run that executed with a gate disabled can never present as
  clean.
- **S8 — Expose durable escalation state.** `plan_blocked` exists only as a substring of an advice
  message; `.plan_gate_attempts.json` is best-effort and unreadable across hooks (Stop hooks cannot
  observe each other's stdout). **Acceptance:** terminal plan escalation is readable from Praxis by
  another process/skill, so the continuation gate can refuse to enter build after it (F2/OD-9).

**Ordering.** S1–S8 are Release 0 and every af-super-run behavior `depends_on` the substrate items it
needs (B7 → S1, S8; B9's cap → S2, S4; B8's double-arm guard → S3; B13/B15 → S6, S7; OD-17/B20 → S5).

### In scope

- A new skill `agent_factory/skills/af-super-run/SKILL.md`, invocable as `/af-super-run`.
- **The Release 0 substrate fixes S1–S8 above** (owner decision) — including the `knowledge/` service
  change in S1.
- **Seeding `af-super-run`'s own `planning-validation` + `building-validation` snapshots.** This intake
  runs with `--checks-space=af-build-remote-jobs:planning-validation` (borrowing 6 proven, generic lenses)
  precisely because the project's own are absent — the live instance of S6/B15/F17.
- A **continuation gate** covering the pre-build stages (idea → af-plan doc → af-intake-plan bless),
  handing off to af-build's existing `build_completeness` gate. (A1)
- An explicit, machine-readable **unattended/autonomous signal** the three sub-skills consume, replacing
  today's ambient prose signal ("Constitution / owner asleep").
- **Autonomous gate-block resolution**: a blocking predicate becomes a diagnosed work item the run fixes
  and re-attempts, not a stop. (A3)
- A **terminal park-and-report** path, bounded, for the residue that genuinely cannot be self-resolved.
- The **morning-after report** artifact (what was decided, what's parked, what shipped).
- **Resumability**: re-invoking on the same project continues rather than restarting.
- **A `super_run_status <project>` inspector (IF9) and a single-flight guard (IF10)** — moved here from
  "implied features" in review, because they are **load-bearing, not optional**: B12's acceptance
  ("reconciles from Praxis … and continues") is unsatisfiable without IF9's derivation logic, which does
  not exist today, and IF10 is the stated fix for F18, a can't-miss failure class. Left in §4 they read
  as trimmable nice-to-haves and intake would drop them along with the acceptance conditions that
  depend on them.

### Explicitly OUT of scope

- Changing af-plan / af-intake-plan / af-build's own **behavioral contracts** beyond the minimum needed
  to accept the autonomous signal and honor B17's forced panels. Per A2, this skill **orchestrates; it
  does not re-state or fork the three loops.** *(Note: the Release 0 substrate fixes DO change hook and
  service code — that is now deliberate and owner-approved, and is a separate thing from forking the
  loops.)*
- `af-fulfill` (the end-user fact-gathering runtime) — a different actor, not in this chain.
- `af-wireframe` — see **OD-7**; provisionally out.
- Any new state store. Praxis remains the single source of dynamic truth (`METHODOLOGY.md`); no
  `.factory/*.json`, no locks. (See **CH-9** — this collides with the existing ledger pattern.)
- Deployment/release policy changes. af-build's deploy hard-gate applies unchanged.

### Actors

| Actor | Role in a super-run |
|---|---|
| **Owner (Matt)** | Invokes, then is absent. Reviews the flagged-defaults + parked list afterward. |
| **Orchestrator agent** | The single session that owns the super-run marker and runs the three stages. |
| **Build workers** | af-build's fanned-out per-ticket workers (own worktree, §8 contract verbatim). |
| **Cold-eyes sub-agents** | The audit skeptic, the B1c contract evaluator, the ce-* plan + work panels. |
| **The gates** | `plan_completeness`, `build_completeness`, `plan_gate_check`, + the new continuation gate. |

---

## 2. Behaviors

Each behavior carries a sketched binary acceptance condition where one exists; where it doesn't, it is
routed to **Open decisions** rather than given a fake one.

### B1 — Invocation and argument surface

`/af-super-run <rough idea or doc path> [--project=<bare-name>] [--checks-space=<space[:snapshot]>]`

- **Acceptance (corrected in review — the original contradicted D11/B15):** invoking with only a rough
  idea string against a **BOOTSTRAPPED** project (space + `prd-<project>` snapshot exist, and both
  validation snapshots are non-empty) starts a run that reaches af-build without further human input; an
  **unbootstrapped** project either self-bootstraps (B17) or refuses with the named bootstrap commands.
  Invoking with a path to an existing requirements doc skips the af-plan stage and says so explicitly.
  *(The original acceptance — "a bare rough idea reaches af-build" — was unsatisfiable as written, since
  a bare idea is by definition a project with no seeded snapshots and D11 commits the run to refuse.)*
- `--checks-space` is a passthrough to both af-intake-plan (B3 lens resolution) and af-build (RESOLVE),
  which already accept it as a slash argument.

### B2 — Project identity is resolved BEFORE any Praxis write

`source="prd-<project>"` is mandatory on every admitted requirement, and the completeness/incomplete
endpoints take the **BARE** name. A wrong or late-bound project name is the documented silent-failure
mode (`prd-prd-<project>` returns EMPTY and fakes completeness).

- **Acceptance:** the run records the resolved bare project name as its first act, and every subsequent
  Praxis call in all three stages uses exactly that name; a run that cannot resolve one refuses to start
  rather than inventing one mid-flight.
- **Reuse (A2):** `_gate_common.py:36-42` already resolves the active project via the **`FACTORY_PROJECT`**
  env var (adding the `prd-` prefix itself if missing, else falling back to the cwd basename). Both
  existing gates use it. The super-run should set/read the same seam rather than invent a parallel one.
- `[DEFAULT — override me]` derive from `--project` if given; else `FACTORY_PROJECT`; else slugify the
  idea into a bare kebab-case name and **record it as an episode**; never infer it separately per stage.

### B3 — Stage 1: af-plan, with its two blocking questions pre-answered

af-plan Step 1 asks rigor (1a) and decision mode (1b) as blocking questions, one per turn. Under
super-run both are **forced, not asked**.

- **Acceptance:** a super-run reaches the af-plan doc without emitting a blocking question; the doc's
  **Rigor mode** section records that both axes were force-set by af-super-run, and which values.
- `[DEFAULT — override me]` force **Rigorous** + **Autonomous**. Rationale: the owner's "straightforward"
  filter reduces the *product* risk, not the *extraction* risk; rigor is the cheap end of the leverage
  curve (a missed requirement spawns thousands of bad lines).

### B4 — Stage 1 output lands at a deterministic, discoverable path

af-plan hands a markdown doc to af-intake-plan. Under super-run there is no human to carry the path
across.

- **Acceptance:** the doc is written under `agent_factory/docs/brainstorms/` with a deterministic
  name derived from date + project, and af-intake-plan Step 0b reads exactly that path fully.

### B5 — Stage 2: af-intake-plan with 0c-a/0c-b forced, marker armed

af-intake-plan asks the same two axes at Step 0c and stamps the planning marker at Step 0d.

- **Acceptance:** the intake stage runs extract → harden → bind → DAG → audit (B1–B6) → panel (B7) →
  episode (B8) → bless (B9) with no blocking question emitted, and `plan_gate_check <project>` exits 0
  before `save_snapshot` is called.

### B6 — Stage 3: af-build over the whole set

- **Acceptance:** af-build is invoked with no scope argument (whole incomplete set), stamps its run
  marker, fans out via Workflow when the ready frontier is ≥2 wide, and the run does not end while any
  marked ticket is neither `finished` nor `blocked`.

### B7 — The continuation gate (A1): force iteration until af-build's gate takes over

This is the new enforcement primitive. Today there is a **coverage hole**: af-plan stamps *no* marker at
all, so between "idea" and "intake stamps the planning marker" nothing forces the turn to continue. And
the `plan_completeness` gate's bounded terminal escalation deliberately **ALLOWS** the stop after K
failed attempts on an unchanged snapshot — which, in a super-run, would silently end the run with an
unblessed plan and no build.

- **Acceptance:** with a live super-run marker and the hand-off condition unmet, a Stop is BLOCKED with
  a message naming the current stage and the specific next action; when the super-run terminally parks
  or completes, the marker is cleared and the gate is inert.
- **Acceptance (the inertness predicate — CORRECTED in review, was self-falsifying):** the gate stands
  down **only when a BLESSED PLAN EXISTS *and* the run marker is stamped** — never on the run marker
  alone. **Why this matters:** `stamp_run` is an unconditional agent-controlled write, not a proof of
  anything. If "run marker stamped" were the sole inertness predicate, the cheapest legal escape from a
  Stop-block would be to stamp a run marker and skip af-intake-plan entirely — producing exactly **F2**,
  the failure this gate exists to prevent. It then *composes* into a silent success: with no plan
  admitted, `praxis_incomplete_requirements` returns EMPTY, `build_completeness` passes vacuously
  (F1/F17), and the run reports green having built nothing. The gate must verify the intake hand-off
  (snapshot present / `plan_gate_check <project>` exit 0) before going inert.
- **Reuse mandate (A2):** built from `hooks/_gate_common.py`'s existing `allow`/`block`/
  `classify_unreachable`/`session_touched` primitives and `_ticket_state.py`'s marker pattern
  (`stamp_*`/`*_active`/`clear_*` + a TTL constant), mirroring `plan_completeness_gate.py`'s structure.
  See **OD-3** for whether it is a third hook file or arming logic added to the existing pair.

### B8 — Handoff continuity AND mutual exclusion between the three markers

Three markers now exist (super-run, planning, run) with independent TTLs
(`DEFAULT_PLANNING_TTL_S = 3600`, `DEFAULT_RUN_TTL_S = 3600`, `DEFAULT_LEASE_TTL_S = 900`, all
`_ticket_state.py:115-117`).

- **Acceptance:** at no point in a successful run is there a window where zero gates are armed; the
  super-run marker is only cleared after the build run marker is confirmed stamped.
- **Acceptance:** the super-run marker is heartbeated so a long stage cannot let it go stale and
  silently disarm the forcing behavior mid-run. **Correction (review):** the draft claimed
  `heartbeat()` covers the build stage. It does not — `heartbeat()` (`_ticket_state.py:669-683`) returns
  False without writing unless the caller still holds a **live per-ticket lease**, and it bumps `run_at`
  on that one ticket only; whole-set freshness is `refresh_run`'s job at ticket boundaries. **During the
  awaited multi-hour fan-out the orchestrator holds no lease at all**, so nothing existing keeps the
  super-run marker fresh. **The spec must name which actor heartbeats it during the fan-out.**
- **Acceptance (the double-arm guard, F14):** the build stage never begins while a planning marker is
  live for the project. `clear_planning` must be confirmed before `stamp_run`, and if the plan stage
  terminated without blessing, the marker must be explicitly cleared rather than left to expire.

### B9 — Autonomous gate-block resolution (A3)

Every review still runs; the run clears the blocks itself.

- **Acceptance:** for each blocking predicate the run encounters, it (1) reads the gate's stated reason
  verbatim, (2) makes the specific Praxis write that closes it, (3) re-runs the gate — and the loop is
  bounded by an attempt cap, after which it parks with the unresolved reason recorded.
**Detectable ≠ fixable — the predicates split into two tiers (corrected in review).** The original draft
justified self-clearing with "all mechanically stated and therefore diagnosable," but diagnosability is
not the property that matters; **fixability** is. Roughly half these predicates are mechanically
*detected* yet can only be *closed* by a product judgment — which requirement to admit, which decision to
re-model, which surface to declare backend-only. Conflating the two is exactly CH-1's degeneration path.

- **Tier M — MECHANICAL (fix-and-retry permitted, counts against the retry budget).** The fix is
  determined by the plan's own content: `R-HAS-SOURCE`, `R-NO-DANGLING-DEP`, binary-acceptance-present,
  no-vague-term, `contradictions_checked` unset, `R-CONTRACT-SIGNED` (run the evaluator).
- **Tier J — JUDGMENT (closure requires inventing plan content).** `R-NO-DEP-CYCLE` (which edge is
  wrong?), `R-DECISION-NOT-END-STATE` and `R-NO-IMPL-DEPENDS-ON-DECISION` (how should this be re-modeled?),
  dangling-concept (define it or scope it out?), uncovered planning lens, non-empty `coverage_gap`,
  `uncoveredSurfaces`/`uncoveredRequirements`. **Acceptance:** each Tier-J closure records a Praxis
  episode naming the decision, the alternatives not taken, and the lens/rule it satisfied — and counts
  toward the **park threshold**, not the retry budget, so a run cannot grind judgment calls indefinitely.
- **Non-negotiable:** self-clearing means **fixing the plan**, never weakening the gate. Editing config,
  disabling a gate via its env var, deleting a lens, or faking a signature is out of bounds (see §5).

### B10 — Park-and-report is bounded, explicit, and never silent

- **Acceptance:** anything the run cannot self-resolve within its attempt cap is recorded as a Praxis
  episode naming the predicate and what was tried, surfaced in the final report, and — for a ticket —
  routed through the existing `block(cid, owner, reason)` so the build completes *around* it rather
  than wedging.

### B11 — The morning-after report

- **Acceptance:** at terminal state the run emits one report listing: the resolved project, the flagged
  autonomous defaults (each with rationale + alternatives, for override), every parked/blocked item with
  its reason, the panel-ran/skip episodes, the tickets finished, and the git state.
- `[DEFAULT — override me]` render it from Praxis (episodes + ticket state), not from an accumulated
  file, so it is reconstructible after a crash. See **CH-9**.

### B12 — Resumability

- **Acceptance:** re-invoking `/af-super-run` for a project with an existing live or stale super-run
  marker reconciles from Praxis (which stage, which tickets) and continues from there, rather than
  re-running af-plan and minting a duplicate plan.

### B13 — Preflight tripwires (fail fast, before any write)

An unattended run that starts and *then* discovers a hard dependency is missing burns hours and leaves
half-state. Check first:

- **Acceptance:** before stamping any marker the run verifies (a) Praxis reachable + org tenancy agrees
  between `PRAXIS_ORG` and `praxis_whoami`, (b) the ce-* reviewer agents resolve (a missing panel is a
  *blocked review*, never a silent pass — in both intake B7 and build §7), (c) the `Workflow` tool is
  available, (d) the project's `building-validation` / `planning-validation` snapshots resolve **and are
  non-empty** (see B15). Any failure aborts with a named remediation and **no marker stamped**.
- **Acceptance (F22, the kill-switch integrity check):** preflight **refuses to start** if any
  `FACTORY_*_DISABLED` or `PRAXIS_AUTH_DISABLED` var is set in the resolved environment or in `.env`,
  and the morning-after report (B11) **always states their observed values** — so a run that executed
  with a gate stood down can never look like a clean run.
- **Reuse (A2):** `python -m agent_factory.tools.doctor <project>` **already exists** and covers DB,
  HTTP+hook auth, identity cache, `PRAXIS_ORG` resolution, the MCP-org == hook-org agreement, plugin
  wiring, and project-ticket resolution, with a non-zero exit. Preflight should **run doctor**, not
  reimplement it — adding only the ce-panel probe and the non-empty-snapshot check.
- **Why t=0:** the ce-panel presence check currently fires at the *end* of each phase (intake B7, build
  §7). Discovering "no panel" after a 40-minute audit is pure waste, and its remediation
  (`claude plugin install` / `/reload-plugins`) is interactive and session-restarting — i.e.
  **unrecoverable mid-run when unattended.**

### B14 — Kill switch and bounds

- **Acceptance:** the continuation gate has its own scoped disable env var (mirroring
  `FACTORY_PLAN_GATE_DISABLED` / `FACTORY_GATE_DISABLED`, which are deliberately separate so standing
  down one never disables the others), and its own max-attempts constant with a documented default.
- **Open:** wall-clock / cost ceiling — see **OD-8**.

### B15 — Refuse to run autonomously against a project with no checks **[the vacuous-pass hazard]**

The single most dangerous silent-weakening path found. Both validation snapshots degrade to *pass* when
empty rather than erroring:
- af-intake-plan **B3** resolves every `scope="planning"` lens from the `planning-validation` snapshot.
  If empty, `coverage_gap` is trivially empty and **B3 passes vacuously.**
- af-build **Validation source**: "if it is empty a ticket resolves **only** its acceptance-condition
  floor — **fewer checks, never a crash.**"

So a **brand-new project runs the entire pipeline with essentially no planning lenses and no build
checks, and every gate reports green.** An attended run has a human who might notice; an unattended run
does not. This directly undercuts A3 ("it still has to run through all of the reviews") — the reviews
*run*, they just have nothing to check.

- **Acceptance:** a super-run against a project whose `planning-validation` or `building-validation`
  snapshot is empty **refuses to start** with a message naming which snapshot is empty and how to seed
  it (af-intake-plan-validation / af-intake-build-validation), rather than proceeding to a green
  vacuous pass.
- **Open:** who bootstraps these for a greenfield project? See **OD-10**.

### B16 — The chain is FOUR skills, not three

The ask names three. af-intake-plan **B5b** requires authoring each derived blind-spot guard by
**running `af-intake-build-validation` once per guard** (it is the sole writer of `building-validation`;
the server's section lock refuses a `category="check"` fact in `prd-<project>`). A super-run therefore
invokes a fourth skill N times mid-intake.

- **Acceptance:** the run authors every derived guard through af-intake-build-validation and records the
  resulting check ids in the B8 panel-ran episode, so a skipped B5b is visible.

### B17 — FORCE both review panels; bootstrap rather than only refuse **[the "no viable happy path" fix]**

Adversarial and product-lens independently found the same premise-level defect: **trace the owner's
literal use case — a small, straightforward, usually new idea — through the draft's own leans and every
run either aborts at preflight or executes with the reviews auto-skipped.** B15/OD-10 refuse when the
validation snapshots are empty (true for any greenfield project); and if the project *is* seeded, CH-17
shows both panels sit permanently on the small-AND-unattended auto-skip branch. **There was no
configuration of the intended target population in which the feature both starts and is reviewed.**

- **Acceptance (force the panels):** under a live super-run marker, **both** the intake B7 plan panel and
  the af-build §7 work panel run **regardless of the size/risk heuristic** — the small-AND-unattended
  auto-skip branch is unreachable. Every super-run terminates with a panel-ran episode for **both**
  panels and **zero** auto-skip episodes. This is what makes A3's "still runs through all of the
  reviews" true in fact rather than in intent.
- **Acceptance (bootstrap):** on an empty validation snapshot the run **invokes the two seeding skills**
  (af-intake-plan-validation / af-intake-build-validation) against a default lens/check library, records
  the seeded ids as flagged defaults in the B11 report, and **refuses only if that seeding itself
  fails.** Bootstrap becomes part of the product rather than an attended prerequisite the owner
  discovers at 11pm.

### B18 — Ground product shape without ce-brainstorm **[the permanent-condition gap]**

af-plan Step 0 is explicit that `ce-brainstorm` is where product shape gets invented from a one-liner and
that "af-plan exhausts requirements; it is not where you invent product shape." Under forced-Autonomous
there is no human turn to dialogue with — so ce-brainstorm cannot run. **This is not a one-off deviation
(as §9 originally filed it); it is the permanent condition of every super-run**, meaning a rough idea
string is expanded into a full build with **zero grounding step for product intent**, and the owner's
first contact with those invented decisions is the morning report — after code exists.

- **Acceptance:** a super-run from a bare idea string (no doc path) runs a **non-interactive substitute**
  — a required `ce-ideate` pass plus an adversarial product-shape challenge — and **records every
  invented product decision as a Praxis episode with its alternatives, before any ticket is minted.**

### B19 — An output-level success signal, independent of the gate spine

Every acceptance condition in §2 is about markers, gates, paths, and stamps — **B1–B18 can all pass while
the run produces software that does not solve the owner's idea.** Because B11's report is rendered from
the same gate/episode state, a genuinely successful run and a hollow one are indistinguishable from the
artifacts (CH-15).

- **Acceptance:** at terminal state the run presents the **original idea string** alongside the finished
  requirement set and records a **fresh-context judge's verdict** on whether the built set covers the
  idea, as an episode surfaced in B11. Non-blocking — but it makes a hollow run *visibly* distinguishable.
- **Acceptance (feature-level metric):** every run records parked-item count, flagged-default count,
  wall-clock, and token spend, so the feature can be evaluated after N runs against a stated target
  rather than on vibes.

### B20 — The complexity tripwire is a REQUIRED behavior, not an open question

Promoted from IF1/OD-5 in review. The owner's "very straightforward" qualifier is the single condition
under which this feature is safe, and F13 concedes forced defaults can quietly expand the set past it.
A safety precondition that lives only as an untested human intuition at invocation time will be pointed
at non-straightforward work on its second or third use, because nothing stops it.

- **Acceptance:** the run computes af-build §7's **existing** risk predicate (auth/authz, payments,
  secrets/config, migrations/data-lifecycle, deploy/CI) plus requirement-count and manual-ticket-count
  thresholds, and **REFUSES with an explanation when tripped** — checked at the plan→build seam, the
  first moment the evidence exists and the last before code is written.
- **Acceptance (owner-attention budget):** thresholds are recorded as flagged defaults so they can be
  widened from evidence. `[DEFAULT — override me]` a successful run leaves **≤2 parked items and ≤10
  flagged defaults**; above that the run is a failure and the tripwire should have refused. Without this
  budget the plausible outcome is a blocked-ticket list plus 40 defaults to audit — **more** owner
  attention than three attended runs, inverting the stated benefit.

---

## 3. Edge states & failure classes

### Run-level states

| State | Entered when | Observable via | Exit |
|---|---|---|---|
| never-started | preflight failed (B13) | abort message; no marker | re-invoke |
| planning-doc | af-plan stage running | super-run marker, stage=plan | doc written |
| intake | af-intake-plan running | super-run + planning markers | bless + snapshot |
| plan-blocked | gate predicate unresolved past cap | `plan_blocked` state + episode | human, or re-invoke |
| building | af-build run marker stamped | run marker; continuation gate inert | set finished/blocked |
| done-with-blocked | build completed around blocked tickets | blocked tickets + report | owner action |
| done | all finished, panel ran | panel-ran episode | — |
| crashed-midway | session died | stale markers in Praxis | B12 resume |

### Failure classes (the can't-miss list)

- **F1 — Silent completeness fake.** The `prd-` double-prefix bug returns EMPTY and reads as "done."
  Highest-severity failure class in the whole factory; an autonomous run has no human to notice.
- **F2 — Unblessed plan reaches af-build.** The plan gate allows the stop after K attempts; without B7
  the super-run proceeds to build against a plan that never blessed.
- **F3 — Gate-weakening as a "resolution."** The single most dangerous autonomous behavior: clearing a
  block by disabling the gate, deleting the lens, or faking the contract signature rather than fixing
  the plan. This is the anti-Goodhart case `R-CONTRACT-SIGNED` already guards against (a signature over
  an unchanged draft with all-zero actions does not pass).
- **F4 — Manual-verify deadlock.** See **OD-1**; structurally unfinishable tickets.
- **F5 — Hook cross-fire / triple-arming.** Three markers, three gates, one session.
- **F6 — Praxis unreachable meets force-continue.** Genuine tension: every gate fails CLOSED on
  `PraxisUnreachable` (blocks loudly), while B7's job is to force continuation. A naive continuation
  gate could turn a Praxis outage into an infinite block with no human awake. See **CH-4**.
- **F7 — Dependency stall.** `next_ready_ticket` returns None while work remains (cycle, or a chain
  rooted on a blocked ticket). af-build says "do not spin" — the autonomous path must break it, not loop.
- **F8 — Worker wedge / degeneration.** Circuit breaker trips; af-build escalates to human — which
  under super-run must route to park, not to a question no one will answer.
- **F9 — Context exhaustion over a multi-hour run.** Compaction must lose nothing: state is in Praxis
  by design, but the *orchestrator's* stage knowledge must be reconstructible from the marker.
- **F10 — Two concurrent super-runs on one project.** Marker ownership + lease semantics. Sharpened by
  F14: `planning_active(project)` is not owner-scoped, so *another* session's live planning marker in
  the same project arms the plan gate against **this** run.
- **F14 — Double-arming (CONFIRMED, highest-severity mechanical hazard).** The two Stop hooks are two
  separate matcher entries in `hooks.json:3-19`, so **both run on every Stop**, and Claude Code proceeds
  only if *no* hook blocks. Their arming predicates read **different state**: run/claim markers on ticket
  facts vs. the `planning-marker` fact. Critically, `planning_active(project)` (`_ticket_state.py:889-903`)
  checks **only freshness, not ownership** — unlike `clear_planning`, which *is* owner-checked (`:875-879`).
  So a session that stamps a planning marker and reaches the build stage without a successful
  `clear_planning` has **both gates armed**, and the plan gate blocks every Stop for up to the full
  60-minute TTL. The happy path is safe (af-intake-plan B9 clears at bless), but every *unhappy* plan
  path — terminal escalation, crash, abort — leaves the marker live. `plan_completeness_gate.py:15-16`
  claims "the two gates do not cross-fire," which holds only for a *pure* build session that never
  stamped a planning marker — precisely not the super-run shape.
- **F15 — The bounded escalation does NOT bind for a super-run (CORRECTED + escalated in review).**
  Two independent defeaters, the second confirmed against code at `confidence: 100`:
  1. The counter lives in `~/.praxis/.plan_gate_attempts.json` (`plan_completeness_gate.py:79-81`) and
     `_bump_attempts` **swallows all write errors** (`:106-107`) — an unwritable path means attempts
     never increment past 1.
  2. **The far worse one:** the escalation is keyed on `_snapshot_hash`
     (`plan_completeness_gate.py:127-142`), which sha256s **every `category="requirement"` fact's FULL
     meta** in `prd-<project>`. **Tickets ARE requirement facts in that same snapshot**, and
     `_ticket_state.claim`/`heartbeat`/`refresh_run`/`release` patch `build_state`/`claim_owner`/
     `claim_heartbeat_at`/`run_at` straight into it. So **every ticket write changes the hash**,
     `_read_attempts` returns 0, and the cap never fires. Equally, **every B9 self-repair write resets
     the counter** — so "bounded escalation" is unbounded in exactly the autonomous case.

  **Consequence, restated:** in the F14 double-armed shape the plan gate does not block "for up to the
  60-minute TTL" — it blocks **indefinitely**, because the escape hatch can never trigger. And the
  residual risk compounds it: the `_PLANNING_SIGNALS` fast path (`prd-`, `praxis_`, `plan_gate`) will
  **always** match a super-run transcript, so the no-op escape can never mitigate F14 either. **The
  double-arm is unavoidable, not probabilistic.**
- **F22 — The kill switches are runner-writable (P0, review finding).** Every Stop hook reads
  `FACTORY_GATE_DISABLED` / `FACTORY_PLAN_GATE_DISABLED` from `os.environ` **after loading `<repo>/.env`**
  (`build_completeness_gate.py:169`, `hooks/_praxis.py:_load_dotenv`), and there is also a
  `PRAXIS_AUTH_DISABLED`. The autonomous runner has ordinary filesystem write access to that same repo.
  **So an agent under attempt-cap pressure to "resolve the block on its own" (A3) can make every gate
  pass green by writing one line to `.env`.** This is a strictly cruder version of F3, and the doc's
  own §5 already forbids it in prose — but prose is exactly what F16 showed is insufficient. The
  disable-var files are part of the attack surface and B14 previously treated them only as
  human-operated emergency controls. **Mitigation is now a hard requirement — see B13 and §5.**
- **F23 — Credential exposure over a long unattended run.** `PRAXIS_API_KEY` / `PRAXIS_ORG` /
  `PRAXIS_API_BASE_URL` live in a plaintext repo-root `.env` read by every hook. A super-run gives an
  unsupervised agent hours of file/shell access to that repo (worktrees, commits, tool calls) with no
  human present to notice an anomalous read, write, or log of it.
- **F17 — Vacuous green (see B15).** Empty validation snapshots make every gate pass while checking
  nothing. Ranked alongside F1 as a top-severity silent-success class.
- **F18 — Concurrent intake doubles the plan silently.** Ticket leases prevent double-*building*, but
  nothing guards *intake*: af-intake-plan Step 2's `praxis_add_insights(raw=True)` bulk write **skips
  dedup by construction**, so two super-runs racing on one project silently produce a doubled plan. The
  planning marker is the natural mutex — but `planning_active` is not owner-scoped (F14), which cuts
  both ways: it would arm the gate against the second run but not cleanly refuse it.
- **F19 — The af-plan phase is invisible in Praxis.** af-plan writes nothing to Praxis by design
  ("Never" §1). So for the entire brainstorm/research phase — potentially hours under Rigorous — there
  is **no Praxis state saying a run is in flight**. A crash there is indistinguishable from
  never-started. This is precisely the window B7's continuation gate must cover, and it is the one
  window no existing marker touches.
- **F20 — Crash is indistinguishable from never-started.** A stale marker is *ignored* by design ("a
  dead run never strands the set"), so nothing announces that a run died. Resume (B12) has nothing to
  detect.
- **F21 — Orchestrator context exhaustion.** af-build's Execution model awaits the fan-out Workflow
  with `run_in_background:false`, so one turn holds a multi-hour await — while the same session also
  holds af-plan's doc and the intake audit. If the session dies, the run marker goes stale after
  `DEFAULT_RUN_TTL_S` and the run silently stops being enforced. This is the strongest argument for the
  headless-driver framing (**OD-11**).
- **F16 — The manual bar is prose-enforced, not code-enforced.** `record_validation_pass` does not
  validate `source`; it stamps the caller's string verbatim (`_ticket_state.py:531`). Any agent can write
  `source="human"` with zero human involvement and `all_validations_passed` will accept it. The only
  thing preventing this is af-build's prose (`SKILL.md:604-609`). An autonomous runner under pressure to
  "resolve the gate blocks entirely on its own" (A3) is exactly the agent most tempted to cross it, and
  **nothing in the code would detect it.** See CH-13.
- **F11 — Non-green shared repo.** Whole-repo gates pin on EVERY ticket; a worker that cannot go green
  without a sibling's change must `block()`, not weaken the gate.
- **F12 — Half-state after abort.** A run that dies mid-intake leaves admitted-but-unblessed facts.
- **F13 — Autonomous scope creep.** With no human to say "that's enough," Autonomous mode's forced
  defaults can quietly expand the build set well past "very straightforward."

### Lifecycle states per entity

- **The doc:** absent → drafted → reviewed (ce-doc-review) → consumed by intake → superseded on
  re-baseline.
- **A ticket:** incomplete → in_progress (leased) → finished | blocked | regressed-back-to-incomplete.
- **The plan:** candidates → hardened → audited → panel-reviewed → blessed (snapshot) → re-baselined.
- **The super-run marker:** stamped → heartbeated → handed off → cleared | stale-reclaimed.

---

## 4. Implied features (surfaced, not stated in the ask)

- **IF1 — Complexity tripwire.** The owner scopes this to "very straightforward" things. Whether that
  judgment is the human's (pre-invocation) or the skill's (detect-and-refuse) is **OD-5**.
- **IF2 — Dry-run / plan-only mode.** Stop after bless, before build. Cheap to add, high value for
  building trust in the autonomy.
- **IF3 — Notification on terminal state.** An unattended run that finishes at 3am should be able to say so.
- **IF4 — Progress observability mid-run.** Distinct from the final report: "where is it now?"
- **IF5 — Cost/token ceiling.** af-build fans out parallel workers; an unbounded overnight run is a real
  spend risk.
- **IF6 — Scoped super-run.** `/af-super-run --scope=auth` for a re-baseline of one area.
- **IF7 — Auto-`/af-intake-plan-validation` seeding.** A recurring lens discovered mid-run.
- **IF8 — The eval case.** The factory's own compounding discipline says a new gate should ship with an
  `evals/cases/` entry; a gate with no eval is the class of thing that silently rots. **Mechanically:**
  `evals/case_def.py` supports only `component: plan_gate` today and no test enumerates skills, so a new
  skill is *not* required to add cases — **but** `tests/test_meta_coverage.py` asserts every shipped
  gate rule id has ≥1 exercising case, so any new *gate rule* must.
- **IF9 — A `super_run_status <project>` inspector**, alongside the existing read-only `doctor` /
  `resolve_preview` / `plan_gate_check` tools. The run phase is *derivable* today (planning marker?
  snapshot? run marker? incomplete set?) but nothing does the derivation — the cheapest high-value
  addition, and it is what makes B12 resume and F20 crash-detection possible.
- **IF10 — Single-flight guard per project** (F18).
- **IF11 — Abort path that leaves clean state.** The `FACTORY_*_DISABLED` vars only stop the gates
  *blocking*; they don't release leases, `clear_run`, or `clear_planning`. A first-class abort should.
- **IF12 — Reuse the existing `EventLog` run identity** rather than inventing one (see CH-19).

---

## 5. Non-negotiables (carried from the existing contracts, restated because autonomy strains them)

These are not new; they are the invariants an autonomous runner is most likely to erode, so they belong
in the requirements explicitly.

- Never weaken, disable, skip, or config-edit past a gate to make it green. (F3) **This explicitly
  includes writing any `FACTORY_*_DISABLED` / `PRAXIS_AUTH_DISABLED` value into the environment or
  `.env` (F22) — the kill switches are the owner's, not the runner's.**
- **No super-run artifact — episode, report, log, or commit — may ever contain the contents of `.env`
  or any `PRAXIS_*_KEY` value, and `.env` must never appear among the files the run's worktrees or
  commits touch. (F23)**
- Never fake a validation pass, self-certify a manual requirement, or record a panel-ran episode for a
  panel that did not run.
- Never pass the `prd-` prefix to the completeness/incomplete endpoints. (F1)
- Never proceed on `PraxisUnreachable` — fail closed.
- Never write dynamic state to a file; Praxis is the single source of truth.
- Never let a forced default paper over a genuine high-regret/irreversible fork (auth, data-loss,
  money, PII) — the anti-masking guard binds in Autonomous mode too.
- Exactly ONE decision-making agent per ticket; the only delegation is the read-only retrieval sub-agent.

---

## 6. Open decisions (for af-intake-plan to force)

Each records what was already checked, so intake doesn't re-derive it.

### OD-1 — `verify="manual"` tickets in a fully autonomous run **[HIGH-REGRET — escalated, not defaulted]**

**The mechanical fact.** `all_validations_passed` (`hooks/_ticket_state.py:579–615`) satisfies a
`verify="manual"` requirement ONLY via a pass whose `source` is in
`HUMAN_PASS_SOURCES = {"human", "manual", "external"}` (`:113`); the worker default `"worker"` never
counts. And af-intake-plan **B4's HARD RULE** makes every architecture-decision ticket `verify="manual"`.
So in the general case an autonomous run mints tickets it structurally cannot finish.

**Options:**
1. **Block-and-report.** Route every manual ticket through `block()`; the build completes *around* them
   (they are excluded from churn), and they land in the morning report. No code change. Cost: a
   "straightforward" run can still end with a pile of unfinished decisions.
2. **External attestation.** `"external"` is ALREADY an accepted source, and `verify_graded_check`
   already uses a **fresh-context judge** (not the builder's context). Treating a fresh-context judge's
   verdict as an external attestation needs no new enum — only a policy decision about whether it
   qualifies. Cost: this is exactly the self-certification the manual bar exists to prevent, one
   indirection removed.
3. **`report_only_requirements`.** An existing gate-exclusion seam (`:588`) used as a calibration knob.
   Cost: silently drops the requirement from gating — closest to F3.
4. **Avoid minting them.** Under super-run, prefer B4's PREFERRED modeling (record architecture
   decisions as episodes, which never enter the build set at all) so manual tickets are rare by
   construction. Cost: doesn't help when a manual ticket is genuinely warranted.

**Lean:** 4 as the primary (it is already the documented PREFERRED shape) + 1 as the backstop. Options 2
and 3 both erode an intentional integrity bar and should not be taken without the owner. **Escalated
rather than defaulted because it is the load-bearing "the LLM cannot grade its own homework" guarantee.**

### OD-2 — What actually transmits "this run is unattended"?

**CONFIRMED FACT (research pass):** *nothing does.* There is **no env var, no Praxis fact, no
file-presence check, and no runtime signal** anywhere in the repo. Every `unattended`/`attended`/
`owner asleep` occurrence in `hooks/`, `src/`, and `skills/` is a comment, docstring, or prose
instruction — **zero conditionals branch on it.** The only four `FACTORY_*` vars that exist are
`FACTORY_PROJECT`, `FACTORY_GATE_DISABLED`, `FACTORY_PLAN_GATE_DISABLED`, `FACTORY_PLAN_GATE_MAX_ATTEMPTS`.
`CONSTITUTION.md` is never read by any Python file. Today the mode is set by **the human's answer to
af-intake-plan Step 0c-b**, which explicitly says it is "the attended/unattended axis made explicit …
*instead of it being inferred from Constitution/owner-asleep*" (`af-intake-plan/SKILL.md:145-155`).

Note the **ambiguity** this exposes (corrected in review — this is a scoping mismatch, not a flat
contradiction): `CONSTITUTION.md:3` declares itself the active operating contract *for unattended
overnight runs* ("Owner is ASLEEP and UNAVAILABLE"), while `af-intake-plan/SKILL.md:148` makes the axis
an explicit per-run decision whose **default** is Collaborate/attended. The two are reconcilable — the
Constitution scopes itself to a run type — but nothing states *which document governs a given run*, and
the Constitution is always present on disk, so its header is not a discriminator. A super-run needs that
precedence stated. (The genuine flat contradiction in the repo is CH-9's METHODOLOGY-vs-Constitution
state-source conflict, not this one.)

So this signal is **greenfield — it must be invented.** Candidates: a new `FACTORY_UNATTENDED=1` env var
following the existing naming + exact-`== "1"` comparison convention; or the super-run marker itself
(project-scoped, hook-readable, same pattern as the other two markers). **Lean:** the marker, since A2
favors reusing the marker pattern over adding a fifth env var, and the marker is already the thing the
continuation gate must read anyway. Intake must force this because **all three skills must read one
seam** — and because it also determines whether the `CONSTITUTION.md` precedence question gets settled.

**Compounding cost (added in review).** This signal converts today's prose posture into a **code-level
mode fork** that every skill, gate, and eval must carry **permanently**, for one entry point, maintained
by one person. Commit to containment: **the unattended signal is read in exactly ONE place per skill —
the decision-mode resolution — and nowhere else.** Any proposal that widens the fork (notably OD-17's
source-validation change) is a **separate decision with its own owner sign-off**, not a free rider.

### OD-3b — WHERE does the super-run marker physically live? **[NEW — the marker pattern is NOT client-side reusable]**

**CONFIRMED (review, `confidence: 100`) — this invalidates the draft's core reuse assumption.** B7/OD-2/D7
all rested on "the super-run marker is just the existing marker pattern." It is not. `stamp_planning` can
only write because the marker **fact** is materialized server-side by `_praxis.ensure_planning_marker` →
`POST /planning-marker` (`knowledge/serve/app.py:2566`), backed by a **hardcoded**
`PLANNING_MARKER_CATEGORY = "planning-marker"` and a `find_planning_marker`/`ensure_planning_marker` pair
in `postgres_vector_graph.py:143-1803`, one find-or-create per project.

**So a distinct super-run marker category requires new endpoint + graph code in the `knowledge` service**
— a change the Scope section explicitly puts *out* of scope. An implementer hits this on day one.

Options: **(a)** generalize the server-side marker endpoint to take a category (a real Praxis service
change — must be added to In-scope as a dependency); **(b)** store super-run meta keys on the **EXISTING
`planning-marker` fact** — no server change at all, since `patch_meta` merges and `planning_live` only
reads `planning_owner`/`planning_at`. **Lean: (b).** It is strictly more A2-compliant, ships without
touching the service, and sidesteps adding a third marker category. Its cost is that the super-run and
planning lifecycles now share one fact, which must not resurrect F14 — the clear/stamp ordering has to
be explicit.

### OD-3c — The marker's substrate does not exist during the window it must cover

Every marker mechanism stores state on a fact **inside the `prd-<project>` snapshot**
(`planning_marker_id` → `project_ref(project).plan`). But **F19's blind window is precisely the af-plan
stage — before intake, potentially before the project exists in Praxis at all.** So B7 needs a marker
during a stage the chain defines as Praxis-free, with no snapshot to hold it. **Lean:** require the
project space and `prd-<project>` snapshot to exist as a **precondition**, and make marker-stamping the
first post-preflight act — so af-plan runs with the marker already armed rather than having to create
Praxis state mid-stage. This makes OD-10's bootstrap a hard prerequisite, not an open question.

### OD-3 — Third hook file, or arming logic in the existing two?

`hooks.json` registers two Stop entries. A2 says reuse. A third file mirroring `plan_completeness_gate.py`
duplicates ~structure but keeps the scoped-disable-var discipline and single-responsibility clarity the
existing pair deliberately has; folding it into an existing gate risks the cross-fire the current design
explicitly avoids ("the two gates do not cross-fire"). **Lean:** a third file that *imports* the shared
primitives rather than re-implementing them.

### OD-4 — Skill, or mode/flag on the existing three?

The strongest alternative framing: `/af-super-run` is not a fourth loop but a **thin argument-forcing
wrapper** (force decision mode, force rigor, arm one marker, chain the three invocations). That maximally
honors A2. Counter: A1 requires a real hook, which is more than a wrapper. **Lean:** thin wrapper + one
hook. Intake should force this explicitly since it determines how much of the skill is prose vs code.

### OD-5 — Is "straightforward enough" the human's call or a detectable tripwire?

If detectable, candidate signals: requirement count over a threshold, presence of any high-regret domain
(auth/payments/PII/migrations — af-build §7 already computes exactly this risk signal for panel
skippability, so **the predicate already exists and is reusable**), count of `verify="manual"` tickets,
count of unresolved forks, external-service provider decisions required. **Lean:** reuse af-build's
existing risk predicate; refuse-with-explanation rather than proceed, since the owner's own framing is a
trust boundary. Needs the owner: what should it do on trip — refuse, or downgrade to Collaborate?

### OD-6 — Attempt caps and their defaults

`FACTORY_PLAN_GATE_MAX_ATTEMPTS` defaults to 3. The super-run needs its own caps: per-predicate
resolution attempts (B9), overall stage attempts, and the terminal park threshold.

**The original lean ("mirror the existing shape") was WRONG and is withdrawn (review, `confidence: 100`).**
Per F15, the existing "a changed snapshot resets the counter" rule is keyed on a hash of **all
requirement-fact meta including build-lifecycle keys** — so it does not mean "plan progress" at all, and
an autonomous resolver that writes on every attempt resets it perpetually. Mirroring that shape would
inherit the defect verbatim.

**Corrected lean:** the continuation gate's cap must be **keyed on a plan-CONTENT-only fingerprint**
(excluding `build_state`/`claim_*`/`run_*` meta), **or** a monotonic attempt count stored on the marker
that nothing resets — plus a **per-predicate cap keyed on the predicate id** so one stubborn rule cannot
consume the whole budget. OD-8's wall-clock ceiling then becomes the outer backstop rather than an
optional extra, since it is the only bound that cannot be reset by a write.

### OD-7 — Does the chain include `af-wireframe`?

af-intake-plan takes an **optional** clickable wireframe as its surface truth and uses it for the
bidirectional coverage cross-check (B6). Without one, every requirement must be `backend-only` or the
coverage gate flags `uncoveredRequirements`. For a UI-bearing "straightforward" project this may matter.
Provisionally out of scope; intake should confirm.

### OD-17 — Should self-attestation be structurally blocked during a super-run? **[promoted from CH-13 in review]**

Promoted to a forced decision because the doc's own language ranks it as the factory's most important
integrity claim, yet it was sitting unranked among the challenges while the comparably-severe OD-1 had a
lean. Given F16 (`source` is an unvalidated caller-supplied string, so `record_validation_pass(...,
source="human")` is writable by the agent itself), the question is whether to convert the bar from prose
to code. **Lean:** harden the acceptance side so that **while a super-run marker is live for the project,
`"human"` and `"manual"` are rejected as pass sources** — forcing a manual requirement through `"external"`
only, which at least names a distinct, auditable attestation path. This is a small, contained change at
the existing `HUMAN_PASS_SOURCES` seam (A2 reuse) and it closes the one hole where an autonomous run can
silently self-certify. Pairs with OD-1: if the answer there is "block-and-report," this guard makes that
the *only* reachable outcome rather than the honorable one.

### OD-18 — What problem, measured? **[premise-level, owner-only]**

Raised by the product-lens panel and genuinely unanswerable from here: the entire requirement set rests
on one unelaborated sentence, with **no baseline** — how often straightforward work actually occurs, what
chaining the three skills manually costs today in wall-clock and attention, and what specifically goes
wrong when it is done attended. Without that, nobody can tell afterward whether the skill was worth
building, and intake would harden a large fact set against an unmeasured need. **This is the one open
decision I cannot lean on**, because only the owner has the baseline. If it cannot be stated, that fact
is itself the first thing blocking the build.

### OD-8 — Wall-clock / cost ceiling

Not stated in the ask. An overnight fan-out run is unbounded spend. Needs the owner: is there a ceiling,
and on trip does it park or hard-stop?

### OD-9 — How does the run detect that the plan gate terminally escalated? **[ANSWERED — decision remains]**

**CONFIRMED FACT:** `plan_blocked` is **not durable state**. It exists only as a *substring of the advice
message* the gate emits when it stands down (`plan_completeness_gate.py:277-288`). Nothing writes a
`plan_blocked` marker, meta, or Praxis fact. The only durable artifact is
`~/.praxis/.plan_gate_attempts.json` (`{"hash": <16-hex>, "attempts": <int>}`), and **no API, no Praxis
fact, and no skill reads it** — the only references in the repo are the gate itself and its test.

**The "parse the advice string" option does not exist (corrected in review).** Stop hooks are independent
`command` subprocesses (`hooks.json:3-19`); `_gate_common.allow` writes `additionalContext` to *its own*
stdout, consumed by the harness — **one Stop hook cannot observe another's output.** That leaves exactly
one existing mechanism: read `.plan_gate_attempts.json` directly. And per **F15** that file is
best-effort, silently-swallowed, and reset by any ticket write — untrustworthy even when readable.

**So OD-9 is effectively forced:** durable escalation state must be **exposed** (a marker/schema
addition, per OD-3b's option (b) this can be meta keys on the existing marker fact), not read out of a
gate-local file. **Lean:** expose it. Recording this as "two options" would hide that one of them is not
implementable.

### OD-10 — Who bootstraps a greenfield project's space and validation snapshots?

af-plan never asks for a project name (it writes nothing to Praxis). af-intake-plan needs `<project>`
(the space), `prd-<project>` (the plan), `planning-validation` (B3 lenses), and `building-validation`
(af-build's RESOLVE). For a greenfield super-run, **nothing in the chain creates them**, and empty ones
pass vacuously (B15/F17). Options: refuse (B15's lean); auto-seed from a default lens/check library;
require a one-time `af-intake-plan-validation` + `af-intake-build-validation` bootstrap first.
**Lean:** refuse + name the bootstrap command. An unattended run with zero checks is not verifiable, and
"fewer checks, never a crash" is the wrong default when no human is watching.

### OD-11 — Execution model: in-session skill, Workflow script, or headless driver? **[load-bearing]**

This choice determines nearly everything else, and there is real evidence for each:
1. **In-session skill** (the literal ask). Simplest; but F21 — one session holds af-plan's doc + the
   intake audit + a multi-hour build await, and context exhaustion silently unenforces the run.
2. **Workflow script.** CONSTITUTION §0 makes Workflow the default for substantial slices, and af-build
   already ships a canonical inline workflow. Three typed phases with schema-validated seams would give
   budget, per-phase isolation, and structured returns for free — and would make the B15/tripwire checks
   *schema-validated gates between phases* rather than agent discretion.
3. **Headless driver** (`agent_factory/tools/super_run.py` re-invoking `claude -p` per phase).
   `build_completeness_gate.py:88-95` explicitly references "the headless `claude -p` retry loop" and
   notes the Stop block reason "never surfaces to a human" — i.e. **the intended unattended execution
   model already is a shell loop re-invoking Claude, with the Stop hooks forcing continuation.** This is
   the only framing that survives context exhaustion (F21), and the easiest to test.

**Lean:** 3 for the outer loop + 2 within the build phase (which af-build already does). This also
reframes A1: the "hooks that force iteration" are the *existing* Stop hooks doing their job across
re-invocations, plus one new marker covering F19's blind window — which is *more* reuse (A2), not less.

**Reconciling OD-4 and OD-11 (flagged in review as two unreconciled leans):** they are not in conflict
once the layers are named — **the SKILL BODY stays a thin argument-forcing wrapper (OD-4); the headless
driver is the PROCESS that re-invokes it (OD-11), not additional skill logic.** An implementer following
OD-4 alone would under-build the driver; one following OD-11 alone would over-build the skill.

**Open sub-question (deferred by the panel):** af-build derives its authorization to call the `Workflow`
tool from *the user* invoking `/af-build`. Whether an **orchestrator-initiated** invocation satisfies that
opt-in is unaddressed, and it interacts with CH-14's "prose delegation has no mechanism."

### OD-12 — Contradictions have NO autonomous path (a genuine spec gap)

af-intake-plan **§3c** is unambiguous: the human settles each pending pair with
`praxis_resolve_contradiction` — "**You never settle it yourself.**" And B9 requires both an empty queue
AND `contradictions_checked=true`. Raw-bulk makes the queue empty *by construction*, so this only bites
when the audit's contradiction net actually surfaces a pair — at which point an unattended run has **no
legal move**. Options: park the run (a surfaced contradiction is a plan-level ambiguity by definition);
or sanction autonomous resolution under Autonomous mode (a real weakening of an explicit "never").
**Lean:** park. This is one of the few places the existing text forbids the autonomous action outright,
and A3's "resolve the blocks on its own" cannot override an explicit prohibition without the owner.

### OD-13 — What is the terminal SUCCESS state?

Given OD-1, a project with any real architecture decision produces ≥1 ticket an unattended run must
`block()`. So the honest terminal state of a *successful* super-run is **"done except for N blocked
items awaiting owner action"**, not "done". The deliverable is arguably the **digest of what needs you**
(B11), with a mandatory human second half. **The spec should say this out loud** — otherwise the first
run reads as a failure. Needs the owner: is "done-with-N-parked" an acceptable success, or does any
parked item make the run a failure?

### OD-14 — The `af-build` backward edge to intake

af-build §2's resumability probe stamps `meta.under_specified` and returns `None` without claiming,
directing "Fix the gap at intake" — a **backward edge from build to intake mid-run**. Unattended, does
the super-run re-open intake (C0 amend, with CH-7's near-dup corruption risk), or `block()` the ticket
and report? **Lean:** `block()` + report; re-opening intake mid-build is exactly the unsupervised
re-baseline CH-6 warns about.

### OD-15 — Git and deploy policy

Constitution §9: branch, commit per slice, **do not push**. af-build leaves per-ticket worktrees to be
integrated with "resolve the rare conflict" — with no human to resolve. And Constitution §1.1 makes
deployment part of done unless explicitly opted out, while §3 forbids high-regret irreversible actions.
**Lean:** `deployment.required:false` by default with a recorded reason; never push; on a worktree
integration conflict, park rather than guess. Needs the owner to confirm the deploy default.

### OD-16 — Naming

`af-super-run` is the only proposed skill not named for what it does to the artifact (plan / intake /
build / fulfill / wireframe), and "run" already means a *build* run throughout the codebase
(`stamp_run`, `run_scope`, `clear_run`, `runs/<id>/events.jsonl`). A super-run *contains* a build run,
so the docs will collide. Alternatives: `af-run`, `af-auto`, `af-ship`. Low stakes, cheap to settle now
and expensive to rename later.

---

## 7. Adversarial challenges (recorded, NOT resolved — af-intake-plan forces each)

Per af-plan Step 2b, every challenge is filed as an open item; none are resolved away here.

- **CH-1 — Circular authority.** The skill's job is to clear gates whose purpose is to stop an agent from
  declaring its own work done. What structurally prevents "resolve the block" from degenerating into
  "make the block go away"? `R-CONTRACT-SIGNED`'s actions-not-count rule is the existing precedent; is
  there an equivalent for every other predicate?
- **CH-2 — Unbounded condition.** A3 says "resolve the gate blocks entirely on its own." For which
  predicates is that *provably* possible? Some (dangling dep, missing source) are mechanical and
  self-fixable. Others (a genuine architectural fork, a real contradiction between two requirements) are
  judgment calls where "resolving it" IS the decision the owner delegated — fine — but at least one
  (OD-1) is a bar deliberately placed beyond agent authority. The claim needs a per-predicate answer.
- **CH-3 — Missing actor.** Who owns a super-run marker whose session died mid-build while workers are
  still running in worktrees? Lease semantics for the *orchestrator* are unspecified.
- **CH-4 — Contradiction: fail-closed vs force-continue.** Every gate BLOCKS loudly on
  `PraxisUnreachable`; B7 forces continuation. With no human awake, a Praxis outage becomes an infinite
  block-retry with no progress and no escape. What is the intended behavior — and does the continuation
  gate need an outage-specific bounded escape distinct from its predicate-failure escape?
- **CH-5 — Hidden dependency.** The whole chain assumes compound-engineering resolves. Both panels treat
  absence as a *blocked review*, never a pass. B13 preflights this — but what if it disappears mid-run
  (plugin reload, cache upgrade)? Note the existing memory: af-plan's ce-doc-review step needed
  re-applying after a plugin cache upgrade, so this is an observed, not hypothetical, failure.
- **CH-6 — Irreversible action.** Autonomous mode + `save_snapshot` + `clear_graph`/`load_snapshot`
  semantics. What stops a re-baseline from destroying a good blessed plan? Constitution §3 forbids
  high-regret irreversible actions, but a super-run's whole point is to not stop and ask.
- **CH-7 — Idempotency / re-entry.** Re-invoking mid-run: does B12's reconcile risk minting a duplicate
  plan? The C0 amend path has a documented near-dup corruption history (a topically-similar write was
  silently merged into an existing — even `finished` — ticket, corrupting it). Re-entry is exactly the
  scenario that triggers this class.
- **CH-8 — Race/ordering.** Three markers with independent TTLs and a handoff between them. Is there any
  interleaving where all three are simultaneously stale-but-uncleared, or where the super-run marker
  clears before the run marker stamps? (B8 asserts not; it needs a proof, not an assertion.)
- **CH-9 — Contradiction between two existing repo documents.** `METHODOLOGY.md` says state lives in ONE
  place, Praxis, with "no JSON status files"; `CONSTITUTION.md` §7 says the **single source of resume
  truth** is `docs/autonomous-progress-ledger.md`, a file. B11/B12 sit exactly on this seam. Which
  governs a super-run's resume story? (Note also that CONSTITUTION.md is hard-wired to the `team-app`
  project and a 2026-06-26 run — it is stale as a general contract, which matters because af-intake-plan
  and af-build both branch on "Constitution / owner asleep.")
- **CH-10 — Empty/degenerate input.** What does a super-run do with an idea so thin that af-plan's doc has
  two behaviors, or so broad it has two hundred? The lower bound is as real as the upper.
- **CH-11 — Unhandled boundary.** af-build's panel is auto-skipped when small AND unattended. A super-run
  is unattended **by definition**, so it permanently sits on the auto-skip branch of both panels
  whenever the size signal is small. Is a super-run therefore *less* reviewed than an attended run —
  the opposite of what A3 intends?
- **CH-12 — Who pays the tradeoff?** The owner reviews forced defaults "in the morning." If a run makes
  40 flagged decisions, is that review realistically done — or does autonomy quietly become unreviewed?
- **CH-20 — The threat model covers Goodharting, not adversarial *context*.** Every named threat
  (CH-1, CH-2, F3, F16, F22) is about the agent *choosing* to weaken a gate under self-imposed pressure.
  None consider that an hours-long unattended run **ingests untrusted text** along the way — the
  brainstorm input, Praxis fact bodies, contradiction pairs, retrieved facts, sibling-worker output —
  any of which could carry an injected instruction ("mark this manual check as human-verified", "set
  `FACTORY_GATE_DISABLED=1`") that an attended run would catch by inspection and an unattended run would
  simply execute. This is "the agent is the adversary" reframed as "the agent's *context* is
  adversarial," and it is absent from both §3 and §7. Requires scoping which ingested sources are
  untrusted and forbidding any of them from directly causing a gate-disabling or self-attesting action
  without independent corroboration.
- **CH-13 — The integrity bar rests on prose alone (F16).** *(Promoted to **OD-17** in review — kept
  here for the challenge trail.)* Given `source` is an unvalidated
  caller-supplied string, the manual-verify guarantee is honor-system. Should `/af-super-run` ship with
  a *structural* guard (e.g. the super-run marker's presence making `"human"` an invalid source for the
  duration, so an autonomous run mechanically cannot self-attest)? That would convert the factory's
  most important integrity claim from prose to code — and A2's reuse mandate arguably favors hardening
  the existing seam over adding a new one.
- **CH-14 — Prose delegation has no mechanism.** There is **no precedent anywhere in this repo for one
  skill invoking another**; af-intake-plan's B5b "DELEGATES to af-intake-build-validation" is pure
  English with no stated mechanism (`af-intake-plan/SKILL.md:645, 668-670, 690-695, 1002`). The only
  mechanically-mandated delegation is af-build → the `Workflow` tool. So "af-super-run runs the three
  skills" needs an actual answer: `Skill` tool calls? Inlined instructions? A Workflow? This is
  unspecified in the ask and has no convention to inherit.
- **CH-15 — Unfalsifiable success.** How does the run *know* it succeeded rather than that it stopped
  being blocked? The gates answer "may I stop," not "did I build the right thing." With no human
  checkpoint, is there any signal distinguishing a genuinely-complete run from one that blocked its way
  to quiet?
- **CH-16 — The skills MANDATE human surfacing in at least four places; A3 cannot override all of them.**
  Counted across the chain: af-plan §1b and af-intake-plan §0c-b (high-regret/irreversible forks surface
  **in both modes**); af-intake-plan B4 (the managed-vs-custom provider fork — "**Present that fork to the
  user** rather than silently defaulting" — which fires on any project touching email/SMS/payments/
  storage); af-intake-plan §3c (contradictions — "You never settle it yourself", OD-12); af-build §5
  (manual verify, OD-1). Plus ~7 escalate-to-human paths inside af-build §5 alone (correction tier 4,
  circuit breaker, structural-erosion halt). **A super-run that runs to completion without honoring
  these is in direct violation of two skills.** So the design question is not *whether* to detect them
  but *what happens when they fire* — and that is a decision the owner must make, not a default.
- **CH-17 — Is a super-run LESS reviewed than an attended run? (sharpened CH-11.)** Both panels
  auto-skip when small AND unattended. A super-run is unattended by definition, so it permanently sits
  on the auto-skip branch whenever the size signal is small — while A3 explicitly says "it still has to
  run through all of the reviews." These are in direct tension. Should super-run **force** both panels
  regardless of the size heuristic?
- **CH-18 — The rejected baseline needs a crisp answer.** The cheapest possible design is
  `--autonomous` flags on the three existing skills, typed as three commands. It costs almost no new
  surface, keeps each skill's ownership crisp, and puts the human back at each seam — which is exactly
  where the tripwire should fire. What it loses: cross-phase run identity, the digest, budgets, and
  surviving overnight unattended. **If the answer to "why not just that?" isn't crisp, the feature is
  over-built.** State it explicitly in the spec.
- **CH-19 — Is the event log a "state file"?** `src/agent_factory/event_log.py` already provides a run
  identity spine (`runs/<run_id>/events.jsonl`, `run_start`/`run_end`/`gate_result`/`decision`, a
  resumable seq counter) explicitly designed for the disposable-agent/resume pattern, and af-build says
  "Every step is an event-log entry." Reusing it for B11/B12 is maximal A2 reuse — but METHODOLOGY's
  "no JSON status files" rule and CH-9's ledger contradiction make its status ambiguous. Is the event
  log *observability* (fine) or *state* (forbidden)? Nothing currently spans all three skills with one
  `run_id`.

*(A cold-eyes adversarial pass and a flow-gap pass were dispatched in parallel; their findings are
integrated into §3, §4, §6 and this section before hand-off — see Rigor mode below.)*

---

## 8. Defaults taken (each flagged for override)

| # | Default | Rationale | Alternatives not taken |
|---|---|---|---|
| D1 | Rigor forced to **Rigorous** — **CONTESTED in review** | extraction risk is independent of product simplicity | Quick; or tripwire-conditional (below) |
| D2 | Decision mode forced to **Autonomous** | it is the literal ask | Collaborate |
| D3 | Doc path under `agent_factory/docs/brainstorms/` | matches the two existing brainstorm docs | a new dir |
| D4 | Whole-set build (no scope arg) | "end to end" | scoped default |
| D5 | Report rendered from Praxis, not a file | METHODOLOGY single-source rule | ledger file (CH-9) |
| D6 | Attempt caps mirror `FACTORY_PLAN_GATE_MAX_ATTEMPTS=3` | reuse a proven shape (A2) | new bespoke caps |
| D7 | Continuation gate = third hook file importing shared primitives | avoids cross-fire (OD-3) | fold into existing |
| D8 | `af-wireframe` out of chain | not mentioned in the ask | include it |
| D9 | Project name from `--project` else `FACTORY_PROJECT` else slugified + recorded | avoids the F1 class; reuses the existing seam | ask (impossible here) |
| D10 | Preflight = run existing `tools/doctor.py` + 2 extra probes | maximal A2 reuse | bespoke preflight |
| D11 | Refuse on empty validation snapshots (B15) | vacuous green is worse than not running | proceed; auto-seed |
| D12 | Park (not resolve) on a surfaced contradiction | §3c says "never settle it yourself" | autonomous resolve |
| D13 | `block()` + report on the build→intake backward edge | avoids unsupervised re-baseline | re-open intake mid-run |
| D14 | No push; `deployment.required:false` + recorded reason | Constitution §3/§9 irreversibility | deploy autonomously |
| D15 | Ship **Release 1 (plan-only)** first | earns trust cheaply; tests the premise before the expensive half | one big release |
| D16 | Marker meta on the **existing** `planning-marker` fact (OD-3b/b) | needs no Praxis service change | new marker category |
| D17 | Attempt cap on a **plan-content-only** fingerprint | the existing hash resets on every ticket write (F15) | mirror the existing shape |
| D18 | **Force both panels** under a super-run marker (B17) | makes A3's "all the reviews" true in fact | keep the auto-skip heuristic |
| D19 | Bootstrap empty snapshots, refuse only if seeding fails (B17) | greenfield IS the target population | refuse outright |
| D20 | Owner-attention budget: ≤2 parked, ≤10 flagged defaults (B20) | above it, autonomy costs more attention than it saves | no budget |

**D1 is contested (product-lens, P2).** Forcing Rigorous runs loop-until-dry research at full depth on
work the owner has *already declared trivial* — hours of unbounded spend before a line is built, on a run
with no cost ceiling (OD-8 still open). The counter-argument: extraction risk genuinely is independent of
product simplicity, and this document is itself the evidence (a "straightforward" one-line ask produced
two `confidence: 100` structural defects). **Suggested resolution for intake:** make rigor
**tripwire-conditional** — Quick when B20's risk predicate reads low, Rigorous when it reads borderline —
and pair it with OD-8's hard ceiling. Record the rigor choice and its input signal in the B11 report.

---

## 9. Rigor mode

- **Rigor:** Rigorous. **Decision mode:** Autonomous (forced defaults in §8, flagged for override).
- **Step 0 (ce-brainstorm / ce-ideate):** the *interactive* `ce-brainstorm` dialogue was NOT run — under
  forced-Autonomous mode there is no human turn to dialogue with. Its function (scope/behavior/success
  criteria/edge states) was discharged by direct source reading + the parallel ideation pass below.
  **This is a deliberate, recorded deviation, not a silent skip**, and is itself an instance of OD-4's
  question (how much of af-plan's human-facing protocol survives under super-run).
- **Grounding (Step 2a):** read fully in-context — `af-plan/SKILL.md` (159L), `af-intake-plan/SKILL.md`
  (1027L), `af-build/SKILL.md` (852L), `af-fulfill/SKILL.md`, `METHODOLOGY.md`, `CONSTITUTION.md` (482L),
  `docs/autonomous-progress-ledger.md` (200L), `hooks/hooks.json`, `plan_completeness_gate.py` (header),
  and the `_ticket_state.py` manual-verify enforcement (`:100–120`, `:505–530`, `:575–625`).
- **Parallel passes dispatched and INTEGRATED:** (1) a repo-mechanics research pass — confirmed F14
  (double-arming, `planning_active` not owner-scoped), F15 (attempts-file silent failure), F16 (`source`
  unvalidated), OD-2 (no attendedness signal exists at all), OD-9 (`plan_blocked` not durable), the
  skill-registration model, and the plan_gate rule/exit-code inventory; (2) a flow-gap/ideation pass
  (`ce-spec-flow-analyzer`) — surfaced B15/F17 (the vacuous-pass hazard), B16 (the chain is four skills),
  OD-11 (execution model), OD-12 (contradictions have no autonomous path), OD-13 (terminal success),
  CH-16 (four mandated human surfaces), CH-18 (the rejected baseline), and the IF9–IF12 additions.
  Both passes' findings are integrated above; none were dropped.
- **Gap lenses (fire-or-pass, Rigorous mode):** failure-modes **FIRED** (§3 F1–F13); security **FIRED**
  (F3 gate-weakening, OD-1 integrity bar, CH-6 irreversible actions); data-lifecycle **FIRED** (CH-6
  snapshot semantics, F12 half-state); rollback **FIRED** (B12 resume, CH-7 re-entry corruption);
  who-pays-the-tradeoff **FIRED** (CH-12 review burden, IF5/OD-8 spend).
- **ce-doc-review (Step 3.5): RAN.** Six-persona panel — `ce-coherence-reviewer`, `ce-feasibility-reviewer`
  (both always-on), plus `ce-adversarial-document-reviewer`, `ce-security-lens-reviewer`,
  `ce-scope-guardian-reviewer`, `ce-product-lens-reviewer` (activated on content signals). **38 findings
  returned; all integrated — none dropped.** The five that changed the design materially:
  1. **[feasibility, P1, conf 100]** The plan gate's bounded escalation **never fires for a super-run** —
     its `_snapshot_hash` covers all requirement-fact meta, and tickets are requirement facts, so every
     ticket write *and every self-repair write* resets the counter. F15/F14/OD-6/D6 all corrected; the
     "mirror the existing shape" lean is **withdrawn**.
  2. **[feasibility, P1, conf 100]** The marker pattern is **not client-side reusable** — the marker fact
     is materialized server-side with a hardcoded category, so a new marker needs a Praxis *service*
     change that Scope excluded. New **OD-3b**, leaning to meta-keys on the existing marker fact.
  3. **[adversarial, P0]** B7's inertness predicate was **self-falsifying** — `stamp_run` is an
     agent-controlled write, so the cheapest escape from the gate was to stamp a run marker and skip
     intake, producing the exact F2 the gate exists to prevent. Predicate corrected to require a blessed
     plan **and** the run marker.
  4. **[adversarial + product-lens, independently]** The target population had **no viable happy path** —
     every run either aborted at preflight (greenfield → empty snapshots) or ran with both panels
     auto-skipped (small + unattended). Fixed by **B17** (force panels + bootstrap).
  5. **[security, P0]** The kill-switch env vars are **runner-writable** via repo-root `.env`, so the
     runner could disable every gate with one line — a cruder F3. Now **F22** + a §5 non-negotiable + a
     B13 preflight refusal.
  Also promoted in review: **A3 amended** (as literally stated it is unachievable), **B9 split into
  Mechanical/Judgment tiers**, **B18/B19/B20** added, **CH-13 → OD-17**, **CH-20** (adversarial context /
  injection) added, **IF9/IF10 moved into scope**, and release increments introduced.

---

## 10. Hand-off

**af-intake-plan inserts this doc into Praxis and runs all planning validation** — it admits each settled
requirement as a `source="prd-<project>"` fact, runs the cold-eyes audit, resolves contradictions, forces
every **Open decision** above, clears the done-gate, and calls `save_snapshot`. **af-plan writes nothing
to Praxis.**
