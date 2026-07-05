# Local Side: What We Build Here

> First-pass partition. This document covers **only the things we build locally in this
> repo** — the "doing" system (the loop that runs code) plus the glue and policy that sit
> between the factory and Praxis. The companion doc [`praxis-gaps.md`](./praxis-gaps.md)
> covers what the knowledge graph owns. Both are graded against
> [`agent-coding-factory-reference.md`](./agent-coding-factory-reference.md).
>
> Decided constraint: **single agent.** Praxis partitions knowledge by tenancy/scope, not by
> agent or domain, so there is no substrate for an agent-level domain split — and the research
> says single-agent matches/beats multi-agent under equal token budgets anyway. We orchestrate
> *phases and tools*, not a crew of agents.
>
> **One carve-out (context hygiene, not orchestration):** the single decision-making agent may
> dispatch a *disposable, read-only retrieval sub-agent* to absorb bulk / multi-file reading and
> return a compact digest, so the parent's window never eats the raw noise. That is a context
> firewall, not a crew — it reads and summarizes, it never decides, edits, writes to Praxis, or
> commits, and it is never chained into a decision. A crew that divides *decision/domain* work or
> writes in parallel remains out. (Spec: `af-build`.)

---

## The boundary rule

We build it locally when it is about **running code, driving the agent loop, or holding
ephemeral task state** — none of which is durable knowledge. Anything that is durable
knowledge, retrieval, dedup, contradiction-handling, or truth-maintenance is Praxis's job
(see the gaps doc). Code lives in git; the running app's state lives in its sandbox; only
*judgments and learnings* round-trip to Praxis.

---

## A. The knowledge port (the glue) — **highest priority**

One narrow module wrapping **all** Praxis access, so the rest of the factory codes against a
clean contract and every Praxis quirk lives in one place. It owns:

- **Endpoint routing:** `/insights` for already-shaped facts (fast, low-loss) vs `/ingest`
  for raw docs (slow, lossy); never block the loop on an ingest.
- **Retrieval assembly:** turn a task into a `/context` call — the query, `top_k`, `as_of`
  when temporal recall is needed, and a token budget.
- **Mount policy:** mount the right read-only packs for a task (e.g. a "golden conventions"
  pack, a sibling-project snapshot, the PRD), unmount when done.
- **Ingestion integrity (shim for Praxis hole H6):** linearize tabular/templated input into
  atomic, lexically-distinct facts, then **audit `/candidates?state=rejected`** and confirm
  the active set is complete before trusting it.
- **Read-your-writes staging:** a small local overlay so a just-written learning is visible to
  the next step before Praxis finishes ingesting (works around async latency, H8).
- **Local fallback cache:** a copy of critical knowledge for when Praxis is cold/unreachable,
  labeled by which source answered.
- **Write-back policy (partly shims H1/H4/H5):** decide what becomes a fact, attach outcome
  metadata, when to promote project→`shared`, when to mount vs. promote.

> Everything else in this repo talks to Praxis *only* through this port.

---

## B. The lifecycle / "doing" system

The reference model's six stages — none of which Praxis performs.

### B1. Intake & spec pipeline
Turn a PRD into a machine-usable spec **and** seed the project pool in Praxis (via the port).
Owns the constitution/spec-template, completeness checking, and the table-linearization step.

### B2. Planning & decomposition
Produce a task DAG with **binary acceptance criteria** and dependency edges, each traceable to
a requirement. Sized so a task fits one context window. (Plan state can be stored as facts so
`as_of` can later answer "what did the plan believe.")

### B3. Implementation (the single agent)
The one coding agent: a unified action space (CodeAct-style) over well-designed tools
(windowed file view, edit, search, run), operating in a **sandbox**. Likely built on Claude
Code / the Agent SDK rather than from scratch. Retrieves its grounding context through the port.

### B4. Verification harness
Run the **external signals** — tests, type-check, lint, build — as blocking gates. Generate
regression tests. This is local because Praxis can't run code, and the research is emphatic
that correction must be grounded in external signal, not self-reflection.

