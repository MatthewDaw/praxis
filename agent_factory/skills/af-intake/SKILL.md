---
name: af-intake
description: >
  The SINGLE write-path and owner of the Praxis plan + ALL planning validation. Two entry modes.
  FULL INTAKE takes af-plan's messy exhaustive brainstorm doc (+ optional clickable wireframe),
  extracts candidate requirements and surface↔requirement bindings DIRECTLY INTO PRAXIS, hardens
  them (self-consistency, contradictions, dedup), then runs all planning validation — the cold-eyes
  architecture/external-service decisions, underspecification routing, cross-requirement gaps,
  coverage+depth checks, and the data-driven scope="planning" checks — convenes the ce-* plan-review
  panel, and hands the human a clearable gate that ends in save_snapshot(space="<project>", snapshot="prd-<project>"). AMEND MODE
  makes ONE additive change to an existing plan as a Praxis fact — either a check (validation or
  planning), OR a genuinely-new requirement ticket where there is nothing to edit; because checks and
  tickets resolve by query and completion is gated on them, matching (or newly-added) tickets
  automatically enter the incomplete set. Use when starting (or re-baselining) a project from a
  brainstorm/PRD + wireframe, or to graft a new check or a lone missing ticket onto an already-hardened
  plan. (Amend adds; it does NOT edit an existing requirement's content — that is a re-baseline FULL
  INTAKE.)
---

## How work flows (this factory's methodology — read first)

State lives in ONE place: **Praxis** (the single source of dynamic truth — see `METHODOLOGY.md`,
`docs/factory-state-contract.md`). There are **no JSON status files, no locks on disk, no self-set
"done" flags**. A ticket (requirement) and a check are Praxis facts; everything about what is
built/claimed/passed is state ON THE TICKET'S Praxis node. The build loop every downstream skill runs
is exactly:

1. FIND   — query Praxis for the next incomplete ticket in scope (incomplete = never-built | regressed |
            stale, derived from recorded outcomes). Pass the BARE project name (e.g. "team-app"); the
            endpoint adds the "prd-" prefix itself — passing "prd-team-app" returns EMPTY and silently
            hides all work.
2. CLAIM  — atomically set the ticket's meta.build_state="in_progress" with claim_owner=you + a heartbeat.
            The claim is a LEASE: refresh the heartbeat while working; a stale lease auto-reclaims so a
            dead agent never strands a ticket. Parallel agents never double-work because a live claim is
            visible to all. (The lease/claim machinery is owned by af-build; intake only references it.)
3. RESOLVE— determine which checks this ticket must pass by QUERY (its tag ∪ its surfaces ∪ semantic
            match against active checks). The ticket NEVER stores its own check list. Truncate any prior
            per-check state, then PIN the freshly-resolved set onto the ticket as this pass's contract.
4. BUILD  — do the work to satisfy the ticket's acceptance condition.
5. VERIFY — run each pinned check; record each pass ON THE TICKET NODE (never on the check — checks are
            read-only during builds). External signals only; never self-judge.
6. FINISH — only when EVERY pinned check passed: record a succeeded outcome and release the lease
            (build_state="finished"). If any check fails, record a failed outcome — that regresses the
            ticket so it re-enters the FIND set and is re-done. Completion is the hard enum
            build_state="finished" — nothing else counts as done.

Praxis is a HARD dependency: if it is unreachable the factory STOPS (the gate blocks) — it never proceeds
on a guess. The factory has a **SINGLE Stop hook — `build_completeness` — and it gates the BUILD phase
only.** There is **no separate planning Stop hook**: planning is **human-gated**. The human clears the
plan once `plan_gate` passes, contradictions are empty, and a panel-ran episode exists (all read live
from Praxis).

**This skill's place in the methodology.** af-intake runs UPSTREAM of the build loop and is the
**single owner of the Praxis plan**: it MINTS the tickets the loop later FINDs, the surface↔requirement
`renders` bindings the RESOLVE step and the wireframe→code build query read, and the checks completion is
gated on. It is also the owner of **all planning validation** — the cold-eyes audit and the plan-review
panel are folded in here. af-intake writes tickets, checks, and bindings to Praxis; it records build/claim/
pass state on nothing and writes ZERO side files. Its sibling **af-plan is now ONLY brainstorming/research**
— it produces a messy, exhaustive doc; af-intake turns that doc into the hardened, blessed `prd-<project>`
snapshot.

**Two entry modes:**
- **FULL INTAKE** (default; a fresh project or re-baseline) — extract → harden → validate/audit → panel
  → human gate → `save_snapshot`. Sections "Full intake" + "Planning validation / the audit" below.
- **AMEND MODE** (an already-hardened plan) — one additive Praxis write: a check (validation or
  planning), OR a lone new requirement ticket where nothing existing needs editing. Matching (or
  newly-added) tickets enter the incomplete set by query. Section "Amend mode" below. This single mode
  replaces the four former check skills (af-add-validation, af-add-planning-check,
  af-redo-ticket-add-validation, af-redo-plan-add-check). Amend is **additive only** — to change an
  existing requirement's content, re-baseline via FULL INTAKE (the Step-3 `on_conflict="surface"` edit
  path); to add work the plan simply lacks, use C0 below.

All Praxis access follows **`docs/af-memory-policy.md`** (tenancy, `insight` vs `ingest`, the tabular audit, mount/save
rules). This is a single decision-making agent that may dispatch the **read-only retrieval sub-agent**
(`af-build` §1a) for bulk reading — never a crew that decides or writes. Record the session in the
event log (`src/agent_factory/event_log.py`).

---

# PART A — FULL INTAKE (the write-path: doc + wireframe → blessed snapshot)

Two inputs, one store. **Inputs:** af-plan's messy exhaustive brainstorm/research doc (the behavioral
truth — `docs/inspiration/` / `docs/brainstorms/`) and, optionally, the clickable wireframe HTML (the
surface truth + the completeness cross-check). **Output:** the hardened, validated, blessed
`prd-<project>` Praxis snapshot — candidate requirement facts, `renders` bindings, and the checks
completion is gated on, all **live in Praxis**. There is no local staging manifest, no `.factory/*.json`.
If Praxis is unreachable, intake **CRASHES AND STOPS** (fail-closed); it never writes work to a side file
and never proceeds as if the writes landed.

Division of labor:
- **The brainstorm doc** is the source of record for *behavior* — rules, data model, acceptance criteria.
  (af-plan produced it; it is deliberately messy and over-complete.)
- **The wireframe** is the source of record for *surfaces* — screens, states, actions, navigation — and
  the **completeness cross-check** (it already enumerates the implied states: empty, offline, error,
  completed, fallback). It is **not** a second behavioral truth.
- This skill **extracts, hardens, validates, and blesses.** af-plan no longer hardens or gates — it only
  brainstorms.

## Step 0a — Expand a thin source with compound-engineering (REQUIRED)

**compound-engineering is a HARD required dependency of this factory** — declared in
`.claude-plugin/plugin.json`, so Claude Code auto-installs it. It is the **required** front-end for
intake, not a "use it if installed" option. A thin source extracted faithfully produces a thin plan.

Before extracting, if af-plan's doc is thin (a rough idea, a few feature sentences) rather than a
complete brainstorm, USE it:
- **`ce-brainstorm`** — resolve scope, behavior, success criteria, and edge states into a real
  requirements doc. Extract from THAT, not the thin description.
