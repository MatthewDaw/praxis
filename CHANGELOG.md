# Changelog

All notable changes to the PRAXIS repository are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versioning aligns with [pyproject.toml](pyproject.toml) (`0.1.0`).

## [Unreleased]

### Added

- **`frontend-react/`** — Vite + React + TypeScript Knowledge Graph dashboard targeting [candidate-api-v1](docs/integration/candidate-api-v1.md); mock mode with 17 candidates exported from `frontend/mock_data.py`; promote/reject/contradiction resolve + eval metrics embed; `npm run build` verified.
- **Candidate API CRUD + tenancy** — FastAPI candidate create/read/update/delete paths now support the React review workflow, with JSON-store and Postgres-store implementations, Cognito JWT verification, password-gated orgs, per-request `X-Praxis-Org` tenancy, and `PRAXIS_AUTH_DISABLED=1` for offline development.
- **Postgres/pgvector knowledge backend** — added `PostgresVectorGraph`, shared write-policy support, database wiring, schema support, and tests for Postgres-backed candidate and graph storage.
- **MCP knowledge server** — added `knowledge/mcp/`, `praxis-mcp` entrypoint, identity tests, MCP setup docs, dashboard setup tab, and `/praxis-login` slash-command prompt for login guidance.
- **Phoenix observability** — optional OpenTelemetry/Phoenix tracing for LLM, embedding, eval runner, and judge surfaces; React dashboard support for Phoenix trace context; `PHOENIX_*` configuration documented in README and `.env.example`.
- **Eval harness improvements** — backend capability gating, per-case model pins via `CLAUDE_CODE_MODEL`, structured OpenRouter grading, per-artifact `output_file` / `writes_file` / `modifies_file` checks, grouped Phoenix spans, expected-fail red specs, negative controls, and retrieval-focused before/after cases.
- **Retrieval and embedding cache path** — added `RetrievingReader`, `CachedEmbedder`, embedder-axis gating, committed embedding fixtures, and a retrieval proposal marked implemented.
- **Cloud deployment surface** — AWS CDK stacks now cover shared networking, RDS/pgvector, Cognito, App Runner backend, CloudFront frontend, Phoenix, Route 53 DNS, and `app.praxiskg.com` / `mcp.praxiskg.com` domain association helpers.
- **CI/deploy automation** — added GitHub Actions deploy workflow plus GitLab per-stack deploy jobs with change detection and AWS website deploy support.
- **Project provenance and licensing** — added MIT license and README note preserving the original GitLab project history for attribution.

### Removed

- **Streamlit human-gate dashboard** — removed `frontend/app.py`, `components/`, `.streamlit/`, `render.yaml`, and `requirements.txt`; React (`frontend-react/`) is the sole dashboard UI; `frontend/` retained as Python contract + mock-data package.
- **Legacy eval cases** — retired weak or misleading eval fixtures, including the `add_via_subtract` reuse steer and contradiction cases that did not discriminate reliably.

### Changed

- **Documentation** — README, AUDIT, Monica pillar docs, wire-up guide, and CHANGELOG updated for React-only dashboard posture.
- **Dependencies** — removed `streamlit` and `pandas` from root `pyproject.toml`.
- **Dashboard workflow** — expanded from mock-only review into live API CRUD, Cognito login, org switching, change-org-password support, slimmer candidate tables, top-centered graph fit, local-log/mock/live data-source wiring, Phoenix trace context, and MCP setup guidance.
- **Session capture** — replaced the heavier `claude+` daemon/PTY workflow with a thinner `claude-trace` launcher path and updated capture storage toward session slices and insights.
- **Infrastructure** — refactored CDK around shared VPC/config primitives, moved hosting to AWS App Runner + CloudFront, added Render Cognito env support, and replaced ad hoc web deploy scripting with domain-aware deploy jobs.
- **Eval case layout** — flattened cases under `knowledge/evals/cases`, clarified reader/ingestion case naming, calibrated lost-in-middle red specs, and deepened retrieval haystacks to keep expected-fail coverage honest.
- **README status** — refreshed implementation status for auth, multi-tenancy, CRUD, hosting, MCP, tracing, and the current Day 4 integration posture.

