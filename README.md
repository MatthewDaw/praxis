# PRAXIS

**PRAXIS is a self-improving knowledge loop for AI coding agents.** It mines Claude Code session logs, extracts durable lessons, gates those lessons through human review, stores approved knowledge in a graph-backed substrate, and makes that knowledge retrievable for future sessions and measurable through evals.

[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](pyproject.toml)
[![React](https://img.shields.io/badge/dashboard-React-61DAFB.svg)](frontend-react/README.md)
[![Go](https://img.shields.io/badge/session--capture-Go-00ADD8.svg)](session-capture/README.md)
[![API Contract](https://img.shields.io/badge/API-contract_v1-4CAF50.svg)](docs/integration/candidate-api-v1.md)

- **Architecture source of truth:** [docs/plans/PRAXIS_Project_Plan.html](docs/plans/PRAXIS_Project_Plan.html)
- **Current demo alignment:** [docs/monica/REHEARSAL_LOG.md](docs/monica/REHEARSAL_LOG.md), [docs/monica/INTEGRATION_SMOKE.md](docs/monica/INTEGRATION_SMOKE.md)
- **Current GitHub remote:** [Antonelli-Tech-Solutions/praxis](https://github.com/Antonelli-Tech-Solutions/praxis)
- **Original project history:** [GitLab - monicapeters/praxis](https://labs.gauntletai.com/monicapeters/praxis)
- **License:** [MIT](LICENSE)

---

## Table of Contents

- [What PRAXIS Does](#what-praxis-does)
- [Current State](#current-state)
- [Pillar Ownership](#pillar-ownership)
- [Architecture](#architecture)
- [Repository Layout](#repository-layout)
- [Onboarding Paths](#onboarding-paths)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Quality Gates](#quality-gates)
- [Deployment](#deployment)
- [Documentation Map](#documentation-map)
- [Operating Standards](#operating-standards)
- [Project History](#project-history)

---

## What PRAXIS Does

Claude Code already writes rich JSONL session transcripts, but those transcripts are usually disposable. PRAXIS treats them as a compounding asset:

```text
session logs -> candidate lessons -> dedup / score / conflict handling
             -> human approval gate -> knowledge graph
             -> get-context retrieval -> future sessions
             -> eval measurement -> repeat
```

The project distinguishes **memory** from **knowledge**:

| Memory | Knowledge |
|--------|-----------|
| Episodic notes saved opportunistically | Generalized lessons with scope and provenance |
| Opaque save decisions | Human-gated promotion lifecycle |
| No clear dedup or conflict policy | Dedup, contradiction handling, and write policies |
| Hard to prove impact | Evals compare cold vs. knowledge-injected runs |

The intended business value is straightforward: when an agent learns a project-specific rule or workflow once, future sessions should retrieve that lesson and avoid repeating the same correction.

---

## Current State

This README is aligned to the repository code and docs as of **2026-06-25**. The implementation is an active capstone/MVP codebase, not a hardened SaaS product.

| Area | Paths | State |
|------|-------|-------|
| React human-gate dashboard | `frontend-react/` | Working Vite/React app with Local Postgres and Remote Postgres data-source presets, mock fixtures/provider support, candidate CRUD, graph views, Phoenix trace links, auth/org UI, and local JSONL upload preview |
| Python dashboard contract layer | `frontend/` | Reference candidate models, mock provider, API client, contract fixtures, and smoke tests |
| Candidate API | `knowledge/serve/` | FastAPI app with health, auth/org routes, API keys, candidate CRUD, promote/reject, contradiction routes, graph/snapshot/source/fold-in endpoints, eval routes, `/insights`, `/ingest`, and `/context` |
| Knowledge substrate | `knowledge/` | Knowledge graph abstractions, in-memory/vector/Postgres graph variants, write policies, ingestor, readers, LLM/embedder seams, and wiring factory |
| Eval harness | `knowledge/evals/` | YAML case registry, fake/Claude/OpenRouter runners, deterministic checks, repo task helpers, cached/live embedding support, and result writing |
| MCP server | `knowledge/mcp/` | MCP entrypoint and identity tests for calling PRAXIS backend tools |
| Session capture | `session-capture/` | Go `claude-trace` wrapper that tails local Claude Code JSONL and uploads S3 slices on push/PR signals when configured |
| Cloud infrastructure | `infra/` | AWS CDK stacks for session slices, Cognito, RDS/Postgres/pgvector, App Runner backend, CloudFront frontend, Phoenix, and DNS helpers |
| Docs and contracts | `docs/` | Project plan, integration contracts, proposal history, deployment notes, dashboard docs, smoke checks, eval design, demo rehearsal notes, and future-work notes |

Known product gaps are documented in [docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md](docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md), [docs/monica/MONICA_COMPLETION_PATH.md](docs/monica/MONICA_COMPLETION_PATH.md), and [AUDIT.md](AUDIT.md). Treat older audit entries as point-in-time history when they conflict with current code.

---

## Pillar Ownership

The project plan assigns each contributor a clear pillar. Preserve those boundaries when reviewing, debugging, or extending the system.

| Owner | Pillar | Primary paths | Responsibility |
|-------|--------|---------------|----------------|
| Matthew Daw | ML & Knowledge Pipeline | `knowledge/`, `knowledge/serve/`, `knowledge/knowledge_graph/`, `knowledge/injestion/`, `knowledge/llm/` | Ingestion, candidate generation, distillation, scoring, provenance, graph persistence, and backend data surfaces |
| Monica Peters | Dashboard & Human Gate | `frontend-react/`, `frontend/`, `docs/monica/` | Human approval dashboard, Python contract layer, provenance/confidence UX, promote/reject flow, contradiction review, and demo evidence |
| Dominic Antonelli | Architecture, Eval & Integration | `knowledge/evals/`, integration docs, deploy/eval automation | Eval harness, replay/measurement proof, integration architecture, hooks, deployment proof, and compounding-gain evidence |

Current implementation note: some historical planning docs mention a `proposed -> suggested -> active` workflow. The current code and updated dashboard docs use `proposed -> active` plus `rejected`.

---

## Architecture

The architecture is defined in [docs/plans/PRAXIS_Project_Plan.html](docs/plans/PRAXIS_Project_Plan.html). The implemented repository maps to that plan as follows:

```text
Claude Code JSONL sessions
  -> session-capture/wrapper (optional S3 slice upload)
  -> knowledge ingestion / distillation seams
  -> knowledge graph write policies and stores
  -> FastAPI candidate + graph API
  -> React human-gate dashboard
  -> MCP / context retrieval
  -> eval harness measuring cold vs. injected behavior
```

### Runtime Surfaces

> **Quickstart:** common dev tasks are defined in the [`justfile`](justfile) at the repo root. Run `just` to list recipes, then e.g. `just backend` (FastAPI on :8000) and `just frontend` (Vite/React dashboard on :5173). The entry points below are what those recipes wrap.

| Surface | Entry point | Purpose |
|---------|-------------|---------|
| Backend API | `uv run python -m knowledge.serve` | Candidate review API, org/auth routes, API keys, graph-backed `/insights`, `/ingest`, and `/context` |
| React dashboard | `cd frontend-react; npm run dev` | Human gate for reviewing, promoting, rejecting, editing, resolving contradictions, and visualizing candidate knowledge |
| Eval runner | `uv run python -m knowledge.evals.run <case_id>` | Runs one eval case with fake, Claude Code, or OpenRouter paths depending on flags/env |
| Repo smoke runner | `uv run python run.py` | Root shim into `knowledge/run.py` for registered eval/dev smoke paths |
| MCP server | `praxis-mcp` or `uv run python -m knowledge.mcp` | Tool surface for retrieving or writing PRAXIS knowledge through a configured backend |
| Session capture | `session-capture/wrapper/claude-trace` | Optional Claude Code launcher that uploads transcript slices when S3 capture is configured |
| AWS CDK | `cd infra; npm run deploy` | Provisions backend, frontend, auth, database, DNS, Phoenix, and session infrastructure |

### Storage Model

| Store | Component | Notes |
|-------|-----------|-------|
| JSON candidate store | `knowledge/serve/store.py` | Local/offline default when no Postgres DSN resolves |
| Postgres candidate store | `knowledge/serve/postgres_store.py` | Enabled when `PRAXIS_DB_URL` or the configured Secrets Manager secret resolves |
| Postgres/pgvector graph | `knowledge/knowledge_graph/knowledge_graph_variants/postgres_vector_graph.py` | Required for persistent graph-backed `/insights` and `/context` |
| S3 session slices | `session-capture/`, `infra/` | Optional raw transcript slice transport for downstream extraction |
| Static fixtures | `docs/integration/fixtures/`, `frontend-react/public/` | Contract and demo data for offline validation |

---

## Repository Layout

```text
praxis/
├── docs/
│   ├── plans/                  # Project plan, proposal, PRD, MVP docs
│   ├── integration/            # API contracts, eval metrics contract, fixtures, wire-up guide
│   ├── monica/                 # Dashboard architecture, demo, smoke, deployment, gap docs
│   ├── matt/                   # MCP docs and future-work eval designs
│   ├── ideation/               # Requirements and baseline ideation
│   └── proposals/              # Current and archived proposal notes
├── frontend/                   # Python contract layer and mock candidate provider
├── frontend-react/             # React/Vite dashboard
├── knowledge/
│   ├── serve/                  # FastAPI backend
│   ├── mcp/                    # MCP server surface
│   ├── evals/                  # Evaluation harness, cases, runners, graders
│   ├── knowledge_graph/        # Graph interfaces, stores, and write policies
│   ├── graph_reader/           # Context readers
│   ├── injestion/              # Ingestor interfaces and prompt ingestor (directory name is historical)
│   ├── llm/                    # LLM and embedder abstractions/implementations
│   └── observability/          # Phoenix/OpenTelemetry tracing seam
├── session-capture/            # Go `claude-trace` wrapper for optional transcript capture
├── infra/                      # AWS CDK infrastructure
├── scripts/                    # Mock/export/render seed helpers
├── .github/workflows/          # AWS deploy and backend-domain workflows
├── Dockerfile                  # Backend API container
├── pyproject.toml              # Python package, scripts, dependencies, pytest config
├── uv.lock                     # Locked Python dependencies
└── run.py                      # Repo-root shim into knowledge/run.py
```

---

## Onboarding Paths

### Software Architect / Team Lead

Start here:

1. [docs/plans/PRAXIS_Project_Plan.html](docs/plans/PRAXIS_Project_Plan.html) for the target architecture and sprint plan.
2. [docs/integration/candidate-api-v1.md](docs/integration/candidate-api-v1.md) for the backend/dashboard contract.
3. [docs/integration/eval-metrics-v1.md](docs/integration/eval-metrics-v1.md) for measurement shape.
4. [infra/README.md](infra/README.md) for AWS deployment and domain ownership.
5. [docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md](docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md) for known gaps and demo risk.

Decision points to understand before changing architecture:

- The dashboard should consume the API contract; it should not import backend graph internals.
- Persistent graph-backed `/insights` and `/context` require Postgres/pgvector.
- Mock/fixture mode is intentional and should stay available for demos and offline development.
- Evals are the credibility layer. UI success alone does not prove the knowledge loop improves future agent behavior.

### Backend / ML Engineer

Primary paths:

- `knowledge/serve/` - FastAPI routes, auth, stores, graph adapters.
- `knowledge/knowledge_graph/` - graph interfaces and stores.
- `knowledge/injestion/` - current ingestor seam.
- `knowledge/llm/` - LLM and embedder implementations.
- `knowledge/evals/` - cases and scoring harness.

Start with:

```powershell
uv sync
uv run pytest knowledge/ -q
uv run python -m knowledge.serve
```

Use `PRAXIS_AUTH_DISABLED=1` for local development without Cognito. Use `PRAXIS_DB_URL` when you need Postgres-backed candidates and graph retrieval.

### Frontend Engineer / Product Designer

Primary paths:

- `frontend-react/src/components/` - dashboard UI.
- `frontend-react/src/api/` - contract client, providers, parsers, graph/ingest/Phoenix clients.
- `frontend-react/src/auth/` - Cognito/org gating.
- `frontend-react/public/` - offline mock fixtures.
- `frontend/` - Python reference contract and mock data used by tests/export scripts.

Start with:

```powershell
cd frontend-react
npm ci
npm run dev
npm test
npm run lint
npm run build
```

Read [frontend-react/README.md](frontend-react/README.md), [docs/monica/ARCHITECTURE_MONICA.md](docs/monica/ARCHITECTURE_MONICA.md), and [docs/monica/monica-wireframes.md](docs/monica/monica-wireframes.md).

### DevOps / Platform Engineer

Primary paths:

- `infra/` - AWS CDK stacks.
- `.github/workflows/` - deployment automation.
- `Dockerfile` - backend API container.
- `frontend-react/render.yaml` and `knowledge/serve/render.yaml` - legacy Render references.

Start with:

```powershell
cd infra
npm ci
npm run build
npm run synth
```

Read [infra/README.md](infra/README.md) before deploying. AWS is the current system of record for hosting; Render files are retained as reference.

### Evaluations / QA Engineer

Primary paths:

- `knowledge/evals/cases/` - YAML eval cases.
- `knowledge/evals/tests/` - eval harness tests.
- `frontend/tests/` - Python contract and smoke tests.
- `frontend-react/src/**/*.test.ts*` - React/Vitest tests.
- `docs/integration/fixtures/` - canonical request/response fixtures.

Start with:

```powershell
uv run pytest knowledge/evals/tests/ -q
uv run pytest frontend/tests/ -q
cd frontend-react
npm test
```

---

## Prerequisites

| Tool | Version | Required for |
|------|---------|--------------|
| Python | 3.12+ | Backend, knowledge package, eval harness, Python contract tests |
| uv | Current recommended | Python dependency sync and command runner |
| Node.js | 20 | React dashboard and AWS CDK; dashboard pin is `frontend-react/.node-version` |
| npm | Bundled with Node | Frontend and infra scripts |
| Go | 1.22+ | `session-capture/wrapper` |
| Docker | Current | Backend API container and App Runner image asset |
| AWS CLI | Configured credentials | CDK deploys, Secrets Manager, S3/RDS/Cognito operations |

---

## Quick Start

### 1. Install Python dependencies

```powershell
uv sync
```

Optional observability dependencies:

```powershell
uv sync --extra observability
```

### 2. Run the backend API locally

Offline JSON-store mode:

```powershell
$env:PRAXIS_AUTH_DISABLED = "1"
uv run python -m knowledge.serve
```

Default local URL: `http://127.0.0.1:8000`.

Check health:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Postgres-backed mode:

```powershell
$env:PRAXIS_AUTH_DISABLED = "1"
$env:PRAXIS_DB_URL = "postgresql://user:password@host:5432/praxis_kg"
uv run python -m knowledge.serve
```

Graph-backed `/insights` and `/context` require the Postgres path. Without it, the API remains useful for candidate workflows, but graph persistence is unavailable.

### 3. Run the React dashboard

Install and start the dashboard:

```powershell
cd frontend-react
npm ci
npm run dev
```

Current local default:

- The React app defaults to the **Local Postgres** data-source preset at `http://localhost:8000`.
- Mock fixtures and mock providers still exist for stable demos/tests, but do not assume that leaving `VITE_PRAXIS_API_BASE_URL` unset forces a public mock-only build.
- Confirm the selected data source in the dashboard before demoing or mutating data.

Local live API mode:

```powershell
# frontend-react/.env.local
VITE_PRAXIS_API_BASE_URL=http://localhost:8000
VITE_PRAXIS_API_TOKEN=
VITE_PRAXIS_ORG_ID=default
VITE_PRAXIS_CONTRACT_VERSION=1
VITE_PRAXIS_AUTH_DISABLED=1
```

Then:

```powershell
npm run dev
```

Pair `VITE_PRAXIS_AUTH_DISABLED=1` with a backend started using `PRAXIS_AUTH_DISABLED=1`. Do not use auth bypass in hosted or shared environments.

### 4. Run evals

Run a specific case with the fake runner:

```powershell
uv run python -m knowledge.evals.run --fake <case_id>
```

Run with the configured real runner:

```powershell
uv run python -m knowledge.evals.run <case_id>
```

Run the repo-root smoke entry:

```powershell
$env:PRAXIS_EVAL_REAL = "0"
uv run python run.py
```

Eval cases live under `knowledge/evals/cases/`. Outputs append under `knowledge/evals/results/`.

### 5. Build optional session capture

```powershell
cd session-capture\wrapper
go build -o claude-trace ./cmd/claude-capture
```

Run an opt-in capture session:

```powershell
$env:PRAXIS_SLICE_BUCKET = "praxis-session-slices"
$env:AWS_REGION = "us-east-1"
.\claude-trace
```

If `PRAXIS_SLICE_BUCKET` or AWS credentials are missing, capture is disabled and the wrapper should behave like plain `claude`.

---

## Configuration

Copy [.env.example](.env.example) when you need a local reference, but do not commit real secrets.

### Backend API

| Variable | Required | Purpose |
|----------|----------|---------|
| `PRAXIS_AUTH_DISABLED` | No | Set `1` for local/offline dev principal instead of Cognito JWT verification |
| `COGNITO_USER_POOL_ID` | If auth enabled | Cognito user pool for JWT verification |
| `COGNITO_CLIENT_ID` | If auth enabled | Cognito app client id / audience |
| `COGNITO_REGION` | No | Cognito region, default `us-east-1` |
| `X-Praxis-Key` | Request header | Scoped API-key alternative to Cognito for API clients |
| `X-Praxis-Org` | Request header | Active org context; defaults to `default` where supported |
| `PRAXIS_DB_URL` | No | Direct Postgres DSN for candidate store and graph-backed routes |
| `PRAXIS_DB_SECRET` | No | Secrets Manager secret name for database credentials |
| `PRAXIS_API_HOST` | No | Uvicorn bind host |
| `PRAXIS_API_PORT` / `PORT` | No | Uvicorn port; `PORT` wins on hosted platforms |
| `PRAXIS_CORS_ORIGINS` | No | Comma-separated allowed origins |
| `PRAXIS_CORS_ORIGIN_REGEX` | No | Regex-based allowed origins |
| `PHOENIX_BASE_URL` | No | Phoenix origin used by trace proxy/deep links |
| `PHOENIX_PROJECT` | For proxy | Phoenix project identifier for read-only trace API queries |
| `PHOENIX_PROJECT_UI_ID` | No | Phoenix UI project node id for deep links |

### React Dashboard

| Variable | Required | Purpose |
|----------|----------|---------|
| `VITE_PRAXIS_API_BASE_URL` | No | Backend API URL; current local default resolves to `http://localhost:8000` through the Local Postgres preset |
| `VITE_PRAXIS_POSTGRES_API_BASE_URL` | No | Remote Postgres-backed API URL; preferred over the generic API URL for the Remote Postgres preset |
| `VITE_PRAXIS_API_TOKEN` | If auth enabled | Legacy/static Bearer token fallback for API calls |
| `VITE_PRAXIS_ORG_ID` | No | Active org sent as `X-Praxis-Org` |
| `VITE_PRAXIS_CONTRACT_VERSION` | No | Contract header, default `1` |
| `VITE_PRAXIS_AUTH_DISABLED` | No | Set `1` only for local React dev paired with backend `PRAXIS_AUTH_DISABLED=1` |
| `VITE_PRAXIS_EVAL_METRICS_URL` | No | Optional eval/evidence panel endpoint |
| `VITE_COGNITO_USER_POOL_ID` | If auth enabled | Cognito user pool id |
| `VITE_COGNITO_CLIENT_ID` | If auth enabled | Cognito app client id |
| `VITE_COGNITO_REGION` | No | Cognito region, default `us-east-1` |

### Evals, LLMs, and Tracing

| Variable | Required | Purpose |
|----------|----------|---------|
| `PRAXIS_EVAL_REAL` | No | Set `0` to force fake/offline eval path in the root runner |
| `OPENROUTER_API_KEY` | For OpenRouter | API key for OpenRouter runner/embedder/judge paths |
| `OPENROUTER_MODEL` | No | Runner model override |
| `OPENROUTER_JUDGE_MODEL` | No | Judge model override |
| `OPENROUTER_EMBED_MODEL` | No | Embedding model override |
| `CLAUDE_CODE_MODEL` | No | Claude Code runner model override |
| `PHOENIX_COLLECTOR_ENDPOINT` | No | Enables OpenTelemetry export to Phoenix |
| `PHOENIX_API_KEY` | If Phoenix auth enabled | Phoenix API key |
| `PHOENIX_TLS_VERIFY` | No | Set `false` only for self-signed Phoenix certs |
| `PHOENIX_PROJECT_NAME` | No | Phoenix project name, default `praxis` |

### Session Capture

| Variable | Required | Purpose |
|----------|----------|---------|
| `PRAXIS_SLICE_BUCKET` | To capture | S3 bucket for transcript slices; unset disables capture |
| `PRAXIS_ORG_ID` | No | Tenant org id used in S3 key/metadata |
| `PRAXIS_USER_ID` | No | Tenant user id; defaults to OS user |
| `AWS_REGION` | No | AWS region for the S3 client |

---

## Quality Gates

Run the narrow gate for the component you changed and the broader gate before merging substantial cross-cutting work.

### Python

```powershell
uv run pytest knowledge/ -q
uv run pytest frontend/tests/ -q
python -m py_compile knowledge\serve\app.py
```

### Eval Harness

```powershell
uv run pytest knowledge/evals/tests/ -q
uv run python -m knowledge.evals.run --fake <case_id>
```

### React Dashboard

```powershell
cd frontend-react
npm ci
npm test
npm run lint
npm run build
```

### Infra

```powershell
cd infra
npm run build
npm run synth
```

### Repository Hygiene

```powershell
git diff --check
```

Contract fixtures are canonical in [docs/integration/fixtures/](docs/integration/fixtures/). When changing API shapes, update the contract doc, fixtures, Python client tests, and React client tests together.

---

## Deployment

AWS is the current hosting system of record.

| Component | Path | Deployment notes |
|-----------|------|------------------|
| Backend API | `Dockerfile`, `infra/lib/backend-service-stack.ts` | App Runner service built from the repo-root Dockerfile |
| Frontend SPA | `frontend-react/`, `infra/lib/frontend-site-stack.ts` | Built with `VITE_*` values and served through S3 + CloudFront |
| Auth | `infra/lib/auth-user-pool-stack.ts` | Cognito user pool and public SPA client |
| Knowledge graph DB | `infra/lib/knowledge-graph-db-stack.ts` | RDS PostgreSQL 16 with pgvector |
| Session slices | `infra/lib/session-slices-stack.ts` | S3/EventBridge style capture pipeline |
| Phoenix | `infra/lib/phoenix-stack.ts` | Optional observability stack |
| DNS | `infra/lib/dns-stack.ts`, `infra/scripts/associate-backend-domain.mjs` | Cloudflare apex with delegated Route 53 subdomains |

Read [infra/README.md](infra/README.md) before deploying. It documents stack order, custom domains, CI behavior, and manual domain association for `mcp.praxiskg.com`.

Legacy Render manifests remain in the repo for reference only:

- [frontend-react/render.yaml](frontend-react/render.yaml)
- [knowledge/serve/render.yaml](knowledge/serve/render.yaml)

---

## Documentation Map

### Canonical Planning and Product Docs

| Document | Use |
|----------|-----|
| [docs/plans/PRAXIS_Project_Plan.html](docs/plans/PRAXIS_Project_Plan.html) | Architecture source of truth and sprint plan |
| [docs/plans/PRD.pdf](docs/plans/PRD.pdf) | Product requirements document |
| [docs/plans/mvp-plan.html](docs/plans/mvp-plan.html) | MVP detail and early eval schema |
| [docs/plans/proposal-praxis.md](docs/plans/proposal-praxis.md) | Historical proposal and problem framing |

### API, Integration, and Smoke Docs

| Document | Use |
|----------|-----|
| [docs/integration/candidate-api-v1.md](docs/integration/candidate-api-v1.md) | Candidate REST contract |
| [docs/integration/eval-metrics-v1.md](docs/integration/eval-metrics-v1.md) | Eval metrics response contract |
| [docs/integration/wire-up.md](docs/integration/wire-up.md) | Self-serve dashboard/API validation |
| [docs/integration/fixtures/](docs/integration/fixtures/) | Canonical contract payloads |
| [docs/monica/INTEGRATION_SMOKE.md](docs/monica/INTEGRATION_SMOKE.md) | Mock/live integration smoke checklist |

### Dashboard and UX Docs

| Document | Use |
|----------|-----|
| [frontend-react/README.md](frontend-react/README.md) | React dashboard setup and feature notes |
| [docs/monica/ARCHITECTURE_MONICA.md](docs/monica/ARCHITECTURE_MONICA.md) | Dashboard architecture |
| [docs/monica/monica-wireframes.md](docs/monica/monica-wireframes.md) | As-built dashboard wireframes and UX notes |
| [docs/monica/DEMO_SCRIPT.md](docs/monica/DEMO_SCRIPT.md) | Act 2 dashboard demo script |
| [docs/monica/REHEARSAL_LOG.md](docs/monica/REHEARSAL_LOG.md) | Demo rehearsal evidence |

### Backend, Infra, and Ops Docs

| Document | Use |
|----------|-----|
| [infra/README.md](infra/README.md) | AWS CDK deployment and DNS |
| [docs/monica/RDS_KG_DEPLOY.md](docs/monica/RDS_KG_DEPLOY.md) | RDS/pgvector setup and backend DB configuration |
| [docs/matt/MCP_SERVER.md](docs/matt/MCP_SERVER.md) | MCP server setup |
| [session-capture/README.md](session-capture/README.md) | `claude-trace` capture wrapper |

### Governance and Project Tracking

| Document | Use |
|----------|-----|
| [AUDIT.md](AUDIT.md) | Historical repo health audit; verify against current code before relying on stale findings |
| [CHANGELOG.md](CHANGELOG.md) | Release notes |
| [docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md](docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md) | Gap tracker and demo risk checklist |
| [docs/monica/MONICA_COMPLETION_PATH.md](docs/monica/MONICA_COMPLETION_PATH.md) | Dashboard completion path |
| [docs/monica/STANDUP_TEMPLATE.md](docs/monica/STANDUP_TEMPLATE.md) | Daily standup template |

---

## Operating Standards

### Security and Privacy

- Do not commit API keys, JWTs, database passwords, Phoenix keys, or AWS credentials.
- Keep raw Claude Code transcripts out of the repo unless they are sanitized fixtures.
- Use `PRAXIS_AUTH_DISABLED=1` only for local/offline development.
- Treat all uploaded session slices as sensitive. The capture wrapper is opt-in and disabled when `PRAXIS_SLICE_BUCKET` is unset.
- Preserve provenance on candidate knowledge: source path, session reference, line offset, or equivalent audit evidence.

### API and Contract Changes

- Update [docs/integration/candidate-api-v1.md](docs/integration/candidate-api-v1.md) before or with any candidate API shape change.
- Update [docs/integration/fixtures/](docs/integration/fixtures/) when request/response examples change.
- Keep Python and React contract clients aligned.
- Preserve an explicit offline fixture path unless the replacement has an equivalent local development and demo path.

### Git and Review

- Keep changes scoped to the component you are modifying.
- Prefer small pull requests with clear validation evidence.
- Use conventional commit style when committing: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`.
- Before pushing, check the branch and remote explicitly.

### Accessibility and UX

- Dashboard flows should keep provenance, confidence, state, and contradiction context visible.
- Approval actions should be explicit and reversible where possible.
- Graph ingest failures should fail soft in the dashboard; a missing graph backend should not block candidate review.

---

## Project History

The original Praxis repository and pre-migration commit history are preserved at [labs.gauntletai.com/monicapeters/praxis](https://labs.gauntletai.com/monicapeters/praxis) for provenance and attribution.

The current GitHub repository is [Antonelli-Tech-Solutions/praxis](https://github.com/Antonelli-Tech-Solutions/praxis). The checked-in code now contains the dashboard, API, knowledge substrate, eval harness, session capture wrapper, infrastructure, and documentation needed for a new contributor to run and extend the MVP locally.
