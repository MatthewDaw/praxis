# SWE-rebench PR-knowledge pilot — RESULTS

> **Template — populated by a live run.** No live end-to-end run has happened yet, so
> this file carries no result numbers. After a run (`uv run python -m
> knowledge.evals.swebench.run --instances N --trials K`), fill the sections below from
> the printed report and `RESULTS.data.json`. Do **not** fabricate numbers.

## Run configuration

- Instances (N): _populated by a live run_
- Trials per arm (K): _populated by a live run_
- Rework rounds (K_rework): _populated by a live run_
- Model: Sonnet (held fixed across arms)
- Grader: official swebench harness, arm64-adapted + `install_config`-injected (WSL2 Docker)
- Date / commit: _populated by a live run_

## Verdict

- Feasibility verdict: _populated by a live run_ (`feasibility met` / `feasibility not met`)
- Reminder: the gate claims **no significance**; a null ITT is an allowed outcome.

## Primary — ITT (all treatment vs all control, unconditioned)

| metric | treatment | control | delta |
|---|---|---|---|
| resolve rate | _ | _ | _ |
| cost-to-correct (mean ± sd) | _ | _ | _ |
| CI95 (wide; flag low-trial) | _ | _ | — |
| cost / resolved | _ | _ | _ |

## Secondary — EXPLORATORY, `R_exist == 1` only

> Pre-treatment stratum, underpowered; no significance claim.

| metric | treatment | control | delta |
|---|---|---|---|
| resolve rate | _ | _ | _ |
| cost-to-correct | _ | _ | _ |

## `R_exist` hit-rate (first-class deliverable)

- Hit-rate: _populated by a live run_ ( _n_rexist_ / _n_instances_ )

## Ingestion cost (separate amortized line)

- Total / amortized-per-instance: _populated by a live run_ (placeholder until a
  distillation-cost probe is wired)
- Facts ingested (total): _populated by a live run_

## Hard-case case studies

_Populated by a live run: instances where `R_exist=1` and the treatment arm resolved
where control did not (or was materially cheaper-to-correct), with the triggering fact._

## Reading caveats

See `README.md` — underpowered by design, residual within-window contamination, the
per-instance-org point-in-time snapshot, and the `install_config` grader adaptation. The
decision rests on hit-rate + directional hard-case signal, not aggregate significance.
