# SWE-rebench PR-Knowledge Eval — Operator's Guide

A hand-off runbook for **running, tuning, and troubleshooting** the eval on your own
machine — written so both you and your Claude Code agents can drive it. For the
unit-by-unit architecture (U1–U8) and the design rationale, see [README.md](README.md);
this guide is the operational layer on top of it.

> **What this is.** A SWE-bench-style A/B harness: for each real sympy issue it ingests
> the pre-`base_commit` PR history into a private Praxis **space**, runs Claude (Sonnet)
> to fix the issue **with** vs **without** Praxis retrieval, grades both with the WSL2
> arm64 SWE-bench harness, and reads out **cost-to-correct**. It is a **feasibility
> study, not a quantitative verdict** — underpowered by design, with residual training
> contamination (see [README.md](README.md#read-this-before-reading-any-number)). Read
> any number through that lens.

---

## 1. Prerequisites (one-time setup)

| Requirement | How | Why |
|---|---|---|
| **Praxis backend up** | Run the **praxis-up** skill (Postgres + FastAPI on `:8000`, `PRAXIS_AUTH_DISABLED=1`) | Ingest, `/context`, `/graph` all hit it |
| **`OPENROUTER_API_KEY`** in the backend's `.env` | `OPENROUTER_API_KEY=sk-or-...` | The backend distills PRs via OpenRouter (`openai/gpt-4o-mini`); without it ingest raises |
| **WSL2 + Docker Desktop** (WSL integration on) | Docker Desktop → Settings → Resources → WSL integration | The grader runs the sympy test env in a Linux arm64 container |
| **`claude` CLI, logged in** | `claude` on `PATH`, authenticated | The agent arms shell out to `claude` on your Claude subscription |
| **`datasets` available** | pass `--with datasets` to `uv run` (it is **not** a project dep) | Loads the `nebius/SWE-rebench` slice |

Quick green-light check (all four should pass):

```bash
curl -fsS http://127.0.0.1:8000/health            # -> {"status":"ok","store":"postgres"}
command -v claude                                  # -> a path
wsl -e bash -lc "docker ps"                        # -> runs without error
grep -q OPENROUTER_API_KEY .env && echo "key set"  # -> key set
```

> **Two different API quotas.** The **backend** uses **OpenRouter** (ingest distillation +
> `/context` embeddings). The **agents** use your **Anthropic/Claude subscription** (the
> `claude` subprocesses). They scale independently — see §5.

---

## 2. Running it

All commands run **from the repo root** (`uv run` resolves the project; `-m` puts the repo
on `sys.path`). If you see `ModuleNotFoundError: knowledge`, prefix with
`PYTHONPATH=$(pwd)`.

### Offline (no backend / Docker / claude) — sanity-check the analysis layer
```bash
uv run python -m knowledge.evals.swebench.run \
  --from-records knowledge/evals/swebench/tests/fixtures/records.sample.json
```

### Live (the real thing) — runs in the background; you'll be notified on exit
```bash
uv run --with datasets python -m knowledge.evals.swebench.run \
  --instances 9 --trials 3 \
  --order hard --since 2025-02-01 \
  --ingest-workers 3 --workers 4 --grade-concurrency 2 \
  --manifest /tmp/instances.manifest.json \
  --out /tmp/RESULTS.data.json \
  > /tmp/swebench-run.log 2>&1 &
```

It's a **multi-hour** job (agent runs + Docker grades). Tail progress with
`tail -f /tmp/swebench-run.log`; per-trial lines look like
`sympy__sympy-27797 trial 1: treat_ok=True ctrl_ok=False treat_cost=... ctrl_cost=...`.

### Artifacts
| File | What |
|---|---|
| `--manifest` | The exact instances chosen (re-loadable for a deterministic rerun) |
| `--out` (`RESULTS.data.json`) | `{records, report, gate, rexist_map}` machine-readable |
| `RESULTS.md` | Narrative readout |
| `.checkouts/` (gitignored) | Shared sympy clone + worktree pool |
| `.mcp-cache/` (gitignored) | Per-instance MCP identity caches |

---

## 3. Choosing instances

| Flag | Default | Effect |
|---|---|---|
| `--instances N` | 10 | How many distinct bugs (coverage) |
| `--trials K` | 3 | Repeats per bug per arm (signal vs. agent noise) |
| `--order recent\|hard` | `recent` | `recent` = newest; `hard` = biggest gold patch (control likely fails → Praxis has headroom) |
| `--since YYYY-MM-DD` | none | Keep only instances merged on/after the date (least-contaminated slice) |
| `--include-leaked` | off | Keep verbatim-leaked instances (the issue pastes a fix line); excluded by default |

**The selection tradeoff.** Only supported sympy versions (1.12–1.14) are gradeable — ~101
of 719. `--order hard` surfaces the bugs a memorized control can't one-shot, but the
hardest skew *older* (more contaminated). `--since` buys recency but the pool is thin
(`--order hard --since 2025-02-01` is only **9** instances, one genuinely large). Pick per
what you're probing; for a feasibility run, `--order hard --since <recent>` is the honest
slice.

> **Leakage screen.** Selection drops only **verbatim** leaks (issue literally pastes a
> fix line, ~8/101). It keeps "the issue names the changed function" cases — that's normal,
> not leakage. A separate runtime **leakage guard** re-checks ingested facts against the
> gold diff and aborts loudly if a *substantive* gold line is restated (trivial lines like a
> bare `return` are ignored — see [ingest.py](ingest.py) `is_substantive_line`).

`N × K × 2` agent runs total (e.g. `9 × 3 × 2 = 54`), each fix-then-grade.

---

## 4. How a run executes (so you know what you're watching)

```
select 9 instances (U1)
  └─ INGEST PRE-LOOP  ──────────────  parallel: --ingest-workers
       per instance: distill pre-base_commit PR window → space, R_exist, seed MCP cache
       (first instance runs alone to create the shared org, then fans out)
  └─ build shared sympy clone + worktree pool (size = --workers)
  └─ ARMS PHASE  ───────────────────  parallel: --workers, grades capped by --grade-concurrency
       per (instance, trial): treatment arm + control arm
         each arm: agent fixes in an isolated worktree → extract patch → grade in WSL Docker
  └─ aggregate → gate → RESULTS
```

Two parallel phases, **different bottlenecks** (next section). Ingestion reuses a populated
space on rerun (stable space id), so a re-run skips re-distillation.

---

## 5. Parallelism & performance — the three knobs

| Knob | Default | Scales with | Bottleneck |
|---|---|---|---|
| `--ingest-workers` | 3 | **OpenRouter** tokens/min + backend throughput | NOT local RAM — thin HTTP clients |
| `--workers` | 1 | **Anthropic** quota + RAM (agent procs) | Shared subscription rate limit |
| `--grade-concurrency` | 2 | **RAM + cores** (Docker test containers) | ~2–3 GB & CPU per grade |

Why they're separate: the **agent** is LLM/network-bound (oversubscribe cores happily), but
the **grade** is a CPU+RAM-heavy Docker test run, and **ingestion** is bound by a remote API
— so each phase gets its own cap. Two safety mechanisms make `>1` sound:
- Each arm borrows an **isolated git worktree** → concurrent agents never edit the same files.
- `UrllibClient` **retries 429/5xx** with backoff, and each instance's PRs are **batched**
  into ≤128 KB `/ingest` posts — so the backend's 30/min per-route limit doesn't throttle
  ingest, and a higher `--ingest-workers` degrades into backoff instead of crashing.

### Tuning by hardware (estimates — confirm by watching for OOM / CPU thrash / 429s)

| Host RAM / cores | WSL cap | `--ingest-workers` | `--workers` | `--grade-concurrency` |
|---|---|---|---|---|
| 16 GB / 8 | ~8 GB | 3 | 4 | 2 |
| 32 GB / 12 | ~16–20 GB | 4–6 | 8 | 4 |
| 64 GB / 16 | ~32 GB | 6–8 | 12 | 6–8 |
| 128 GB / 16+ | ~48 GB | 8 | 16 | **~cores** |

**Raise the WSL memory cap** or the extra host RAM never reaches the grades — WSL2 defaults
to ~50% of host. In `C:\Users\<user>\.wslconfig`:
```ini
[wsl2]
memory=24GB
processors=12
```
then `wsl --shutdown` to apply.

### The ceilings that bite *regardless* of RAM
1. **Cores cap grades** — they're CPU-bound test runs; past ~`num_cores` containers they thrash. `--grade-concurrency ≈ min(RAM ÷ 3 GB, cores)`.
2. **Anthropic quota caps `--workers`** — every agent draws the same subscription; low-teens is where 429s, not RAM, bind.
3. **OpenRouter tier caps `--ingest-workers`** — credits → req/s + the OpenAI tier for `gpt-4o-mini`. The retry absorbs brief overruns; sustained ones back off.
4. **One local backend** services all ingest + retrieval — more client RAM doesn't speed it up.

> **Diagnosing the limit you hit:** OOM / WSL killed → lower `--grade-concurrency`. Agent
> 429s in the log → lower `--workers`. Ingest slow but CPU idle → it's API-bound, raising
> workers won't help. CPU pinned at 100% during grading → too many grades for your cores.

---

## 6. Reading the results

`RESULTS.md` (and the `report`/`gate` in `RESULTS.data.json`) give:
- **Cost-to-correct** — cumulative agent $ to the first resolved patch, treatment vs control. The headline.
- **ITT** (primary) — unconditioned all-treatment vs all-control; a **null ITT is allowed**.
- **R_exist** (secondary, exploratory) — pre-treatment relevance oracle; the hit-rate is the feasibility signal, labeled exploratory everywhere.
- **Gate** — `"feasibility met"` rests on the R_exist hit-rate + a directional read, **never** on statistical significance.

**Always read with the caveats:** ~9×3 is underpowered (CIs ±20–30 pp), and the control is a
*reduced-but-nonzero* memorization baseline (SWE-rebench ≤2025-04 is inside the training
window). A tight-looking interval is not power you have. See
[README.md](README.md#read-this-before-reading-any-number).

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Praxis backend not reachable …` | Backend down | Run the **praxis-up** skill; re-check `/health` |
| `set OPENROUTER_API_KEY …` (in backend log) | No key for distillation | Put `OPENROUTER_API_KEY` in `.env`, restart backend |
| `ModuleNotFoundError: datasets` | Missing on live path | Use `uv run --with datasets …` |
| `ModuleNotFoundError: knowledge` | Wrong cwd | Run from repo root, or `PYTHONPATH=$(pwd)` |
| `no instances selected` | Filters too tight | Loosen `--since` / drop `--order hard`; remember only sympy 1.12–1.14 grade |
| `LeakageError: … restates gold diff` | A **substantive** gold line really is in an ingested fact | Real contamination for that instance — exclude it; do **not** weaken the guard |
| Run dies on a 429 mid-ingest | Sustained over the rate limit past retries | Lower `--ingest-workers`; the batching + retry already absorb normal load |
| Grade returns wrong/empty results | swebench arch mismatch | Handled by the runtime arm64 monkeypatch ([grader.py](grader.py)); never edit vendored swebench |
| WSL "out of memory" / container killed | `--grade-concurrency` too high for WSL cap | Lower it, or raise the WSL `memory=` and `wsl --shutdown` |

---

## 8. Cost expectations (rough)
- **Ingest (OpenRouter, `gpt-4o-mini`):** ~$0.001/PR-distillation → a 9-instance run ≈ **$0.45**. A self-imposed ~$10/day account cap bounds large sweeps, not normal runs.
- **Agents (Anthropic subscription):** `N×K×2` runs of Sonnet, up to 40 turns each — the dominant cost, drawn from your subscription, not a per-call bill.
- **Grades:** local compute only (Docker), no API cost.

---

## 9. Quick reference

```bash
# First real run on a 16 GB box:
uv run --with datasets python -m knowledge.evals.swebench.run \
  --instances 9 --trials 3 --order hard --since 2025-02-01 \
  --ingest-workers 3 --workers 4 --grade-concurrency 2 \
  --manifest /tmp/m.json --out /tmp/RESULTS.data.json > /tmp/run.log 2>&1 &

# Beefier box (64 GB / 16 cores, WSL cap raised):
#   --ingest-workers 8 --workers 12 --grade-concurrency 6

# Deterministic rerun of the same instances (reuses ingested spaces):
#   …same command, same --manifest path…

# Offline re-analysis only (no backend/Docker/claude):
uv run python -m knowledge.evals.swebench.run --from-records <records.json>
```

Key files: [run.py](run.py) (orchestrator + flags), [instances.py](instances.py)
(selection), [ingest.py](ingest.py) (PR window → space, batching, retry, leakage guard),
[runner.py](runner.py) (agent arm), [grader.py](grader.py) (WSL arm64 grade),
[README.md](README.md) (architecture).
