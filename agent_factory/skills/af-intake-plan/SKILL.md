---
name: af-intake-plan
description: >
  The write-path and owner of the Praxis PLAN — the `prd-<project>` snapshot — and its blessing audit.
  One of THREE section-locked intake commands, each the sole writer of one canonical snapshot in the
  project space: this one writes ONLY the plan; af-intake-build-validation writes the `building-validation`
  checks; af-intake-plan-validation writes the `planning-validation` lenses. FULL INTAKE takes af-plan's
  messy exhaustive brainstorm doc (+ optional clickable wireframe), extracts candidate requirements and
  surface↔requirement bindings DIRECTLY INTO PRAXIS, hardens them (self-consistency, contradictions,
  dedup), then runs the planning audit — the cold-eyes architecture/external-service decisions,
  underspecification routing, cross-requirement gaps, coverage+depth checks, and the data-driven
  scope="planning" lenses (READ from the `planning-validation` snapshot) — convenes the ce-* plan-review
  panel, and hands the human a clearable gate that ends in save_snapshot(space="<project>",
  snapshot="prd-<project>"). As part of the audit it also DERIVES the build's known blind-spot guards —
  the tricky edge cases and every-site refactors af-build tends to silently drop — and authors each as a
  build-validation check by DELEGATING to af-intake-build-validation (it never writes the check section
  itself, so the single-writer lock holds; see B5b). AMEND (C0) adds ONE genuinely-new requirement TICKET the plan is simply
  missing; because tickets resolve by query and completion is gated on them, it enters the incomplete set
  automatically. Use when starting (or re-baselining) a project from a brainstorm/PRD + wireframe, or to
  graft a lone missing ticket onto an already-hardened plan. To add a CHECK — a build gate or a planning
  lens — use af-intake-build-validation / af-intake-plan-validation instead. (Amend adds; it does NOT edit
  an existing requirement's content — that is a re-baseline FULL INTAKE.)
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

**This skill's place in the methodology.** af-intake-plan runs UPSTREAM of the build loop and is the
**single owner of the Praxis PLAN** — the `prd-<project>` snapshot: it MINTS the tickets the loop later
FINDs and the surface↔requirement `renders` bindings the RESOLVE step and the wireframe→code build query
read. It also runs the **planning audit** — the cold-eyes challenge and the plan-review panel that gate a
plan before it is blessed — READING the `planning-validation` lenses (it does not author them; that is
af-intake-plan-validation). af-intake-plan writes only the plan to Praxis; it records build/claim/pass
state on nothing and writes ZERO side files. Its sibling **af-plan is now ONLY brainstorming/research** —
it produces a messy, exhaustive doc; af-intake-plan turns that doc into the hardened, blessed
`prd-<project>` snapshot.

**af-intake-plan writes ONLY the plan.** It is one of three section-locked intake commands, each the sole
writer of one canonical snapshot in the project space: **af-intake-plan → `prd-<project>`**;
**af-intake-build-validation → `building-validation`** (the checks af-build reads);
**af-intake-plan-validation → `planning-validation`** (the lenses this skill's audit reads). The server's
write-time section invariant enforces the split (a `category="check"` fact is refused in the plan), so
checks can never co-mingle with the plan even by mistake.

**Two entry modes (both write the plan):**
- **FULL INTAKE** (default; a fresh project or re-baseline) — extract → harden → validate/audit → panel
  → human gate → `save_snapshot`. Sections "Full intake" + "Planning validation / the audit" below.
- **AMEND (C0)** (an already-hardened plan) — add ONE lone new requirement TICKET the plan is simply
  missing; it enters the incomplete set by query. Section "Amend" (Part C) below. Amend is **additive
  only** — to change an existing requirement's content, re-baseline via FULL INTAKE. To add a CHECK (a
  build gate or a planning lens), use af-intake-build-validation / af-intake-plan-validation instead.

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

## Step 0d — Arm the planning marker (so the plan Stop hook enforces this session)

Before extracting, **stamp the planning marker**: `_ticket_state.stamp_planning(project, owner)`. This
writes a session-owned, heartbeated marker on the `prd-<project>` snapshot that ARMS the
`plan_completeness` Stop hook — from here until bless the hook blocks the planning turn from ending until
the plan mechanically blesses (B9). Re-stamp periodically to heartbeat it (the marker goes stale after
`DEFAULT_PLANNING_TTL_S`). It is CLEARED at bless (B9). A build session stamps a *run* marker, not this
one, so the two Stop hooks never cross-fire.

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
  confirmation). Drives the phase-gate split downstream. A **pure architecture-decision ticket is always
  `"manual"`** (B4's HARD RULE); the plan gate rejects an `architecture-decision` ticket left
  `verify="automated"` (`R-DECISION-NOT-END-STATE`).
- **`meta.surfaces`** — wireframe screen ids governed, or `["backend-only"]`. Seeds the `renders`
  bindings written in Step 4.
- **`meta.defines` / `meta.references`** — concepts, for the H14 dangling-reference gate.
- **`meta.depends_on`** — prerequisite requirement ids that must be `finished` before this one is
  buildable (the build-order DAG `af-build`'s `next_ready_ticket` walks). A best-draft now; the DAG is
  mapped and validated in **Step 5**. Empty for a requirement with no prerequisites. A prerequisite is a
  real **build** dependency only — **NEVER a pure architecture-decision ticket** (B4's HARD RULE); the
  plan gate rejects any edge whose target is tagged `architecture-decision` (`R-NO-IMPL-DEPENDS-ON-DECISION`).
- **`meta.scope`** — `"mvp"` or `"post-mvp"` (the tier tag only; NOT the project identity).
- **`meta.tags`** — identity tags (concepts / surfaces / semantics). A ticket carries identity, **NEVER
  an authored list of its checks**; *which checks apply* is a fresh query (tag ∪ surface ∪ semantic)
  resolved at build time. Tag honestly so that query resolves correctly. A **pure architecture-decision
  ticket carries the NEUTRAL tag `["architecture-decision"]` ONLY** — never an impl-domain tag (`cdk`,
  `token-verification`, `frontend`, `database`, …), so it resolves ZERO implementation checks (B4's HARD
  RULE).

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

**Stamp the `contradictions_checked` marker (positive evidence detection RAN, KTD4).** An empty
`praxis_get_contradictions` queue is NOT evidence of consistency — the raw-bulk path skips detection, so
"empty" can mean "never ran". The `plan_completeness` hook therefore requires a `contradictions_checked`
marker on the planning marker fact IN ADDITION to an empty queue. Once you have actually RUN detection
over the snapshot (the surface-mode conflict pass, or the audit's contradiction net for a raw-admitted
set), set `contradictions_checked=true` on the planning marker; a raw-bulk write that has not yet run the
net must leave it **`false`** honestly. The gate blocks until it is `true` AND the queue is empty.

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

**A pure architecture DECISION is NEVER a prerequisite** (B4's HARD RULE). Every relation above is a real
*build* dependency — something that must physically exist first. An architecture decision is a **choice**,
not a build artifact: it is baked into the IMPL ticket's own content/acceptance, not listed as its
`depends_on`. Making a decision a prerequisite is the Auth0→Cognito wedge (B4's worked example) — the
decision sits topologically FIRST yet can only go green LAST, so the build's ready frontier is decisions
nothing can satisfy and the run wedges. The plan gate rejects any `depends_on` edge whose target is tagged
`architecture-decision` (`R-NO-IMPL-DEPENDS-ON-DECISION`).

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

**Granularity — a non-independently-greenable change set is ONE ticket, never split peers.** `depends_on`
only orders tickets that each stand alone; it does NOT license splitting a single indivisible change across
tickets that each need a *sibling's* edit to compile or pass. The universal-ish build gates
(`backend-build`, `backend-vitest`) pin on **EVERY** backend ticket, so each isolated worker must leave the
**WHOLE** backend compiling and its tests green with only its own slice landed. If a set of changes is not
independently compilable/greenable, author it as **ONE ticket** (or an explicit ordered chain) — never as
peer tickets that each red the shared whole-repo gate until a sibling lands. Worked example: the R7
verifier rewrite needs R8's identity change to keep the suite green — those two must be **ONE ticket or an
ordered chain**, not two peers that each red `backend-vitest`.

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

## B1c — Sign the contract (a SEPARATE evaluator, adversarial, then signs)

The planner **never grades its own contract.** After extraction + the B1 challenge, dispatch a
**separate, read-only evaluator sub-agent** (the read-only retrieval sub-agent contract) whose only job
is to **falsify / cut / merge / tighten** the candidate testable assertions — then **sign** the result.
The evaluator records ONE `contract-signed` episode carrying the assertion COUNT and its ACTIONS:

```
praxis_record_episode(
  text = "contract-signed for prd-<project>: evaluator adversarially reviewed and signed the "
         "testable-assertion contract",
  # meta.episode payload mirrors contract_signature.build_signed_payload(n, actions, signer):
  #   kind="contract-signed", n_assertions=<N>, actions={cut,merged,added}, signer="evaluator"
  outcome = "signed",
)
```

**Gate on ACTIONS, not a padded count (anti-Goodhart, KTD3).** The HARD bless predicate (`R-CONTRACT-SIGNED`,
enforced by `plan_gate_check` at B6/B9) is "**signed AND ≥1 real evaluator action recorded**" — a
signature over an unchanged draft (all-zero actions) does NOT pass. The count is recorded for visibility
only; a requirement below ~10 concrete assertions is **FLAGGED for the evaluator** (`below_floor`), never
hard-rejected.

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
   own space, by default** (the typed `project_ref` seam in `hooks/_ticket_state.py` resolves to
   `(space=<project>, snapshot=planning-validation)`) — separate from the `prd-<project>` snapshot this
   intake writes; ticket/plan writes are unaffected. **Override — slash argument ONLY** (no env seam):
   `/af-intake-plan --checks-space=<space[:snapshot]>` reads the checklist from a different `(space, snapshot)`
   this run (pass an `override=(space, snapshot)` pair into the resolve call). The extensible lenses
   live ONLY in Praxis (added via `af-intake-plan-validation`, which writes them into the project space's
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

### HARD RULE — a pure architecture DECISION is modeled as a decision, NEVER as a disguised implementation ticket

A pure architecture decision ("we use AWS Cognito, not Auth0"; "the transport is Postmark") is a **CHOICE**,
not a build target. Model it **EITHER** as an owned-decision / episode fact (**PREFERRED** —
`praxis_record_episode`, so it never enters the build set at all), **OR** — if it is admitted as a
`category="requirement"` ticket — it **MUST** obey **ALL** of the following. This is stated once, here,
where architecture decisions are produced; the Step-1 field rules and the Step-5 `depends_on` section
cross-reference it.

- **NEUTRAL tag ONLY** — `meta.tags = ["architecture-decision"]`, **NEVER** an impl-domain tag (`cdk`,
  `token-verification`, `frontend`, `database`, …). The neutral tag resolves **ZERO** implementation
  checks, so the decision ticket carries no build gate it cannot itself satisfy.
- **`meta.verify = "manual"`** — a human **accepts or overrides** the decision at the gate (the
  flagged-default override point of Step 0c / B9). A decision is never an automated end-state.
- **DECISION-LEVEL acceptance** — `"<X> is the accepted design decision"`, **NEVER** an implementation
  end-state (`"cdk synth emits three UserPools"`, `"no @auth0/auth0-react import remains"`) that
  duplicates a downstream implementation ticket.
- **NEVER a `depends_on` prerequisite of its own implementation ticket.** The decision is baked into the
  **IMPL ticket's content/acceptance** instead. Impl tickets depend ONLY on real build prerequisites (data
  producer → consumer, entity → its surfaces, shared infra → first user — Step 5), never on a decision.

**The plan gate mechanically enforces this.** `agent_factory.plan_gate.evaluate_plan` **REJECTS** the
malformed shape via **`R-DECISION-NOT-END-STATE`** (a ticket tagged `architecture-decision` must be
`verify="manual"`, never an automated end-state) and **`R-NO-IMPL-DEPENDS-ON-DECISION`** (no ticket may
`depends_on` an `architecture-decision` ticket). The gate enforces these properties **only on a ticket
that IS correctly tagged `architecture-decision`** — a decision mis-tagged with impl-domain tags is not
even seen as a decision, which is exactly the mis-modeling this rule prevents up front. Once a decision is
tagged correctly, the rest is enforced for free; the human's only job is to **model decisions as
decisions**.

**Worked example of the anti-pattern — the Auth0→Cognito wedge (tickets D1–D5).** The Auth0→Cognito
migration was planned as five decision tickets that EACH carried **impl-domain tags** (`cdk`,
`token-verification`, `frontend`), had **impl end-state acceptance** (`"cdk synth emits three UserPools"`,
`"no @auth0/auth0-react import remains"`), and were **`depends_on` prerequisites of the very impl tickets
that would satisfy them.** So they sat topologically **FIRST** (everything depended on them) but could
only go green **LAST** (only the downstream impl work satisfies an impl end-state). A fresh `/af-build`'s
entire ready frontier was therefore decisions that **NOTHING could satisfy** — the run wedged immediately.
Modeled correctly, D1–D5 collapse to a single **`verify="manual"`, `["architecture-decision"]`-tagged**
ticket (or a recorded episode) with **decision-level acceptance and no dependents**, and the real
UserPool/token/frontend work becomes **ordinary impl tickets** that bake the chosen design into their own
acceptance and depend only on genuine build prerequisites.

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

## B5b — Guard the build's known blind spots (author build-validation checks via af-intake-build-validation)

A hardened requirement whose acceptance *names* an edge case still does not make the build PROVE it:
af-build self-reports "done" against the acceptance floor unless a declared **check** forces the case.
Two failure classes survive every per-requirement audit yet are exactly what the build agent silently
drops — this step turns each into a runnable build gate so "done" is gated on the behavior, not the
agent's word:

1. **The tricky edge case** — the empty / offline / invalid / boundary / race / idempotency / partial-
   failure case that is easy to code past. B1's adversarial lenses SURFACE these into requirement
   acceptance; here you promote the highest-risk ones to a **runnable check** whose `run` is a test that
   exercises exactly that case, so a build that skips the case exits non-zero and re-opens the ticket.
2. **The every-site change (huge refactor / cross-cutting sweep)** — a change that must land in EVERY
   file / call-site (a provider swap, an API/method rename, a config-key migration, a banned-import
   purge). The build agent runs the sweep and misses N of M sites; per-requirement review never sees the
   whole set, and a half-done rename often still COMPILES GREEN (the dangerous kind). The guard is a
   **scan/grep check that asserts ZERO stragglers remain** — `! grep -rq '<old pattern>' <scope>` (or a
   codemod/lint rule) — so an incomplete sweep exits non-zero. This is the correct home for the
   "no `@auth0/auth0-react` import remains" assertion that **B4's HARD RULE forbids on a decision ticket**:
   model it here as a build-validation scan check, not as an impl end-state on the architecture decision.

Neither guard is a requirement (a check is not a plan fact) and this skill **may not write the
`building-validation` section itself** (section-lock; the server refuses a `category="check"` fact in
`prd-<project>`). So the split of labor is: **DERIVE the guard set HERE** — the plan-side judgment of
WHICH cases and WHICH sweeps are high-risk — then **author each guard by RUNNING af-intake-build-validation**
(the sole writer of `building-validation`), once per guard. af-intake-plan decides; the sibling writes.

**Derive the guard set — batch across the whole plan, not per-requirement (stop-sooner, same discipline
as B1/B4):**
- **Edge-case guards** — from B1's fired lenses, keep the cases where the miss is EXPENSIVE OR INVISIBLE
  (silent partial failure, data loss, auth bypass, race, idempotency, money/PII) AND the case is
  automatable. Each becomes a check whose `run` is a test hitting exactly that case. Scope it per
  af-intake-build-validation Step 1: **surface-bind** a UI case (`meta.surfaces` + the `renders` edge, so
  it can never resolve onto a backend-only ticket), **tag-scope** a domain case (`["auth"]`), `["*"]`
  only for a truly universal invariant.
- **Every-site guards** — from B4's architecture decisions and any requirement describing a rename /
  migration / purge across the codebase, name each cross-cutting change that must be TOTAL. Each becomes
  a completeness check — typically `! grep -rq '<old>' <scope>` (zero stragglers) or a codemod/lint gate
  — scoped `["*"]` so it re-runs on EVERY ticket until the sweep is total (a straggler in any ticket's
  slice re-reds the gate).

Keep the set HIGH-SIGNAL: a guard for every trivial case is noise that slows every build. Guard the cases
whose miss is costly or silent, and the sweeps whose partial application still builds green — not the
obvious ones the acceptance floor already forces.

**Author each guard through the sibling command.** For each derived guard run **`af-intake-build-validation`**
with its criterion + `run` + `applies_to`/surface. That command owns the `building-validation` write, runs
`resolve_preview --by-check` to confirm the guard lands ONLY on the intended tickets (tighten an over-broad
tag before it ships), and closes on a zero-exit `resolve_preview --require-coverage`. Because the write goes
through the sibling, the single-writer invariant on `building-validation` still holds — af-intake-plan never
writes the check section directly.

**Decision mode (Step 0c) applies to the guard set, not to whether it runs.** In **Collaborate**, confirm
the derived guards with the human (which sweeps are truly total; which edge cases are worth a permanent
gate) before authoring. In **Autonomous**, author the low-regret guards, record them via
`praxis_record_episode`, and surface the list for override at B9. A guard is cheap to delete and expensive
to omit, so bias toward authoring the every-site scan guards (near-zero false-positive) and the
high-severity edge-case guards. Record the authored guard-check ids in the **B8 panel-ran episode** so the
step cannot be silently skipped.

## B6 — Cross-requirement coverage + depth (the mechanical gate)

The mechanical half is executable, not eyeballed:
- **Bidirectional coverage (H14, surfaces)** — `praxis_surface_coverage(project, scope="mvp")` must come
  back with both `uncoveredSurfaces` AND `uncoveredRequirements` empty (or each exception justified).
- **Dangling concept reference (H14, concepts)** — every domain concept a requirement *references* is
  *defined* by some admitted requirement or explicitly declared out of scope. (This is the hole that let
  an undefined "team streak" in: R2 referenced it, nothing defined it.) Tag each requirement with the
  concepts it `defines`/`references`.
- **The plan_gate (ENFORCED, mechanical — not prose)** — run **`python -m agent_factory.tools.plan_gate_check <project>`**.
  It reads the **LIVE** `prd-<project>` requirement facts straight from Praxis, maps each to a Requirement
  (threading its `tags`, `verify`, and `meta.decision` marker), and runs `evaluate_plan` over them with the
  project pinned. **A non-zero exit is a HARD BLOCK on the bless** — the human literally cannot clear B9
  while it exits non-zero. Surface its `reasons` **verbatim** (it prints them to stderr). Exit `0` =
  admitted, `1` = rejected, `2` = Praxis unreachable or empty plan. Because it reads the `meta.decision`
  marker, an **impl-tagged decision no longer slips**: a ticket carrying the decision marker is recognized
  as a decision even when its tags are pure impl (no `architecture-decision` tag), so
  `R-DECISION-NOT-END-STATE` and `R-NO-IMPL-DEPENDS-ON-DECISION` still bite. Pinning the project is what
  makes the gate require every requirement's `source` to equal `prd-<project>` exactly (the `R-HAS-SOURCE`
  rule), so a source-less or mis-scoped plan is mechanically **rejected** — the drift that went uncaught
  when the gate ran without project+source. The same run also checks binary-acceptance present,
  no-vague-term, no dangling concept reference, and the **build-order DAG** (Step 5): `R-NO-DANGLING-DEP`
  rejects a `depends_on` naming a requirement not in the plan, and `R-NO-DEP-CYCLE` rejects a dependency
  cycle — both would otherwise surface only as a run-time stall. Covered by `evals/cases/plan_gate/` (run
  `pytest tests/test_eval_cases.py`); add a `case.yaml` whenever a fresh gate edge case is found, so the
  gate's coverage compounds.

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
  text = "af-intake-plan audit+panel ran for prd-<project>: challenged <N> requirements; "
         "lenses fired=[...]; near-dups reconciled=[...]; arch decisions written=[...]; "
         "test layers=[...]; blind-spot guards authored via af-intake-build-validation="
         "[edge-case=<check_ids>, every-site=<check_ids>] (B5b); "
         "tech-decision critic loop-until-dry passes=<k>, missing=[]; "
         "plan panel composition=[...], findings emitted=<m>",
  outcome = "succeeded",
)
```

This is the **only** thing the validation leaves behind besides the graph edits. It is a tiny
assertion-of-record so the act of auditing **cannot be silently skipped** — NOT a findings state machine,
NOT a status manifest.

## B9 — The gate blesses (auto when the predicate holds; human only on a failing predicate)

Planning is guarded by the **`plan_completeness` Stop hook** (`hooks/plan_completeness_gate.py`, the
second `Stop` entry): while the planning marker is armed (Step 0d) it BLOCKS the planning turn until the
plan mechanically blesses, then **auto-blesses (ALLOWS) with no human**. The human is summoned **only on a
failing predicate** — auto-bless raises *structural* rigor (it adds the signed-contract + contradictions-
checked predicates); it does not replace the qualitative human review, which becomes **sampled on a
failing predicate**. A bounded terminal escalation (`FACTORY_PLAN_GATE_MAX_ATTEMPTS`, default 3) means an
unresolvable predicate on an unchanged snapshot escalates to a human instead of re-blocking forever.

Report status against each condition; never declare it yourself. The plan blesses only once ALL hold,
checked **live from Praxis**:
- Every requirement maps to ≥1 binary acceptance condition (or is an explicitly-deferred owned decision).
- Every requirement carries `source="prd-<project>"` (`R-HAS-SOURCE`).
- **`python -m agent_factory.tools.plan_gate_check <project>` exits `0`** over the live requirements (B6) —
  a non-zero exit is a **HARD BLOCK**; the gate cannot be cleared while plan_gate_check rejects, and its
  reasons must be surfaced verbatim and cleared first. This now INCLUDES `R-CONTRACT-SIGNED`: a
  **signed contract** (B1c) with recorded evaluator actions must exist, or the gate blocks.
- The **`contradictions_checked` marker is set** for the snapshot AND zero unresolved contradictions (an
  empty queue with no marker is NOT evidence of consistency — the raw-bulk path skips detection, KTD4);
  no dangling concept reference (H14); bidirectional surface coverage clean (B6).
- Every can't-miss failure class addressed-or-excluded with logged rationale (data loss, auth bypass,
  irreversible action, silent partial failure).
- **Every known every-site refactor and high-severity edge case carries a build-validation guard check**
  (B5b) authored via af-intake-build-validation — or is explicitly recorded (episode) as not-guardable /
  routed to a `verify="manual"` ticket. A high-risk sweep or edge case with no guard and no recorded
  exception is an open blind spot, not a blessable plan.
- The **panel-ran episode exists** (B8) — the audit and plan-review panel actually ran.

**Stop by information-gain, not exhaustion.** When the next question's expected information gain is low
and the gate is reachable, say so and STOP asking. Beware the under-specification trap: zero
contradictions on a thin plan is not "done," it's "nothing was claimed yet."

When the gate blesses: **`save_snapshot(space="<project>", snapshot="prd-<project>")`** (PRD-only
— mounts aren't carried), then **CLEAR the planning marker** (`_ticket_state.clear_planning(project,
owner)`) so the `plan_completeness` hook goes inert for this session. This dumps working memory into the
`prd-<project>` snapshot in the project's space. Render the prose PRD from the facts for human review. This snapshot is the durable plan; the build
loop consumes it later. Editing later = `load_snapshot(space="<project>", snapshot="prd-<project>",
mode="replace")` → edit → re-save, or use Amend mode. (The `prd-<project>` snapshot is MUTABLE — the
build loop reads and writes ticket state on it directly; read-only applies only to mounts and load/dump
copy semantics.)

---

# PART C — AMEND (add ONE missing ticket to the plan)

This command's amend path is **C0 only: add a genuinely-new requirement TICKET the plan is simply
missing** — writing the `prd-<project>` snapshot, the section this command owns.

**To add a CHECK, use the section-locked sibling command — NOT this one:**
- a **build-time validation check** ("must pass before a ticket is done") → **`af-intake-build-validation`**
  (writes the `building-validation` snapshot);
- a **planning lens** ("how to plan" the audit must close) → **`af-intake-plan-validation`** (writes the
  `planning-validation` snapshot).

Splitting checks out is deliberate: each of the three snapshots (`prd-<project>` / `building-validation` /
`planning-validation`) has exactly one writer command, and the server's write-time section invariant
refuses a `category="check"` fact in the `prd-<project>` plan — so a check can never co-mingle with the
plan even by mistake.

**Amend is additive, never a content edit.** C0 adds a requirement that *did not exist*; it does not
rewrite an existing ticket's statement/acceptance — that is a re-baseline FULL INTAKE (Step-3
`on_conflict="surface"` edit). Praxis is a HARD dependency: if the write cannot reach Praxis, **fail
closed** (error and stop) — never fall back to a file.

> **`on_conflict="surface"` is NOT a dedup guard for an additive ticket.** `on_conflict` governs
> **contradictions only** — an *additive, non-contradictory* near-duplicate is invisible to it. Before
> this was fixed, a C0 write for a genuinely-new ticket that was merely *topically similar* to an
> existing one was silently **merged** by the ingestion dedup into the nearest fact (`action:"merged"`),
> appending its text into that fact's `content` — even a already-`finished` ticket's — corrupting it. So
> do NOT rely on `surface` to keep a new ticket distinct. What keeps it distinct is the **ticket-identity
> write path**: because a C0 write is `category="requirement"` carrying `meta.build_state="incomplete"`,
> the server routes it through an **identity-keyed upsert** (keyed on `meta.requirement_id`, redact-only,
> NO text-dedup) — a distinct/new `requirement_id` (or none) always lands as a **fresh distinct fact**,
> and only a write reusing an EXISTING `requirement_id` updates that one ticket in place. A new ticket can
> therefore never mutate a different (or finished) ticket. To decide "is this actually an edit of an
> existing ticket?", judge it yourself first (`praxis_facts_by` / `praxis_get_context` for a near-dup); if
> it IS a restatement of existing content, that is a re-baseline (FULL INTAKE), not C0.

## C0 — New ticket (a genuinely-new requirement, nothing to edit)

When the amendment is a **requirement the plan is simply missing** — not a rule over existing work, and
not a rewrite of an existing ticket — admit it as a ticket the same shape Full-intake and the plan panel
mint: **identity only** (tags, surfaces, semantics), NEVER an authored check list. This is the one Amend
path that writes the **`prd-<project>` snapshot** (where tickets live), not a check snapshot.

Confirm tenancy first per `docs/af-memory-policy.md` §0: the factory operates in the **project-derived
org** — `identity.factory_org()` (the `PRAXIS_ORG` pin, else the per-project MCP-cache selection),
**never** a hardcoded `agent-factory`. The **one hard rule**: the MCP-tool org (`praxis_whoami` /
`praxis_select_org`) and the hook-client org (`PRAXIS_ORG`) must **AGREE** — the fail-loud
`praxis_select_org` guard enforces it, refusing a mismatch by naming both orgs. A fresh session simply
proceeds in the project's pinned org; it must **NOT** call `praxis_select_org("agent-factory")`.

Then write it DIRECTLY into the `prd-<project>` snapshot by passing `space`/`snapshot` — the write's
dedup/contradiction net runs against the plan in that snapshot, so there is no load→working-memory→save
round-trip:

```
praxis_add_insight(
  insight  = "<requirement — ONE semicolon-joined sentence>",
  source   = "prd-<project>",
  category = "requirement",
  meta     = { "build_state": "incomplete", "tags": ["<class-tag>", ...],
               "scope": "mvp | post-mvp", "surfaces": ["<screen-id>", ...],
               "requirement_id": "<R-id, OPTIONAL>" },  # include to make a re-file update-in-place
  space    = "<project>",          # REQUIRED — write into the plan snapshot itself,
  snapshot = "prd-<project>",      # NOT working memory (invisible to the build)
)
```

1. **The `build_state="incomplete"` on this write is what makes it a TICKET**, and the server routes a
   requirement ticket through the identity-keyed path — so it lands as a **distinct new fact**
   (`action:"added"`) and is NEVER text-merged into a similar existing ticket. `on_conflict` is
   irrelevant here (it only ever gated contradictions, never additive near-dup merges), so do not pass it
   expecting it to guard the dup. **Optionally set `meta.requirement_id`:** with it, re-filing the *same*
   ticket updates that one fact in place (a true restatement) instead of minting a twin; without it, every
   write is a fresh fact. If the "new" requirement is really an EDIT of an existing ticket's content, you
   are in the wrong path — that content edit belongs to FULL INTAKE (Step-3), not Amend.
   **Recovery (a pre-fix corrupted ticket):** if an older plan has a ticket whose `content` was appended
   into by a silent merge, restore it with `praxis_edit_fact(<id>, content="<original>", on_conflict="none")`
   (a literal in-place rewrite, no reconcile), then re-file the intended new ticket — which now lands
   distinct.
2. If it renders a surface, bind it against the SAME snapshot:
   `praxis_bind_surface(requirement_id, screen_id, project, space="<project>", snapshot="prd-<project>")`
   (the `renders` edge) so surface-bound checks resolve onto it at build.
3. VERIFY it landed where the build reads:
   `praxis_incomplete_requirements(<project>)` (BARE name — this endpoint reads the `prd-<project>`
   snapshot) should now list it, or `praxis_facts_by(category="requirement", space="<project>",
   snapshot="prd-<project>")`.

**No regression step for a new ticket.** A new ticket is born `build_state="incomplete"` with `source="prd-<project>"`,
so it enters `incomplete_requirements` for free — there is nothing pre-existing to re-open. Confirm with
`praxis_incomplete_requirements(<project>)` (BARE name). Never author a check list onto it — which checks
apply is the build's fresh RESOLVE query (tag ∪ "*" ∪ surface), same as every other ticket.

> **When C0 vs. re-baseline?** One or a few clearly-additive missing tickets against an otherwise-stable
> plan → C0. A wave of changes, edits to existing requirements' content, or anything the audit/panel
> should re-examine as a set → re-baseline FULL INTAKE. C0 does NOT re-run the audit or plan panel, so
> reserve it for additions that don't move the plan's coverage story.

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
- **Never leave a known every-site refactor or high-severity edge case (B5b) without a build-validation
  guard check** — the every-site scan (`! grep -rq '<old>' <scope>`) and the tricky-case test are exactly
  what af-build silently drops; author each via af-intake-build-validation or record an explicit exception.
  And **never write the `building-validation` section directly from this skill** — DERIVE the guards here,
  DELEGATE the write to af-intake-build-validation (its sole writer), preserving the single-writer lock.
- **Never pass on a missing ce panel** — if the compound-engineering reviewers aren't available, record NO
  panel-ran episode and surface the remediation; absence is a blocked review, never a silent skip.
- **Never skip the audit or panel silently** — every skip records a reason as a Praxis episode; the
  panel-ran episode is what proves it ran.
- **Never pass the prefixed project name** to the completeness/incomplete endpoints — `prd-<project>`
  becomes `prd-prd-<project>`, returns EMPTY, and fakes completeness. Pass the BARE name.
- **In Amend mode: never touch `pinned_checks` or the claim lease, and never build, fix, or run the
  check** — this command's amend only admits a new requirement ticket as identity + state
  (C0); checks are declared via `af-intake-build-validation` / `af-intake-plan-validation`. The build owns RESOLVE, CLAIM, PIN, and per-check pass
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
