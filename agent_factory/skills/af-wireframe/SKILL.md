---
name: af-wireframe
description: >
  Turn a PRD into complete, clickable HTML wireframe(s) in one shot, with one rendered screen per
  Praxis surface. Use when the human says "build a wireframe for this PRD", "wireframe this", or
  points at a spec/PRD and wants to see the screens. The skill reads EVERY source doc, generates
  navigable inert HTML for every surface bound in Praxis, and self-audits coverage by querying
  Praxis surface bindings — an unrendered surface is an incomplete requirement — so the human does
  not have to say "did you check the PRD?" or hand back missing screens.
---

## How work flows (this factory's methodology — read first)

State lives in ONE place: Praxis. There are no JSON status files, no locks on disk, no self-set "done"
flags. A ticket (requirement) and a check are Praxis facts; everything about what is built/claimed/passed
is state ON THE TICKET'S Praxis node. To do ANY unit of work you follow exactly this loop:

1. FIND   — query Praxis for the next incomplete ticket in scope (incomplete = never-built | regressed |
            stale, derived from recorded outcomes). Pass the BARE project name (e.g. "team-app"); the
            endpoint adds the "prd-" prefix itself — passing "prd-team-app" returns EMPTY and silently
            hides all work.
2. CLAIM  — atomically set the ticket's meta.build_state="in_progress" with claim_owner=you + a heartbeat.
            The claim is a LEASE: refresh the heartbeat while working; a stale lease auto-reclaims so a
            dead agent never strands a ticket. Parallel agents never double-work because a live claim is
            visible to all.
3. RESOLVE— determine which checks this ticket must pass by QUERY (its tag ∪ its surfaces ∪ semantic
            match against active checks). The ticket NEVER stores its own check list. Truncate any prior
            per-check state, then PIN the freshly-resolved set onto the ticket as this pass's contract.
4. BUILD  — do the work to satisfy the ticket's acceptance condition.
5. VERIFY — run each pinned check; record each pass ON THE TICKET NODE (never on the check — checks are
            read-only during builds). External signals only; never self-judge.
6. FINISH — only when EVERY pinned check passed: record a succeeded outcome and release the lease
            (build_state="finished"). If any check fails, record a failed outcome — that regresses the
            ticket so it re-enters the FIND set and is re-done.

Praxis is a HARD dependency: if it is unreachable the factory STOPS (the gate blocks) — it never proceeds
on a guess. The single Stop gate (build_completeness) enforces this loop: it blocks the turn from ending
while you hold an unfinished claim or scoped incomplete tickets remain.

In this skill: a wireframe ticket's "build" is rendering a screen, and its checks are the surface
bindings — one screen per Praxis surface, where an unrendered surface IS an incomplete requirement. FIND
the surfaces/requirements to render from the graph, CLAIM nothing extra (rendering is one pass over the
incomplete set), RESOLVE coverage from the live surface bindings, BUILD the inert HTML, VERIFY by reading
the bindings back, and FINISH by recording each rendered-surface outcome so the graph — not a checklist —
proves coverage.

# Factory Wireframe

**`af-wireframe` is a sibling output of the explore stage** — the visual counterpart to `af-plan`.
Where `af-plan` emits a messy text exploration doc, `af-wireframe` emits clickable HTML wireframe(s);
run it instead of, or alongside, the text doc. Either way the surfaces it produces are **handed to
`af-intake`**, which binds surface↔requirement edges and admits the plan. This skill consumes those
bindings back once they exist (and renders against them); before they exist it raises the missing
surfaces to `af-intake` so they get created.

Produce a **feature-complete, clickable, inert** wireframe from a PRD with a single instruction.
Completeness is the skill's job, and it is measured against **Praxis, the single source of dynamic
truth** — not a local checklist. Every screen the wireframe must show exists as a **surface** bound
to one or more requirements in the project's `prd-<project>` graph. **An unrendered surface is an
incomplete requirement.** The skill renders every bound surface and self-audits by reading those
bindings back live.

## State rules (non-negotiable)

- **Praxis holds the surfaces, requirements, and bindings.** `af-intake` produced the
  surface↔requirement bindings (the `renders` edges) this skill consumes. You never invent a
  parallel manifest of "what screens exist" — you query Praxis.
- **Praxis is a HARD dependency, fail-CLOSED.** If Praxis is unreachable/unauthenticated, STOP —
  do not generate from a guess and do not declare coverage. There is nothing local to fall back to.
