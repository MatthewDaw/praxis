---
date: 2026-06-26
topic: praxis-pr-knowledge-eval-pilot
---

# Praxis PR-Knowledge Eval Pilot — Requirements

## Summary

A feasibility-and-instrumentation pilot that measures whether Praxis helps an agent fix real GitHub issues. For each instance in a recent SWE-rebench sympy slice, ingest the pre-`base_commit`, non-fix PR window into a fresh per-instance Praxis org; run Sonnet to fix the issue with vs without Praxis (agentic MCP retrieval, rework loop driven by the agent's own reproduction test); grade with the WSL2 arm64 SWE-bench harness. The headline readouts are the unconditioned (ITT) effect and the rate at which relevant knowledge even exists — the leading indicator for whether a powered study is worth building.

## Problem Frame

The application-side question behind Praxis is whether knowledge distilled from a repo's own development history makes a coding agent measurably better and cheaper at fixing issues. The existing dogfood eval answers a narrow, sealed-box version (footgun-avoidance on curated cases); this pilot tests the realistic version — an agent fixing an actual issue end to end, with vs without Praxis.

Three forces make an honest verdict hard, and shape the pilot into a feasibility study rather than a quantitative gate:

- **Contamination.** Frontier models have memorized famous benchmark fixes (SWE-bench Verified file-localization from issue text alone runs ~72%), so a Verified control is a *memorization floor*, not a zero-knowledge baseline — biasing the whole experiment toward null. A recent, less-memorized substrate is required to get an interpretable control.
- **Power.** At pilot scale (~10 instances × 3 trials), per-arm CIs are ±20–30pp while the expected effect is small (published knowledge-augmentation lifts are ~4pp). No quantitative claim survives that noise.
- **Prior art predicts null-on-average.** The closest published system to Praxis (CommitDistill: mine commits/PR discussions into typed knowledge units, retrieve, inject) was null on aggregate but **+0.12–0.14 on hard cases where the control failed**. We should expect the same shape and instrument for it.

The pilot's job is therefore to (a) prove the harness on a decontaminated substrate, (b) measure how often relevant knowledge even exists for real issues, and (c) produce honest directional estimates and hard-case case studies that decide whether a powered, strictly-decontaminated run is worth it.

## Key Decisions

- **Per-instance org isolation (point-in-time snapshot).** Each instance gets a fresh Praxis org holding only PRs merged before its `base_commit`. This makes the org a point-in-time snapshot, immune to the temporal-contradiction problem: there is no "future" knowledge in the org to contradict the past, and in-window contradictions resolve to the latest-as-of-`base_commit` value on the stock ingest path. Isolation rides on the org dimension; the user identity stays fixed. (The temporal-reuse alternative — one org per repo with `valid_at` backdating — requires temporal-supersession semantics Praxis's ingest path does not yet have, and is deferred.)

- **Substrate: SWE-rebench recent sympy slice now; strict 2026 mining later.** SWE-rebench (`nebius/SWE-rebench`) is SWE-bench-format-compatible and sympy-rich (719 sympy instances), and its randomly-mined recent PRs are far less memorized than Verified. Its current release ends 2025-04, so it is *less* contaminated, not strictly post-cutoff for a Jan-2026 model. Strict post-cutoff (Feb–Jun 2026) fresh-mining — genuinely unseen by the model — is reserved for the powered scale-up.

- **ITT primary, pre-treatment relevance stratum secondary.** Gating the comparison on "Praxis retrieved relevant knowledge" is post-treatment collider conditioning (the clinical-trial per-protocol fallacy) — the control units in that subset are not a valid counterfactual, and it is not fixable by analysis. The valid design: unconditioned ITT as the primary readout, plus a pre-specified, exploratory secondary that stratifies on a *pre-treatment* `R_exist` oracle (does relevant knowledge exist in the org at all, independent of whether retrieval fired).

- **Agentic MCP retrieval, with a silence threshold.** Treatment delivers Praxis as the `praxis_get_context` MCP tool (faithful to real use), not pre-injection. Because one irrelevant-but-topical fact measurably degrades Sonnet (15–25% drops, steeper than GPT), retrieval must prefer silence — leaning on Praxis's existing retrieval floor to inject nothing rather than weak facts. Pre-injection is held only as a tie-breaker diagnostic if the headline result is null.

- **Cost-to-correct via a rework loop with agent-authored repro tests.** The agent first writes a failing test reproducing the issue, then fixes until its own repro passes; on a graded failure it reworks with the full issue text + its repro, never seeing the hidden gold tests. This is established practice (Agentless, Dynamic Cogeneration) and leak-free by construction; the self-repro is a direction signal, the gold tests are the sole correctness oracle.

- **Ingestion cost is amortized; retrieval cost is separated to avoid double-counting.** Per-instance re-ingestion is an artifact of the isolation mechanism, not how Praxis runs in production, so ingestion cost is reported as a separate, amortized line — never charged in full against each issue's cost-to-correct. Retrieved-context tokens already live inside the agent's own cost; only the incremental retrieval *operation* overhead (query embedding + MCP round-trip) is tracked separately.

## Requirements

**Substrate and instance selection**

- R1. Pilot instances are drawn from SWE-rebench's most recent sympy slice (filtered by `created_at` to the newest available), in SWE-bench format (gold patch, test patch, `FAIL_TO_PASS`/`PASS_TO_PASS`). Target ~10 instances.
- R2. Selection is naturalistic (by solvability/build-ability within the recent slice), not cherry-picked for knowledge relevance.
- R3. Each selected instance is hand-screened to exclude solution-in-issue leakage (the fix appearing in the issue text); the screening outcome is recorded per instance.

**Knowledge ingestion**

- R4. For each instance, create a fresh Praxis org and ingest the merged PRs in a bounded window before `base_commit`, oldest-PR-first, landing facts as active.
- R5. The instance's own fix-PR — and any PR that restates the fix — is excluded from its ingestion window.
- R6. A leakage guard verifies no ingested fact restates that instance's gold diff before the arm runs.
- R7. Orgs are per-instance with no cross-instance fact sharing; trials of the same instance share that instance's org (retrieval is read-only).

**Arms and agent execution**

- R8. Two arms with the model held fixed (Sonnet): Treatment exposes `praxis_get_context` pinned to the instance's org; Control has no Praxis access. Prompts, tools, and budgets are otherwise identical.
- R9. The agent runs where it can execute the repo's own tests (never the hidden gold tests) and, in Treatment, reach the Praxis MCP.
- R10. Treatment retrieval prefers silence: it relies on Praxis's retrieval floor so low-confidence queries contribute no facts rather than weak ones.

**Rework loop**

- R11. The agent first authors a failing test reproducing the issue from its description, then fixes until that self-authored test passes.
- R12. On a graded failure, the agent is re-prompted up to K rounds (cap small, e.g. 2) with the full original issue text, a "still not resolved" signal, and its own repro — never the gold tests.
- R13. The self-authored repro is a direction signal only; the hidden gold tests are the sole correctness oracle.

**Grading**

- R14. Each attempt is graded by the WSL2 arm64 SWE-bench harness against the instance's `FAIL_TO_PASS` + `PASS_TO_PASS`; resolved = both pass. Agent patches are LF-normalized before grading.
- R15. Patch quality is spot-checked on resolved instances to guard against weak-test false positives.

**Metrics and cost accounting**

- R16. Per attempt and arm, record resolve outcome, agent cost, tokens, turns, and rework rounds. Cost-to-correct = cumulative agent cost to the first resolved attempt.
- R17. Report both average cost per instance and effectiveness-aware cost-per-resolved (the established SWE-bench cost metrics).
- R18. Track ingestion cost (server-side distillation cost/tokens for the PR window) separately and report it amortized — not charged in full against any instance's cost-to-correct.
- R19. Track retrieval-operation overhead (query embedding + MCP round-trip) separately; do not re-count retrieved-context tokens already captured in agent cost.

**Analysis**

- R20. Run ≥3 trials per instance per arm; report point estimates with explicit wide CIs and trial-level variance. No statistical-significance claims.
- R21. Compute a pre-treatment `R_exist` oracle per instance: an offline "perfect-retriever" probe of whether relevant knowledge exists in its org, independent of whether retrieval fired in Treatment. Report the `R_exist` hit-rate as a first-class outcome.
- R22. Primary readout is the unconditioned ITT effect (all T vs all C). The secondary, pre-specified and labeled exploratory, is T vs C within the `R_exist = 1` stratum.

## Success Criteria

The pilot succeeds — i.e., justifies building a powered, strictly-decontaminated scale-up — when all three hold:

- The harness runs end to end on the decontaminated substrate: SWE-rebench instances build and grade on the WSL2 arm64 setup, and the full ingest → two-arm → rework → grade loop completes.
- The `R_exist` hit-rate is non-trivial — relevant knowledge actually exists for a meaningful fraction of real issues (if it almost never does, the direction is questionable regardless of effect size).
- The directional ITT and `R_exist = 1` estimates, plus hard-case case studies, are promising enough to motivate a ~130-instance/arm confirmatory run.

A null ITT is an *expected, acceptable* pilot outcome and is not by itself a kill, given the power and residual-contamination caveats. The go/no-go rests on the hit-rate and the directional hard-case signal, not on aggregate significance.

## Scope Boundaries

Deferred to scale-up (not built for the pilot):

- Option B — one org per repo with `valid_at` backdating and temporal-supersession reuse.
- Strict post-cutoff (Feb–Jun 2026) fresh-mined instances.
- The powered ~130-instance/arm confirmatory run (likely needs a cloud x86 grader).
- Multi-model comparison (pilot is Sonnet-only) and the pre-injection delivery variant (kept only as a null-result diagnostic).
- django and non-pure-Python repos (matplotlib, scikit-learn) — sympy-only for the pilot until arm64 builds are confirmed elsewhere.

## Dependencies / Assumptions

- **Grader re-validation on SWE-rebench (first implementation step).** The arm64 grader is proven on Verified sympy; SWE-rebench instances' env-specs building and grading on arm64 is unverified. Gate the pilot on a gold-patch → RESOLVED check for one recent SWE-rebench sympy instance before any A/B.
- **In-container agent execution.** The agent must run with test-execution and (in Treatment) network access to the host Praxis MCP — new infra beyond the host-side, edits-only smoke tests already done.
- **LF patch normalization.** Agent edits originate on the Windows host; patches must be LF-normalized before grading (known from prior smoke work).
- **Praxis backend availability.** Per-instance orgs created against a running backend, with each agent's MCP config pinned to its instance's org.
- **Underpowered by design.** All conclusions are directional; the pilot cannot and will not claim significance.

## Outstanding Questions

Resolve before planning:

- Concrete definition of the `R_exist` oracle — what counts as "relevant knowledge exists" for an instance (e.g., oracle retrieval over the org against the gold-changed files / issue text, above a fixed relevance bar).

Deferred to planning:

- In-container agent + MCP wiring approach (Claude Code inside the SWE-bench container vs a checkout with the repo's test env plus network to the host MCP).
- Ingestion window size (N PRs before `base_commit`) and the exact fix-PR / fix-restating-PR exclusion logic.
- Rework cap K.

## Sources / Research

- CommitDistill (arXiv 2605.18284) — closest prior system to Praxis (PR/commit history → typed knowledge units → inject); null on aggregate, +0.12–0.14 on hard cases. Sets the expected effect shape.
- Post-treatment / per-protocol conditioning bias — Frangakis & Rubin (2002) principal stratification; ITT-vs-per-protocol literature. Basis for the ITT-primary + pre-treatment-stratum gate.
- On Randomness in Agentic Evals (arXiv 2602.07150) and stochasticity/ICC work (arXiv 2512.06710) — σ > 1.5pp at temperature 0; small-N power. Basis for the feasibility reframe and ≥3-trial + wide-CI reporting.
- SWE-bench contamination/leakage — memorization audit (arXiv 2512.10218), SWE-Bench+ (arXiv 2410.06992: ~33% solution-in-issue), weak-test analyses. Basis for substrate choice and R3/R15 screening.
- Agent-authored reproduction — Agentless (arXiv 2407.01489), Dynamic Cogeneration (arXiv 2601.19066), SWT-Bench. Basis for the rework loop; full-issue-text and capped-iteration refinements.
- Context-quality effects on Claude — SWE Context Bench (arXiv 2602.08316), long-context degradation (arXiv 2510.05381). Basis for the silence-over-low-confidence retrieval decision.
- Established cost metrics — SWE-Bench+ average-cost-per-instance and effectiveness-aware cost-per-resolved. Basis for R17.
