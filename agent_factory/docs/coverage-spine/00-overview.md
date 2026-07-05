# The Coverage Spine — overall design

> Consolidated design for reframing the agent factory around **data-driven coverage gates**.
> Companion files: [`01-praxis-changes.md`](01-praxis-changes.md) · [`02-planner.md`](02-planner.md) · [`03-eval-agent.md`](03-eval-agent.md) · [`05-coverage-engine.md`](05-coverage-engine.md) · [`06-validation-harness.md`](06-validation-harness.md).

## Core principle
Praxis is the foundation. The factory's method is **heavy planning → exhaustive verification → insert the verified plan into Praxis, so that NO decision remains for execution** (execution is a mechanical reducer). Build features so they are ~80% a planning-surface problem.

## The one spine
Every substantive gate is the same machine:

> **for every item in a coverage SET, prove it is addressed against a TARGET; recompute from evidence; never trust a self-flag; loop-guard; fail-CLOSED (Praxis unreachable ⇒ block); block until zero holes.**

The gate spine has collapsed to the single `hooks/build_completeness_gate.py` — every in-scope
ticket's pinned checks (the coverage SET, resolved live from Praxis) must be passed. The old
per-phase gates (plan-audit, review, wireframe, preflight) are gone: their work is now ordinary
tickets/checks in Praxis that this one gate enforces. The gate reads Praxis live and fails
**closed** (Praxis unreachable ⇒ block).

The reframe: **make the coverage SET come from Praxis instead of being hard-coded**, and recognize that planning, validation (the coding agent), and the eval are all *the same spine with different parameters*.

## The parameters that vary (none forks the spine)
| Parameter | Planning | Coding agent (validation) | Plan-repro eval |
|---|---|---|---|
| Coverage **SET** | planning checklist (Praxis `planning` snapshot) | validation checks bound to the surface (Praxis `validation` snapshot) | the **golden** feature set (checked-in file) |
| **TARGET** | the plan / requirement graph | the code | the reproduced plan |
| Per-item **evaluator** | semantic "is this represented in the plan?" | "does the code pass this?" (run test / agent-eval) | semantic "is this feature covered?" |
| **Remediation** on a hole *(lives in the AGENT, not the gate)* | ask the user **or** expand the plan | run/write a test, then fix the code | ask the user **or** expand the plan |

Gates never remediate — they block and report. The differing responses are the agent loop the gate drives, so they don't split the engine.

## Data-driven gates (the mechanism)
The single gate reads Praxis **live** (via the stdlib-only `hooks/_praxis.py` client — no MCP auth
replicated, see `docs/factory-state-contract.md`) and **fails closed** if Praxis is unreachable.
There is **no `.factory/*.json` manifest** — that pattern is deleted. The flow is:

> **at ticket start the skill RESOLVES which checks apply by QUERY (tag ∪ surface ∪ semantic) against
> the active checks in Praxis, and PINS the resolved set onto the TICKET NODE (`meta.pinned_checks`)
> as this pass's completion contract → the build records each pass ON THE TICKET NODE → the gate reads
> the ticket's pinned checks live and enforces "every pinned check is passed."**

Result: the hook is fully generic (it knows no hard-coded `GAP_LENSES`); all check content lives in
Praxis. **Adding a check = adding a Praxis fact. No code change, no file written.** A check is
declarative and read-only during builds; it owns its own applicability predicate (`meta.applies_to` /
bound surface), and a ticket never carries an authored list of its checks — *which checks apply is a
query resolved fresh at start*, never pre-bound.

Check rigor by kind:
- `deterministic` → a registered `Gate` in `src/agent_factory/gate.py` `REGISTRY` (today's `plan_gate` rules); re-run live.
- `agent-evaluated` → an **independent** (evaluator ≠ author) recorded pass + evidence on the ticket node; closure is recomputed from the evidence (exactly how challenges/findings work today).

## Eval vs. gate (same spine, different epistemics)
- The **eval** scores against a **GOLDEN** (ground truth — the known-good plan) → it *measures the planner's hole rate*. Offline.
- The **live gate** has no golden; it enforces against a **CHECKLIST** of considerations from Praxis → it *forces the planner to address each*. Inline.
- The eval is therefore the **meta-proof of the gate**: if the checklist-driven planner reproduces the golden with zero holes, the checklist has no holes.
- **The golden's `derived: true` features are the evidence for what the planning checklist must contain** (e.g. `AUTH-password-reset` being derived ⇒ the checklist needs a "credential-recovery flow" item). The eval and the planning checklist co-design each other.

## Brownfield = greenfield
Nothing in the spine cares about existing vs. empty code. A refactor is just a plan whose target acts on existing code; the gates and coding agent don't branch.

## Closed-loop learning (why this compounds)
- A fix — *especially of something built wrong the first time* — must persist a **lesson** to Praxis (`category="learning"`), promoted to `general-pool` when general. Enforce with a gate (`lesson_gate`).
- A lesson is **proven by an eval** that reproduces the mistake-prone situation and asserts the fix — across any surface (planning, validation, …). A lesson with no passing eval is "unproven."
- `factory-fix` is the thin **write path**: fix + PR + add the check/lesson to Praxis.

## Current workstreams
1. **Planning eval** (this thread) — coverage of a plan reproduced from `docs/inspiration/` vs. the golden. See `03-eval-agent.md`. Lives in `evals/plan_repro/`.
2. **Validation harness / checks on `../team-app`** — BUILT + wired live
   (`src/agent_factory/validation_target.py` + `af-build` wiring + the
   `af-intake` add-check amend mode; see
   [`06-validation-harness.md`](06-validation-harness.md)). **Checks live entirely in Praxis**
   (`category="check"`, `scope="validation"`, `meta.applies_to`/`meta.run`); `af-intake` is the write
   path. `af-build` pulls checks from Praxis + regresses bound tickets, runs each
   `meta.run` as a blocking gate at verify time, `build_completeness_gate`
   forces the re-pick. Insert a check → ticket regresses → coding agent must make it pass. No file.
3. **The shared coverage engine** (`evals/plan_repro/coverage.py`) — built once, instantiated by both planning-coverage and validation-coverage. Design: [`05-coverage-engine.md`](05-coverage-engine.md) (per-part sweep + thorough per-part query + targeted adversarial; scales to thousands of insights).

## What exists today (ground truth)
- Gate machinery: `hooks/*.py`, `src/agent_factory/gate.py` (uniform `Reason`/`Verdict`/`Gate`/`REGISTRY`), `src/agent_factory/plan_gate.py` (deterministic rules), `src/agent_factory/build_target.py`.
- Eval harness: `evals/` (deterministic `case.yaml` cases under `evals/cases/plan_gate/`; loader rejects non-deterministic input).
- The golden: live Praxis `prd-team-app` graph (~78 requirements) → extracted to `evals/plan_repro/team-app/golden-features.yaml`.

## Relevant memories
`factory-dev-methodology`, `af-planning-validation-snapshots`, `factory-closed-loop-learning` (in the user's memory dir).