- **No `.factory/*.json` state. Ever.** This skill writes NO build/coverage/wireframe manifest, no
  checklist, no status file. It does not record completion in JSON. The only durable record of "this
  surface is rendered" is in Praxis (a satisfied surface binding / a requirement that is no longer
  incomplete).
- **You touch state through the Praxis knowledge-port policy** (`docs/af-memory-policy.md`). Reads use the Praxis surface
  tools (`surface_coverage`, `list_surface_bindings`, `requirements_for_surface`,
  `incomplete_requirements`); the rendered-surface outcome you write goes through the same port,
  using the canonical meta keys from `docs/factory-state-contract.md`. Hook-level callers use
  `hooks/_praxis.py` (`surface_checks`, `incomplete_requirements`, `record_outcome`, …) — same
  contract. **Pass the BARE project name** to anything that takes `project` (the endpoint prepends
  `prd-` itself; `prd-team-app` returns EMPTY and would hide all the work).

## Operating rules (non-negotiable)

- **Read EVERY source, in full — never from memory or one file.** A PRD is usually several docs
  (a requirements list + a developer spec + a flow sketch). Discover them all (`ls`/glob the PRD
  folder), read each completely. The most detailed doc (acceptance criteria, data model, API) is
  authoritative; reconcile the others against it.
- **Clickable but inert.** Screens navigate like a real app (links/tabs/back), but every action
  button is a no-op (a tiny "(prototype)" toast) — no fetch, no backend, no commands.
- **Self-contained.** Each wireframe is one standalone `.html` (embedded CSS+JS), openable via
  `file://` with no build step.
- **Low-fidelity but real.** Wireframe aesthetic (greys, dashed inputs, requirement tags), not
  final visual design — but real layout and real navigation.

## Step 1 — Ingest the whole PRD

1. Locate the PRD: the path the human named, else search `docs/inspiration/`, `docs/`,
   `docs/brainstorms/` for spec/requirements/PRD files. **List the directory** and read **every**
   matching doc in full. Do not stop at the first file.
2. Note the authoritative doc (the one with acceptance criteria / data model / endpoints).

## Step 2 — FIND + RESOLVE: pull the surfaces and requirements from Praxis

Resolve **what to render** by querying the project graph — this is the contract you build against,
and it lives in Praxis, not in a file you author.

1. List the project's surfaces and their bindings: `surface_coverage` and `list_surface_bindings`
   give every surface and the requirements each `renders`. Use `requirements_for_surface(screen_id)`
   to see, per surface, exactly which requirements it must satisfy on screen.
2. List the **incomplete** requirements: `incomplete_requirements("<project>")` — **the BARE project
   name** (`GET /requirements/incomplete?project=<project>`; the server forms `prd-<project>`
   itself). Any requirement with no rendered surface shows up here — that is the gap the wireframe
   must close.
3. Cross-check the PRD docs against the bound surfaces. If a doc clearly implies a screen that has
   **no surface** in Praxis, that is a missing binding, not a private note: surface it back to the
   human / `af-intake` so the binding gets created — the wireframe's job is to render the
   graph's surfaces completely, not to grow a shadow inventory beside it.