### Fixed

- **Candidate API** — aligned CRUD handlers with per-call multi-tenant store APIs and allowed `praxiskg.com` origins in CORS preflight.
- **Auth** — allowed clock-skew leeway in Cognito JWT verification.
- **Docker/App Runner** — run the virtualenv Python directly so App Runner health checks pass, and exclude `infra/` from the Docker build context.
- **Infrastructure deploys** — corrected App Runner DNS target lookup, made domain association wait for `ACTIVE`, and made ACTIVE checks case-insensitive/idempotent.
- **Eval reliability** — de-flaked recency checks, made `scoped_conflict` a real discriminator, normalized `safety_user_overrides_graph`, and clarified model/backend failures.

---

## [0.1.0] — 2026-06-18

Sprint Day 2 deliverables across all three pillars.

### Added

- **`knowledge/` package** — in-memory knowledge graph, prompt ingestor, whole-file graph reader, and `build_trio()` wiring factory (`feat/knowledge`).
- **Eval harness** under `knowledge/evals/` — YAML case registry, deterministic check plugins, `FakeRunner` for offline runs, and `ClaudeCodeRunner` / `ClaudeCodeJudge` for real subscription-backed evals.
- **Repo-root eval entrypoint** — `run.py` shim and `knowledge/run.py` debugger entry; `PRAXIS_EVAL_REAL=0` toggles offline mode.
- **Session capture** — Go `claude+` CLI (host / ls / stop) with PTY daemon, JSONL transcript tailer, and DynamoDB writer (`session-capture/`).
- **AWS CDK infra** — `infra/` stack provisioning the `praxis-sessions` DynamoDB table.
- **Dashboard API integration** — `ApiDataProvider` and contract v1 client with 409/400 retry logic; eval metrics embed component (`PRAXIS_EVAL_METRICS_URL`).
- **Integration contracts** — `docs/integration/candidate-api-v1.md`, `eval-metrics-v1.md`, JSON fixtures, and self-serve wire-up guide.
- **Render deploy blueprint** — `frontend/render.yaml` and deploy documentation for portfolio mock demo.
- **Repository audit** — [AUDIT.md](AUDIT.md) point-in-time health review.

### Changed

- **Repository layout** — inlined `session-capture` (dropped submodule); moved CDK from submodule to repo-root `infra/`.
- **Dashboard polish** — human-gate workflow refinements, contradiction resolution panel, confidence badges, mock/live mode banner.
- **MVP documentation** — added `docs/plans/mvp-plan.html`; parked post-MVP design under `docs/matt/future-work/`.
- **README** — updated to reflect actual code layout (`knowledge/`, `session-capture/`) instead of planned `pipeline/` / `eval/` paths.

### Fixed

- Reverted and re-landed knowledge loop after harness iteration (`Revert` + clean `feat/knowledge` merge).

---

## [0.0.2] — 2026-06-17

Sprint Day 1 — foundation, dashboard MVP, and project documentation.

### Added

- **Streamlit human-gate dashboard** — modular `frontend/` with candidate list/detail, state machine (`proposed → suggested → active`), provenance display, and mock fixtures.
- **Project documentation** — HTML project plan, pillar DRAFT plans (Matthew, Monica, Dominic), dashboard wireframes, and editable architecture SVG.
- **Team Cursor rules** — shared quality standards, dashboard patterns, pipeline/eval contracts, GitLab sync workflow.
- **Initial README** — problem statement, loop diagram, MVP scope, team roster, and sprint timeline.

### Changed

- Relocated Monica pillar docs under `docs/monica/`; added demo script and standup template.
- Dashboard architecture document aligned with implemented Streamlit module boundaries.

---

## [0.0.1] — 2026-06-17

### Added

- Initial repository commit — capstone proposal, PRD references, and team role plans.

[Unreleased]: https://labs.gauntletai.com/monicapeters/praxis/-/compare/main...HEAD
[0.1.0]: https://labs.gauntletai.com/monicapeters/praxis/-/commits/main
[0.0.2]: https://labs.gauntletai.com/monicapeters/praxis/-/commits/main
[0.0.1]: https://labs.gauntletai.com/monicapeters/praxis/-/commits/main
