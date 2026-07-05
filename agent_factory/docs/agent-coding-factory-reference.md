# What an Agent Coding Factory Entails — Reference Model

> A **neutral, tool-independent** reference for what an agent coding factory is and the
> features it needs. Deliberately says nothing about Praxis — this is the "what good looks
> like" baseline. The next document maps this model against Praxis to find the holes we
> have to build. Synthesized from 2024–2026 research and production systems (Factory.ai,
> OpenHands, SWE-agent, Devin, Spec Kit, Kiro, Claude Code, Cursor, plus the arXiv lines
> cited inline).

---

## 0. Definition

An agent coding factory is an autonomous or semi-autonomous system that takes a request
(issue, ticket, PRD, spoken intent) and drives it to **shipped software** — commits, PRs,
green CI, merge, optionally deploy — with minimal human touch per cycle, and **gets better
over time** by capturing what it learns. Two properties separate a factory from a one-shot
coding agent:

1. **A closed lifecycle** — it owns the path from intake to ship, not just code generation.
2. **Compounding memory** — each run improves the next; knowledge is externalized, not lost
   when the context window clears.

---

## 1. The Lifecycle (six stages)

Each stage consumes a defined input and produces a defined artifact for the next stage.

### Stage 1 — Intake & Requirements
Turn an unstructured request into a machine-usable specification.
- **Produces:** a structured spec artifact (user journeys, acceptance criteria, success metrics).
- **Signature pattern:** spec-driven development — Spec Kit's `constitution → specify → plan
  → tasks → implement`; Kiro's `specify → plan → execute` with GIVEN/WHEN/THEN acceptance
  criteria; a **constitution / memory bank** of immutable project-wide rules that every later
  pass respects.
- **Capabilities:** intent extraction from NL; schema/template enforcement with completeness
  checklists; spec↔code traceability; a human approval gate before proceeding.

### Stage 2 — Planning & Decomposition
Break the spec into a task graph.
- **Produces:** a DAG of tasks, each with a **binary, verifiable completion condition** and
  explicit dependency edges, each traceable back to a requirement ID.
- **Granularity rule:** a task should fit in one agent context window without losing coherent
  state (≈ a small PR). Too large → loses coherence; too small → loses context.
- **Capabilities:** dependency-graph construction + topological sort; parallelism analysis
  (which tasks run concurrently); binary acceptance-criteria formulation; requirement→task
  traceability.

### Stage 3 — Implementation
Execute one task node against the codebase.
- **Produces:** modified files, executed commands, test output — recorded in an append-only
  event log.
- **Signature patterns:** **CodeAct** (executable code as the unified action space — ~20%
  over JSON-tool baselines); **Agent-Computer Interface** (SWE-agent: windowed file viewer,
  linter-gated editor, directory search — ~2× the same model's score vs raw bash); codebase
  graph retrieval (Factory's HyperCode/ByteRank); multi-trajectory sampling + test-based
  selection.
- **Capabilities:** unified action space; well-designed ACI tools; codebase retrieval;
  a **sandboxed execution environment** (Docker is the floor and *not* a true sandbox — 2025
  runc CVEs; VM-grade isolation like E2B/Landlock+seccomp is the stricter standard).

### Stage 4 — Verification & Acceptance
Decide pass/fail per task; loop back on failure.
- **Layers:** existing test suite (primary oracle); type-check/lint/build as **blocking**
  gates; agent-authored regression tests; security static analysis pre-commit; risk-tiered
  action confirmation.
- **Capabilities:** test-runner integration; blocking type/lint/build gates; agent test
  generation; pre-commit security analysis; failure→Stage-3 feedback with error context appended.

### Stage 5 — Integration & Shipping
Close the loop to a merged/deployed change.
- **The loop:** branch → commit → draft PR → CI runs → (CI fails → re-enter Stage 3 with the
  CI log as context) → CI passes → ready/auto-merge per policy → optional deploy + monitor.
- **Capabilities:** git/PR automation; CI-result observation + re-entry; PR description
  generation; merge-policy enforcement; a **monitoring→intake feedback channel** that turns
  production signals into new backlog items.

### Stage 6 — Learning / Compounding
Capture outcomes into durable, reusable memory.
- **Memory taxonomy:** *semantic* (code structure, signatures, call graphs), *episodic* (past
  decisions + rationale, bug-resolution history), *procedural* (team conventions, recurring
  workflow templates).
- **Measured impact:** persistent memory ≈ 15–28% savings in token cost and task-completion
  time on repeat tasks (Cognee 2026).
- **Capabilities:** session-persistent store (graph + vector, not just the window); episodic
  capture of decisions + why; procedural templates for recurring workflows; constitution /
  memory-bank propagation across sessions.

---

## 2. Cross-Cutting Subsystems

The lifecycle runs on four subsystems that span every stage.

### 2A. Knowledge / Context / Memory
- **Tiered context (Codified Context, arXiv:2602.20478):** **hot** always-loaded constitution
  (~conventions, build commands, routing) · **warm** per-task domain specs loaded on trigger ·
  **cold** on-demand docs retrieved via a small tool surface. (In one real 108K-line codebase:
  ~24% of codebase size in knowledge infra, 1–2 hrs/week upkeep; *null retrievals* were used
  to find undocumented subsystems.)
- **Context engineering (write / select / compress / isolate):** just-in-time retrieval;
  token budgeting with soft/hard limits per tier; **compress** (summarize, don't drop) at
  ~70–80% fill; **isolate** per agent/subtask to prevent cross-contamination.
- **Context rot:** driven by *semantic accumulation*, not raw token distance — re-injecting
  the goal lets an agent run 100K tokens cleanly; without it, drift in ~16 steps. Practical
  degradation around **8–12 supervisor round-trips**.
- **Retrieval must be hybrid:** semantic for concepts ("how does auth work"), exact/keyword
  for symbols/error-codes/file-paths. Pure-semantic on symbol names gives false positives;
  pure-keyword misses synonyms.
- **Scoping:** global (reusable patterns, prefs) · project-scoped (conventions, architecture,
  known bugs) · session-scoped (current task state). Scope at query time so service A's auth
  doesn't surface service B's payments.
- **Provenance:** append-only event stream recording which retrieved doc / memory / tool
  output drove each decision — enables attributing a bad output to stale memory vs bad
  retrieval vs model error.

### 2B. Orchestration Topology
- **Default to single-agent.** Under equal token budgets, single-agent matches or beats
  multi-agent on multi-hop reasoning (arXiv:2604.02460). Multi-agent costs ~4×–15× the tokens.
  Add an agent only when you can **name the boundary**: true parallelism with isolation,
  context-corruption filtering, weak-model+hard-task specialization, or a security domain.
- **Production-consensus hybrid:** a **supervisor/coordinator planning layer** + a **parallel
  execution tier** with **git-worktree isolation** per subagent, results validated by the
  supervisor before merge. Avoids both the swarm's duplicate/cascade failures and the
  supervisor's serial bottleneck.
- **Typed handoffs** (structured "what passes, what scope, what's expected back") beat
  conversational context-passing; role boundaries enforced (reviewer never writes features).
- **Event-stream architecture (OpenHands):** append-only EventLog = audit + deterministic
  replay + session recovery, in one structure.
- **Mediated tool access** (registry/resolver + per-call risk rating + confirmation policy)
  for multi-agent/side-effecting work; direct access only for low-stakes single-agent.

### 2C. Verification, Self-Correction & Quality
- **Gate taxonomy:** pre-flight (schema/type/AST before commit) · revision (test-gated retry,
  bounded by max_reflections) · escalation (route to human/senior agent) · abort (max-iteration
  cap + circuit breaker).
- **External signal is mandatory.** Intrinsic self-correction (no external signal) *degrades*
  coding performance — the model favors its own output. Ground correction in test/build/tool
  results (arXiv:2406.01297).
- **Separate evaluator model for LLM-as-judge** — never the generator's own weights
  (self-preference inflates win-rate ~10%). Use adversarial/multi-model review; treat
  diff/consensus as signal, not union.
- **Four-tier loop hierarchy:** execution → correction → strategy (replan) → human escalation,
  with explicit trip conditions (N identical failures → replan; M replans → human).
- **Typed exceptions** (tool-failure / planning-failure / context-overflow / value-conflict)
  routed to distinct handlers (SHIELDA) — not catch-all retry.
- **Structural-health sensors:** track complexity/coupling/verbosity *deltas across iterations*,
  not just final state.

### 2D. Observability
- **Mandatory event log:** every model call, tool call, memory read/write, decision branch —
  with timestamp, context ID, outcome, token count.
- **Causal trace graph:** parent→child spans from request through all sub-actions; enables
  root-cause replay. **Compare success vs failure trajectories** (46% better root-causing than
  single-trace).
- **Aggregate metrics:** steps/task, tokens, error-rate by class, latency per gate,
  cost-per-checkpoint; eval-score time-series to catch version drift.

---

## 3. Autonomy & Human-in-the-Loop
- **Autonomy level is a design decision separate from capability** (SAE-style levels mapped to
  agents, arXiv:2506.12469). An L4-capable factory can be *run* at L2 as a risk control.
- **Put human gates at the highest-leverage points:** irreversible actions (deploys, deletes,
  external API calls), low-confidence checkpoints, and strategy-loop exhaustion.
- **Earn autonomy incrementally:** advance the ceiling on demonstrated performance per task
  class, not on raw model capability.

---

## 4. Consolidated Capabilities Checklist

**Lifecycle**
- [ ] Structured spec with completeness checklist + constitution/memory-bank
- [ ] Binary acceptance criteria; task DAG with dependency edges + traceability
- [ ] Unified action space (CodeAct) + well-designed ACI tools
- [ ] Sandboxed (ideally VM-grade) execution environment
- [ ] Test/type/lint/build as blocking gates; agent-authored tests; pre-commit security scan
- [ ] Git/PR/CI loop with failure re-entry; merge policy; optional deploy + monitor feedback

**Knowledge**
- [ ] Three-tier context loader (hot/warm/cold) with just-in-time selection
- [ ] Hybrid retrieval (semantic + exact/keyword), namespace-scoped (global/project/session)
- [ ] Context-budget manager + compaction (summarize, not drop) + saturation detector
- [ ] Persistent cross-session stores (semantic/episodic/procedural)
- [ ] Provenance logging of what grounded each decision

**Orchestration**
- [ ] Single-agent default; topology selector with explicit multi-agent justification
- [ ] Coordinator/decomposer + parallel execution tier with worktree isolation
- [ ] Typed handoff contracts; goal re-anchoring at every rollover
- [ ] Checkpoint/replay to external storage; token-budget enforcer

**Verification & reliability**
- [ ] Gate taxonomy (pre-flight / revision / escalation / abort)
- [ ] External-signal-grounded correction loops; separate evaluator model
- [ ] Four-tier loop hierarchy + degeneration detector + circuit breaker
- [ ] Structural-health sensors; typed exception handling

**Memory safety**
- [ ] Gated writes with contradiction detection (don't write what contradicts core facts)
- [ ] Dual-track storage (mutable graph + immutable episodic log) + periodic reconciliation
- [ ] Access-scoped retrieval; temporal decay/expiry of stale entries

**Observability & autonomy**
- [ ] Append-only event log + causal trace graph + trajectory comparison
- [ ] Aggregate metrics + eval-score time-series
- [ ] Configurable autonomy ceiling; human gates at irreversible/low-confidence points

---

## 5. Risks the Factory Must Defend Against

| Risk | Severity | Core mitigation |
|---|---|---|
| Structural erosion over iterations (SlopCodeBench: 77% of runs) | High | Per-iteration complexity-delta gates; structural sensors |
| Context rot / window overflow | High | Pre-emptive compaction; iteration caps; goal re-injection |
| Error compounding in long pipelines ((1-p)^N) | High | Atomic verifiable units; consensus voting; verified intermediates |
| Self-preference bias in evaluation | High | Separate/ensemble evaluator model |
| Memory poisoning (0.1% poison → >80% attack success) | High | Gated writes; contradiction detection; access-scoped retrieval |
| Task / goal drift | Med-High | Goal anchoring each step; max-deviation detector |
| Reward / verification gaming | Med-High | Eval separate from optimization; process rewards; sandboxing |
| Degeneration loops | Med | Repetition detection; session refresh |
| Hallucinated APIs / schema violations (38% of failures) | Med | Pre/post-execution schema + AST validators |
| Catastrophic forgetting across sessions | Med | Persistent structured memory with provenance |
| Version drift (model/provider updates) | Med | Model pinning + canary eval |

---

## 6. The Big Insight for Design

A coding factory is **two cooperating systems**: a *doing* system (the lifecycle — decompose,
code, verify, ship, in a sandbox) and a *knowing* system (the cross-cutting memory — tiered
context, hybrid retrieval, provenance, gated learning). Most public progress (CodeAct, ACI,
SWE-Bench scores) is on the *doing* system; the *knowing* system is where compounding —
the thing that makes it a *factory* and not just an agent — actually lives, and it's the
less-solved half.

---

## Key Sources
- Codified Context (arXiv:2602.20478) · OpenHands SDK (arXiv:2511.03690) · CodeAct (2402.01030)
- SWE-agent / ACI (NeurIPS 2024) · Single- vs multi-agent under equal budget (2604.02460)
- Goal drift (2505.02709) · SlopCodeBench (2603.24755) · Inside the Scaffold (2604.03515)
- SSGM memory governance (2603.11768) · Six Sigma Agent (2601.22290) · AgentFixer (2603.29848)
- Levels of Autonomy (2506.12469) · Self-correction limits (2406.01297) · SHIELDA (2508.07935)
- Factory.ai (software-factory, code-droid-technical-report) · Spec Kit · Kiro · Cognee memory benchmark