Always confirm the graph covers these categories (a missing surface here is the usual "you missed
X"); if the PRD demands one and Praxis has no surface for it, that's a missing binding to raise:

- **Personas / roles** — and **split distinct personas into separate apps.** If two user types
  have fundamentally different jobs (a player vs a coach/admin), they are **two apps sharing an
  entry point**, not one app with toggles. The graph models them as distinct surface sets.
- **Per-persona screens** — one rendered screen per surface that persona reaches.
- **Auth & onboarding** — sign-up, **log in (returning user)**, **invite/redeem + invalid-code
  error**, **consent (minors)**, **disclaimers** ("not therapy", terms/privacy).
- **Data-model entities** — each surface that views/edits one (incl. options/config like
  `options_json`, thresholds, timezones).
- **Admin / config / scheduling** — content editors AND a **scheduler** surface if scheduling is
  in scope; settings/toggles from the data model.
- **Notifications** — each reminder type + a settings surface.
- **Metrics / dashboards** — every named metric, plus trend/distribution/per-entity views.
- **Implied app states** (a surface renders these even when the PRD doesn't draw them):
  **empty, loading, error, offline/queued-sync, success/completed/locked, "not set" fallback,
  validation/"what's missing"**. Acceptance criteria like "shows existing submission state" or
  "returns error listing missing components" are implied states — render them on the owning surface.
- **Privacy/safety** — what each role may/may not see; audit trails; moderation.
- **Post-MVP** — only when the human asks for it; if included, badge it clearly as `post-MVP`.

## Step 3 — BUILD: generate the wireframe(s)

- **One file per app/persona; one rendered screen per surface.** Give every surface from Step 2 a
  reachable screen (`id="s-<surface>"`), and render its bound requirements (and their implied
  states) on it.
- A persona whose PRD context is mobile (athlete/player) gets a **mobile-responsive** layout
  (full-screen on a phone via `100dvh` + safe-area, framed mockup only on desktop, bottom tab bar).
  A desktop/console persona (coach/admin) gets a sidebar layout. Shared entry point, separate files.
- Navigation: tabs / sidebar / back-links wired with a tiny show-hide router; non-nav detail
  screens (e.g. a message thread) reachable from their list and guarded so they don't break nav.
- Tag each requirement, implied state, and post-MVP item visibly so the screen-to-graph mapping is
  obvious.

## Step 4 — VERIFY + FINISH: self-audit coverage against Praxis (mandatory)

Coverage is proven by reading the **graph** back, not a local checklist:

1. **Re-read the PRD docs once** against the rendered screens — anything implied but not rendered
   means either a screen you owe or a surface binding you owe (raise the latter).
2. For **every surface** returned by `surface_coverage` / `list_surface_bindings`, confirm there is
   a real, reachable screen (`id="s-<surface>"`) and that every nav target resolves (no dead links).
   A surface with no rendered screen is, by definition, an **incomplete requirement** — fix it.
3. Re-run `incomplete_requirements("<project>")` (**BARE project name**). Anything a rendered surface
   now satisfies should no longer dangle; record each rendered-surface outcome through
   the knowledge-port policy (`docs/af-memory-policy.md`) / `record_outcome(cid, success=True)` so the graph reflects what is built. **Do
   not write any local file to mark this.**
4. Produce a **coverage table** mapping each surface (and the requirement ids it renders) → the
   screen it appears on, including implied states. If a surface is intentionally out (post-MVP not
   requested, or pure backend with no screen), say so explicitly with the reason — never silently
   drop it; an out-of-scope surface should be reflected as such in the graph, not just omitted.
5. Only after the audit passes do you report, leading with the coverage table that proves it.

**The one completeness gate.** There is no wireframe gate and no checklist file. The factory's single
`build_completeness` Stop gate enforces "are there incomplete tickets/checks for this scope?" live
against Praxis. An unrendered surface is an incomplete requirement, so it is caught by that one gate
automatically — you cannot declare the build done while a bound surface has no screen. Your obligation
is to leave Praxis honest: render the surfaces, record the outcomes, and let the live graph (not
optimism) decide done.

## Step 5 — Handoff

- Save the file(s) in the project (e.g. the app repo root); offer a `file://` link.
- Report: the apps produced, the surface→screen coverage table (with requirement ids), any genuine
  open product questions (real forks only — not things the PRD already answers), any **missing
  surface bindings** you raised back to intake, and what's out-of-scope with reasons.

## Anti-patterns (the exact failures to never repeat)

- Building from memory or a single PRD file → **read them all**.
- Authoring a private list of "screens to build" beside Praxis → **render the graph's surfaces**;
  the bindings are the contract.
- Writing any `.factory/*.json` (status/attempts/coverage/checklist) → **forbidden**; coverage lives
  in Praxis surface bindings.
- Passing the already-prefixed `prd-<project>` to the incomplete-requirements query → **pass the
  BARE name**; the prefixed form returns EMPTY and hides all the work.
- One app with role toggles when the roles are really separate products → **split into apps**.
- Drawing only the happy path → **render empty/error/offline/completed/fallback states** on the
  owning surface.
- Buttons that run real logic → **inert prototype only**.
- Saying "this covers everything" without re-querying Praxis → **prove it with the surface coverage
  table**; let the live graph, not optimism, decide done.
- Continuing when Praxis is unreachable → **fail closed and stop**; there is no local fallback.

## Compounding

When a human correction reveals a class of miss (a forgotten state, a persona split, a missing
surface), fix it at the source: get the missing **surface/binding into Praxis** (via
`af-intake`) so the gap is a first-class incomplete requirement next time, and record a
learning via the knowledge-port policy (`docs/af-memory-policy.md`). The next wireframe starts from a stricter graph, not a stricter file.
