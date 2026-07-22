# Ideation — Surviving context exhaustion in the af-build loop

- **Date:** 2026-07-22
- **Focus:** Auto-clear/compact context at ticket/wave boundaries so a long af-build job stops crashing out of context.
- **Mode:** repo-grounded (agent_factory / af-build)
- **Trigger:** an af-build run crashed out of context mid-job.

## Grounding context

- **Loop shape:** af-build's *default* is an ultracode Workflow that fans the dependency-ready frontier out to parallel one-ticket workers, **each in its own worktree with a fresh context**. Only a strictly-linear DAG (≤1 ready) or a missing Workflow tool drops to the **inline** path, where the main session grinds every ticket in *one* accumulating context. (`agent_factory/skills/af-build/SKILL.md` §Execution model, §8 worker contract.)
- **State model:** all durable state lives on the Praxis ticket node (`build_state`, lease keys, `required_validations`, `pinned_checks`, run marker) — *no JSON status files*. SKILL §Long-horizon already asserts "losing the window should lose nothing" and prescribes "compact early, don't drop" at ~50–60% via a fixed compaction artifact. The crash proves that assertion is currently aspirational, not enforced.
- **Resumability already shipped (plan 003):** `resumability.py`'s structural probe + a claim-time guard in `_ticket_state.py` — a ticket is only claimed if it's reconstructable from Praxis state alone.
- **User constraint:** work runs in **waves**; every subagent in a wave must finish before any reset — so a reset can only land at the `parallel()` **wave barrier**, never mid-wave.

### Feasibility envelope (the hard constraint that prunes everything)

Claude Code today offers **no** way to do the literal ask:
- No hook or env var exposes context-% / token usage.
- `/compact` and `/clear` are interactive-only — not callable from a hook, slash-command output, or `-p` mode.
- There is **no `PreCompact` hook**. Auto-compaction exists but its threshold isn't tunable or observable.
- The **only** real context-isolation lever is the **subagent/worktree boundary**: fresh context per worker, summaries-only back to the parent.

**Consequence:** the design target is not "trigger a clear at 70%." It's **"never let the orchestrator accumulate, and make any reset lossless."**

### Topic axes
1. **Detection** — approximating "context is getting full" with no API.
2. **Trigger point** — landing a reset at the wave barrier.
3. **Reset mechanism** — shedding context without a programmatic `/clear`.
4. **State handoff** — what survives so the loop continues (largely solved by Praxis).
5. **Prevention/architecture** — restructuring so a reset is rarely needed.

---

## Survivors (ranked)

### 1. Thin orchestrator: delegate *every* ticket to a fresh subagent, including on the inline path
The fan-out path already gives each ticket a fresh worker context — which is exactly why it's *hard to crash on context*. The crash almost certainly lives on the **inline path** (linear DAG or Workflow-absent), where the main session runs FIND→FINISH for every ticket in one window. Fix: the inline loop should spawn a one-ticket **subagent** per ticket (same §8 worker contract, `isolation: worktree`) and keep only a schema-capped one-line summary. The orchestrator's context then grows by ~1 line/ticket instead of by every file read, eval run, and retry.
- **Axis:** prevention. **Basis:** `direct:` — fan-out already does this; inline "grinds the set inline" is called out in SKILL as the fragile path.
- **Why it matters:** removes the accumulation at its source rather than mopping it up. Likely fixes the actual crash.

### 2. Per-wave orchestrator relay (checkpoint-and-continue across a fresh agent)
At each wave barrier, after workers join and worktrees merge, the orchestrator hands the *next* wave to a **fresh orchestrator subagent** and returns — bounding any single orchestrator context to one wave's lifetime. Because Praxis holds all state and the resumability probe guarantees reconstructability, the relay carries nothing but the run id.
- **Axis:** reset mechanism. **Basis:** `direct:` — resumability.py + run marker already make the orchestrator disposable.
- **Why it matters:** the closest *buildable* thing to "clear between waves," using the one boundary Claude Code actually gives you (subagent spawn = fresh context).

