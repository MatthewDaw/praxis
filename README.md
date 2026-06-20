# PRAXIS

**Self-improving knowledge loop for Claude Code agents.**

[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](pyproject.toml)
[![Streamlit](https://img.shields.io/badge/dashboard-Streamlit-FF4B4B.svg)](frontend/app.py)
[![React](https://img.shields.io/badge/dashboard-React-61DAFB.svg)](frontend-react/README.md)
[![Go](https://img.shields.io/badge/session--capture-Go-00ADD8.svg)](session-capture/README.md)
[![Contract](https://img.shields.io/badge/API-contract_v1-4CAF50.svg)](docs/integration/candidate-api-v1.md)

Claude Code's auto-memory saves a few notes between sessions — but it's an unverified black box: no human approval, no deduplication, no measurement. **PRAXIS** mines the full JSONL session logs the agent already produces, distills durable lessons, runs them through a confidence score and human-approval gate, and injects only promoted knowledge into future sessions — so the agent provably stops relearning the same things and gets better over time.

> **Memory vs. knowledge.** Auto-memory captures scattered, episodic notes. PRAXIS produces generalized, deduplicated, confidence-scored, human-approved, measured knowledge with full provenance.

**Remote:** [GitLab — monicapeters/praxis](https://labs.gauntletai.com/monicapeters/praxis)  
**Architecture source of truth:** [docs/plans/PRAXIS_Project_Plan.html](docs/plans/PRAXIS_Project_Plan.html)

---

## Table of contents

- [The loop](#the-loop)
- [Problem](#problem)
- [Implementation status](#implementation-status)
- [Sprint TODO (team tracker)](#sprint-todo-team-tracker)
- [MVP scope](#mvp-scope)
- [Success criteria](#success-criteria)
- [Team & pillars](#team--pillars)
- [Sprint timeline](#sprint-timeline)
- [Repository layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Testing](#testing)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)
- [Changelog](#changelog)

---

## The loop

```text
raw logs → extract candidate lessons → consolidate/dedup/generalize → confidence score
         → [human approval gate] → Knowledge Graph → get-context tool → future sessions
         → measure improvement → repeat
```

```mermaid
flowchart LR
  JSONL[Claude Code JSONL logs] --> Ingest[Ingest and segment]
  Ingest --> Detect[Learning moment detection]
  Detect --> Distill[LLM distillation]
  Distill --> Consolidate[Cluster dedup score]
  Consolidate --> Gate[Human gate dashboard]
  Gate -->|proposed to suggested to active| KG[Knowledge Graph]
  KG --> GetContext[get-context tool]
  GetContext --> Agent[Future sessions]
  Agent --> JSONL
  KG --> Substrate["CLAUDE.md / skills substrate"]
  GetContext --> Eval[Eval harness]
  Eval --> Gate
```

---

## Problem

Coding agents are less amnesiac than they used to be, but durable knowledge still lives in the gaps:

- **No quality gate** — save decisions are opaque; wrong patterns can be memorized from one-off mistakes.
- **No dedup, decay, or conflict resolution** — stale and contradictory notes coexist indefinitely.
- **No measurement** — nothing verifies a saved memory actually helped a later session.
- **Per-repository only** — nothing carries across projects, models, or domains.
- **Underused raw material** — full JSONL transcripts (`~/.claude/projects/<project>/<session>.jsonl`) record every mistake, correction, and success; auto-memory skims in-flight and discards the rest.

PRAXIS treats that exhaust as a compounding asset.

---

## Implementation status

Point-in-time snapshot as of **2026-06-19** (Sprint Day 3). Live gap tracker: [docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md](docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md). See [AUDIT.md](AUDIT.md) for the Jun 18 repo health review (partially stale on Candidate API — now shipped).

| Area | Path | Owner | Status |
|------|------|-------|--------|
| Human-gate dashboard (Streamlit) | `frontend/` | Monica Peters | **Demo-ready** — mock fixtures, contract v1 API client, Render deploy blueprint |
| Knowledge Graph dashboard (React) | `frontend-react/` | Monica Peters (client) / Matthew Daw (server) | **Demo-ready (mock)** — Vite + TypeScript UI targeting same candidate-api-v1; Matthew validates his REST server without Streamlit |
| Knowledge substrate | `knowledge/` | Matthew Daw | **Foundation** — in-memory graph, prompt ingestor, whole-file reader, wiring factory |
| Eval harness | `knowledge/evals/` | Dominic Antonelli | **Partial** — 63 YAML cases, deterministic checks, real Claude Code runner + offline FakeRunner |
| Session capture | `session-capture/` | Dominic Antonelli | **Working** — Go `claude+` PTY daemon, JSONL tailer, DynamoDB writer |
| Cloud infra | `infra/` | Team | **Scaffolded** — AWS CDK: DynamoDB sessions table + RDS PostgreSQL 16/pgvector KG store |
| Candidate REST API | `knowledge/serve/` | Matthew Daw | **Shipped** — FastAPI contract v1; JSON store + optional Postgres via `PRAXIS_DB_URL` |
| Eval metrics endpoint | `knowledge/serve/` (`GET /metrics`) | Dominic Antonelli | **Fixture only** — contract v1 documented; dashboard embed ready; real batch data pending |
| CI pipeline | — | Team | **Not yet** — manual test runs only |

**Integration posture:** The dashboard runs fully offline when `PRAXIS_API_BASE_URL` is unset. Set env vars per [docs/integration/wire-up.md](docs/integration/wire-up.md) to wire live backend and eval metrics without code changes.

**Critical path (Jun 29 demo):** Matthew pipeline (JSONL → candidates → promote→KG) and Dominic measurement spine (cold vs injected + compounding curve). Dashboard Act 2 is mock-ready today.

---

## Sprint TODO (team tracker)

> **Last updated:** 2026-06-19 (Sprint Day 3). Detailed day-by-day gaps: [PLAN_ALIGNMENT_GAP_CHECKLIST.md](docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md). Tick items in MRs; Monica updates this block at daily sync.

| Milestone | Date | Days away |
|-----------|------|-----------|
| Integration complete (Day 7) | Tue Jun 24 EOD | 5 |
| Feature freeze (Day 8) | Wed Jun 25 EOD | 6 |
| Practice 1 | Wed Jun 25 | 6 |
| Hard freeze (Day 10) | Fri Jun 27 EOD | 8 |
| Demo branch `demo-jun29` | Sun Jun 28 AM | 9 |
| **Gauntlet showcase** | **Mon Jun 29** | **10** |

### Critical path (P0 — blocks live demo)

**Matthew (ML & Knowledge Pipeline)**

- [ ] JSONL ingest + episode segmentation (`knowledge/ingestion/` — rename from `injestion/` when ready)
- [ ] LLM distillation → structured candidate `{when, lesson, scope, evidence, form}` with provenance `logs/*.jsonl:<line>`
- [ ] Learning-moment detector (heuristics minimum for demo)
- [ ] **Promote → `KnowledgeGraph.write`** (approval in dashboard must persist to graph, not candidate store only)
- [ ] PostgreSQL RDS provisioned + `PRAXIS_DB_URL` live ([RDS_KG_DEPLOY.md](docs/monica/RDS_KG_DEPLOY.md))
- [ ] Acknowledge Monica P0 eval cases in [MATTHEW_HANDOFF.md](knowledge/evals/cases/MATTHEW_HANDOFF.md) (`quirky_exhaustive_switch`, `quirky_config_load_order`)

**Dominic (Arch, Eval & Integration)**

- [ ] Paired **cold vs injected** runner (same case, empty graph vs seeded insight)
- [ ] **Correction counting** + token/time in eval results
- [ ] Eval metrics GET per [eval-metrics-v1.md](docs/integration/eval-metrics-v1.md) (real batch data, not fixture-only)
- [ ] Quirky benchmark wired for Acts 1 & 3 (`quirky_exhaustive_switch` pairing)
- [ ] Compounding curve output proving **≥50% correction reduction** (or dated honest narrative + fixture fallback)

**Monica (Dashboard & Human Gate)**

- [x] Human-gate UI mock-complete (Streamlit + React, contract v1 clients)
- [x] P0 eval cases + `test_cases.py` green
- [ ] Live integration smoke: promote/reject/resolve chain on Matthew API ([INTEGRATION_SMOKE.md](docs/monica/INTEGRATION_SMOKE.md))
- [ ] Timed **Act 2** rehearsal ≤3.5 min ([DEMO_SCRIPT.md](docs/monica/DEMO_SCRIPT.md))

**Team**

- [ ] End-to-end path: **log → candidates → promote → eval improved**
- [ ] 3-act demo script with fallback matrix (Act 1 clip OK; Act 2 mock OK today; Act 3 fixture metrics fallback documented)

### Remaining sprint schedule (open work)

- **Day 3 (Jun 19 — today):** Matthew: distillation + provenance · Dominic: injection tooling · Monica: Act 2 rehearsal
- **Day 4 (Jun 20):** Matthew: embeddings/HDBSCAN/contradiction pipeline · Dominic: PR/ticket replay skeleton
- **Day 5 (Jun 21):** Matthew: freq/recency/breadth + decay · Dominic: token/time tracking
- **Day 6 (Jun 23):** Matthew: E2E pipeline + RDS · Dominic: cold vs injected runner · Monica: live API smoke
- **Day 7 (Jun 24):** Matthew: get-context tool + promote→KG · Dominic: demo data bundle · **integration complete EOD**
- **Day 8 (Jun 25):** Batch evals + compounding measurement · Monica Practice 1 · **feature freeze EOD**
- **Days 9–10 (Jun 26–27):** Demo polish, user-flow video, Practice 2, hard freeze Fri 27
- **Showcase (Jun 29):** 10-min live demo (3 acts)

### Demo & presentation gates

- [ ] **Practice 1** — Wed Jun 25 — all 3 acts ≤10 min; mock dashboard OK
- [ ] **Practice 2** — Fri Jun 27 — live API + eval metrics preferred
- [ ] **Practice 3** — Sun Jun 28 PM — dress rehearsal; no code after unless P0
- [ ] Tag **`demo-jun29`** — Sun Jun 28 AM
- [ ] Backup recording captured
- [ ] Monica: user-flow video ≤3 min (React primary)
- [ ] Monica: manual a11y pass (tab + screen reader) per [DAYS_9_10_REMAINING.md](docs/monica/DAYS_9_10_REMAINING.md)
- [ ] Dominic: full 3-act spoken script (Act 1 dumb agent · Act 2 distillation · Act 3 smart agent + scoreboard)

### P1 (after P0 or stretch)

- [ ] HDBSCAN cluster/dedup (Matthew)
- [ ] Pipeline-side contradiction detection (Matthew)
- [ ] Real confidence scorer from logs (Matthew)
- [ ] GitHub hook / PR automation on promote (Dominic)
- [ ] GitLab CI: `pytest knowledge/`, `frontend/tests/`, `frontend-react` Vitest
- [ ] Root `pyproject.toml` pythonpath fix
- [ ] State-distribution chart (Monica stretch)

### How to update this list

- Monica ticks items at daily 10am sync; pillar leads confirm in MR before checking `[x]`
- Link MR/commit in standup notes under `docs/monica/standup-notes/`
- Do not check integration/E2E items until smoke doc or pytest proves it
- After Jun 25 feature freeze: bugfixes + measurement only

---

## MVP scope

| In scope | Out of scope |
|----------|--------------|
| Ingest + segment real Claude Code JSONL logs | Training models from scratch |
| Learning-moment detection (heuristics + LLM) | Hosted SaaS |
| LLM distillation with provenance | Non–Claude-Code agents |
| Cluster/dedup + confidence scoring | Real-time mid-session learning |
| Knowledge Graph as primary knowledge store | |
| get-context tool (session + codebase + graph → injected context) | |
| Streamlit + React human-gate dashboards in `frontend/` and `frontend-react/` (`proposed → suggested → active`) | |
| Complementary injection via generated `CLAUDE.md` / skills | |
| Eval harness measuring correction rate before/after (VCS-agnostic PR/ticket replay) | |

**Implemented beyond MVP shell:** contradiction-resolution UI (dashboard); React client for Matthew API validation.

**Stretch goals:** trained classifier for learning moments; substrate bake-off (markdown/skills vs. vector RAG vs. knowledge graph); confidence decay and re-verification; pipeline-side contradiction **detection**; cross-project knowledge.

---

## Success criteria

- **Primary metric:** ≥50% fewer user corrections on benchmark tasks vs. cold runs, with no regression in task success rate.
- **Compounding proof:** visible correction-rate curve falling across sessions.
- **Demo outcome:** point PRAXIS at a repo's logs → ranked candidate lessons with evidence in minutes → human promotes the good ones → re-run shows quantified improvement (corrections, failures, tokens, time).

---

## Team & pillars

Three Gauntlet AI Fellows, each owning one end-to-end pillar for a 9–10 day focused sprint:

| Lead | Pillar | Focus |
|------|--------|-------|
| **Matthew Daw** | ML & Knowledge Pipeline | Ingestion, learning-moment detection, LLM distillation, consolidation/dedup/scoring, knowledge graph, provenance |
| **Monica Peters** | Dashboard & Human Gate | Streamlit + React human-gate dashboards, approval workflow, contradiction resolution UI, credibility metrics |
| **Dominic Antonelli** | Architecture, Eval & Integration | System design, eval harness, VCS-agnostic replay automation, session capture wrapper, deployment, compounding-curve proof |

Daily 15-minute syncs; all code reviewed by at least one other member before merge.

---

## Sprint timeline

Sprint **Day 1 = Wednesday, June 16, 2026** (Thursday June 18 skipped). See the confidential project plan for the detailed day-by-day schedule.

**You are here:** Day 3 evening (Jun 19) — parallel core build; integration target **Jun 24** (Day 7).

| Phase | Days | Milestones |
|-------|------|------------|
| Foundation & design | 1–2 | Architecture, data contracts, dashboard shell, eval skeleton, cold-run baseline |
| Parallel core build | 3–5 | Full pipeline, human-gate UI, scoring/decay, eval replay automation |
| Integration | 6–7 | Dashboard ↔ backend API, injection, eval harness, promotion triggers replay |
| Measurement | 8 | Compounding curve, threshold tuning, edge-case polish |
| Demo & handoff | 9–10 | Live demo script, documentation, presentation practice (internal **Jun 26–27**) |
| **Gauntlet showcase** | — | **Mon Jun 29** — 10-minute live presentation |

Team freeze gates and three practice runs: [docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md](docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md).

---

## Live demo (3 acts)

1. **Dumb agent** — fresh repo with deliberate quirks; agent stumbles, gets corrected; log captured.
2. **Distillation** — PRAXIS surfaces scored candidates linked to transcript lines; human promotes `suggested → active`.
3. **Smart agent** — sibling task nails quirks first try; side-by-side scoreboard plus compounding curve across a pre-run batch.

Demo script: [docs/monica/DEMO_SCRIPT.md](docs/monica/DEMO_SCRIPT.md)

---

## Repository layout

```text
praxis/
├── docs/                      # Plans, proposals, integration contracts, fixtures
│   ├── integration/           # candidate-api-v1, eval-metrics-v1, wire-up, JSON fixtures
│   ├── monica/                # Dashboard pillar — architecture, wireframes, deploy, demo
│   ├── matt/future-work/      # Post-MVP knowledge-graph eval design (parked)
│   └── plans/                 # MVP plan (mvp-plan.html)
├── .cursor/rules/             # Team Cursor rules (shared, dashboard, pipeline-eval, git-sync)
├── frontend/                  # Streamlit human-gate UI (Monica)
│   ├── app.py                 # Entry — provider wiring only
│   ├── components/            # List, detail, badges, contradiction panel, eval embed
│   ├── models/                # Candidate types (API contract surface)
│   ├── services/              # DataProvider, mock + API clients, contract_v1
│   ├── tests/                 # Contract fixture + mock workflow tests
│   ├── mock_data.py           # Local fixtures — no backend required
│   └── render.yaml            # Render.com deploy blueprint
├── frontend-react/            # React Knowledge Graph dashboard (Monica — Matthew API client)
│   ├── src/                   # Vite + TypeScript — same contract v1 as Streamlit
│   ├── public/mock-candidates.json
│   └── README.md              # Matthew self-serve wire-up (VITE_* env vars)
├── knowledge/                 # Knowledge substrate + eval harness (Matthew & Dominic)
│   ├── knowledge_graph/       # KnowledgeGraph ABC + InMemoryGraph
│   ├── injestion/             # Ingestor ABC + PromptIngestor
│   ├── graph_reader/          # WholeFileReader → Claude tool adapter
│   ├── evals/                 # YAML cases, deterministic checks, Claude Code runner
│   ├── wiring.py              # build_trio() factory
│   └── run.py                 # Debugger entry — ingest smoke + eval runner
├── session-capture/           # Go claude+ wrapper — PTY daemon + DynamoDB capture
│   └── wrapper/               # cmd/claude-plus, internal/{pty,daemon,capture,store}
├── infra/                     # AWS CDK — DynamoDB sessions + RDS knowledge-graph DB
├── run.py                     # Repo-root shim → knowledge/run.py
├── pyproject.toml             # Python 3.12+ deps (uv/pip)
├── uv.lock                    # Locked Python dependencies
└── README.md
```

> **Note:** Early plans referenced top-level `pipeline/` and `eval/` directories. Current implementations live under `knowledge/` (including `knowledge/evals/`) and `session-capture/`. API contracts are path-agnostic.

---

## Prerequisites

| Tool | Version | Used for |
|------|---------|----------|
| Python | ≥ 3.12 | Dashboard, knowledge package, eval harness |
| [uv](https://docs.astral.sh/uv/) | latest (recommended) | Dependency install and script runner |
| Go | ≥ 1.22 | Building `session-capture/wrapper` |
| Node.js | ≥ 20 | React dashboard (`frontend-react/`), AWS CDK deploy (`infra/`) |
| AWS CLI | configured | DynamoDB session capture (optional — wrapper runs without it) |

---

## Quick start

### 1. Install Python dependencies

From the repo root:

```powershell
uv sync
```

Or with pip:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e .
```

### 2. Run the human-gate dashboard

```powershell
cd frontend
Remove-Item Env:PRAXIS_API_BASE_URL -ErrorAction SilentlyContinue
uv run streamlit run app.py
```

Mock mode loads fixtures from `mock_data.py` — no backend required. See [docs/integration/wire-up.md](docs/integration/wire-up.md) for live API and eval-metrics wiring.

**Render deploy (portfolio demo):** [docs/monica/RENDER_DEPLOY.md](docs/monica/RENDER_DEPLOY.md)

### 2b. Run the React dashboard (Matthew API client)

```powershell
cd frontend-react
npm install
npm run dev
```

Mock mode loads `public/mock-candidates.json` — no backend required. Set `VITE_PRAXIS_API_BASE_URL` in `.env.local` for Matthew's live server. See [frontend-react/README.md](frontend-react/README.md) and [docs/integration/wire-up.md](docs/integration/wire-up.md).

### 3. Run the eval harness

Offline (no Claude Code subscription):

```powershell
$env:PRAXIS_EVAL_REAL = "0"
uv run python run.py
```

Real Claude Code (uses subscription credits):

```powershell
uv run python run.py
```

Registered cases live in `knowledge/evals/cases/`. Results append to `knowledge/evals/results/`.

### 4. Build session capture (optional)

```powershell
# Deploy CDK stacks (DynamoDB sessions + optional RDS knowledge-graph DB)
cd infra
npm install
npm run deploy
# Knowledge-graph RDS setup: docs/monica/RDS_KG_DEPLOY.md

# Build claude+ wrapper
cd ..\session-capture\wrapper
go build -o claude+ ./cmd/claude-plus

# Host a session (streams to DynamoDB when AWS creds present)
$env:SESSION_TABLE = "praxis-sessions"
$env:AWS_REGION = "us-east-1"
.\claude+
```

Full wrapper docs: [session-capture/README.md](session-capture/README.md)

### 5. Enable LLM tracing (optional)

Trace every LLM, embedding, and Claude Code agent/judge call to a self-hosted
[Arize Phoenix](https://phoenix.arize.com/) instance. **Off by default** — when
`PHOENIX_COLLECTOR_ENDPOINT` is unset, tracing is a no-op, so tests and offline
runs never touch the network.

```powershell
# 1. Install the tracing dependencies (OpenTelemetry SDK + OTLP exporter)
uv sync --extra observability

# 2. Set the Phoenix vars in .env (see .env.example)
#    PHOENIX_COLLECTOR_ENDPOINT=https://your-phoenix-host
#    PHOENIX_API_KEY=...          # Phoenix UI -> Settings -> API Keys
#    PHOENIX_TLS_VERIFY=false     # only for a self-signed cert (IP-based deploy)

# 3. Run any eval — spans stream to Phoenix
uv run python -m knowledge.evals.run --openrouter pathlib_preference
```

Spans land under the `praxis` project (override with `PHOENIX_PROJECT_NAME`),
each carrying model, token counts, and cost. The CDK stack that stands up
Phoenix itself lives in [infra/lib/phoenix-stack.ts](infra/lib/phoenix-stack.ts).

---

## Configuration

| Variable | Required | Component | Purpose |
|----------|----------|-----------|---------|
| `PRAXIS_API_BASE_URL` | No | Dashboard | Candidate REST API base URL; unset → mock fixtures |
| `PRAXIS_API_TOKEN` | No | Dashboard | Bearer token for API auth |
| `PRAXIS_CONTRACT_VERSION` | No | Dashboard | API contract version header (default `1`) |
| `PRAXIS_EVAL_METRICS_URL` | No | Streamlit dashboard | GET endpoint returning eval metrics JSON for compounding-curve embed |
| `VITE_PRAXIS_API_BASE_URL` | No | React dashboard | Same as `PRAXIS_API_BASE_URL`; unset → mock fixtures |
| `VITE_PRAXIS_API_TOKEN` | No | React dashboard | Bearer token for API auth |
| `VITE_PRAXIS_EVAL_METRICS_URL` | No | React dashboard | Eval metrics JSON URL for compounding-curve embed |
| `VITE_PRAXIS_CONTRACT_VERSION` | No | React dashboard | API contract version header (default `1`) |
| `PRAXIS_EVAL_REAL` | No | Eval harness | Set to `0` for offline FakeRunner; default runs real Claude Code |
| `PHOENIX_COLLECTOR_ENDPOINT` | No | Observability | Phoenix collector base URL; unset → tracing disabled (needs `observability` extra) |
| `PHOENIX_API_KEY` | No | Observability | Phoenix API key (UI → Settings → API Keys) when Phoenix auth is on |
| `PHOENIX_TLS_VERIFY` | No | Observability | Set `false` for a self-signed Phoenix cert; default verifies TLS |
| `PHOENIX_PROJECT_NAME` | No | Observability | Phoenix project spans land under (default `praxis`) |
| `PRAXIS_DB_URL` | No | Candidate API | Postgres DSN; when set, API uses `PostgresCandidateStore` instead of JSON file |
| `PRAXIS_DB_SECRET` | No | Candidate API | AWS Secrets Manager secret name (default `praxis/knowledge-graph/db`) |
| `PRAXIS_ORG_ID` | No | Candidate API | Multi-tenant org scope (default `default`) |
| `PRAXIS_USER_ID` | No | Candidate API | Multi-tenant user scope (default `default`) |
| `PRAXIS_SHARED` | No | Candidate API | Org-shared graph visibility (`true`/`false`) |
| `SESSION_TABLE` | No | Session capture | DynamoDB table name for transcript streaming |
| `AWS_REGION` | No | Session capture, Candidate API | AWS region (DynamoDB writer; Secrets Manager for RDS creds) |

Secrets are **environment-only** — never commit tokens or credentials.

RDS + pgvector deploy and Postgres-backed candidate API: [docs/monica/RDS_KG_DEPLOY.md](docs/monica/RDS_KG_DEPLOY.md).

---

## Testing

**Knowledge package** (run from repo root):

```powershell
uv run pytest knowledge/ -q
```

**Eval case registry** (63 YAML cases):

```powershell
uv run pytest knowledge/evals/tests/test_cases.py -q
```

**Dashboard contract tests** (set `PYTHONPATH` from repo root):

```powershell
$env:PYTHONPATH = "frontend"
uv run pytest frontend/tests/ -q
```

**React dashboard** (Vitest + lint + build — rehearsal gate per [DAYS_9_10_REMAINING.md](docs/monica/DAYS_9_10_REMAINING.md)):

```powershell
cd frontend-react
npm test
npm run lint
npm run build
```

Contract fixtures are canonical in [docs/integration/fixtures/](docs/integration/fixtures/).

---

## Documentation

| Document | Description |
|----------|-------------|
| [docs/plans/PRAXIS_Project_Plan.html](docs/plans/PRAXIS_Project_Plan.html) | **Source of truth** — team plan, architecture overview, 9-day schedule |
| [docs/plans/mvp-plan.html](docs/plans/mvp-plan.html) | MVP core contracts and eval schema |
| [docs/plans/proposal-praxis.md](docs/plans/proposal-praxis.md) | Capstone proposal — problem, direction, risks (historical) |
| [docs/integration/candidate-api-v1.md](docs/integration/candidate-api-v1.md) | **Matthew ↔ Monica** candidate REST contract + fixtures |
| [docs/integration/eval-metrics-v1.md](docs/integration/eval-metrics-v1.md) | **Dominic ↔ Monica** eval metrics JSON contract |
| [docs/integration/wire-up.md](docs/integration/wire-up.md) | Self-serve Streamlit + React wire-up (no pairing) |
| [docs/monica/RDS_KG_DEPLOY.md](docs/monica/RDS_KG_DEPLOY.md) | RDS PostgreSQL 16 + pgvector — AWS CLI, Secrets Manager, Postgres candidate store |
| [infra/README.md](infra/README.md) | AWS CDK stacks overview |
| [frontend-react/README.md](frontend-react/README.md) | React Knowledge Graph dashboard — Matthew API validation |
| [docs/monica/ARCHITECTURE_MONICA.md](docs/monica/ARCHITECTURE_MONICA.md) | Dashboard pillar architecture — Streamlit stack, API boundaries |
| [docs/monica/monica-wireframes.md](docs/monica/monica-wireframes.md) | Dashboard as-built spec and UX notes |
| [docs/monica/DEMO_SCRIPT.md](docs/monica/DEMO_SCRIPT.md) | Three-act live demo script |
| [docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md](docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md) | Team gap checklist, Scrum Master duties, demo freeze gates |
| [docs/monica/STANDUP_TEMPLATE.md](docs/monica/STANDUP_TEMPLATE.md) | Daily 15-min standup template |
| [docs/Matthew-Daw-ML-Pipeline-PlanDRAFT.md](docs/Matthew-Daw-ML-Pipeline-PlanDRAFT.md) | ML pipeline pillar plan |
| [docs/Dominic-Antonelli-Architecture-Eval-PlanDRAFT.md](docs/Dominic-Antonelli-Architecture-Eval-PlanDRAFT.md) | Architecture, eval & integration pillar plan |
| [session-capture/README.md](session-capture/README.md) | Go wrapper — claude+ CLI, DynamoDB capture |
| [AUDIT.md](AUDIT.md) | Full-repo health audit (2026-06-18) |
| [CHANGELOG.md](CHANGELOG.md) | Version history and release notes |

Agent and editor guidance for contributors lives in [`.cursor/rules/`](.cursor/rules/):

- `praxis-shared.mdc` — commits, reviews, TypeScript style, provenance standards
- `praxis-dashboard.mdc` — human-gate UI patterns (Monica's pillar)
- `praxis-pipeline-eval.mdc` — pipeline data contracts, eval harness, integration (Matthew & Dominic)
- `praxis-git-sync.mdc` — GitLab main sync workflow

---

## Contributing

- Use [conventional commits](https://www.conventionalcommits.org/) (`feat`, `fix`, `chore`, `docs`, `refactor`, `test`) with `#<issue>` references.
- Open small, focused **GitLab** merge requests with clear descriptions; at least one peer review required before merge.
- Sync your dev branch with `origin/main` before starting work or opening an MR (`git fetch origin main; git merge origin/main`).
- Preserve **provenance** on every candidate/lesson object (source log path + line offset) in code and UI.
- Promotion actions should trigger **VCS-agnostic eval replay** (scripted PR/ticket scenarios) for before/after measurement.
- All code must pass lint and type checks before review.

---

## License

TBD — Gauntlet AI capstone project (2026).

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the full version history. Current release: **0.1.0** (2026-06-18).