### B5. Integration & shipping
git branch/commit, PR, observe CI, re-enter B3 with the CI log on failure, merge per policy.
Optionally deploy + route monitoring signals back into B1.

---

## C. Orchestration & long-horizon control (single-agent)

Not a crew — a controller around the one agent, with one permitted helper: a disposable,
read-only **retrieval sub-agent** the controller calls to read/search/summarize large surfaces and
return a compact digest (see the carve-out at the top + `af-build`). It is a context
firewall, never a second decision-maker.

- **Phase controller / task loop:** walk the DAG, dispatch tasks, gate transitions.
- **Goal re-anchoring:** re-inject the objective + success criteria at every context rollover
  (the cheap, proven defense against drift).
- **Context assembly, budgeting & compaction:** decide what to pull from Praxis (hot
  constitution always in; warm/cold to a hard ceiling well below the rot threshold), and
  **summarize, don't drop** at **~50–60% fill** (the lower band leaves recovery headroom). The
  compaction artifact has fixed fields — end goal · current approach · steps completed · dead-ends
  & why they failed · key file locations + roles · next step + acceptance condition (see
  `af-build`). This is the local realization of the tiered hot/warm/cold pattern *over*
  Praxis retrieval.
- **Checkpoint / replay:** serialize loop state to external storage (and snapshot the Praxis
  graph at phase boundaries via the port).
- **Saturation detector + circuit breaker:** watch round-trip count / context fill; iteration
  caps; trip on degeneration (repeated identical outputs/errors).

## D. Verification, self-correction & quality (local logic)

- **Gate taxonomy:** pre-flight (schema/AST/type before commit) · revision (test-gated retry,
  bounded) · escalation · abort.
- **Four-tier loop:** execution → correction → strategy (replan) → human, with explicit trip
  conditions.
- **Separate evaluator model** for any LLM-as-judge step (never the generator's weights).
- **Structural-health sensors:** track complexity/coupling/verbosity **deltas across
  iterations** — the structural-erosion defense (prompting alone doesn't fix it).
- **Typed exception handling:** tool-failure / planning-failure / context-overflow /
  value-conflict routed to distinct handlers.

## E. Observability

- **Append-only event log** of every model call, tool call, **port read/write**, decision
  branch — timestamp, context ID, outcome, tokens.
- **Causal trace graph** (request → sub-actions) + success-vs-failure trajectory comparison.
- **Aggregate metrics:** steps/task, tokens, error-rate by class, cost-per-checkpoint.

> Note: decision-level provenance lives here locally; **fact-level** provenance/derivation is
> the Praxis-side ask (gaps H1/H5). The event log is also what we'd mine to *produce* the
> outcome metadata the port writes back.

## F. Autonomy & human gates

- **Configurable autonomy ceiling** (run at a lower level than capability as a risk control).
- **Human gates** at irreversible actions (deploy/delete/external calls), low-confidence
  checkpoints, and strategy-loop exhaustion.

---

## Priority order (rough)

1. **Knowledge port (A)** — nothing else can talk to Praxis cleanly without it; also the home
   of the H6 ingestion-integrity shim we already know we need.
2. **Intake/spec + plan (B1–B2)** — get the first PRD into a project pool correctly and audited.
3. **Single agent + verification harness (B3–B4)** — the minimal doing-loop that can build and
   self-check one task.
4. **Orchestration/long-horizon control (C) + self-correction (D)** — make the loop survive
   long runs without eroding.
5. **Shipping (B5), observability (E), autonomy (F)** — close the loop and instrument it.

---

## What we explicitly do NOT build (it's Praxis's job — see gaps doc)

Retrieval/ranking, dedup/merge, contradiction detection + resolution, temporal validity,
read-time composition, and the durable fact store. We *consume* these through the port; we do
not reimplement them. Where Praxis has holes (H1–H8), we either shim minimally in the port or
flag them for a Praxis improvement — we don't grow a second knowledge system here.