### 3. Lossless-resume hardening: make `/af-build` idempotently resume, so a crash costs one wave, not the job
Turn the failure mode from fatal into a retry. Guarantee that re-invoking `/af-build` after any death (context crash, OOM) reconstructs the working set from the pinned `as_of` view + `pinned_checks` + log and continues. This is *claimed* today but wasn't true enough to survive this crash — promote it to a tested invariant with a wave-boundary "resume receipt."
- **Axis:** state handoff. **Basis:** `direct:` — SKILL §Long-horizon already describes the reconstruction; plan 003 shipped the probe. Gap is enforcement/testing, not design.
- **Why it matters:** cheapest insurance. Even if prevention (1/2) regresses, the job never loses more than the current wave.

### 4. Wave-boundary "compact early" ritual driven by a transcript-size heuristic
You can't read context-%, but a `Stop`/`PostToolBatch` hook *can* stat the `transcript_path` (bytes/lines) and, past a threshold, write a `compaction_due` flag onto the run marker in Praxis. The loop reads that flag **at the wave barrier** and runs af-build's existing compaction-artifact protocol (end goal, current approach, dead-ends, next binary acceptance) before the next wave. Detection is approximate but the trigger point is exact and safe.
- **Axis:** detection + trigger. **Basis:** `reasoned:` — transcript byte-size is a monotone proxy for fill; the compaction artifact already exists in SKILL.
- **Why it matters:** the honest version of "detect 70%" — a heuristic signal wired to the one safe boundary, no fictional API.

### 5. Workflow budget-gate + relaunch a fresh Workflow run between wave-batches
The Workflow tool exposes `budget.spent()/remaining()`. Cap output per Workflow run; when the batch approaches the cap, the script ends cleanly and the skill relaunches a **fresh** Workflow run (new engine, fresh agent contexts) for the remaining frontier. Bounds the parent that awaits the Workflow, and each relaunch resumes from Praxis state.
- **Axis:** prevention. **Basis:** `direct:` — `budget` + `resumeFromRunId` are real Workflow-tool features.
- **Why it matters:** protects the fan-out path's *await/integration* context, which is the fan-out path's only real accumulation point.

### 6. Schema-cap what re-enters the orchestrator per ticket
Force every worker/frontier return through a tight JSON schema (ticket id, pass/fail, ≤N-token note) so no worker can leak raw file contents or verbose eval output back up. Bounds per-ticket orchestrator growth by construction — the "init process only reaps children" model.
- **Axis:** prevention. **Basis:** `reasoned:` — frontier agent is already a "cheap read-only dispatcher"; extend the discipline to worker returns.
- **Why it matters:** makes idea #1's "one line per ticket" a guarantee, not a hope.

---

## Rejected (with reasons)

- **Hook that triggers `/clear` at 70%** (the original framing) — **not buildable**: no context-% signal reaches any hook, and `/clear`/`/compact` can't be invoked programmatically. This is the headline finding, not a nitpick.
- **`PreCompact` hook to save state before compaction** — the hook does not exist.
- **Tune `autoCompactThreshold` to 0.7** — not exposed as a setting.
- **External Agent-SDK Python orchestrator calling `claude -p` per ticket** — real, but abandons the Claude Code skill/Workflow model for a bespoke coordinator; strictly heavier than idea #2/#5 for the same benefit. Over-engineered.
- **`--resume` the same session between batches** — reuses (accumulates) the same context; doesn't reset anything.

---

## Recommended thread to pull first

**#1 + #3 together**: make the inline path delegate per-ticket to a fresh subagent (stop the accumulation), and harden idempotent resume (make any residual crash cost one wave). #2 and #4 are the natural follow-ons if crashes persist after that. Everything here respects the hard constraint that the reset lands only at the wave barrier.
