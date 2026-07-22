# PRAXIS

**PRAXIS is a human-gated knowledge loop for AI coding agents.** It turns session
logs and other engineering artifacts into reviewed, graph-backed knowledge that
can be retrieved in future coding sessions and measured with evals.

[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](pyproject.toml)
[![React](https://img.shields.io/badge/dashboard-React-61DAFB.svg)](frontend-react/README.md)
[![Go](https://img.shields.io/badge/session--capture-Go-00ADD8.svg)](session-capture/README.md)
[![API Contract](https://img.shields.io/badge/API-contract_v1-4CAF50.svg)](docs/integration/candidate-api-v1.md)

- **Production app:** [https://djuqmwjrcs2yx.cloudfront.net/](https://djuqmwjrcs2yx.cloudfront.net/)
- **Gauntlet AI GitLab repository:** [monicapeters/praxis](https://labs.gauntletai.com/monicapeters/praxis)
- **Current repository:** [Antonelli-Tech-Solutions/praxis](https://github.com/Antonelli-Tech-Solutions/praxis)
- **Architecture source of truth:** [docs/plans/PRAXIS_Project_Plan.html](docs/plans/PRAXIS_Project_Plan.html)
- **API contract:** [docs/integration/candidate-api-v1.md](docs/integration/candidate-api-v1.md)
- **License:** [MIT](LICENSE)

---

## Contents

- [What This Repository Contains](#what-this-repository-contains)
- [System Model](#system-model)
- [Repository Map](#repository-map)
- [First 30 Minutes](#first-30-minutes)
- [Role-Based Onboarding](#role-based-onboarding)
- [Runtime Configuration](#runtime-configuration)
- [Validation](#validation)
- [Deployment](#deployment)
- [Documentation Index](#documentation-index)
- [Engineering Standards](#engineering-standards)
- [Project History](#project-history)

---

## What This Repository Contains

PRAXIS is an active capstone/MVP codebase. It is built around a single product
loop:

```text
session logs / source artifacts
  -> distillation and write policy
  -> proposed facts in Postgres/pgvector
  -> human review in the dashboard
  -> active knowledge graph
  -> MCP/context retrieval
  -> eval measurement
```

The implementation is intentionally multi-surface:

| Surface | Primary paths | What it does |
|---------|---------------|--------------|
| FastAPI backend | `knowledge/serve/` | Tenant-aware candidate, graph, org, API key, ingest, insight, snapshot, eval, derivation, surface-binding, and context routes |
| Knowledge graph | `knowledge/knowledge_graph/` | Facts, edges, write policies, graph readers, Postgres/pgvector persistence, derivation and retrieval behavior |
| Human-gate dashboard | `frontend-react/` | React/Vite UI for org login, candidate review, graph inspection, contradiction review, Phoenix links, evidence panels, and API-backed workflows |
| Python contract layer | `frontend/` | Reference candidate models, contract client, mock data, Phoenix proxy, and smoke tests |
| Eval harness | `knowledge/evals/` | YAML cases, deterministic checks, runners, graders, cached/live embeddings, and result writing |
| MCP server | `knowledge/mcp/` | `praxis-mcp` tool surface for retrieval and backend interaction |
| Session capture | `session-capture/` | Go `claude-trace` wrapper for optional Claude Code transcript slice upload |
| Infrastructure | `infra/` | AWS CDK stacks for App Runner, CloudFront/S3, Cognito, RDS/Postgres/pgvector, Phoenix, DNS, and session storage |

Current implementation note: `knowledge/serve/app.py` requires a resolvable
Postgres DSN for the backend data routes. The backend health route stays open,
but the product data plane is Postgres-backed, not a JSON-file fallback.

---

## System Model

PRAXIS separates four concerns that are often blurred in agent-memory systems:

| Concern | PRAXIS implementation |
|---------|-----------------------|
| Capture | Session traces, uploaded source artifacts, and manual insight writes preserve provenance |
| Distillation | Candidate facts are generated, deduplicated, conflict-checked, and assigned lifecycle state |
| Governance | Humans promote, reject, edit, and resolve contradictions before knowledge becomes active |
| Measurement | Evals compare behavior and expose whether knowledge retrieval is improving outcomes |

At runtime, the backend treats the Postgres facts table as the source of truth.
The dashboard candidate model is a read projection over facts. Graph views,
contradiction review, MCP retrieval, snapshots, and context endpoints all read
from the same tenant-scoped graph.

```text
React dashboard
      |
      | candidate-api-v1, X-Praxis-Contract: 1
      v
FastAPI backend
      |
      | tenant = X-Praxis-Org + authenticated principal
      v
Postgres 16 + pgvector facts graph
      |
      +--> MCP context retrieval
      +--> eval harness
      +--> snapshots / mounts / derivations
      +--> insight and ingest write paths
```

Authentication is required for data routes. For local development only, set
`PRAXIS_AUTH_DISABLED=1` on the backend and `VITE_PRAXIS_AUTH_DISABLED=1` in the
React app to use the fixed development principal.

---

## Repository Map

```text
praxis/
├── docs/
│   ├── integration/          # REST contracts, fixtures, eval metrics, smoke docs
│   ├── monica/               # Dashboard architecture, demo, gap, and rehearsal docs
│   ├── plans/                # Project plan, PRD, MVP plan, proposal history
│   ├── matt/                 # MCP and future-work docs
│   └── proposals/            # Proposal notes and archived planning material
├── frontend/                 # Python contract client, mock data, Phoenix proxy, tests
├── frontend-react/           # React/Vite dashboard
├── infra/                    # AWS CDK infrastructure
├── knowledge/
│   ├── serve/                # FastAPI app, stores, auth, DB bootstrap, route tests
│   ├── knowledge_graph/      # Graph interfaces, Postgres graph, write policies
│   ├── evals/                # Eval cases, runners, deterministic checks, results
│   ├── graph_reader/         # Retrieval readers
│   ├── injestion/            # Historical spelling; ingestion interfaces and prompt ingestor
│   ├── llm/                  # LLM and embedder implementations
│   ├── mcp/                  # MCP server entrypoint and tests
│   └── observability/        # Phoenix/OpenTelemetry tracing setup
├── migrations/               # Yoyo migrations for the Postgres facts spine
├── praxis_client/            # Client package surface
├── scripts/                  # Fixture export and helper scripts
├── session-capture/          # Go Claude Code capture wrapper
├── specs/                    # Feature/specification work items
├── tools/                    # Project utilities
├── docker-compose.yml        # Local pgvector Postgres
├── justfile                  # Common local development commands
├── pyproject.toml            # Python package, scripts, dependencies, pytest config
└── run.py                    # Repo-root shim into knowledge/run.py
```

---

## First 30 Minutes

These steps get a local engineer to a working backend and dashboard. Commands
are shown for PowerShell from the repository root unless noted.

### 1. Install Prerequisites

| Tool | Expected version | Used by |
|------|------------------|---------|
| Python | 3.12+ | Backend, knowledge graph, evals, tests |
| `uv` | Current | Python dependency management and command runner |
| Docker Desktop | Current | Local Postgres/pgvector |
| Node.js | `20` for the dashboard | `frontend-react/` |
| npm | Bundled with Node | Dashboard and CDK dependencies |
| Go | 1.22+ | Optional session-capture wrapper |
| AWS CLI | Configured only for cloud work | CDK deploys, Secrets Manager, S3/RDS/Cognito |

### 2. Install Python Dependencies

```powershell
uv sync
```

Optional tracing dependencies:

```powershell
uv sync --extra observability
```

### 3. Start Local Postgres

The local database is a Dockerized Postgres 16 image with pgvector enabled.

```powershell
just db-up
just db-bootstrap
```

Expected local DSN:

```text
postgresql://praxis:praxis@localhost:5433/praxis_kg
```

If your local `.env` does not already define it, set:

```powershell
$env:PRAXIS_DB_URL = "postgresql://praxis:praxis@localhost:5433/praxis_kg"
$env:PRAXIS_AUTH_DISABLED = "1"
```

### 4. Start the Backend

In terminal 1:

```powershell
just backend
```

Equivalent command:

```powershell
uv run python -m knowledge.serve
```

Default URL:

```text
http://localhost:8000
```

Health check:

```powershell
Invoke-RestMethod http://localhost:8000/health
```

Expected shape:

```json
{ "status": "ok", "store": "postgres" }
```

### 5. Start the Dashboard

In terminal 2:

```powershell
cd frontend-react
npm ci
npm run dev
```

Default URL:

```text
http://localhost:5173
```

For local auth bypass, create or update `frontend-react/.env.local`:

```env
VITE_PRAXIS_API_BASE_URL=http://localhost:8000
VITE_PRAXIS_ORG_ID=default
VITE_PRAXIS_CONTRACT_VERSION=1
VITE_PRAXIS_AUTH_DISABLED=1
```

The dashboard defaults to the **Local Postgres** data-source preset. It expects
the FastAPI backend at `http://localhost:8000`.

### 6. Create or Join an Org

Data routes are tenant-scoped by `X-Praxis-Org`. In local auth-bypass mode the
backend uses a fixed dev principal, but that principal still needs org
membership. Use the dashboard org flow or the `/orgs` and `/orgs/join` API
routes to create or join the org you are testing against.

### 7. Run the Core Test Gates

From the repository root:

```powershell
uv run pytest knowledge/ -q
uv run pytest frontend/tests/ -q
```

From `frontend-react/`:

```powershell
npm test
npm run lint
npm run build
```

---

## Role-Based Onboarding

### Architect or Technical Lead

Read these first:

1. [docs/plans/PRAXIS_Project_Plan.html](docs/plans/PRAXIS_Project_Plan.html)
2. [docs/integration/candidate-api-v1.md](docs/integration/candidate-api-v1.md)
3. [docs/integration/eval-metrics-v1.md](docs/integration/eval-metrics-v1.md)
4. [infra/README.md](infra/README.md)
5. [AUDIT.md](AUDIT.md)

Key decisions to preserve:

- The dashboard consumes the REST contract; it should not import backend graph internals.
- The facts table is the source of truth for candidate, graph, contradiction, and context behavior.
- The mock/demo fixture path is useful for UI tests and demos, but the backend data plane is Postgres.
- Evals are the proof layer. A working UI does not prove the knowledge loop improves agent behavior.

### Backend or ML Engineer

Primary paths:

- `knowledge/serve/`
- `knowledge/knowledge_graph/`
- `knowledge/injestion/`
- `knowledge/llm/`
- `knowledge/evals/`
- `migrations/`

Useful commands:

```powershell
uv sync
just db-up
just db-bootstrap
just backend
uv run pytest knowledge/ -q
```

When changing API shape, update the contract, fixtures, Python client tests, and
React client tests together.

### Frontend Engineer or Product Designer

Primary paths:

- `frontend-react/src/components/`
- `frontend-react/src/api/`
- `frontend-react/src/auth/`
- `frontend-react/src/config/`
- `frontend-react/public/`
- `frontend/`

Useful commands:

```powershell
cd frontend-react
npm ci
npm run dev
npm test
npm run lint
npm run build
```

Read [frontend-react/README.md](frontend-react/README.md),
[docs/monica/ARCHITECTURE_MONICA.md](docs/monica/ARCHITECTURE_MONICA.md), and
[docs/monica/monica-wireframes.md](docs/monica/monica-wireframes.md).

### Evaluation or QA Engineer

Primary paths:

- `knowledge/evals/cases/`
- `knowledge/evals/tests/`
- `frontend/tests/`
- `frontend-react/src/**/*.test.ts*`
- `docs/integration/fixtures/`

Useful commands:

```powershell
uv run pytest knowledge/evals/tests/ -q
uv run pytest frontend/tests/ -q
uv run python -m knowledge.evals.run --fake <case_id>
cd frontend-react
npm test
```

Eval cases are YAML-backed and live under `knowledge/evals/cases/`. Results are
written under `knowledge/evals/results/`.

### Platform or DevOps Engineer

Primary paths:

- `infra/`
- `.github/workflows/`
- `Dockerfile`
- `docker-compose.yml`
- `knowledge/serve/render.yaml`
- `frontend-react/render.yaml`

Useful commands:

```powershell
cd infra
npm ci
npm run build
npm run synth
```

AWS CDK is the current deployment path. Render manifests remain for historical
or reference use.

### Session Capture Engineer

Primary path:

- `session-capture/`

Build the wrapper:

```powershell
cd session-capture\wrapper
go build -o claude-trace ./cmd/claude-capture
```

Session capture is opt-in. If `PRAXIS_SLICE_BUCKET` or AWS credentials are not
configured, the wrapper should behave like normal Claude Code execution.

---

## Runtime Configuration

Do not commit real secrets. Keep local overrides in `.env` or tool-specific
local env files.

### Backend

| Variable | Required | Purpose |
|----------|----------|---------|
| `PRAXIS_DB_URL` | Local or direct DB mode | Postgres DSN used by the backend and migrations |
| `PRAXIS_DB_SECRET` | Cloud secret mode | AWS Secrets Manager secret name for database credentials |
| `PRAXIS_AUTH_DISABLED` | Local only | Set `1` to bypass Cognito and use the fixed dev principal |
| `COGNITO_USER_POOL_ID` | Auth enabled | Cognito user pool for JWT verification |
| `COGNITO_CLIENT_ID` | Auth enabled | Cognito app client id/audience |
| `COGNITO_REGION` | Auth enabled | Cognito region, default `us-east-1` |
| `PRAXIS_API_HOST` | Optional | Uvicorn bind host, default `127.0.0.1` |
| `PRAXIS_API_PORT` / `PORT` | Optional | Backend port; hosted platforms commonly provide `PORT` |
| `PRAXIS_CORS_ORIGINS` | Optional | Comma-separated explicit CORS origins |
| `PRAXIS_CORS_ORIGIN_REGEX` | Optional | Regex CORS override |
| `OPENROUTER_API_KEY` | LLM paths | Required for live LLM/embedder-backed write/eval paths |
| `PHOENIX_COLLECTOR_ENDPOINT` | Optional | Enables OpenTelemetry trace export |

Data clients send:

| Header | Purpose |
|--------|---------|
| `Authorization: Bearer <token>` | Cognito JWT unless `PRAXIS_AUTH_DISABLED=1` is active |
| `X-Praxis-Key` | Scoped API-key alternative where supported |
| `X-Praxis-Org` | Active tenant/org context |
| `X-Praxis-Contract: 1` | Candidate API contract version |

### React Dashboard

| Variable | Required | Purpose |
|----------|----------|---------|
| `VITE_PRAXIS_API_BASE_URL` | Local/live API | Backend URL, usually `http://localhost:8000` locally |
| `VITE_PRAXIS_POSTGRES_API_BASE_URL` | Remote preset | Hosted Postgres-backed API URL |
| `VITE_PRAXIS_API_TOKEN` | Auth/token flows | Static bearer token fallback |
| `VITE_PRAXIS_ORG_ID` | Optional | Default org sent as `X-Praxis-Org` |
| `VITE_PRAXIS_CONTRACT_VERSION` | Optional | Contract header, default `1` |
| `VITE_PRAXIS_AUTH_DISABLED` | Local only | Set `1` only when backend also uses `PRAXIS_AUTH_DISABLED=1` |
| `VITE_COGNITO_USER_POOL_ID` | Auth enabled | Cognito user pool id |
| `VITE_COGNITO_CLIENT_ID` | Auth enabled | Cognito app client id |
| `VITE_COGNITO_REGION` | Optional | Cognito region |

### Session Capture

| Variable | Required | Purpose |
|----------|----------|---------|
| `PRAXIS_SLICE_BUCKET` | To upload slices | S3 bucket for transcript slices |
| `PRAXIS_ORG_ID` | Optional | Org metadata for captured slices |
| `PRAXIS_USER_ID` | Optional | User metadata; defaults to OS user where supported |
| `AWS_REGION` | Cloud upload | AWS region for S3 |

---

## Validation

Run focused checks for the component you changed. Run the broader gate before
merging cross-cutting work.

### Python and Backend

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

Run these from `frontend-react/`:

```powershell
npm test
npm run lint
npm run build
```

### Infrastructure

Run these from `infra/`:

```powershell
npm run build
npm run synth
```

### Repository Hygiene

```powershell
git diff --check
git status --short --branch
```

---

## Deployment

AWS is the current hosting system of record.

| Component | Path | Deployment notes |
|-----------|------|------------------|
| Backend API | `Dockerfile`, `infra/lib/backend-service-stack.ts` | App Runner service built from the repo-root Dockerfile |
| Frontend SPA | `frontend-react/`, `infra/lib/frontend-site-stack.ts` | Vite build deployed to S3 and served through CloudFront |
| Auth | `infra/lib/auth-user-pool-stack.ts` | Cognito user pool and public SPA client |
| Knowledge graph DB | `infra/lib/knowledge-graph-db-stack.ts` | RDS PostgreSQL 16 with pgvector |
| Session slices | `infra/lib/session-slices-stack.ts` | S3-backed transcript slice storage |
| Phoenix | `infra/lib/phoenix-stack.ts` | Optional observability stack |
| DNS | `infra/lib/dns-stack.ts`, `infra/scripts/associate-backend-domain.mjs` | Cloudflare apex with Route 53 delegated subdomains |

Read [infra/README.md](infra/README.md) before deploying. It documents stack
order, required AWS context, CI behavior, and domain association.

---

## Documentation Index

| Document | Use |
|----------|-----|
| [docs/plans/PRAXIS_Project_Plan.html](docs/plans/PRAXIS_Project_Plan.html) | Architecture source of truth and sprint plan |
| [docs/plans/PRD.pdf](docs/plans/PRD.pdf) | Product requirements |
| [docs/integration/candidate-api-v1.md](docs/integration/candidate-api-v1.md) | Candidate REST contract |
| [docs/integration/eval-metrics-v1.md](docs/integration/eval-metrics-v1.md) | Eval metrics contract |
| [docs/integration/fixtures/](docs/integration/fixtures/) | Canonical request/response payloads |
| [frontend-react/README.md](frontend-react/README.md) | Dashboard setup and feature notes |
| [docs/monica/ARCHITECTURE_MONICA.md](docs/monica/ARCHITECTURE_MONICA.md) | Dashboard architecture |
| [docs/monica/INTEGRATION_SMOKE.md](docs/monica/INTEGRATION_SMOKE.md) | Integration smoke checklist |
| [docs/monica/DEMO_SCRIPT.md](docs/monica/DEMO_SCRIPT.md) | Dashboard demo script |
| [docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md](docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md) | Known gaps and demo risk |
| [docs/matt/MCP_SERVER.md](docs/matt/MCP_SERVER.md) | MCP server setup |
| [infra/README.md](infra/README.md) | AWS deployment and DNS operations |
| [session-capture/README.md](session-capture/README.md) | Claude Code capture wrapper |
| [AUDIT.md](AUDIT.md) | Historical repo health audit; verify against current code before relying on older findings |
| [CHANGELOG.md](CHANGELOG.md) | Release notes |

---

## Engineering Standards

### Security and Privacy

- Never commit API keys, JWTs, database passwords, Phoenix keys, AWS credentials,
  raw production transcripts, or unsanitized customer/session data.
- Use `PRAXIS_AUTH_DISABLED=1` only for local development.
- Treat session slices and uploaded logs as sensitive by default.
- Preserve provenance on promoted knowledge: source path, session reference,
  artifact id, line offset, or equivalent audit evidence.

### API and Contract Discipline

- Keep `docs/integration/candidate-api-v1.md`, fixtures, backend routes, Python
  client tests, and React client tests aligned.
- Keep `X-Praxis-Contract: 1` behavior backward-compatible unless the contract is
  explicitly versioned.
- Preserve offline/mock fixture support for tests and demos even when adding live
  backend features.

### Development Workflow

- Keep pull requests small and scoped to one component or contract change.
- Run the narrow validation gate for the component you changed.
- Include exact validation evidence in PR descriptions.
- Check branch, remote, and working tree before pushing.

### UX Principles

- Dashboard review flows must keep confidence, provenance, lifecycle state, and
  contradiction context visible.
- Promote/reject/resolve actions should be explicit and auditable.
- Backend, graph, ingest, or Phoenix failures should fail soft in the dashboard
  where possible; candidate review should not be blocked by noncritical evidence
  surfaces.

---

## Project History

The original Praxis repository and pre-migration history are preserved at
[labs.gauntletai.com/monicapeters/praxis](https://labs.gauntletai.com/monicapeters/praxis)
for provenance and attribution.

The current GitHub repository is
[Antonelli-Tech-Solutions/praxis](https://github.com/Antonelli-Tech-Solutions/praxis).
This checkout contains the backend, dashboard, knowledge graph, eval harness,
session capture wrapper, infrastructure, and documentation needed for a new
engineer to run and extend the MVP locally.
