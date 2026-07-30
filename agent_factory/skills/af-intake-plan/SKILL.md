---
name: af-intake-plan
description: >
  The write-path and owner of the Praxis PLAN — the `prd-<project>` snapshot — and its blessing audit.
  One of THREE section-locked intake commands, each the sole writer of one canonical snapshot in the
  project space: this one writes ONLY the plan; af-intake-build-validation writes the `building-validation`
  checks; af-intake-plan-validation writes the `planning-validation` lenses. FULL INTAKE takes af-plan's
  messy exhaustive brainstorm doc (+ optional clickable wireframe), extracts candidate requirements and
  surface↔requirement bindings DIRECTLY INTO PRAXIS, hardens them (self-consistency, contradictions,
  dedup), then runs a DELIBERATELY SMALL validation: ONE cold-eyes challenge pass over the whole set
  (five gap lenses swept once each, near-dups, missing acceptance), the forced architecture and
  external-service decisions, a test strategy, and one executable mechanical gate — then blesses. The
  ce-* plan-review panel is OPT-IN (default OFF; it fires only on a large or high-stakes plan, since the
  challenge pass already ran cold eyes). Tickets are sized as coherent red-to-green units, target 15-25
  per feature, merged by default rather than atomized per acceptance bullet. Where the plan contains an
  every-site sweep it authors ONE completeness guard by DELEGATING to af-intake-build-validation (it
  never writes the check section itself, so the single-writer lock holds).
  AMEND (C0) adds ONE genuinely-new requirement TICKET the plan is simply
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
plan once `plan_gate` passes, contradictions are empty, and a validation episode exists (all read live
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

**0c-a. Rigor — how DEEP the single challenge pass goes, never how WIDE** (mirrors af-plan Step 1a):
**Quick** runs B1's pass once; **Rigorous** re-runs it until a fresh pass surfaces nothing new, capped
at 3 passes. In both modes the five gap lenses sweep the whole set **once each** — rigor never turns
them into a lens-by-requirement matrix. Note the mode in the B5 validation episode.

**0c-b. Decision mode — how every genuine fork gets settled** once the resolve-before-you-ask ladder
(Step 3a) cannot answer it from sources. This is the **attended/unattended axis made explicit** — the
human's answer sets it deliberately instead of it being inferred from Constitution/owner-asleep:
- **Collaborate** (default / attended) — surface each genuine product fork as a blocking question; the
  human decides. Drives the ambiguity forge (Step 3b), the underspecification ladder's step 4 (Step
  3a), and the B4 architecture/provider decisions interactively.
