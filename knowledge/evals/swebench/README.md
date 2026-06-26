# SWE-rebench PR-knowledge eval pilot

A feasibility-and-instrumentation A/B harness that measures whether Praxis helps an
agent fix real GitHub issues. For each instance in a recent SWE-rebench sympy slice it
ingests the pre-`base_commit` non-fix PR window into a per-instance Praxis **space**,
runs Sonnet to fix the issue **with** vs **without** Praxis (agentic MCP retrieval + a
reproduction-test rework loop), grades with the WSL2 arm64 SWE-bench harness, and reads
out cost-to-correct as an unconditioned ITT effect plus a pre-treatment
relevance-stratified secondary.

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
```

**Instance selection** (`--order`, `--include-leaked`). By default `select` takes the
newest supported-version (sympy 1.12–1.14) instances and drops only **verbatim**-leaked
ones (the issue literally pastes a fix line — ~8 of 101). It does *not* drop the far more
common "issue names the changed function" cases, which aren't real leakage. `--order hard`
instead picks the largest-gold-patch instances (more files / failing tests) — the bugs a
contaminated control is least likely to one-shot. Caveat: the hardest instances skew older
(more training-memorized), so `hard` trades recency for difficulty; pick per what you're
probing.

The `--from-records` path needs **neither** the backend nor Docker — it only runs U7's
pure aggregate/gate/report over a committed records dict.

A **live run requires** the Praxis backend up (dev tenant) and **WSL2 Docker**:

- **Praxis backend (dev tenant).** Bring it up with the **praxis-up** skill (Postgres +
  FastAPI on `:8000`). If the backend is down, the live run exits with a clear
  `Praxis backend not reachable at <url>; run the praxis-up skill` message, not a
  traceback. Per-instance orgs are created against the dev tenant.
- **WSL2 + Docker Desktop** (WSL integration enabled) for the arm64 swebench grader. The
  agent runs in a host checkout with an `install_config` venv; gold grading happens in
  the canonical container.

The live path also builds, per instance: a checkout reset to `base_commit`, an
`install_config` venv (so the agent's own repro test runs), and a per-instance MCP
identity cache pinning the instance's org for the treatment arm. These live-orchestrator
responsibilities are factored in `run.py` (`_load_instances`, `_seed_mcp_cache`, and the
`run_arm_wired` wrapper) and are exercised manually outside CI.

## Outputs

- `RESULTS.data.json` — machine-readable `{records, report, gate, rexist_map}`.
- `RESULTS.md` — narrative readout (populated by a live run).
