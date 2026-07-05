# Agent Factory: Vision

> High-level document. This explains *what* we're building and *why* — an agent factory
> that uses Praxis as its knowledge backbone to build real software projects end to end.
> It is intentionally above the implementation. For the substrate it builds on, see
> [`praxis-and-how-we-use-it.md`](./praxis-and-how-we-use-it.md). The first project it must
> build is described in [`inspiration/`](./inspiration/).

---

## 1. The goal

Build an **agent factory**: a system that takes a project's requirements and drives the
work of building that project to completion — planning it, implementing it, verifying it,
and learning from the process so the next project is built better.

The factory is not a single agent. It is a harness that orchestrates agents, and its
defining feature is **memory that compounds**. Every project it builds should make it
better at building the next one. That memory lives in **Praxis**, a knowledge graph we
lean on as heavily as possible.

Two things are being built in parallel, and they validate each other:

1. **The factory** — the reusable harness (this repo).
2. **The first project** — a Team Mental Performance app (the `inspiration/` spec), used as
   the factory's first real workload. Building it is how we discover where the factory and
   Praxis are strong and where they fall short.

## 2. Why Praxis is the center

A coding agent's quality is gated by the context it's given. The hard, recurring problems —
what knowledge is relevant to this task, what contradicts what, what we already learned and
shouldn't relearn, what's a duplicate — are *knowledge problems*, and Praxis already solves
them (similarity retrieval, distillation, dedup, contradiction handling, provenance).

So the factory's bet is: **push all knowledge-shaped work into Praxis, and keep the harness
thin.** The harness authenticates, writes facts, asks for context, and audits what landed.
It does not build its own retrieval, its own dedup, or its own truth-maintenance — Praxis is
those things. This is the strong opinion the whole design is organized around.

## 3. The two halves of the factory

The factory has two modes of work. (Whether these are two literal phases or one continuous
loop is an open design question — see §6 — but the *work* is real either way.)

### 3.1 Plan building — putting the project into the graph

Take a project's requirements (the `inspiration/` PRD) and turn them into an **isolated
knowledge state** for that project inside Praxis. This is the project's source of truth: the
requirements, constraints, data model, and decisions, stored as atomic facts the executor
can query.

Isolation comes from **tenancy**, not snapshots: the project's facts live under a
per-project principal (`shared = false`), so they're queryable in their own right but never
leak into other projects. The factory queries them alongside the **general pool** of shared,
reusable knowledge in a single call.

This phase has one non-negotiable discipline: because Praxis ingestion silently drops
tabular/near-duplicate facts and the PRD is tabular-heavy, plan-building must shape
requirements into atomic, distinct facts and **audit what was rejected** before trusting the
plan. A plan built on a silently-incomplete graph is the factory's worst failure mode.

### 3.2 Execution — building the project from the graph

Build the actual software. For each unit of work, the factory:

1. **retrieves** the relevant context from Praxis — the project's own facts plus applicable
   general rules,
2. **acts** — an agent does the work (writes code, runs it, tests it),
3. **verifies** the result,
4. **writes back** — confirmed fixes and learnings go into the graph, gated through the
   contradiction engine so they sharpen the memory instead of poisoning it.

Execution pulls from a **more general pool** than plan-building: project facts *and* the
accumulated cross-project knowledge. As the factory works and finds fixes, those learnings
are inserted back, and the loop continues. Generalizable learnings are promoted from the
project pool into the shared general pool so future projects inherit them.

## 4. How knowledge is partitioned

Two layers, both queryable in one `/context` call, distinguished by provenance:

- **General pool** (`shared = true`) — conventions, patterns, and learnings that should help
  every project. This is what compounds across projects.
- **Project pool** (per-project principal, `shared = false`) — one project's requirements and
  in-flight learnings. Isolated, disposable, promoted-from selectively.

The PRD requirements seed the **project pool**. The general rules and accumulated learnings
live in the **general pool**. The factory's intelligence grows by moving vetted knowledge
from the former into the latter over time.

## 5. The first workload: the Team Mental Performance app

The factory's first real job is to build the app specified in `inspiration/` — a team-oriented
mental-performance app built around a one-screen daily flow (a shared daily prompt, a team
habit checklist, a quick personal check-in, and aggregate-only team participation stats),
with coach/captain/athlete roles and a coach admin surface.

We chose this as the first workload because it is **realistically sized**: a real data model,
real role-based access, real screens and flows, and a tabular-heavy spec — which immediately
exercises the factory's hardest knowledge problem (ingestion integrity). Building it tells us
where Praxis is sufficient and where the factory harness has to fill the gap.

The expectation is explicit: **Praxis alone will not build this end to end.** That gap is the
reason the factory harness exists. Each place the factory has to step in is a place we learn
something worth feeding back into the general pool.

## 6. What's settled vs. open

**Settled (these are decisions, grounded in how Praxis works):**
- Praxis is the single durable memory; the harness stays thin.
- Isolation is via tenancy + the `shared` flag, not snapshots.
- Snapshots are for checkpoint/rollback only.
- Plan-building must audit the rejected pile; the tabular PRD cannot be trusted to ingest cleanly.
- Knowledge-shaped work (retrieval, dedup, contradictions, provenance) is Praxis's job, not ours.

**Open (to resolve as we design the build):**
- One continuous loop vs. two literal phases (plan / execute).
- How hermetic each task's knowledge inputs should be (declared up front vs. queried ad hoc).
- The promotion gate from project pool → general pool.
- How verification outcomes feed back into fact trust so the pool self-cleans.
- What the orchestrator actually is (a librarian that owns the Praxis boundary, with thin
  execution agents, is the leading candidate).

## 7. How to read the docs

- [`praxis-and-how-we-use-it.md`](./praxis-and-how-we-use-it.md) — the substrate: what Praxis
  is and how we use it.
- [`agent-factory-vision.md`](./agent-factory-vision.md) — this document: the goal and shape.
- [`inspiration/`](./inspiration/) — the first project's requirements, the factory's first workload.
- *(next)* an architecture/build document — how the factory is actually constructed on top of all this.