- **`ce-ideate`** — surface the adjacent and IMPLIED features the source never stated (the derivations a
  naive extraction drops). Feed accepted ones in as candidates. This is the *generative* sibling of the
  planning-checklist lenses: ideate proposes implied features up front; the lenses FORCE the implied
  decisions in the audit (Part B).

Skip only when the source is already a complete, hardened spec — and say so explicitly; never skip
silently.

## Step 0b — Read the sources (read-fully guard)

1. **Read the brainstorm/prose docs FULLY in your own context** (no limit/offset). They are the named
   source of behavioral truth — do not delegate them away. List the doc folder; read every doc.
2. **Delegate the wireframe surface enumeration** to the read-only sub-agent (if a wireframe exists).
   Wireframe HTML is large and mechanical; have the sub-agent return a compact **surface inventory** —
   one row per screen (`id="s-X"`), its title, the states it shows, and its inert actions (`go(...)`,
   button labels) — filtering ruthlessly. The parent never ingests the raw HTML.

## Step 0c — Choose rigor and decision mode (ask both, one per turn)

Before extracting, fix two axes with the human — **two** blocking questions, **one per turn** (never
stacked). If af-plan's doc already records a rigor mode, confirm it rather than re-asking from blank.

**0c-a. Rigor — how hard the audit pushes** (mirrors af-plan Step 1a): **Quick** runs each lens once;
**Rigorous** runs the gap-lenses (failure-modes, security, data-lifecycle, rollback, who-pays)
fire-or-pass per requirement and loops the completeness critic **until-dry** (B1, B5). Note the mode in
the panel-ran episode (B8).

**0c-b. Decision mode — how every genuine fork gets settled** once the resolve-before-you-ask ladder
(Step 3a) cannot answer it from sources. This is the **attended/unattended axis made explicit** — the
human's answer sets it deliberately instead of it being inferred from Constitution/owner-asleep:
- **Collaborate** (default / attended) — surface each genuine product fork as a blocking question; the
  human decides. Drives the ambiguity forge (Step 3b), the underspecification ladder's step 4 (Step
  3a), and the B4 architecture/provider decisions interactively.
- **Autonomous (force decisions / unattended)** — never block on a fork: take the low-regret default on
  every one, record it with `praxis_record_episode` (decision + "forced default: source silent, owner
  autonomous", alternatives not taken), and surface it for **override at the B9 gate** rather than
  asking mid-intake.

Decision mode changes **only how a fork is settled** — it never weakens validation. The audit (Part B),
the plan-review panel (B7), the mechanical gates (B6), and the human's final blessing (B9) are
**unconditional** in both modes; in Autonomous mode the human still clears B9, reviewing the forced
defaults there instead of one at a time. **Anti-masking guard:** a forced default may NEVER paper over a
genuine high-regret/irreversible fork (auth model, data-loss semantics, money, PII exposure) — those
surface to the human even in Autonomous mode.

## Step 1 — Extract candidates (two passes, then reconcile)

**Pass A — behavioral, from the doc.** Atomize the rules into binary conditions. A good brainstorm doc
is already near-structured (epics + acceptance + data model + API), so this is *atomize + mint binary
conditions + dedupe across sections*, not invention. Over-generate; the hardening + audit gates are the
filter.

**Pass B — surface, from the wireframe inventory.** Each screen, each state, each action becomes a
candidate. This is where the implied states (offline / empty / invalid-invite / completed / fallback)
become first-class requirements instead of being forgotten.

**Reconcile.** Merge duplicates by *concept* (the same rule stated twice is ONE candidate with two
citations) so you don't admit five near-duplicates and lean entirely on Praxis dedup. **This Step-1
reconcile IS the dedup for the raw fast-lane write path** (Step 2). Where doc and wireframe disagree,
keep BOTH as candidates and let the audit's contradiction pass settle it (e.g. wireframe shows a coach
1:1 inbox; prose says post-MVP — surfaces as a pending pair; human tags scope).

### The candidate shape (a Praxis fact, not a file record)

Each candidate is admitted **directly to Praxis** as a fact. There is NO staging file — Praxis *is* the
staging store as well as the source of truth. The conceptual shape:

```jsonc
{
  // `content` → the fact statement (ONE atomic behavior, single semicolon-joined sentence)
  "content": "completion = daily rep submitted AND all three ratings present; the habit checklist is recorded but never gates completion",
  "category": "requirement",
  "source": "prd-team-app",          // PROJECT IDENTITY — mandatory; see field rules
  "meta": {
    "acceptance": "given a rep + effort/focus/support all set, status=complete; with the checklist left unchecked, status is still complete",
    "verify": "automated",            // or "manual"
    "surfaces": ["s-today"],          // wireframe screen ids, or ["backend-only"]
    "defines": ["completion"],
    "references": ["daily rep", "ratings", "habit checklist"],
    "depends_on": [],                 // prerequisite requirement_ids ("R8") — NEVER fact ids/cids — FINISHED first (build-order DAG; see Step 5)
    "scope": "mvp",                   // mvp | post-mvp — the TIER tag, not the project
    "citations": ["Brainstorm §3", "Epic D", "wireframe-player.html#s-today"],
    "tags": ["completion", "today-screen"]   // identity tags; check applicability queries these later
  }
}
```

Field rules:
- **`content`** — ONE atomic behavior, a **single semicolon-joined sentence** (the Praxis
  sentence-fragmentation workaround — multi-sentence insights split per sentence; see CONSTITUTION §8).
- **`source`** — `"prd-<project>"` (here `prd-team-app`). The **project identity** the completeness
  query and the done-gate's `R-HAS-SOURCE` rule key off. **Mandatory** — a candidate without `source` is
  rejected. Keep it distinct from `meta.scope` (the mvp/post-mvp tier).
- **`meta.citations`** — cite doc section/epic AND wireframe screen(s) (`file.html#s-X`). Prose
  provenance lives in meta; `source` is reserved for project identity.
- **`meta.acceptance`** — a draft binary condition ("when X, system does Y, observable via Z"). If the
  doc gives one, use it; else leave a best-draft and flag it for the ambiguity forge (Step 2).
- **`meta.verify`** — `"automated"` (a command the loop runs — the default) or `"manual"` (needs human
  confirmation). Drives the phase-gate split downstream.
- **`meta.surfaces`** — wireframe screen ids governed, or `["backend-only"]`. Seeds the `renders`
  bindings written in Step 4.