- **Autonomous (force decisions / unattended)** — never block on a fork: take the low-regret default on
  every one, record it with `praxis_record_episode` (decision + "forced default: source silent, owner
  autonomous", alternatives not taken), and surface it for **override at the B5 bless gate** rather than
  asking mid-intake.

Decision mode changes **only how a fork is settled** — it never weakens validation. The audit (Part B),
the mechanical gate (B3) and the final blessing (B5) are
**unconditional** in both modes; in Autonomous mode the human still clears B9, reviewing the forced
defaults there instead of one at a time. **Anti-masking guard:** a forced default may NEVER paper over a
genuine high-regret/irreversible fork (auth model, data-loss semantics, money, PII exposure) — those
surface to the human even in Autonomous mode.

## Step 0d — Arm the planning marker (so the plan Stop hook enforces this session)

Before extracting, **stamp the planning marker**: `_ticket_state.stamp_planning(project, owner)`. This
writes a session-owned, heartbeated marker on the `prd-<project>` snapshot that ARMS the
`plan_completeness` Stop hook — from here until bless the hook blocks the planning turn from ending until
the plan mechanically blesses (B5). Re-stamp periodically to heartbeat it (the marker goes stale after
`DEFAULT_PLANNING_TTL_S`). It is CLEARED at bless (B5). A build session stamps a *run* marker, not this
one, so the two Stop hooks never cross-fire.

## Step 1 — Extract candidates (two passes, then reconcile)

### SIZE THE TICKETS FIRST (read before extracting)

A ticket is **one coherent unit of work an agent takes from red to green in a single sitting** — not
one sentence from the doc, and not one bullet of acceptance criteria. Extraction's default failure
mode is producing forty tiny tickets that each name a fragment of the same job, which inflates the
build loop's overhead, multiplies claim/resolve/verify round-trips, and buries the real work in
bookkeeping.

**Target 15-25 tickets for a feature; treat 30+ as a signal you over-divided, not as thoroughness.**
Merge by default. Concretely, these belong in ONE ticket, never split:

- **A mechanism and the thing that feeds it** — a detector and the whitelist derived from it; a
  corpus and the check that consumes it. Neither is shippable alone.
- **A rule set authored in one place** — every rule that lands in the same prompt, rubric, or config
  file is one authoring job, however many rules it contains.
- **A classifier and its verdict states** — do not give each branch of one decision its own ticket.
- **A change and the tests it breaks** — see the granularity rule in Step 5.
- **A behavior and its own edge cases** — the edge cases belong in that ticket's acceptance
  condition, which is exactly what acceptance conditions are for. A separate ticket per edge case is
  the single biggest source of ticket sprawl.

Split only when both halves are **independently shippable and independently verifiable**. When you
cannot decide, merge — a ticket that is slightly too big costs one longer sitting, while two that
should have been one costs a wedged dependency edge and two rounds of overhead.

**Pass A — behavioral, from the doc.** Atomize the rules into binary conditions, then **group them
back up** into the ticket-sized units above. A good brainstorm doc is already near-structured (epics
+ acceptance + data model + API), so this is *atomize → mint binary conditions → group → dedupe
across sections*, not invention. Over-generate **candidate behaviors**, then consolidate before
admitting — the grouping is yours to do here, not the audit's to clean up later.

**Pass B — surface, from the wireframe inventory.** Each screen becomes a candidate, and its states
and actions become **acceptance conditions on that screen's ticket** — not tickets of their own. This
is where the implied states (offline / empty / invalid-invite / completed / fallback) stop being
forgotten, and the way to keep them is to name them in the acceptance condition of the screen they
belong to. One ticket per screen is the right grain; one per state is the sprawl.

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
  `"manual"`** (B2's HARD RULE); the plan gate rejects an `architecture-decision` ticket left
  `verify="automated"` (`R-DECISION-NOT-END-STATE`).
- **`meta.surfaces`** — wireframe screen ids governed, or `["backend-only"]`. Seeds the `renders`
  bindings written in Step 4.
- **`meta.defines` / `meta.references`** — concepts, for the H14 dangling-reference gate.
- **`meta.depends_on`** — prerequisite requirement ids that must be `finished` before this one is
  buildable (the build-order DAG `af-build`'s `next_ready_ticket` walks). A best-draft now; the DAG is
  mapped and validated in **Step 5**. Empty for a requirement with no prerequisites. A prerequisite is a
  real **build** dependency only — **NEVER a pure architecture-decision ticket** (B2's HARD RULE); the
  plan gate rejects any edge whose target is tagged `architecture-decision` (`R-NO-IMPL-DEPENDS-ON-DECISION`).
- **`meta.scope`** — `"mvp"` or `"post-mvp"` (the tier tag only; NOT the project identity).
- **`meta.tags`** — identity tags (concepts / surfaces / semantics). A ticket carries identity, **NEVER
  an authored list of its checks**; *which checks apply* is a fresh query (tag ∪ surface ∪ semantic)
  resolved at build time. Tag honestly so that query resolves correctly. A **pure architecture-decision
  ticket carries the NEUTRAL tag `["architecture-decision"]` ONLY** — never an impl-domain tag (`cdk`,
  `token-verification`, `frontend`, `database`, …), so it resolves ZERO implementation checks (B2's HARD
  RULE).

## Step 2 — Write candidates to Praxis (the write-path)

Extraction is the **highest-leverage error point** — a bad requirement spawns thousands of bad lines.
Review leverage is inverse to distance-from-execution, so scrutiny concentrates here, at the plan.

### THE DEFAULT WRITE PATH IS DIRECT-TO-SNAPSHOT (never working memory)

**Write every candidate STRAIGHT INTO `(space=<project>, snapshot="prd-<project>")`** by passing BOTH
`space` and `snapshot` on the write. Working memory is **not** a staging area for a plan:

```
praxis_add_insight(
  insight  = "<requirement — ONE semicolon-joined sentence>",
  category = "requirement",
  source   = "prd-<project>",
  meta     = { "requirement_id": "R1", "build_state": "incomplete", "acceptance": "...", ... },
  space    = "<project>",          # REQUIRED
  snapshot = "prd-<project>",      # REQUIRED
  raw      = True,                 # skip dedup + the per-item LLM conflict check
)
```

**Why this is the default, and why the old working-memory flow is a footgun.** Working memory is a
single shared graph per principal — it accumulates every prior project's facts (a real session found
**208 unrelated facts** left over from two earlier intakes). The old flow ended in
`save_snapshot(space, snapshot)`, which **OVERWRITES the destination snapshot with the whole of working
memory**. That has two failure modes, both silent:
- **Leak** — every unrelated working-memory fact is dumped into `prd-<project>`. They carry no
  `source="prd-<project>"`, so `plan_gate_check` rejects on `R-HAS-SOURCE`… *if* you are lucky enough
  for it to be caught there.
- **CLOBBER (worse)** — if the plan was already authored into the snapshot, `save_snapshot` **replaces
  it with working memory and destroys the plan**, along with the planning marker fact living on it.

Direct-to-snapshot has neither failure mode: the facts are already where the build reads, nothing is
staged, and no overwrite is ever needed.

**`build_state="incomplete"` on the write is what makes it a TICKET**, and the server routes a
requirement ticket through an **identity-keyed upsert** (keyed on `meta.requirement_id`, redact-only, NO
text-dedup) — so each write lands as a distinct fact (`action:"added"`) and can never be text-merged
into a similar existing ticket. Setting `meta.requirement_id` makes a re-file update that one ticket in
place, which is what makes the whole extraction **idempotent and resumable**. Every record MUST carry
`source="prd-<project>"`; check each result's `ok`/`id`/`action`.

`raw=True` still embeds each fact (retrievable) and still redacts secrets, but **skips Praxis dedup AND
the per-item LLM conflict check** — avoiding the timeout the normal path hits at scale and the dedup that
wrongly collapses near-duplicate requirements. **You own clean, non-conflicting data on this path** —
safe only because Step-1 Reconcile already deduped and the **audit's cold-eyes conflict pass (Part B) is
the contradiction net.**

> **Bulk caveat.** `praxis_add_insights` (the bulk sibling) currently exposes **no `space`/`snapshot`
> parameter**, so it can only write to working memory and MUST NOT be used to admit a plan. Until it
> accepts them, admit with per-item `praxis_add_insight(..., space=, snapshot=, raw=True)` calls —
> issue them in parallel batches to keep the round-trips cheap.

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

**A pure architecture DECISION is NEVER a prerequisite** (B2's HARD RULE). Every relation above is a real
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

**The DAG is VALIDATED, not just authored — it is part of the mechanical gate (B3).** Run the plan gate
(`agent_factory.plan_gate.evaluate_plan`); its `R-NO-DANGLING-DEP` rule rejects a `depends_on` naming a
requirement not in the plan, and `R-NO-DEP-CYCLE` rejects a cycle (A needs B needs A) — either of which
would otherwise become a silent run-time **stall** when `next_ready_ticket` finds nothing claimable. A
plan does not pass intake with a dangling or circular dependency.

**Granularity — see "SIZE THE TICKETS FIRST" in Step 1 for the sizing target (15-25, merge by
default). This rule is its hard floor: a non-independently-greenable change set is ONE ticket, never split peers.** `depends_on`
only orders tickets that each stand alone; it does NOT license splitting a single indivisible change across
tickets that each need a *sibling's* edit to compile or pass. The universal-ish build gates
(`backend-build`, `backend-vitest`) pin on **EVERY** backend ticket, so each isolated worker must leave the
**WHOLE** backend compiling and its tests green with only its own slice landed. If a set of changes is not
independently compilable/greenable, author it as **ONE ticket** (or an explicit ordered chain) — never as
peer tickets that each red the shared whole-repo gate until a sibling lands. Worked example: the R7
verifier rewrite needs R8's identity change to keep the suite green — those two must be **ONE ticket or an
ordered chain**, not two peers that each red `backend-vitest`.

---

# PART B — PLANNING VALIDATION (one challenge pass, one gate, one bless)

Validation is deliberately **SMALL**. An earlier version of this skill ran eleven audit steps and
reliably turned a one-skill plan into a multi-hour ceremony that produced more planning artifact
than the thing being planned. The steps that actually caught defects were the **cold-eyes
challenge**, the **forced architecture decisions**, and the **mechanical gate**. Everything else is
now opt-in or gone.

The one structural rule that survives: **the agent that drafted a requirement is never its only
skeptic.** A model judging its own output inflates its pass rate, and a per-requirement reviewer
cannot see cross-requirement gaps. So the challenge is one pass by a separate reader over the
WHOLE set.

## B1 — One cold-eyes challenge pass (whole set, never per-requirement)

Dispatch ONE read-only sub-agent (`af-build` §1a) over the admitted facts plus the source doc. It
did not write the requirements, so it challenges harder; it sees the whole set, so
cross-requirement gaps surface. It reports, for the plan as a whole:

- **The five gap lenses, swept ONCE each across all requirements** — failure modes, security,
  data lifecycle, rollback, who-pays-the-tradeoff. One sweep per lens. **Never a
  lens-by-requirement matrix** — that is 5×N judgments for no extra signal and is what made the
  old audit unaffordable.
- **Near-duplicate / subsumed requirement pairs**, with which one is canonical.
- **Any requirement carrying no binary acceptance condition.**
- If the project's `planning-validation` snapshot holds lenses (authored by
  **af-intake-plan-validation**), apply them as extra items in the same sweep — READ them, do not
  pin them or build a coverage contract around them.

### VALIDATION FIXES THE PLAN — IT DOES NOT GROW IT

The audit's job is to **fill holes and correct bad posture in the tickets that already exist**. Its
failure mode — the one that makes intake unaffordable — is closing every finding by minting a new
ticket, so a 20-ticket plan comes out of validation at 45 with a pile of capability nobody asked
for. **Validation should be roughly ticket-count-NEUTRAL.**

**Hole vs. feature — apply this test before every finding.**

- A **hole** is a behavior the plan already claims but no ticket owns properly: an unfalsifiable or
  missing acceptance condition, a wrong/missing `depends_on` edge, an unstated refusal on a
  catastrophic path, a contradiction between two tickets, a near-dup pair. **Fix holes.**
- A **feature** is capability the plan never claimed. "It would also be good if…" is a feature, no
  matter how sensible. **Features are NOT findings.** Record one as a post-mvp note or an open
  decision on the doc and move on — never as a ticket, and never as a widened acceptance condition.

**Closure hierarchy — take the FIRST option that works, in this order:**

1. **`praxis_edit_fact` on an existing ticket** — tighten a weak acceptance condition, correct the
   posture, add the missing refusal, fix a `depends_on` edge. **This closes the large majority of
   real findings and is the default.** An unowned behavior is almost always a missing clause in some
   existing ticket's acceptance, not a missing ticket.
2. **`praxis_reject_fact`** on the loser of a near-dup pair (canonical one survives).
3. **A recorded episode** — for a dismissal (with the reason), a forced default, or a deferred
   owned-decision. A decision is an episode, never a ticket, unless it needs the B2 HARD-RULE shape.
4. **`praxis_add_insight` for a genuinely new ticket — the LAST resort, with a bar.** Permitted only
   when the gap is a behavior that **no existing ticket could own even with an amended acceptance
   condition**, and you can name why. **Hard cap: 2 new tickets per validation pass.** Wanting more
   is not thoroughness — it means EXTRACTION was wrong, so go back and re-group Step 1 rather than
   bolting findings onto a bad decomposition.

A free-text "considered and fine" is not closure. But neither is a new ticket for something an edit
could have carried.

**Record ONE episode when the pass completes**, naming the lens results, the near-dups
reconciled, and the edits made. That episode IS the contract signature the mechanical gate reads
(`R-CONTRACT-SIGNED`) — the evaluator's recorded ACTIONS are the signature, so there is no
separate contract-negotiation step. Its text must name `prd-<project>`, because the gate scopes
the signature to this project (an unscoped match once let one project's signature satisfy
another's gate).

**Rigor (Step 0c) sets the DEPTH of this one pass, not its width.** Quick runs it once; Rigorous
re-runs it until a fresh pass surfaces nothing new, capped at 3 passes.

## B2 — Forced technical decisions (architecture, external services, tests)

Behavioral requirements say *what* the product does and routinely leave the *how* unspecified.
Force the project-wide technical decisions **this** system needs to be buildable — derive them
from the doc and the requirements, since a CLI, a web app, an ML pipeline, and a library need
different ones. Resolve each on the Step-3a ladder (doc → mounted conventions → low-regret
default recorded as an episode → ask per decision mode → defer), then persist it **by editing the
requirement it governs** — or as an episode when it is a pure choice. Admitting a decision as its own
ticket is the exception, not the default, and only in the B2 HARD-RULE shape below; a decision ticket
that gates nothing will be closed by assertion and is better recorded as an episode.

**Named, non-skippable: external-service providers.** Whenever any requirement implies an
external service (email, SMS, push, payments, object storage, geocoding, …), the concrete
provider/transport is a **forced decision** — choose it, record how it is configured and
secreted, and require a dev/local transport that surfaces the side effect (e.g. logs the reset
link). "Sends email" with no chosen transport is the canonical planning failure. Surface the
**managed-vs-custom fork** explicitly: a managed auth provider may bundle credential email and
remove the standalone choice entirely.

**Test strategy, one pass.** Name the layers THIS platform needs (a library needs public-API
contract tests; a mobile app needs a device/simulator layer; a data pipeline needs data-contract
and eval gates) and give each a **binary, CI-enforced condition**. A layer with no binary
condition is a hope, not a strategy. No until-dry critic loop.

**Every-site sweeps get ONE guard.** If the plan contains a change that must land at EVERY call
site (a provider swap, a rename, a config-key migration, a banned-import purge), author a single
completeness check — typically `! grep -rq '<old pattern>' <scope>` — by running
**af-intake-build-validation** (the sole writer of `building-validation`). A half-done rename
often still compiles green, which is why the acceptance floor alone does not catch it. One guard
per sweep, not a guard per edge case.

### HARD RULE — a pure architecture DECISION is modeled as a decision, never as a disguised implementation ticket

A pure architecture decision ("we use Cognito, not Auth0") is a **CHOICE**, not a build target.
Prefer recording it as an episode so it never enters the build set at all. If it is admitted as a
`category="requirement"` ticket it MUST obey all of:

- **Neutral tag ONLY** — `meta.tags = ["architecture-decision"]`, never an impl-domain tag
  (`cdk`, `frontend`, `database`, …), so it resolves ZERO implementation checks.
- **`meta.verify = "manual"`** — a human accepts or overrides it; a decision is never an
  automated end-state.
- **Decision-level acceptance** — `"<X> is the accepted design decision"`, never an
  implementation end-state (`"cdk synth emits three UserPools"`) that duplicates a downstream
  ticket.
- **Never a `depends_on` prerequisite of its own implementation ticket.** Bake the decision into
  the IMPL ticket's content/acceptance instead.

**The gate enforces this** via `R-DECISION-NOT-END-STATE` and `R-NO-IMPL-DEPENDS-ON-DECISION`,
recognizing a decision by the neutral tag OR the `meta.decision` marker. Note the corollary: do
**not** put anything in `meta.decision` that is not an architecture decision — a flagged default
belongs in `meta.flagged_default`, or the gate will demand `verify="manual"` on an ordinary
ticket.

**Why this rule exists (the wedge).** Five Auth0→Cognito decisions were once planned as tickets
carrying impl-domain tags, impl end-state acceptance, and `depends_on` edges FROM the impl work
that would satisfy them. They sat topologically FIRST but could only go green LAST, so a fresh
build's entire ready frontier was decisions nothing could satisfy, and the run wedged
immediately.

## B3 — The mechanical gate (executable, not eyeballed)

- **`python -m agent_factory.tools.plan_gate_check <project>`** — reads the LIVE `prd-<project>`
  facts and runs `evaluate_plan` with the project pinned. **Non-zero exit is a HARD BLOCK on the
  bless.** Surface its reasons verbatim. Exit `0` admitted, `1` rejected, `2` Praxis unreachable
  or empty plan. It covers: `R-HAS-SOURCE` (every requirement's `source` equals `prd-<project>`),
  binary acceptance present, no vague terms, no dangling concept reference, the signed contract
  from B1, and the **build-order DAG** — `R-NO-DANGLING-DEP` and `R-NO-DEP-CYCLE`, either of
  which would otherwise surface only as a run-time stall when `next_ready_ticket` finds nothing
  claimable.
- **Bidirectional surface coverage** — `praxis_surface_coverage(project, scope="mvp")` returns
  both `uncoveredSurfaces` and `uncoveredRequirements` empty, or each exception justified. A
  project with no surfaces at all (a CLI, a library) is `backend-only` by construction; say so
  rather than reporting a vacuous clean.
- **`meta.references` hygiene.** Every concept a requirement references must be `defined` by some
  requirement or declared out of scope (`--out-of-scope`). Reference plan concepts only —
  ambient vocabulary and pre-existing factory artifacts belong in `meta.citations`.

New gate edge cases earn a `case.yaml` under `evals/cases/plan_gate/` so the gate's coverage
compounds (`pytest tests/test_eval_cases.py`).

## B4 — The plan-review panel (OPT-IN; default OFF)

**Default OFF.** B1 already ran cold eyes over the whole set, so a second full panel is
redundant on an ordinary plan. Convene the compound-engineering panel ONLY when a trigger fires:

- more than 25 new/changed requirements since the last blessed snapshot, OR
- the plan touches auth, payments, data migrations, or PII, OR
- it introduces a new cross-cutting abstraction or framework.

When it runs, **two personas suffice** — `ce-coherence-reviewer` (contract/convention coherence)
and `ce-feasibility-reviewer` (unsatisfiable targets) — plus `ce-security-lens-reviewer` on the
high-stakes trigger only. Dedupe across personas first, then close each finding through **the same
closure hierarchy as B1** — edit an existing ticket by default, a check for a rule that must hold
across the plan, an episode for a dismissal, and a new ticket only under B1's last-resort bar. The
panel's 2-new-ticket cap is shared with B1's, not additional to it: a review pass is not a licence to
re-scope the plan.

**A skip is recorded, never silent** — when no trigger fires, record a skip episode naming the
size signal. If the panel IS triggered but the ce agents do not resolve, that is a **blocked
review**: surface the remediation (`claude plugin install compound-engineering@compound-engineering-plugin`)
and do not bless.

## B5 — Bless

Record ONE episode asserting what ran (`af-intake-plan validated prd-<project>: challenge passes=<k>,
lenses=[...], near-dups reconciled=[...], decisions written=[...], test layers=[...], every-site
guards=[...], panel=<ran|skipped:reason>`), then report status against each predicate — never
declare it yourself. All are read LIVE from Praxis:

- Every requirement maps to ≥1 binary acceptance condition, or is an explicitly-deferred owned decision.
- Every requirement carries `source="prd-<project>"`.
- **`plan_gate_check` exits `0`** (B3) — a HARD BLOCK otherwise, reasons cleared first.
- The `contradictions_checked` marker is set AND the contradiction queue is empty. An empty queue
  with no marker is NOT evidence of consistency — the `raw=True` path skips detection, so
  "empty" can mean "never ran". B1's pass is what earns the marker.
- Every can't-miss failure class addressed-or-excluded with logged rationale (data loss, auth
  bypass, irreversible action, silent partial failure).
- Every every-site sweep carries its guard check (B2), or a recorded exception.
- **Validation did not inflate the plan.** State the ticket count before and after Part B in the B5
  episode. Net growth above **2 tickets** (B1's and B4's shared cap) means findings were closed by
  minting scope instead of by fixing the tickets — go back and convert them to edits before blessing.
  A validation pass that leaves the count flat, or lower after near-dup rejection, is the normal and
  healthy outcome.
- The B5 episode exists.

The `plan_completeness` Stop hook (armed in Step 0d) blocks the planning turn until these hold,
then **auto-blesses with no human**; the human is summoned only on a failing predicate, and an
unresolvable predicate on an unchanged snapshot escalates after
`FACTORY_PLAN_GATE_MAX_ATTEMPTS` (default 3) rather than re-blocking forever.

**Stop by information-gain.** When the next question's expected gain is low and the gate is
reachable, say so and STOP. But beware the inverse: zero contradictions on a thin plan is not
"done", it is "nothing was claimed yet".

### Blessing a plan that already lives in the snapshot (the DEFAULT)

Because Step 2 writes candidates **directly into `prd-<project>`**, the plan is already durable
when the gate clears. Blessing is a **VERIFY-then-RELEASE, not a save**:

1. **VERIFY it is where the build reads** — `praxis_facts_by(category="requirement",
   space="<project>", snapshot="prd-<project>")` returns the expected count, and
   `praxis_incomplete_requirements("<project>")` (**BARE** name) lists the tickets. That readback
   IS the durability proof.
2. **DO NOT CALL `save_snapshot`.** On this path it is not a no-op — it **OVERWRITES
   `prd-<project>` with working memory**, destroying the plan you just authored and the planning
   marker on it. Calling it here is a data-loss bug, not a belt-and-braces step.
3. **CLEAR the planning marker** — `_ticket_state.clear_planning(project, owner)` — so the
   `plan_completeness` hook goes inert for this session.
4. **Render the prose PRD** from the facts for human review.

> **Legacy path (only if the plan was staged in working memory).** Bless with
> `save_snapshot(space="<project>", snapshot="prd-<project>")` BEFORE clearing the marker, and
> first confirm working memory contains **nothing but** this plan's facts (`praxis_list_graph`) —
> otherwise the save leaks every unrelated fact into the plan. Prefer converting to the
> direct-to-snapshot path.

Editing later is an in-place write to the snapshot (`praxis_add_insight`/`praxis_edit_fact` with
`space`/`snapshot` set, or Amend mode). Do NOT round-trip through
`load_snapshot(mode="replace")` → edit → `save_snapshot`.


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
               "acceptance": "<binary observable condition>",  # REQUIRED (see below)
               "verify": "automated | manual",                 # REQUIRED (see below)
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

**`acceptance` + `verify` are mandatory, or the ticket is minted unclaimable.** af-build's
`start_ticket` runs the pre-claim structural resumability probe BEFORE it leases: a ticket with neither
an acceptance condition nor a resolved check, or with no `verify` mode, is never claimed — it is
stamped `meta.under_specified` and routed back to intake. A C0 write missing them therefore lands in
the incomplete set and then sits there looking merely queued, which is the silent stall this note
exists to prevent. Declare both on the write.

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
- **Never let validation grow the plan.** The audit fills holes and corrects posture in the tickets
  that exist; it is roughly ticket-count-NEUTRAL. Close a finding by EDITING an existing ticket
  (B1's closure hierarchy); a new ticket is the last resort, capped at 2 per pass and shared with the
  panel. Wanting more means extraction was wrong — re-group Step 1 instead of bolting findings on.
- **Never treat a feature as a finding** — capability the plan never claimed is not a hole, however
  sensible. Record it as a post-mvp note or an open decision; never as a ticket, and never by
  widening an existing acceptance condition to smuggle it in.
- **Never over-divide the plan** — a ticket is one coherent red-to-green sitting, not one sentence of
  the doc, one acceptance bullet, or one edge case. Target 15-25 tickets and merge when undecided
  (Step 1, "SIZE THE TICKETS FIRST"); 40 tiny tickets is a worse plan than 20 right-sized ones, not a
  more thorough one.
- **Never run a lens-by-requirement matrix** — the five gap lenses sweep the whole set ONCE each (B1).
  5×N judgments buy no extra signal and are what made the old audit unaffordable.
- **Never admit a candidate without `source="prd-<project>"`** — that is the project identity the
  completeness query and the `R-HAS-SOURCE` gate filter on; `meta.scope` (mvp/post-mvp) is NOT a
  substitute.
- **Never write a planning fact on `auto_resolve`** — it silently rejects the loser and hides the
  conflict; incremental edits use `on_conflict="surface"`, fresh bulk uses `raw=True` (with the audit as
  the contradiction net).
- **Never treat a write timeout as a failure** — the write usually landed; **read back** (`list_graph` /
  `get_context`) before retrying, or you'll create duplicates.
- **Never admit a plan through working memory** — write candidates DIRECTLY into
  `(space=<project>, snapshot="prd-<project>")` by passing both `space` and `snapshot` (Step 2). Working
  memory is a shared, dirty graph; staging a plan there risks leaking unrelated facts into the plan.
- **Never call `save_snapshot` on a plan that was authored directly into the snapshot** — it OVERWRITES
  the snapshot with working memory, destroying the plan and the planning marker on it. On the default
  path, bless is a readback (`facts_by` / `incomplete_requirements`) + `clear_planning`, never a save.
- **Never `clear_graph` without a confirmed save-before-clear snapshot**, and never let mounted reference
  knowledge leak into the `prd-<project>` snapshot.
- **Never let the agent that drafted a requirement be its only skeptic** — the audit and the plan panel
  are cold-eyes sub-agents / ce-* reviewers.
- **Never "close" a challenge or near-dup with a free-text note alone** — close it with the Praxis write
  that fixes it (edit/add the requirement, declare the check, reject/narrow the duplicate + re-save the
  snapshot) or a recorded dismissal/deferral episode.
- **Never bless a plan** while any audit-surfaced requirement/check is still incomplete, any open
  challenge is unresolved, the B5 validation episode is missing, or `plan_gate` does not pass — and never with
  no automated test strategy, a platform-required test layer missing, or a CI gate lacking a binary
  condition.
- **Never leave a known every-site refactor (B2) without a build-validation
  guard check** — the every-site scan (`! grep -rq '<old>' <scope>`) and the tricky-case test are exactly
  what af-build silently drops; author each via af-intake-build-validation or record an explicit exception.
  And **never write the `building-validation` section directly from this skill** — DERIVE the guards here,
  DELEGATE the write to af-intake-build-validation (its sole writer), preserving the single-writer lock.
- **Never pass on a missing ce panel** — if the compound-engineering reviewers aren't available, record NO
  validation episode and surface the remediation; absence is a blocked review, never a silent skip.
- **Never skip the audit or panel silently** — every skip records a reason as a Praxis episode; the
  B5 validation episode is what proves it ran.
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
