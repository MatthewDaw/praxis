# Agent Factory

A Praxis-backed **agent factory**, delivered as a Claude Code plugin: it turns a PRD into a
clickable wireframe, a hardened plan, and a built-and-deployed app. **[METHODOLOGY.md](/METHODOLOGY.md)
is the single canonical statement of how it works — read it first.**

**The whole factory is one loop, run against one source of truth.** State lives in exactly ONE
place — **Praxis**. There are no JSON status files, no on-disk locks, no self-set "done" flags. A
ticket (requirement) and a check are Praxis facts; everything about what is built / claimed / passed
is state *on the ticket's Praxis node*. Every unit of work is the same loop:

> **FIND** the next incomplete ticket in scope → **CLAIM** it (a heartbeated lease, not a lock) →
> **RESOLVE** which checks apply *by query* (never a pre-authored list) → **BUILD** → **VERIFY** each
> pinned check, recording the pass on the ticket node → **FINISH** (release as finished) only when
> every pinned check passed.

A **single Stop-hook gate** (`hooks/build_completeness_gate.py`) reads Praxis live and enforces this
loop. Praxis is a **hard dependency**: if it is unreachable the gate **fails closed and BLOCKS** — it
never proceeds on a guess.

- **Knowing system** → [Praxis](https://github.com/Antonelli-Tech-Solutions/praxis) knowledge graph
  (retrieval, dedup, contradiction handling, provenance, requirement-completeness), via the
  `praxis_*` MCP tools. It is the single source of dynamic truth.
- **Doing system + glue** → this repo: skills that drive the loop, the one gate hook that enforces
  it, and small deterministic helpers in `src/agent_factory/`.

---

## The pipeline at a glance

```
 PRD (docs/inspiration/*.txt) ─┐
                               ├─►  af-plan        →  explore / research → messy plan doc
                               │                         (sibling: af-wireframe → clickable HTML
                               │                          wireframes; a surface with no screen
                               │                          = an incomplete ticket)
                               │
            plan + wireframe ──►  af-intake        →  admit + harden requirements in Praxis (tickets),
                               │                        then ALL validation in one write-path:
                               │                        cold-eyes challenge + tech/test sweep,
                               │                        planning checks, and the plan-finalization
                               │                        panel — findings become tickets/checks
                               │                        → save_snapshot("prd-<project>")
                               │
            blessed plan ──────►  af-build         →  FIND→CLAIM→RESOLVE→BUILD→VERIFY→FINISH
                               │                        loop over incomplete tickets, live vs Praxis
                               │                        (missing env dep = a failing check),
                               │                        always running validation + the work-review
                               │                        panel over the diff; findings = tickets
                               └────────────────────►   → shipped
```

There is **one** Stop-hook gate (`hooks/build_completeness_gate.py`). Everything the old per-phase
gates used to enforce is now either a **ticket** or a **check** in Praxis, and this gate enforces the
one question they all reduce to — *"are there incomplete tickets/checks for the active build scope?"*
— read **live from Praxis, fail-closed**. There are **no `.factory/*.json` manifests** and no status
files of any kind. The gate stays inert for ordinary repo conversation; the build loop arms it by
**claiming** a ticket. A supervisor that fanned work out to sub-agents owns no claim of its own, so
the gate is naturally inert for it while the builders (each claiming under their own session) work —
no special subagent-deferral plumbing.

---

## One-time setup

### 1. Install the plugin

```bash
git clone https://github.com/MatthewDaw/agent_factory.git
```

In Claude Code, register the clone as a **local directory** marketplace and install it:

```
/plugin marketplace add /absolute/path/to/agent_factory
/plugin install agent-factory@agent-factory-local
```

A *directory* marketplace reads the **live repo**, so the plugin's skills and gate hooks always
reflect your working tree (restart the session to pick up edits). Run `/plugin` to confirm
`agent-factory` is enabled.

### 2. compound-engineering (the cold-eyes panel) — auto-installed

The factory declares [compound-engineering](https://github.com/EveryInc/compound-engineering-plugin)
as a hard plugin **dependency** (in `.claude-plugin/plugin.json` + `marketplace.json`), so enabling
`agent-factory` auto-installs it. If it's ever missing, the review panel **blocks the phase** (never
silently skips) until you install it:

```
/plugin marketplace add EveryInc/compound-engineering-plugin
/plugin install compound-engineering@compound-engineering-plugin
```

### 3. Python on PATH

The gate hook is a Python script. **Ensure `python` is on PATH** — `python --version` should work in
a plain shell. The gate's own logic fails **closed** (Praxis unreachable ⇒ it BLOCKS), but it can
only do that if Claude Code can launch it; if `python` is missing the hook process can't start at
all and the harness has nothing to enforce. Treat Python-on-PATH as a hard prerequisite, not
optional.

### 4. Raise the Stop-hook block cap

A real plan/build legitimately makes the gates block many times while the model iterates. Claude
Code's default cap (9 consecutive blocks → force-override) is far too low. In `~/.claude/settings.json`:

```json
"env": { "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "250" }
```

(Takes effect next session.)

### 5. Connect Praxis

The factory's memory is **Praxis** — a separate knowledge-graph service, reached over its `praxis_*`
MCP tools. Installing this plugin does **not** install Praxis. You need:

**a. A running Praxis backend.** From the [Praxis repo](https://github.com/Antonelli-Tech-Solutions/praxis):

```bash
uv run --no-sync python -m knowledge.serve     # serves http://127.0.0.1:8000
```

`curl http://127.0.0.1:8000/health` should return `200`.

**b. The Praxis MCP, registered per project.** The MCP is configured per-repo in
`~/.claude.json`. A Praxis *identity/org* is pinned by a local cache file (`PRAXIS_MCP_CACHE`) —
**reuse the same cache across every repo that should share a plan**, so they're the same authenticated
user on the same org (don't create a fresh cache/org per repo — it'd be empty). Run, from each repo
that needs Praxis:

```bash
claude mcp add praxis -e PRAXIS_MCP_CACHE=/abs/path/to/your-cache.json -- \
  uv run --directory /abs/path/to/praxis python -m knowledge.mcp
```

**c. The right space.** In a session, verify before relying on it:
- `praxis_whoami` → authenticated, and your build org is the **active** org (`praxis_select_org <org>` if not).
- `praxis_list_snapshots` / `praxis_list_graph` → you actually see your project's snapshot + requirements.

> The factory runs in a dedicated Praxis org. Tenancy is single-principal, so projects are
> partitioned with **snapshots + read-only mounts**, not per-project user ids. All access flows
> through the knowledge-port policy ([docs/af-memory-policy.md](/docs/af-memory-policy.md)). See
> [docs/praxis-and-how-we-use-it.md](/docs/praxis-and-how-we-use-it.md).

---

## End-to-end walkthrough

Throughout, `<project>` is your project's slug (e.g. `team-app`); requirements are stored under
`source="prd-<project>"`. Paste the prompts into Claude Code with the plugin enabled and Praxis
connected. Run **attended** the first time (you answer the audit's questions and clear gates).

### Step 1 — Wireframe (optional but recommended)

`af-wireframe` turns a PRD into complete, clickable HTML wireframes (split by persona, e.g. a
mobile player app + a web admin console), with a coverage gate that won't let it claim done until
every requirement + implied state maps to a screen.

> Build a complete, clickable HTML wireframe for the app described by the PRD in `docs/inspiration/`.
> Read every doc there. Cover the full MVP and post-MVP, split by user persona, mobile-responsive
> for the player app. Output to `wireframe-rebuild/`, and show me a coverage table.

### Step 2 — Plan (plan → intake = admit + all validation)

This is one continuous, human-controlled phase that ends with a blessed `prd-<project>` snapshot.
`af-plan` explores/researches into a messy plan doc; `af-intake` is the single write-path that
admits the requirements **and** runs all the validation (audit, planning checks, plan-finalization
panel) before the snapshot.

> Run af-intake to turn the prose PRD in `docs/inspiration/` plus the approved wireframes
> (`wireframe-rebuild/wireframe-player.html`, `wireframe-rebuild/wireframe-admin.html`) into the
> hardened `prd-<project>` requirement set in Praxis. Use **Rigorous** mode. Admit during ingestion
> with `source="prd-<project>"`, then run af-intake's validation (the cold-eyes audit and the
> plan-finalization panel) before `save_snapshot`.

What happens, and where you're involved:
1. **af-plan** explores and researches the PRD (behavior) + wireframe (surfaces) into a messy
   candidate inventory, reconciles duplicates, and pauses for you to review the candidates before
   admission.
2. **af-intake** admits each requirement as a Praxis ticket (`source="prd-<project>"` = the
   project identity; `meta.scope` = `mvp`/`post-mvp` tier; `meta.verify` = `automated`/`manual`).
   Large plans use the **raw bulk fast-lane** (`add_insights(raw=True)`) to avoid the per-item dedup
   that times out / over-merges; small edits keep live contradiction surfacing.
3. **af-intake validation** runs an independent **cold-eyes** pass: adversarially challenges every
   requirement, routes underspecification (research / default / ask you / defer), forces a derived
   technical-architecture sweep **and a mandatory test strategy + CI**, and **reconciles** near-dup
   requirements in the graph. Anything it surfaces becomes a **ticket or a check in Praxis** — so the
   one completeness gate enforces it. (A small "panel-ran" Praxis episode records that the validation
   happened so it cannot be silently skipped — not a findings state machine.)
4. `save_snapshot("prd-<project>")` blesses the plan.
5. **af-intake's plan-finalization panel** runs the compound-engineering reviewers over the *whole*
   plan (coherence / feasibility / scope / security / completeness). Each finding lands as a Praxis
   ticket/check; an open finding is just an incomplete ticket the completeness gate enforces. The
   review is skippable for small work, but **never silently** — a skip records a reason
   (`praxis_record_episode`).

### Step 3 — Build (preflight → fan-out build → deploy → work-review)

Run this in the **app repo**, in a session with Praxis pointed at the same org (so it sees the plan).

> Run af-build to build the app from the blessed `prd-<project>` snapshot into this repo.
> Build the MVP + automated-verify set (fan out in parallel), gate every slice via af-build's verify
> step, deploy to the techDecisions target, and run the work-review before shipping.

What happens (every slice follows **FIND→CLAIM→RESOLVE→BUILD→VERIFY→FINISH**, all state in Praxis):
1. **Env dependencies are checks, not a separate gate** — the build's external dependencies derived
   from the plan's techDecisions (credentials, API keys, services, tooling) become **failing checks**
   on their tickets. An unprovisioned dependency is just an incomplete ticket; the one completeness
   gate refuses to pass while it fails, and the message tells you *exactly* what to provide. It never
   stubs a credential.
2. **Fan-out build** — each pass computes the *buildable frontier* (the mvp+automated build set,
   dependencies satisfied) and **fans it out as parallel worktree-isolated builders via a Workflow**
   (not a serial queue). Each builder **claims** its ticket (a heartbeated lease), **resolves** the
   ticket's checks by query and pins them, builds, gates through **af-build's verify step** (external
   signals only — tests / type-check / build) recording each pass **on the ticket node**, and **releases as
   finished** only when every pinned check passed. A failed check records a failed outcome — the
   ticket regresses and re-enters the FIND set.
3. **The one completeness gate** — "done" is mechanical and read **live from Praxis**:
   `praxis_incomplete_requirements(prd-<project>)` over the build set must be empty and no session
   may hold an unfinished claim. There is no manifest — the gate reads ticket `meta.build_state` and
   lease/claim state live. (Post-MVP and manual-verify requirements are excluded/deferred — never
   block the gate.) Pass the **bare** project name to the query; the endpoint prepends `prd-` itself.
4. **Deploy** — deployment + its verification are themselves enforced as tickets/checks: the build
   isn't done until it's deployed and verified, unless you explicitly opt out
   (`deployment.required:false` + a recorded reason).
5. **af-build's work-review panel** — the panel reviews the whole diff before "shipped"; findings land
   as tickets/checks the same gate enforces.

> **Resuming:** completeness is outcome-grounded and stateless on disk, so you can stop and restart a
> build any time — a fresh `af-build` re-queries `incomplete_requirements` live and **resumes
> exactly where it left off** (only the not-yet-finished tickets remain; a dead agent's stale lease
> auto-reclaims so nothing dangles).

---

## The skills

Claude Code activates these from intent (or invoke by name, e.g. `af-plan`). There are **four**
invocable skills:

| Skill | Role |
|---|---|
| **af-plan** | Explore / research the PRD into a messy candidate plan doc — surface requirements, contradictions, and open questions. |
| **af-wireframe** | Sibling of af-plan — one-shot PRD → complete, clickable HTML wireframes, self-audited coverage; hands surfaces to af-intake. |
| **af-intake** | The single write-path: admit + harden requirements in Praxis **and** run all validation — the cold-eyes audit (adversarial challenge, underspecification routing, technical + **test-strategy** sweep, near-dup reconciliation), the planning checks, and the plan-finalization panel. Includes an **amend mode** for adding validation/planning checks to an existing plan. |
| **af-build** | The build loop — claim a ticket, resolve+pin its checks, build, verify against **external** signals only, deploy — and always run validation + the work-review panel. The "go work unfinished" entry point. |

The Praxis knowledge port is now an internal reference doc, not a skill:
[**docs/af-memory-policy.md**](/docs/af-memory-policy.md) — the single policy for all Praxis
reads/writes, cited by the four skills and the hooks.

## The gate (`hooks/`)

The gate spine collapses to **one** Stop-hook forcing function, which reads Praxis live and
fails **closed** (if Praxis is unreachable it BLOCKS, never passes):

| Gate | Enforces |
|---|---|
| `build_completeness_gate.py` | No incomplete tickets/checks for this scope — every pinned check on every in-scope ticket has passed (and the build is deployed unless opted out), read live from Praxis. |

Everything the old per-phase gates (preflight / wireframe / plan-audit / review) did becomes a ticket
or a check in Praxis: a wireframe surface with no screen is an **incomplete requirement**, a missing
env dependency is a **failing check**, a review/audit finding is a **ticket/check**. The one
completeness gate enforces them all. The gate reads its state live from Praxis via
`hooks/_praxis.py` + `hooks/_ticket_state.py` — see
[docs/factory-state-contract.md](/docs/factory-state-contract.md) for the canonical meta keys, the
per-ticket lifecycle, and the lease/claim semantics. There are **no `.factory/*.json` status files**;
`.factory/` is ignored only for genuinely-static local config.

## Key conventions

- **`source="prd-<project>"`** is the project identity (what the completeness query and the
  `R-HAS-SOURCE` gate filter on). It is **not** `meta.scope`, which is the `mvp`/`post-mvp` tier.
- **Build target = `mvp` + `automated`** (`src/agent_factory/build_target.py`); `post-mvp` and
  `manual` are excluded/deferred so the forced gate can't chase or trap on them.
- **Raw bulk inserts** (`add_insights(raw=True)`) for a whole-plan admission skip Praxis dedup; the
  intake reconcile + the audit's cold-eyes pass are the dedup/contradiction net there.

## Autonomous (overnight) mode

[CONSTITUTION.md](/CONSTITUTION.md) is the operating contract for **unattended** runs: it drives the
same plan → execute → verify loop with no human, records every owned decision as a Praxis episode,
defaults to **fanning out via Workflow** for substantial work, and treats the gates' attended pauses
as deferred owned-decisions for morning review. Read it before launching an overnight loop.

## Layout

```
.claude-plugin/                # plugin.json + marketplace.json (declares the CE dependency)
METHODOLOGY.md                 # the single canonical statement of how the factory works — read first
CONSTITUTION.md                # the autonomous-run operating contract
skills/                        # af-wireframe / intake / plan / audit / review / execute / churn / verify / memory
hooks/
  build_completeness_gate.py   # THE single Stop-hook gate (reads Praxis live, fails closed)
  _praxis.py                   # stdlib-only Praxis HTTP client (the single source of dynamic truth)
  _ticket_state.py             # per-ticket lifecycle: claim/lease/heartbeat, resolve+pin checks, release
src/agent_factory/
  plan_gate.py                 # deterministic plan done-gate (acceptance / vague / dangling / source)
  build_target.py              # mvp+automated build-set selector
  validation_target.py         # resolve a ticket's validation checks from Praxis
  gate.py                      # shared gate Verdict/Reason contract
  tabular.py                   # deterministic table linearizer (H6 ingestion shim)
  event_log.py                 # append-only run log
docs/factory-state-contract.md # canonical: meta keys, ticket lifecycle, lease, check-resolution-as-query
evals/cases/plan_gate/         # plan-gate eval cases
tests/                         # unit tests
docs/                          # vision, reference model, Praxis notes, plans
```

## Develop

```bash
uv run --with pytest pytest -q
```

See [docs/](/docs/) for the deeper picture — the
[vision](/docs/agent-factory-vision.md), the neutral
[reference model](/docs/agent-coding-factory-reference.md), and
[what we build here vs. what Praxis owns](/docs/factory-local-components.md).
