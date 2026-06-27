# SWE-rebench PR-knowledge eval pilot

A feasibility-and-instrumentation A/B harness that measures whether Praxis helps an
agent fix real GitHub issues. For each instance in a recent SWE-rebench sympy slice it
ingests the pre-`base_commit` non-fix PR window into a per-instance Praxis **space**,
runs Sonnet to fix the issue **with** vs **without** Praxis (agentic MCP retrieval + a
reproduction-test rework loop), grades with the WSL2 arm64 SWE-bench harness, and reads
out cost-to-correct as an unconditioned ITT effect plus a pre-treatment
relevance-stratified secondary.

> **Running it on your machine?** Start with the **[Operator's Guide](OPERATING.md)** —
> prerequisites, the run command, instance selection, the three performance knobs +
> hardware tuning, troubleshooting, and how to read the results. This README is the
> architecture/reference; OPERATING.md is the runbook.

## Read this before reading any number

This is a **feasibility study, not a quantitative verdict.** The deliverable is the
harness (and the `R_exist` hit-rate + a directional read), not a significant effect
size. Specifically:

- **Underpowered by design.** ~10 instances × a few trials per arm yields very wide
  confidence intervals (±20–30pp territory). The analysis reports CIs *wide* and flags
  low trial counts; do not read a tight-looking interval as power we don't have.

- **Residual within-window contamination.** The SWE-rebench snapshot ends ≤ 2025-04,
  inside the model's training cutoff, so the control arm is a *reduced-but-nonzero*
  memorization floor rather than a clean no-knowledge baseline. Strict post-cutoff
  decontamination is deferred (see the plan's scope boundaries).

- **Per-instance-space mechanism.** Each instance gets a private **space** (named working
  graph) inside one fixed `swebench_eval` org — effective tenant `dev-user::space:<id>` —
  holding only PRs merged before its `base_commit`, ingested oldest-first as active facts,
  with the fix-PR (and any fix-restating PR) excluded and a leakage guard that fails loudly
  if an ingested fact restates the gold diff. A space per instance is a point-in-time
  snapshot — immune to the temporal-contradiction problem on the stock ingest path — and
  the stable space id makes a **rerun reuse** the prior snapshot instead of re-distilling.
  (Spaces replaced the heavier org-per-instance: the eval is one tenant running many
  isolated working graphs, which is exactly what spaces are for.)

- **`install_config` grader adaptation.** The official swebench harness mis-grades
  SWE-rebench instances out of the box. The grader (U2) monkeypatches `arch="arm64"`
  across the three `make_test_spec` import sites and injects the instance's own
  `test_cmd` + `parse_log_pytest` into the in-memory swebench maps. Nothing under the
  installed swebench is edited.

- **A null ITT is an allowed outcome.** The primary ITT is unconditioned (all treatment
  vs all control); the `R_exist=1` stratum is **exploratory** and labeled as such
  everywhere. The feasibility decision rests on the `R_exist` hit-rate + a directional
  hard-case signal, *not* on aggregate significance. The gate never claims significance,
  and a flat ITT alone does not flip the verdict to fail.

## Running it

```bash
# Offline: re-aggregate a committed records dict (no backend, no Docker, no claude).
uv run python -m knowledge.evals.swebench.run \
  --from-records knowledge/evals/swebench/tests/fixtures/records.sample.json

# Live: full pipeline (select -> ingest -> arms -> grade -> analyze).
uv run python -m knowledge.evals.swebench.run --instances 10 --trials 3

# Bias selection toward HARD bugs (biggest gold patch) where a no-knowledge control
# plausibly fails — i.e. where Praxis has headroom to move the resolve rate, not just cost:
uv run python -m knowledge.evals.swebench.run --instances 10 --trials 3 --order hard

# Least-contaminated slice: only instances merged on/after a date (nearest the training
# cutoff). Composes with --order hard for the recent-and-hard corner:
uv run python -m knowledge.evals.swebench.run --instances 10 --trials 3 --order hard --since 2025-02-01

# Parallel: run 2 (instance, trial) jobs at once, capping concurrent Docker grades at 2.
# Safe because each arm borrows an isolated git worktree; tune for your RAM (see below).
uv run python -m knowledge.evals.swebench.run --instances 9 --trials 3 --order hard --since 2025-02-01 \
  --workers 2 --grade-concurrency 2
```

**Parallelism** (`--ingest-workers`, `--workers`, `--grade-concurrency`). The live run has
two parallel phases with separate knobs, because they're bottlenecked on different
resources:

- **Ingest pre-loop** (`--ingest-workers`, default 3). Per-instance ingestion (distill the
  PR window into the instance's space, compute `R_exist`, seed its MCP cache) is independent
  across instances and almost entirely **LLM/IO-bound on the backend**, so it fans out well
  even past core count. The first instance runs alone (it first-creates the shared eval
  *org*; distinct spaces never race, but a concurrent first org-create could); the rest run
  under a bounded pool. Each instance's PRs are **batched** into 1–2 `/ingest` posts (≤128 KB
  each) rather than one post per PR, so the per-route **30/min request** limit
  (`knowledge.serve.rate_limit`) no longer throttles ingestion — and since the client has no
  429 backoff, that batching is also what keeps a higher `--ingest-workers` from 429-crashing
  the run. The remaining ceiling is the backend's concurrent-distillation throughput + the
  shared API tokens/min (not cores), so you can push past 3 until those saturate.
- **Arms phase** (`--workers`, default 1; `--grade-concurrency`, default 2). The run flattens
  work into `(instance, trial)` jobs and runs `--workers` at once. Each arm borrows an
  **isolated git worktree** from a pool sized to `--workers` (all worktrees share one sympy
  clone's object store, so they're cheap), so concurrent agents never edit the same files.
  The `claude` agent is LLM/network-bound (oversubscribe cores happily), but the **grade** is
  a WSL Docker container building + running sympy's tests — CPU- and memory-heavy — so
  `--grade-concurrency` caps concurrent grades *independent of* `--workers`.

On a 16 GB box, `--ingest-workers 3 --workers 4 --grade-concurrency 2` is a reasonable
shape; drop `--grade-concurrency 1` if WSL OOMs.

**Instance selection** (`--order`, `--include-leaked`, `--since`). By default `select` takes
the newest supported-version (sympy 1.12–1.14) instances and drops only **verbatim**-leaked
ones (the issue literally pastes a fix line — ~8 of 101). It does *not* drop the far more
common "issue names the changed function" cases, which aren't real leakage. `--order hard`
instead picks the largest-gold-patch instances (more files / failing tests) — the bugs a
contaminated control is least likely to one-shot. Caveat: the hardest instances skew older
(more training-memorized), so `hard` trades recency for difficulty; pick per what you're
probing. `--since YYYY-MM-DD` keeps only instances created on/after that date — the
least-contaminated slice nearest the model's training cutoff — and composes with `--order`.
Note the corner is thin: `--order hard --since 2025-02-01` is only **9** instances (and just
one, sympy-27797, is a genuinely large patch), so the recent-and-hard slice trades fleet
size for decontamination.

The `--from-records` path needs **neither** the backend nor Docker — it only runs U7's
pure aggregate/gate/report over a committed records dict.

A **live run requires** the Praxis backend up (dev tenant) and **WSL2 Docker**:

- **Praxis backend (dev tenant).** Bring it up with the **praxis-up** skill (Postgres +
  FastAPI on `:8000`). If the backend is down, the live run exits with a clear
  `Praxis backend not reachable at <url>; run the praxis-up skill` message, not a
  traceback. Per-instance orgs are created against the dev tenant.
- **WSL2 + Docker Desktop** (WSL integration enabled) for the arm64 swebench grader. The
  agent runs in a host worktree; gold grading happens in the canonical container.

The live path builds one shared sympy clone with a worktree pool (each arm borrows an
isolated tree, reset to the instance's `base_commit`), and per instance a MCP identity
cache pinning that instance's **space** for the treatment arm. These live-orchestrator
responsibilities are factored in `run.py` (`_load_instances`, `_build_worktree_pool`,
`_seed_mcp_cache`, `make_grade_fn`, and the `run_arm_wired` wrapper) and are exercised
manually outside CI.

## Outputs

- `RESULTS.data.json` — machine-readable `{records, report, gate, rexist_map}`.
- `RESULTS.md` — narrative readout (populated by a live run).