- **`meta.defines` / `meta.references`** — concepts, for the H14 dangling-reference gate.
- **`meta.depends_on`** — prerequisite requirement ids that must be `finished` before this one is
  buildable (the build-order DAG `af-build`'s `next_ready_ticket` walks). A best-draft now; the DAG is
  mapped and validated in **Step 5**. Empty for a requirement with no prerequisites.
- **`meta.scope`** — `"mvp"` or `"post-mvp"` (the tier tag only; NOT the project identity).
- **`meta.tags`** — identity tags (concepts / surfaces / semantics). A ticket carries identity, **NEVER
  an authored list of its checks**; *which checks apply* is a fresh query (tag ∪ surface ∪ semantic)
  resolved at build time. Tag honestly so that query resolves correctly.

## Step 2 — Write candidates to Praxis (the write-path)

Extraction is the **highest-leverage error point** — a bad requirement spawns thousands of bad lines.
Review leverage is inverse to distance-from-execution, so scrutiny concentrates here, at the plan.

**Admit the whole batch via the raw fast-lane.** A fresh intake is a *bulk* admission:
`praxis_add_insights(insights=[...], raw=True)` in ONE round-trip. `raw=True` still embeds each fact
(retrievable) and still redacts secrets, but **skips Praxis dedup AND the per-item LLM conflict check** —
which is what avoids the timeout the normal path hits on large batches (e.g. 71 items) and the dedup that
wrongly collapses near-duplicate requirements. **You own clean, non-conflicting data on this path** —
which is why it is only safe HERE: Step-1 Reconcile already deduped, and the **audit's cold-eyes conflict
pass (Part B) is the contradiction net.** Every record MUST carry `source="prd-<project>"`; each result
returns `ok`/`id`/`action`/`retrievable` — a bad item errors without aborting the rest, so check them.

> **Raw-bulk caveat for contradictions.** Because `raw=True` runs NO conflict detection,
> `praxis_get_contradictions` is empty *by construction* — that emptiness is NOT evidence of consistency.
> For a raw-admitted set the contradiction net is (1) the Step-1 reconcile (kills dups before the write)
> and (2) the audit's cold-eyes cross-requirement conflict pass (Part B). Treat the audit, not the empty
> queue, as the contradiction gate for bulk inserts.

**Incremental edits use the surfacing path.** A single requirement add/change later (not a fresh bulk
intake) uses `praxis_add_insight(..., on_conflict="surface")` — which keeps **live contradiction
surfacing** (the per-item conflict/claim check). With `on_conflict="surface"` a detected contradiction is
**surfaced, not auto-resolved**: both facts are kept (incumbent `active`, newcomer `proposed`, neither
rejected) and a pending pair appears in `praxis_get_contradictions` with a resolvable `pair_id`; the
human settles it with `praxis_resolve_contradiction(pair_id, keep_id | "all" | custom_text)`. **Never**
write a planning fact on `auto_resolve` — it silently rejects the loser and hides the conflict.

**`source="prd-<project>"` is the project identity — NOT `meta.scope`.** A requirement tagged only with
`scope="team-app"` and no `source="prd-<project>"` is the exact generation drift that went uncaught: it
never matched the completeness filter, so the build wrongly believed every requirement was done. Every
admitted requirement MUST carry `source="prd-<project>"`.

### Review checkpoint (compute it FROM Praxis, not from a file)

- **Attended (default): present a compact review surface computed from Praxis** — counts by `source` and
  `meta.scope` (e.g. "37 candidates: 31 mvp, 6 post-mvp", via `facts_by`); the **bidirectional coverage
  cross-check** once Step-4 bindings exist (`praxis_surface_coverage(project, scope="mvp")` — every
  surface with no backing requirement = `uncoveredSurfaces`, every mvp requirement with no surface and no
  `backend-only` = `uncoveredRequirements`); and a short **flagged list** (low-confidence extractions,
  doc↔wireframe conflicts you preserved, placeholder acceptance conditions). If a candidate is wrong, the
  human **edits the fact in Praxis directly** (`praxis_edit_fact` / `praxis_reject_fact`) — corrections
  happen there, not in a side file. On approval, continue to hardening.
- **Unattended (Constitution / owner asleep): do not pause** — there is no one to approve. The candidates
  are already in Praxis; record a `praxis_record_episode` ("intake: extracted N candidates, auto-admitted,
  owner reviews AM" + the flagged list as notes) so morning review has the counts, the coverage
  cross-check, and the flagged list — all queried back from Praxis, no file.

## Step 3 — Harden (self-consistency, contradictions, dedup)

Hardening makes the admitted set *self-consistent* and *fully specified*. Work one requirement (or one
tight cluster) at a time. **Fan out via Workflow where it helps** (the default for a substantial pass;
CONSTITUTION §0): parallel research sub-agents to resolve underspecification, a judge panel to weigh a
contested fork, an adversarial reviewer over the candidate set whose job is to falsify. Run gap-finding
**loop-until-dry**. Workflows *inform* — they research, challenge, rank — but they NEVER settle a
contradiction, author a fact, or clear the gate. You remain the sole agent that writes to the graph; the
human still resolves each pending pair and clears the final gate.

**a. Resolve before you ask (mandatory gate before any question).** Never surface a fork until you have
tried to answer it, in order: (1) the **doc/source text** — re-read the section; if it answers, use it
and cite the line, don't ask; (2) **mounted knowledge** — `get_context` against `general-pool`,
`constitution`, and any mounted prior `prd-<project>`; if a fact/invariant answers it, use it; (3)
**conventional default** — if the source is *silent* and a clear low-regret default exists (streak resets
to 0 on a miss; DST uses local wall-clock), take it, record it with `praxis_record_episode` (the decision
+ "source silent → conventional default", alternatives = options not taken), and surface it for
*override* rather than asking; (4) **then settle per decision mode (Step 0c)** — a **genuine product
fork** (source open AND no default clearly right AND reasonable choices materially differ) is, in
**Collaborate** mode, a blocking question saying what you already checked; in **Autonomous** mode, a
low-regret default recorded via `praxis_record_episode` and surfaced for override at B9 — EXCEPT a
high-regret/irreversible fork (auth, data-loss, money, PII), which surfaces to the human in both modes.
**Anti-masking guard:** a "conventional default" (or a forced default) may NEVER paper over a genuine
fork — if unsure, treat it as a fork (ask in Collaborate, or default-and-flag conspicuously in
Autonomous). An underspecified area must *visibly* become research, a question, or a flagged deferral,
never a quiet guess.

**b. Admission gate + ambiguity forge.** A requirement is not hardened until it carries ≥1 **binary
acceptance condition** ("when X, the system does Y, observable via Z"). When an answer uses a vague term
("fast", "secure", "most users"), offer multiple-choice disambiguations (`p95 < 200ms` / `p99 < 1s` /
"feels instant in demo") that mint the testable fact. Tag each condition `automated` or `manual` (in
`meta`): automated = a command the loop runs (test/build/type-check/lint — the default; always prefer
it); manual = needs a human to confirm (UX feel, a visual) and the executor **may not self-check it**.

**c. KG self-consistency (incremental edits).** For incremental edits made on `on_conflict="surface"`,
the surface is **`praxis_get_contradictions`** — read it, present each pending pair as a paired diff
("Req A: sessions expire in 24h / Req C: sessions are persistent"), and the human settles each with
`praxis_resolve_contradiction(pair_id, keep=…)` (`keep="<id>"` to keep one side, `keep="all"` for a false
positive where both genuinely hold, or `custom_text` to reconcile). You never settle it yourself. A
requirement that conflicts with a mounted `constitution` invariant surfaces as the same kind of pending
pair. (For the raw-bulk path, the contradiction net is the audit — see the caveat in Step 2.)

**d. A human correction is a fact, not an override.** When the human corrects a *factual* claim, admit it
the same way (`add_insight(..., on_conflict="surface")`) so a correction that is itself wrong, or clashes
with something settled, *surfaces* and is reconciled rather than silently absorbed. When a correction
invalidates earlier research, re-open and re-edit the affected requirements directly.

**Escape hatch:** a requirement the human deliberately owns but can't yet make testable is recorded as an
**owned-decision** fact (tagged as such), not forced binary — but it cannot pass the done-gate until it
has an acceptance condition or is explicitly deferred.

## Step 4 — Persist the surface↔requirement binding (first-class `renders` relation)

The binding is a **first-class typed graph edge in Praxis** — `renders` (requirement fact → surface
fact) — not metadata, not a file. After candidates are admitted, persist each candidate's
`meta.surfaces`: for each screen id call **`praxis_bind_surface(requirement_fact_id, screen_id, project,
title, file, states)`** — it ensures the surface fact (`category="surface"`, idempotent on `screen_id`)
AND adds the `renders` edge in one call. A `backend-only` requirement gets no bind — it's reached by
task/DAG dependency.

This edge is the bridge the wireframe→code build step queries: to build a screen it calls
**`praxis_requirements_for_surface(project, screen_id)`** and gets exactly the active requirement facts
governing that screen — a per-screen hermetic context (behavior from Praxis, layout from the wireframe
HTML in git). Rejecting/deleting a requirement drops it from these queries automatically (active-only
filtering + `ON DELETE CASCADE`); no `meta.surfaces` bookkeeping to sync.

## Step 5 — Map the build-order dependency DAG (`depends_on`)

`af-build` works **one ticket at a time** and only ever pops a ticket whose prerequisites are already
`finished` (`next_ready_ticket`). That ordering is **not derived at build time** — intake must author it
now, as a `depends_on` edge set on each requirement, so the build loop has a realizable order to walk.

**Derive each prerequisite from what a requirement actually needs to exist first** — not from authoring
order or screen layout. The relations that create a genuine build-order dependency:
- **data producer → consumer** — a feature that reads/aggregates data another feature produces depends on
  the producer (participation% depends on daily-completion + active-roster; a nightly rollup depends on
  the write it summarizes).
- **identity/authz → protected behavior** — anything behind login depends on the auth requirement;
  authorization depends on authentication.
- **entity definition → its surfaces** — a screen that renders/edits an entity depends on the requirement
  that defines that entity's create/store behavior.
- **shared infra → its first user** — the data store + migrations, the chosen external-service transport
  (B4), or a base schema a feature relies on.

Set `meta.depends_on = [requirement_id, ...]` on each requirement via `praxis_edit_fact` (or at admit).
A requirement with no prerequisite keeps `[]`.

**CANONICAL FORMAT — the ONE dependency key is the target's `requirement_id` (e.g. `"R8"`), NEVER its
fact id / cid.** There is a single storage format for dependency edges; do not mix the two. Every consumer
resolves `depends_on` by `requirement_id`: the plan gate (`R-NO-DANGLING-DEP`), the build loop
(`next_ready_ticket`), and the dashboard graph (`graph_adapter` materializes the `depends` edges by
mapping `requirement_id -> node`). Writing a fact id instead is a silent failure — it names no
requirement, so the plan gate flags it dangling, the build loop never treats it as a prerequisite, and the
graph draws **no edge** (this is exactly why a snapshot authored with cids rendered no dependency edges).
When you have only a target's fact id in hand, look up that fact's `meta.requirement_id` and store *that*.

**The DAG is VALIDATED, not just authored — it is part of the mechanical gate (B6).** Run the plan gate
(`agent_factory.plan_gate.evaluate_plan`); its `R-NO-DANGLING-DEP` rule rejects a `depends_on` naming a
requirement not in the plan, and `R-NO-DEP-CYCLE` rejects a cycle (A needs B needs A) — either of which
would otherwise become a silent run-time **stall** when `next_ready_ticket` finds nothing claimable. A
plan does not pass intake with a dangling or circular dependency.

---

# PART B — PLANNING VALIDATION (the audit + the panel)

Once candidates are admitted, hardened, and bound, intake runs **all planning validation** before the
human can bless the plan. This is the cold-eyes pass whose entire output is **writes into Praxis** —
requirement edits, new requirements, reconciled near-duplicates, and planning/validation checks — plus a
single **panel-ran episode** that proves the audit happened. It authors **no state file**: there is no
`plan-audit.json`, no findings state machine. An audit gap that isn't resolved lives in Praxis as an
**incomplete requirement or check**.

**Why a separate cold-eyes pass, not inline:** a skeptic firing in the same breath that drafted a
requirement is self-review — the weak kind (a model judging its own output inflates its pass rate). So
dispatch the **read-only retrieval sub-agent** (`af-build` §1a) as the skeptic: it reads the admitted
facts (`praxis_list_graph` / `get_context` on the live graph) plus the doc and wireframe and tries to
**break** each requirement. It didn't write them, so it challenges harder. It also sees the **whole set**,
so cross-requirement gaps (a missing interaction, an unhandled handoff) — invisible per-requirement —
become visible.

## B1 — Adversarial challenge (cold eyes, every requirement)

For each admitted requirement, the skeptic files **≥1 falsifiable challenge** from: missing actor,
unbounded condition, unhandled empty/error/boundary case, hidden dependency, idempotency, race/ordering,
and **cross-requirement gap** (the case that falls between two requirements). In **rigorous** mode each
gap-lens must explicitly **fire-or-pass** per requirement: `failure-modes`, `security`, `data-lifecycle`,
`rollback`, `who-pays`.

**Evaluate the lenses by BATCH, not per-requirement (stop-sooner).** Take ONE lens and sweep it across
ALL requirements in a single pass — five sweeps total — recording fire/pass per requirement, rather than
re-deriving all five lenses for each requirement (5×N judgments, the dominant cost on a large plan). For
a large plan, fan the five lens-sweeps out as a Workflow (one builder per lens). The same batch-by-
dimension discipline applies to the technical sweep (B4) and the test-strategy derivation (B5).

A challenge isn't done until **closed by a Praxis write**:
- **resolved** — the plan changed: `praxis_edit_fact` to tighten, or
  `praxis_add_insight(category="requirement", ...)` to add the missing one. The edit/add IS the closure.
- **dismissed** — doesn't hold: record *why* with a non-empty reason as `praxis_record_episode`.
- **deferred** — a genuine owned-decision: record it as a deferred owned-decision episode (explicit, not
  silent); if it still blocks the build, leave the affected requirement **incomplete** in Praxis.

## B2 — Near-duplicate / overlap challenges WRITE BACK to the graph

The cold-eyes pass is the **only** dedup/reconcile step for any plan admitted via the `raw=True`
fast-lane — `raw` deliberately skips Praxis dedup, so reconciliation is the audit's job, and a near-dup
is **not closed until the graph reflects it**:
- **redundant / subsumed** → keep the canonical fact and **`praxis_reject_fact`** the loser (drops it
  from active queries). Record *why* + a cross-link, then **re-save the snapshot**.
- **distinct-but-overlapping** → **`praxis_edit_fact`** to NARROW the overlapping fact (strip the
  duplicated clause so it defers to the canonical one; `edit_fact` requires BOTH `title` and `content`).
  Persist the relationship as a `references` entry in meta, then re-save the snapshot.
- **genuinely distinct / complementary** → no graph change; record the cross-link rationale (an episode)
  so a future reader knows the overlap was considered.

## B3 — Validate the plan against the planning checklist (the two-tier coverage GATE)

The planning checklist is NOT applied freeform. A free-running audit reliably DROPS ~14% of the lenses
(measured: the real pipeline surfaced 128/148 and silently missed 20). So intake runs the lenses as the
**SAME two-tier validation `af-build` uses for code** — lens-application becomes a HARD coverage contract,
not the agent's memory. Identical engine (`hooks/_ticket_state.py`); the only difference is the scope and
the subject (the plan's Praxis facts, not built code). The plan-anchor `plan_subject` is the fact this
`prd-<project>` plan's coverage contract hangs on.

1. **RESOLVE** — `resolve_validation_requirements(plan_subject, project, scope="planning")` returns EVERY
   active `scope="planning"` lens (global considerations; the whole plan must satisfy each — not
   tag-bound). The lenses are read from a **DEDICATED `planning-validation` snapshot inside the project's
   own space, by default** (the `checks_ref` seam in `hooks/_ticket_state.py` resolves to
   `(space=<project>, snapshot=planning-validation)`) — separate from the `prd-<project>` snapshot this
   intake writes; ticket/plan writes are unaffected. **Override — slash argument ONLY** (no env seam):
   `/af-intake --checks-space=<space[:snapshot]>` reads the checklist from a different `(space, snapshot)`
   this run (pass a `checks_ref=(space, snapshot)` override into the resolve call). The extensible lenses
   live ONLY in Praxis (added via Amend mode, C2, which must write them INTO the project space's
   `planning-validation` snapshot); a lens added there is enforced on the next plan with no code change.
2. **PIN the coverage contract** — `pin_requirements(plan_subject, lenses)`: every lens is now a
   requirement this plan MUST cover.
3. **SYNTHESIZE a covering validation per lens** — for each lens, author a concrete, runnable validation
   that PROVES the plan satisfies it over the Praxis plan facts (its `run` is a check/query whose exit
   code is the signal; it `covers` the lens id), then `pin_validations(plan_subject, [...])`. E.g. for
   `external-service-provider-decision`: a check that enumerates every external-service-dependent
   requirement and asserts a named provider decision exists (B4 is HOW you cover this lens); for a
   states lens: every screen requirement has loading/empty/error; for a metric-definition lens: every
   surfaced metric carries an exact definition. A lens whose `applies_when` this product doesn't meet is
   covered by a validation that records *why it's N/A* (its pass), never silently skipped.
4. **RUN + RECORD** — run each validation; `record_validation_pass(plan_subject, vid, passed)`.
5. **CLOSE THE GAPS** — `coverage_gap(plan_subject)` must be EMPTY (a lens with no covering validation =
   the plan is not validated) AND `all_validations_passed(plan_subject)` True. A FAILING lens validation
   is a real plan hole: **admit the requirement that satisfies it, or — unattended — record the
   low-regret forced-decision default as an episode** (B4), then re-run that validation. The plan is NOT
   blessed until every lens is covered AND passes.

This makes planning validation deterministic and identical in shape to build validation: **what the
eval's depth scorer measures (lens × whole-plan) is exactly what this gate enforces** — add a lens to
Praxis and it becomes both an eval hole and an intake gate failure, with no code change.

## B4 — Technical architecture sweep (end-to-end) → written into Praxis

Behavioral requirements describe *what* the product does; they routinely leave the *how* — the
cross-cutting technical architecture — unspecified. This sweep forces every project-wide technical
decision to be made explicitly (or consciously deferred), so the build never quietly invents an
architecture nobody chose.

**Derive the decisions dynamically — there is NO fixed list.** Enumerate what *this* system needs to be
buildable from the doc, the admitted requirements, and the *kind* of software it is. A web app differs
from a CLI, an ML/data pipeline, a game, an embedded device, or a library. *Illustrative only* (a typical
web app): auth + authz, data store + migrations, backend stack + API style, frontend framework + styling
+ build tooling, hosting/deploy + CI + environments, secrets/config, external services, testing + the
verify oracle, observability, data-privacy. These are prompts, not the list.

**Named, non-skippable decision — external-service providers (the forced provider decision).** Whenever
ANY requirement implies an external service (email/SMTP or transactional email, SMS, push, payments,
object/file storage, geocoding, …), the concrete **provider/transport is a forced decision**: choose it
(per decision mode — in **Collaborate** push the user, in **Autonomous** take a low-regret default +
`praxis_record_episode` and flag for override at B9), record how it is configured/
secret'd, AND require a working **dev/local transport that surfaces the side effect** (e.g. logs the reset
link). A "sends email" capability with no chosen transport is the canonical planning failure — the
password-reset link never delivered because nothing was decided. Surface the **managed-vs-custom fork**
explicitly, because a managed platform may bundle the service and remove the standalone choice: e.g. for
auth credential emails (password reset, verification), **AWS Cognito** (or a similar managed auth
provider) ships this email built-in — choosing it eliminates the separate email-transport decision,
whereas custom/self-rolled auth additionally requires choosing + wiring a standalone transport (SES,
Postmark, SMTP). Present that fork to the user rather than silently defaulting. The
`external-service-provider-decision` planning check (pulled in B3) enforces this on every plan; leaving it
unchosen is an anti-masking violation.

**Each decision becomes durable Praxis state.** Resolve each like an underspecified requirement (doc →
mounted conventions → low-regret default + `praxis_record_episode` → ask → defer) and persist it: write
the chosen decision **into the requirement(s) it governs** (`praxis_edit_fact`) or as a first-class
architecture requirement (`praxis_add_insight(category="requirement", ...)`), and log rationale +
alternatives as an episode. None may be silently skipped; a default may never paper over a genuine owner
fork.

## B5 — Test strategy is mandatory (derive the layers for THIS system)

A plan with no test strategy (or one that skips a layer this platform lives or dies on) is exactly the
silent gap mechanical checks wave through. An explicit, **platform-appropriate, automated test strategy +
CI is a MANDATORY outcome of every audit**. **Derive the right LAYERS for THIS system; there is NO fixed
checklist.** *Illustrative only:* a **library** → unit + public-API/contract tests + semver-aware release
CI; a **web app** → unit + integration + e2e on critical flows + merge-gating CI/CD; a **mobile app** →
unit + integration + UI/e2e on a real device or simulator + build/sign CI (the device/simulator layer is
the one a generic plan silently omits); a **CLI** → unit + integration + a packaging/install smoke test;
a **data/ML pipeline** → unit + data-contract/schema tests + pipeline integration + eval gates on model
quality.

**Persist each chosen layer + the CI/CD setup as Praxis state with a BINARY acceptance condition** — the
same bar as any requirement. The natural home is a **live validation check** per layer (`category="check"`,
an applicability predicate, a binary criterion) and/or a first-class testing requirement. A layer with no
binary, CI-enforced condition is not a strategy; it's a hope, and the build gate treats the requirement it
should have produced as missing (incomplete).

**Then run the completeness critic — the dynamic pushback.** Dispatch an *independent* cold-eyes
sub-agent whose only job is: *"to actually build this system, what technical decisions are still
unmade?"* It reads the doc + requirements + the decisions already written and names what's missing for
**this** product, and **explicitly interrogates the test strategy** ("is it COMPLETE and APPROPRIATE for
THIS platform?"). Write what it surfaces into Praxis, resolve those too, and **loop until it returns
nothing new** (loop-until-dry). It does not sign off while a platform-appropriate layer or the CI gate is
missing or unenforced.

## B6 — Cross-requirement coverage + depth (the mechanical gate)

The mechanical half is executable, not eyeballed:
- **Bidirectional coverage (H14, surfaces)** — `praxis_surface_coverage(project, scope="mvp")` must come
  back with both `uncoveredSurfaces` AND `uncoveredRequirements` empty (or each exception justified).
- **Dangling concept reference (H14, concepts)** — every domain concept a requirement *references* is
  *defined* by some admitted requirement or explicitly declared out of scope. (This is the hole that let
  an undefined "team streak" in: R2 referenced it, nothing defined it.) Tag each requirement with the
  concepts it `defines`/`references`.
- **The plan_gate** — run **`agent_factory.plan_gate.evaluate_plan(requirements, project="<project>")`**,
  passing the project explicitly, with each requirement carrying its `source="prd-<project>"`, and report
  its `reasons`. The `project=` argument is mandatory: with it the gate requires every requirement's
  `source` to equal `prd-<project>` exactly (the `R-HAS-SOURCE` rule), so a source-less or mis-scoped plan
  is mechanically **rejected** — the drift that went uncaught when the gate ran without project+source. It
  also checks binary-acceptance present, no-vague-term, no dangling concept reference, and the
  **build-order DAG** (Step 5): `R-NO-DANGLING-DEP` rejects a `depends_on` naming a requirement not in the
  plan, and `R-NO-DEP-CYCLE` rejects a dependency cycle — both would otherwise surface only as a run-time
  stall. Covered by `evals/cases/plan_gate/` (run `pytest tests/test_eval_cases.py`); add a `case.yaml`
  whenever a fresh gate edge case is found, so the gate's coverage compounds.

## B7 — The plan-review panel (holistic cold-eyes, the emergent layer)

The per-requirement audit (B1–B6) judges each item against its own contract. It does NOT stand back at
the **whole artifact at once**, so a defect that is *correct per item but wrong in aggregate* sails
through — e.g. a **source/scope contract inconsistency** (every requirement well-formed, but `source`
convention and scope boundary disagree across the set) or an **unsatisfiable build target** (a manual /
post-MVP item routed into the automated build set, individually plausible, collectively impossible).
These are *emergent*: invisible per-requirement, obvious to a diverse panel reading the whole fact-set.
So after the audit, convene the panel over the whole `prd-<project>` fact-set + tech decisions.

**compound-engineering is the DEFAULT, required panel** — not "use them if installed." Its ce-* reviewer
agents are the default cold-eyes review panel, spawned via the **Agent tool** (each a different reviewer
with its own lens; they did not write the plan, so they challenge harder and disagree). **Do not reinvent
reviewers.**

**PRESENCE CHECK first.** Verify the ce reviewer agents resolve via the Agent tool / `/code-review`.
- **Present** → spawn the panel.
- **Absent** → do NOT proceed and do NOT record a panel-ran episode (this scope has not been reviewed);
  surface the remediation (`claude plugin install compound-engineering@compound-engineering-plugin` /
  `/reload-plugins`). A missing panel is a **blocked review**, never a silent pass — distinct from a
  deliberate, recorded skip.

**Lenses (≥1 independent reviewer each, over the WHOLE set — not one requirement):**

| Lens | ce subagent type | Catches (e.g.) |
|---|---|---|
| contract / convention coherence | `ce-coherence-reviewer` | the source/scope inconsistency |
| architecture / feasibility | `ce-feasibility-reviewer` | the unsatisfiable manual / post-MVP target |
| scope / strategy | `ce-scope-guardian-reviewer` | scope creep, mismatch to STRATEGY |
| security | `ce-security-lens-reviewer` | missing authz/PII/secret decisions across reqs |
| completeness / product | `ce-product-lens-reviewer`, `ce-design-lens-reviewer` | gaps between requirements, unmet user journeys |

**Emit each finding into Praxis** (deduped — merge multiple reviewers' reports of the same issue into one
finding, carrying the strongest severity, BEFORE emitting): a **missing/changed requirement** → a new
**ticket** (`meta.build_state="incomplete"`, identity tags/surfaces, NEVER an authored check list); a
**"this must hold across the plan" rule** → a **check** (its own `meta.applies_to` predicate, which
RESOLVEs onto matching tickets later). Create via `docs/af-memory-policy.md`. There is no separate review gate: the one
`build_completeness` gate already refuses "done" while any emitted finding's ticket/check is incomplete.

**Skippable — explicit policy, never silent.** Compute a size signal (new/changed requirements since the
last blessed snapshot; `small` = `value <= threshold`, default 20). small AND attended → propose skip to
the human, who confirms → record a skip episode; no confirm → run the panel. small AND unattended →
auto-skip → record a skip episode so the heuristic compounds. NOT small → review is mandatory (a human MAY
force-skip only with an explicit recorded reason). Every skip leaves a reason in a Praxis episode; a skip
is the *absence* of a panel-ran episode plus the *presence* of a skip episode — never a fabricated
panel-ran assertion.

## B8 — Record the panel-ran episode (the only residue)

When the sweep is done — every requirement challenged, every planning check applied, every architecture
decision and test layer written into Praxis, the plan-review panel run — record **ONE**
`praxis_record_episode` asserting the audit + panel ran for `prd-<project>`:

```
praxis_record_episode(
  text = "af-intake audit+panel ran for prd-<project>: challenged <N> requirements; "
         "lenses fired=[...]; near-dups reconciled=[...]; arch decisions written=[...]; "
         "test layers=[...]; tech-decision critic loop-until-dry passes=<k>, missing=[]; "
         "plan panel composition=[...], findings emitted=<m>",
  outcome = "succeeded",
)
```

This is the **only** thing the validation leaves behind besides the graph edits. It is a tiny
assertion-of-record so the act of auditing **cannot be silently skipped** — NOT a findings state machine,
NOT a status manifest.

## B9 — The human clears the gate, then bless

Planning is **human-gated** — there is no planning Stop hook. Report status against each condition; never
declare it yourself. The human may bless only once ALL hold, checked **live from Praxis**:
- Every requirement maps to ≥1 binary acceptance condition (or is an explicitly-deferred owned decision).
- Every requirement carries `source="prd-<project>"` (`R-HAS-SOURCE`).
- `plan_gate.evaluate_plan` passes over the live requirements (B6).
- Zero unresolved contradictions; no dangling concept reference (H14); bidirectional surface coverage
  clean (B6).
- Every can't-miss failure class addressed-or-excluded with logged rationale (data loss, auth bypass,
  irreversible action, silent partial failure).
- The **panel-ran episode exists** (B8) — the audit and plan-review panel actually ran.

**Stop by information-gain, not exhaustion.** When the next question's expected information gain is low
and the gate is reachable, say so and STOP asking. Beware the under-specification trap: zero
contradictions on a thin plan is not "done," it's "nothing was claimed yet."

When the human clears the gate: **`save_snapshot(space="<project>", snapshot="prd-<project>")`** (PRD-only
— mounts aren't carried). This dumps working memory into the `prd-<project>` snapshot in the project's
space. Render the prose PRD from the facts for human review. This snapshot is the durable plan; the build
loop consumes it later. Editing later = `load_snapshot(space="<project>", snapshot="prd-<project>",
mode="replace")` → edit → re-save, or use Amend mode. (The `prd-<project>` snapshot is MUTABLE — the
build loop reads and writes ticket state on it directly; read-only applies only to mounts and load/dump
copy semantics.)

---

# PART C — AMEND MODE (one additive change to an existing plan)

Amend mode **replaces the four former check skills** (af-add-validation, af-add-planning-check,
af-redo-ticket-add-validation, af-redo-plan-add-check). It makes **one additive Praxis write** to an
already-hardened plan — a **check** (validation or planning, C1/C2), or a **new requirement ticket** the
plan is simply missing (C0). Because **checks and tickets resolve by query** and **completion is gated on
them**, matching work re-enters the incomplete set automatically; there is no separate "redo" skill.
Optionally force `build_state="incomplete"` on the matched tickets for *immediacy* (so the next build run
picks them up at once instead of merely on their next natural RESOLVE).

**Amend is additive, never a content edit.** C0 adds a requirement that *did not exist*; it does not
rewrite an existing ticket's statement/acceptance — that is a re-baseline FULL INTAKE (Step-3
`on_conflict="surface"` edit). The `on_conflict="surface"` guard on the C0 write is exactly what catches
"this 'new' ticket is actually an edit of an existing one" and routes you back to FULL INTAKE.

Praxis is a HARD dependency: if the write cannot reach Praxis, **fail closed** (error and stop) — never
fall back to a file. A check is **declarative and read-only during builds** — edited only on an explicit
user request like this one, never as work completes. A check **owns its own applicability predicate**
(`meta.applies_to` is an **array** of tags, `["*"]` = always; optionally `meta.applies_when` / bound
surfaces); it **never** names specific tickets, and no ticket carries an authored list of its checks.
WHICH checks apply is the fresh RESOLVE query (tag ∪ surface ∪ semantic) at the point of use. You only
write the predicate; the build (for validation) or the audit (for planning) resolves it.

**Write the check INTO the project space's check snapshot that RESOLVE reads, or it is invisible.**
Checks are now **per-project**: resolution reads them from a dedicated snapshot inside THIS project's
space, so an amended check only takes effect if it lands at the right `(space, snapshot)`:
`scope="validation"` → `(space=<project>, snapshot=building-validation)` (read by af-build);
`scope="planning"` → `(space=<project>, snapshot=planning-validation)` (read by af-intake's audit). Write
the check into that snapshot via the snapshot-bound write path (`praxis_select_space("<project>")` sets
the client's space default, then target the `building-validation` / `planning-validation` snapshot on the
write). Writing the check into the `prd-<project>` snapshot instead leaves it unresolved under the check
seam — a silent no-op. (Confirm tenancy first per `docs/af-memory-policy.md` §0.)

> **Per-project, not global.** `building-validation` (renamed from the old global `coding-validation`) and
> `planning-validation` are SNAPSHOTS in each project's own space — there is no single global checks
> space anymore. A check authored here governs only this project; re-seed each project's check snapshots.

Decide the **kind** from the request: a **new ticket** the plan lacks (C0), a **validation check**
(C1), or a **planning check** (C2). The snapshot-targeting prose just above governs C1/C2 (checks live
in the per-project check snapshots); C0 instead writes the requirement into the `prd-<project>` snapshot
— see C0 for its own target.

## C0 — New ticket (a genuinely-new requirement, nothing to edit)

When the amendment is a **requirement the plan is simply missing** — not a rule over existing work, and
not a rewrite of an existing ticket — admit it as a ticket the same shape Full-intake and the plan panel
mint: **identity only** (tags, surfaces, semantics), NEVER an authored check list. This is the one Amend
path that writes the **`prd-<project>` snapshot** (where tickets live), not a check snapshot.

Do it through the load→edit→save cycle so the add runs its dedup/contradiction net against the existing
plan (a bare snapshot-targeted write has nothing to compare against):

1. `load_snapshot(space="<project>", snapshot="prd-<project>", mode="replace")` — pull the current plan
   into working memory (confirm tenancy first per `docs/af-memory-policy.md` §0).
2. Admit the requirement, carrying the project identity + incomplete state:

```
praxis_add_insight(
  insight  = "<requirement — ONE semicolon-joined sentence>",
  source   = "prd-<project>",
  category = "requirement",
  meta     = { "build_state": "incomplete", "tags": ["<class-tag>", ...],
               "scope": "mvp | post-mvp", "surfaces": ["<screen-id>", ...] },
  on_conflict = "surface",
)
```

3. **`on_conflict="surface"` is the guard**, never `raw=True`/`auto_resolve`: if the "new" requirement
   near-dups an existing one, it surfaces as a contradiction (`praxis_get_contradictions`) instead of
   silently minting a twin. Settle it with `praxis_resolve_contradiction`. If it turns out to *be* an
   existing ticket needing new wording, you are in the wrong path — that content edit belongs to FULL
   INTAKE (Step-3), not Amend.
4. If it renders a surface, bind it: `praxis_bind_surface(requirement_id, screen_id, ...)` (the `renders`
   edge) so surface-bound checks resolve onto it at build.
5. `save_snapshot(space="<project>", snapshot="prd-<project>")` — persist. A requirement left only in
   working memory (or written into a check snapshot) is invisible to the build — the requirement analog
   of the wrong-snapshot no-op above.

**No C3 regression step.** A new ticket is born `build_state="incomplete"` with `source="prd-<project>"`,
so it enters `incomplete_requirements` for free — there is nothing pre-existing to re-open. Confirm with
`praxis_incomplete_requirements(<project>)` (BARE name). Never author a check list onto it — which checks
apply is the build's fresh RESOLVE query (tag ∪ "*" ∪ surface), same as every other ticket.

> **When C0 vs. re-baseline?** One or a few clearly-additive missing tickets against an otherwise-stable
> plan → C0. A wave of changes, edits to existing requirements' content, or anything the audit/panel
> should re-examine as a set → re-baseline FULL INTAKE. C0 does NOT re-run the audit or plan panel, so
> reserve it for additions that don't move the plan's coverage story.

## C1 — Validation check (a build-time "must pass before done" rule)

Infer from the one-liner: **criterion** (the fact text, e.g. "login works end-to-end against the live
service"); **run** (the command that proves it, non-zero exit = fail — discover the repo's real e2e
command, don't assume); **applies_to** (an ARRAY of requirement-class tags, e.g. `["auth"]`; `["*"]` =
every ticket); optional **applies_when** / **surfaces**. Write via `docs/af-memory-policy.md`:

**`applies_to` hygiene — this is what makes frontend/backend separation AUTOMATIC (mechanical, not LLM
judgment):**
- **Universal gate** (typecheck, build, lint, test — must run on EVERY ticket) → `applies_to: ["*"]`.
  The wildcard lane resolves it onto every ticket, including tag-less/backend ones.
- **Context gate** (a rule tied to a domain class — auth, notifications, seed) → a **specific tag**
  (`["auth"]`, `["notifications"]`), so it lands ONLY on tickets carrying that tag.
- **Frontend/UI gate** (Playwright E2E, visual-render, axe a11y, no-console-errors) → **surface-bind it**
  (`meta.surfaces` + the `renders` edge below), NOT `["*"]`. A backend-only ticket renders no surface, so
  a surface-bound UI check **can never resolve onto it** — the guarantee is structural, not the build
  agent's N/A judgment. Do **not** author a UI check as `["*"]`+`applies_when` and lean on the agent to
  skip it; that is the soft filter this convention replaces.

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

Idempotent on `meta.check_id`: if one exists, `praxis_edit_fact` it rather than duplicating. If it binds
to surfaces, also create the `renders` edge (`praxis_bind_surface(check_id, screen_id, ...)`) so the
surface lane of RESOLVE finds it. The check takes effect on the **next build run** with no further action:
at each ticket's RESOLVE step `resolve_validation_requirements` picks it up by tag/surface match, `pin_requirements` writes it
into that ticket's `meta.pinned_checks` contract, and the ticket is FINISHED iff every pinned check passed.

## C2 — Planning check (a "how to plan" lens the audit must apply)

Infer: **criterion** (the consideration, e.g. "any app with user accounts needs a credential-recovery
(password reset) flow"); **angle** (a short lens label — `auth`, `states`, `security`, `data-lifecycle`,
`rollback`, `privacy`); **applies_to** (`["*"]` for always, else the gating tags). Write via `docs/af-memory-policy.md`:

```
praxis_add_insight(
  insight  = "<criterion>",
  source   = "planning-checklist",
  category = "check",
  scope    = "planning",
  meta     = { "check_id": "<stable-slug>", "applies_to": ["<tag>", ...] | ["*"], "angle": "<lens-label>" },
  on_conflict = "surface",
)
```

Keep `source="planning-checklist"` (the lens-library identity) but land the check in THIS project's
`planning-validation` snapshot (`space=<project>`) — the checklist is no longer a single global library.
The active `scope="planning"` checks in that snapshot ARE the planning checklist Part B pulls (B3). It
takes effect on the **next plan** for this project: the audit queries the project's active planning checks
and must close every lens whose `applies_to` matches.

## C3 — Re-enter the work (the "redo" that needs no redo skill)

(C0 needs none of this — a new ticket is born incomplete and already sits in `incomplete_requirements`.
This step is for C1/C2, where the change is a check over *existing* work.)

The amended check **automatically** re-enters matching work, because completion is gated on checks
resolved by query — there is nothing to re-author. For **immediacy**, optionally force the matched work
incomplete now:

- **Validation check** → resolve the regression set by the SAME query the build's RESOLVE uses (never by
  writing anything onto the check or hand-listing checks on tickets): **tag match** (requirements whose
  `meta.tags` intersect the check's `applies_to`, via `praxis_facts_by(category="requirement", meta=...)`),
  **surface match** (`praxis_requirements_for_surface` / `praxis_checks_for_surface`), and any **explicit
  ids** the user named. If a target requirement lacks the class tag, add it to its `meta.tags` via
  `praxis_edit_fact` (ticket **identity**, not a check list; preserve all existing meta). Then regress each
  matched ticket by STATE only: set `meta.build_state="incomplete"` (merge meta, preserve everything else)
  AND `praxis_record_outcome(fact_id, success=False)` so it re-enters `incomplete_requirements`. A
  never-built ticket is already incomplete — leave it. **Do NOT touch `meta.pinned_checks`, the claim
  lease (`claim_owner`/`claim_at`/`claim_heartbeat_at`/`claim_lease_ttl`), or the check fact** — the
  build's RESOLVE/PIN steps re-pin the fresh check set at the next ticket start. Confirm with
  `praxis_incomplete_requirements(<project>)` (BARE name).
- **Planning check** → re-arm the audit via the panel-ran episode model. Adding a new planning check makes
  the latest panel-ran episode STALE (it covered a checklist that no longer includes this lens). Record a
  re-arm episode so the audit cannot be treated as still-passed:

  ```
  praxis_record_episode(
    text="Re-armed prd-<project> plan audit: planning checklist extended with check <check_id> (<angle>); prior panel-ran is stale and the audit must reconvene to close the new lens.",
    outcome="pending",
  )
  ```

  The human's planning gate (B9) is satisfied only by a panel-ran episode covering the CURRENT active
  checklist; because the new check post-dates the last panel-ran assertion, the plan is no longer
  blessable until Part B reconvenes and closes the new lens for every requirement it bears on.

**Report**: the check you wrote (id, kind, applies_to, run/angle, criterion); the tickets regressed (id +
text) and that they now show incomplete, OR that the audit is re-armed. If any Praxis call failed, report
the failure — never claim success.

---

## Never

- **Never write or read a `.factory/*.json` file** — no candidate manifest, no findings state machine, no
  validation/checklist file, no audit manifest, no local build/validation state of any kind. Candidates,
  bindings, checks, and all state live in Praxis — the single source of dynamic truth. JSON is static
  config only.
- **Never proceed if Praxis is unreachable** — fail closed: crash and stop. Do not buffer work to a file.
- **Never treat the wireframe as a behavioral source of truth** — behavior comes from the doc; the
  wireframe contributes surfaces, states, and the coverage cross-check.
- **Never emit a multi-sentence `content`/statement** — one semicolon-joined sentence (fragmentation
  workaround).
- **Never author a list of checks onto a candidate/ticket, and never pre-bind a check onto a requirement**
  — a ticket carries identity (tags, surfaces, semantics); which checks apply is a fresh query resolved
  later (RESOLVE at build, the audit at plan), never pre-bound here.
- **Never admit a candidate without `source="prd-<project>"`** — that is the project identity the
  completeness query and the `R-HAS-SOURCE` gate filter on; `meta.scope` (mvp/post-mvp) is NOT a
  substitute.
- **Never write a planning fact on `auto_resolve`** — it silently rejects the loser and hides the
  conflict; incremental edits use `on_conflict="surface"`, fresh bulk uses `raw=True` (with the audit as
  the contradiction net).
- **Never treat a write timeout as a failure** — the write usually landed; **read back** (`list_graph` /
  `get_context`) before retrying, or you'll create duplicates.
- **Never `clear_graph` without a confirmed save-before-clear snapshot**, and never let mounted reference
  knowledge leak into the `prd-<project>` snapshot.
- **Never let the agent that drafted a requirement be its only skeptic** — the audit and the plan panel
  are cold-eyes sub-agents / ce-* reviewers.
- **Never "close" a challenge or near-dup with a free-text note alone** — close it with the Praxis write
  that fixes it (edit/add the requirement, declare the check, reject/narrow the duplicate + re-save the
  snapshot) or a recorded dismissal/deferral episode.
- **Never bless a plan** while any audit-surfaced requirement/check is still incomplete, any open
  challenge is unresolved, the panel-ran episode is missing, or `plan_gate` does not pass — and never with
  no automated test strategy, a platform-required test layer missing, or a CI gate lacking a binary
  condition.
- **Never pass on a missing ce panel** — if the compound-engineering reviewers aren't available, record NO
  panel-ran episode and surface the remediation; absence is a blocked review, never a silent skip.
- **Never skip the audit or panel silently** — every skip records a reason as a Praxis episode; the
  panel-ran episode is what proves it ran.
- **Never pass the prefixed project name** to the completeness/incomplete endpoints — `prd-<project>`
  becomes `prd-prd-<project>`, returns EMPTY, and fakes completeness. Pass the BARE name.
- **In Amend mode: never touch `pinned_checks` or the claim lease, and never build, fix, or run the
  check** — Amend only declares the rule (C1/C2) or admits a new requirement ticket as identity + state
  (C0), and (optionally) regresses ticket STATE; the build owns RESOLVE, CLAIM, PIN, and per-check pass
  records.
- **In Amend mode: never edit an existing requirement's content** — C0 is strictly additive (a ticket
  that did not exist). A rewrite of an existing statement/acceptance is a re-baseline FULL INTAKE, not an
  amend; the `on_conflict="surface"` guard on the C0 write exists to catch this and bounce it there.

## Compounding

This skill is where the factory *learns its own blind spots* and where extraction errors get cheapest to
kill. When a correction reveals a class of miss (a requirement the doc stated but extraction dropped, a
wireframe state with no rule, a recurring doc↔wireframe clash, an emergent defect a per-item check
structurally can't see), tighten the relevant pass above and record an `docs/af-memory-policy.md` learning so the next
intake starts from a stricter extractor. A lens that keeps firing on a defect class is a signal to harden
it into a declarative check via **Amend mode** (validation or planning), so the next plan and the next
build catch it per-item for free. Before finishing a full intake, also: append new ambiguity patterns to
the `general-pool` library; offer to **promote** genuinely-new cross-project invariants into the
`constitution` snapshot; and write decision records (episodes) to the event log.
