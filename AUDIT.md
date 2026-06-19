# PRAXIS Repository Audit

**Audit date:** 2026-06-18 (EOD refresh for `frontend-react/`)  
**Branch:** `monica/dashboard-human-gate`  
**Sync:** up to date with `origin/monica/dashboard-human-gate`  
**Auditor scope:** Full-repo health, docs/code alignment, integration readiness, test posture  
**Source of truth:** [docs/PRAXIS_Project_Plan.html](docs/PRAXIS_Project_Plan.html), [README.md](README.md)

---

## Executive summary

PRAXIS is a three-pillar capstone sprint building a self-improving knowledge loop for Claude Code agents. The repository is **actively scaffolding** with uneven maturity across pillars:

| Pillar | Owner | Location | Maturity |
|--------|-------|----------|----------|
| Dashboard & Human Gate | Monica Peters | `frontend/`, `frontend-react/` | **High** — Streamlit demo-ready on mock; React client shipped for Matthew API validation |
| ML & Knowledge Pipeline | Matthew Daw | `knowledge/` (+ planned `pipeline/`) | **Early** — in-memory KG, ingestor skeleton; no REST API yet |
| Architecture, Eval & Integration | Dominic Antonelli | `knowledge/evals/`, `session-capture/`, `infra/` | **Partial** — eval harness skeleton + session capture Go wrapper |

**Overall verdict:** The human-gate pillar is demo-ready on mock fixtures and contract-tested for Days 6–7 integration. The backend candidate API (`GET/POST /candidates/*`) is **not implemented** in-repo. README layout (`pipeline/`, `eval/`) **lags actual code layout** (`knowledge/`, `session-capture/`). No GitLab CI pipeline exists yet.

**Primary risks before demo (Days 9–10):**

1. Matthew's candidate REST server is the critical path for live integration.
2. Root `pytest` fails without `PYTHONPATH=frontend` — easy fix, currently undocumented in `pyproject.toml`.
3. No CI gate — regressions rely on manual test runs.
4. Eval compounding proof depends on Dominic's metrics endpoint and real replay runs.

---

## Audit context

```text
Sprint Day 1 = 2026-06-16 (Wed)
Today        = 2026-06-18 (Thu — skipped work day per plan; audit run on dev branch)
Integration  = Days 6–7 target (2026-06-23 – 2026-06-24)
Demo         = Days 9–10 (2026-06-26 – 2026-06-27)
```

This audit reflects the tree on `monica/dashboard-human-gate` after syncing with `origin/main`. It is a point-in-time snapshot, not a merge request.

---

## Repository layout — planned vs actual

### README expected layout

```text
praxis/
├── pipeline/          # Matthew — ingest, detect, distill, score, KG
├── eval/              # Dominic — harness, benchmark, metrics
├── frontend/          # Monica — Streamlit human gate
└── frontend-react/    # Optional future React UI
```

### Actual layout (2026-06-18)

| Path | Status | Notes |
|------|--------|-------|
| `frontend/` | ✅ Present | 17 Python modules; Streamlit app, components, services, tests |
| `frontend-react/` | ✅ Present | Vite + React + TS; contract v1 client; mock + live API modes; `npm run build` passes |
| `pipeline/` | ❌ Missing | Matthew's distillation/API work lives under `knowledge/` today |
| `eval/` | ❌ Missing | Eval harness under `knowledge/evals/`; not top-level `eval/` |
| `knowledge/` | ✅ Present | KG, ingestion, graph reader, eval harness (29 `.py` files) |
| `session-capture/` | ✅ Present | Go `claude+` wrapper, DynamoDB capture (50 non-vendored `.go` files) |
| `infra/` | ✅ Present | AWS CDK — DynamoDB sessions table (2 `.ts` files) |
| `docs/` | ✅ Rich | Plans, integration contracts, fixtures, pillar docs |
| `.cursor/rules/` | ✅ Present | Team quality standards |
| `.gitlab-ci.yml` | ❌ Missing | No automated CI |

**Docs/code drift:** README, Monica architecture, and integration docs reference `pipeline/` and `eval/` as canonical paths. Implementations currently sit in `knowledge/` and `session-capture/`. This is not blocking integration (API contracts are path-agnostic) but **will confuse onboarding** until README or directory names converge.

---

## Pillar assessments

### 1. Dashboard & Human Gate (`frontend/`) — Monica

**Status:** Demo-ready on mock; API client implemented; awaiting live backend.

| Area | Finding |
|------|---------|
| Entry | `app.py` — slim wiring only; UI in `components/`, data in `services/` |
| State machine | `proposed → suggested → active` via `next_promotion_state()`; `decayed` + unknown states handled |
| Provenance | Required on `Candidate`; displayed in detail view |
| Contradictions | Side-by-side panel with resolve/defer; `cand_9` ↔ `cand_16` in mock |
| Confidence UX | `ConfidenceBreakdown` + badge components; low-confidence promote warning |
| Eval embed | `eval_metrics_embed.py` — fetches `PRAXIS_EVAL_METRICS_URL` or shows placeholder |
| API client | `ApiDataProvider` — stdlib `urllib`, contract v1 headers, 409/400 retry logic |
| Deploy | `render.yaml` — mock-only portfolio deploy; env vars for live API |
| Boundary rule | ✅ No imports from `pipeline/` or `eval/` (verified) |

**Tests (11 passing with `PYTHONPATH=frontend`):**

- `test_contract_fixtures.py` — 7 tests against `docs/integration/fixtures/*.json`
- `test_mock_gate_workflow.py` — 4 smoke tests for promote/contradiction chain

**Gaps:**

- Live API never exercised in CI (no server in repo).
- `pyproject.toml` does not add `frontend` to `pythonpath` — root `pytest` collection fails (see Test posture).
- Accessibility pass and user-flow video remain manual (see `docs/monica/DAYS_9_10_REMAINING.md`).

---

### 1b. Knowledge Graph Dashboard (`frontend-react/`) — Monica (Matthew API client)

**Status:** Demo-ready on mock; contract v1 client implemented; `npm run build` passes.

| Area | Finding |
|------|---------|
| Stack | Vite 8 + React 19 + TypeScript — separate `package.json`, no Python deps |
| Contract | Mirrors `frontend/services/contract_v1.py` — promote retry on 400/422, 409 handling |
| Mock data | `public/mock-candidates.json` — 17 rows exported from `frontend/mock_data.py` |
| Features | Table + card views, detail, confidence breakdown, contradictions, eval embed |
| Env | `VITE_PRAXIS_API_BASE_URL`, `VITE_PRAXIS_EVAL_METRICS_URL`, token + contract version |
| Docs | `frontend-react/README.md` — Matthew self-serve wire-up |

**Gaps:**

- No automated tests yet (TypeScript `tsc -b` only).
- Live API not exercised in CI (same blocker as Streamlit — no server in repo).

---

### 2. ML & Knowledge Pipeline (`knowledge/`) — Matthew

**Status:** Foundation scaffolding; not yet a production distillation pipeline or REST API.

| Module | Purpose | Maturity |
|--------|---------|----------|
| `knowledge_graph/` | `KnowledgeGraph` ABC + `InMemoryGraph` | Working — 6 unit tests |
| `injestion/` | `Ingestor` ABC + `PromptIngestor` | Skeleton — typo in package name (`injestion` vs `ingestion`) |
| `graph_reader/` | `WholeFileReader` → Claude tool shape | Working — 3 unit tests |
| `wiring.py` | `build_trio()` factory | Used by eval harness |
| `run.py` | Debugger entry — ingest smoke + eval runner | Dev utility |

**Missing vs MVP scope (README / project plan):**

- JSONL ingest + segment
- Learning-moment detection
- LLM distillation with provenance
- Cluster/dedup + confidence scoring
- **Candidate REST API** (`GET /candidates`, `POST /candidates/{id}/promote`, etc.)
- DynamoDB-backed KG (session logs → DynamoDB via `session-capture`, but KG is in-memory only)

**Tests:** 30 passing under `knowledge/` (graph, injestor, graph_reader, eval_def, eval run, poetry checks).

---

### 3. Architecture, Eval & Integration — Dominic

**Status:** Split across `knowledge/evals/`, `session-capture/`, `infra/`.

#### Eval harness (`knowledge/evals/`)

| Component | Status |
|-----------|--------|
| Case registry | 2 YAML cases (`example_add_function`, `iambic_poem`) |
| `FakeRunner` | Offline deterministic runs |
| `ClaudeCodeRunner` / `ClaudeCodeJudge` | Real Claude Code integration (credit-consuming) |
| Deterministic checks | `builds.py`, `poetry.py` |
| Baseline writer | `knowledge/evals/results/baseline.jsonl` (gitignored) |

Harness is runnable via `uv run python knowledge/run.py` or `python -m knowledge.evals.run`.

#### Session capture (`session-capture/`)

- Go `claude+` daemon: PTY host, session multiplexer, JSONL tailer, DynamoDB writer
- CDK stack provisions `praxis-sessions` table
- **Local blocker:** `go` not installed on audit machine — Go tests not executed
- Third-party `vt10x` vendored with upstream TODOs (acceptable for capstone scope)

#### Infra (`infra/`)

- `SessionsTableStack` — single DynamoDB table
- No API Gateway, Lambda, or candidate-service stack yet

**Missing vs plan:**

- Top-level `eval/` directory and quirky benchmark repo
- Eval metrics HTTP endpoint for dashboard (`PRAXIS_EVAL_METRICS_URL`)
- VCS-agnostic promotion replay automation
- Compounding-curve measurement on fixed benchmark tasks

---

## Integration contracts

Contracts are **well-defined** and **client-implemented**:

| Contract | Doc | Fixtures | Client | Server |
|----------|-----|----------|--------|--------|
| Candidate API v1 | `docs/integration/candidate-api-v1.md` | `docs/integration/fixtures/` | `frontend/services/api_client.py`, `contract_v1.py` | ❌ Not in repo |
| Eval metrics v1 | `docs/integration/eval-metrics-v1.md` | `eval-metrics.json` | `frontend/components/eval_metrics_embed.py` | ❌ Not in repo |
| Wire-up guide | `docs/integration/wire-up.md` | — | Self-serve checklist | — |

**Handshake quality:** Fixtures match client parsers (verified by contract tests). `X-Praxis-Contract: 1` header documented. Promote fallback (`{}` body on 400/422) implemented in client.

**Integration stop condition (Monica pillar):** Dashboard runs fully offline with mock provider when `PRAXIS_API_BASE_URL` is unset — ✅ satisfied.

---

## Test posture

### Results (2026-06-18)

| Suite | Command | Result |
|-------|---------|--------|
| Knowledge | `pytest knowledge -q` | **30 passed** |
| Frontend | `PYTHONPATH=frontend pytest frontend/tests -q` | **11 passed** |
| Root (default) | `pytest knowledge frontend/tests -q` | **2 collection errors** (frontend imports) |
| Go (session-capture) | `go test ./...` | **Not run** — Go toolchain absent |

### Root pytest failure (actionable)

`frontend/tests/` imports `from models.candidate import ...` expecting `frontend/` on `PYTHONPATH`. `pyproject.toml` sets `pythonpath = ["."]` only.

**Recommended fix:**

```toml
[tool.pytest.ini_options]
pythonpath = [".", "frontend"]
```

Or document that all contributors must run frontend tests from `frontend/` with `$env:PYTHONPATH = "frontend"` (wire-up.md currently says `"."` from `frontend/` dir — **incorrect** for imports).

### Coverage gaps

| Area | Tests | Gap |
|------|-------|-----|
| `ApiDataProvider` HTTP | None | No mock server / `responses` harness |
| Streamlit UI | None | Smoke tests hit provider only, not rendered UI |
| Session capture Go | Unknown | Requires Go install |
| End-to-end loop | None | No single test: ingest → candidate → promote → eval |

---

## CI/CD and quality gates

| Check | Status |
|-------|--------|
| GitLab CI (`.gitlab-ci.yml`) | ❌ Not present |
| Pre-commit hooks | ❌ Not configured |
| Type checking (mypy/pyright) | ❌ Not configured |
| Lint (ruff/eslint) | ❌ Not configured |
| Render deploy | ✅ `frontend/render.yaml` for portfolio mock |

`docs/monica/DAYS_9_10_REMAINING.md` lists optional GitLab CI for `frontend/tests/` when repo CI is live.

---

## Security & operational notes

| Item | Assessment |
|------|------------|
| API token handling | `PRAXIS_API_TOKEN` via env only — ✅ no hardcoded secrets found |
| `.gitignore` | Minimal — `knowledge/evals/results/*.jsonl`, `__pycache__` |
| `frontend/venv/` | Local venv present; not in `.gitignore` — risk of accidental commit |
| PII/secrets in logs | Not audited at content level; pipeline scrubbing not implemented |
| DynamoDB capture | Optional when AWS creds absent — graceful degradation documented |

---

## Documentation quality

| Document | Alignment with code |
|----------|---------------------|
| `README.md` | ⚠️ Layout section outdated (`pipeline/`, `eval/` paths) |
| `docs/monica/ARCHITECTURE_MONICA.md` | ✅ Accurate for dashboard; references future `pipeline/` |
| `docs/integration/*` | ✅ Matches client implementation |
| `docs/monica/DEMO_SCRIPT.md` | ✅ Actionable for mock rehearsal |
| `session-capture/README.md` | ✅ Matches Go layout |

---

## Prioritized recommendations

### P0 — Before Days 6–7 integration

1. **Matthew:** Implement candidate REST API per `candidate-api-v1.md` (can live in `knowledge/` or new `pipeline/` — pick one path and update README).
2. **Fix pytest pythonpath** in `pyproject.toml` so `pytest` from repo root passes all 41 tests.
3. **Update README layout** to reflect `knowledge/`, `session-capture/`, `infra/` until `pipeline/` and `eval/` exist.

### P1 — Before demo (Days 9–10)

4. **Dominic:** Stand up eval metrics GET endpoint per `eval-metrics-v1.md`.
5. **Add GitLab CI** — at minimum: `pytest` (knowledge + frontend), optional Go test job.
6. **Run live integration smoke** — `PRAXIS_API_BASE_URL` + promote/resolve against real server; document results in MR.
7. **Complete manual demo checklist** in `docs/monica/DAYS_9_10_REMAINING.md`.

### P2 — Polish / tech debt

8. Rename `knowledge/injestion/` → `ingestion/` when Matthew agrees (breaking import change).
9. Add `.gitignore` entries for `frontend/venv/`, `.venv/`, `node_modules/`.
10. ~~Scaffold or remove empty `frontend-react/` to avoid confusion.~~ **Done (2026-06-18)** — React dashboard shipped; see `frontend-react/README.md`.
11. Add HTTP-level tests for `ApiDataProvider` with a stub server.

---

## Quick reference — run commands

```powershell
# Sync dev branch (start of session)
git fetch origin main; git merge origin/main

# All Python tests (after pythonpath fix — today use frontend path)
$env:PYTHONPATH = "frontend"
.\.venv\Scripts\pytest knowledge frontend/tests -q

# Mock dashboard
cd frontend
Remove-Item Env:PRAXIS_API_BASE_URL -ErrorAction SilentlyContinue
.\venv\Scripts\streamlit run app.py

# Live API mode (when server exists)
$env:PRAXIS_API_BASE_URL = "http://localhost:8000"
.\venv\Scripts\streamlit run app.py

# Eval harness (offline)
$env:PRAXIS_EVAL_REAL = "0"
uv run python knowledge/run.py

# Session capture (requires Go + AWS)
cd infra; npm install; npm run deploy
cd ../session-capture/wrapper; go build -o claude+ ./cmd/claude-plus
```

---

## Appendix — file inventory

| Path | `.py` / `.go` / `.ts` files (excl. venv, third_party) |
|------|------------------------------------------------------|
| `frontend/` | 17 Python |
| `knowledge/` | 29 Python |
| `session-capture/wrapper/` | ~50 Go (incl. internal; excl. `third_party/vt10x`) |
| `infra/` | 2 TypeScript |
| `docs/integration/fixtures/` | 4 JSON fixtures |

**Total automated tests:** 41 Python (`def test_` across 9 test modules).

---

## Audit sign-off

| Field | Value |
|-------|-------|
| Branch audited | `monica/dashboard-human-gate` |
| Commits ahead of `origin/main` | Dev-branch-only work (not enumerated in this audit) |
| Next audit trigger | After Matthew candidate API lands, or before MR to `main` |
| Owner action | Monica: pytest pythonpath fix + README drift MR; Matthew/Dominic: P0 server endpoints |

*Generated as part of local dev session on 2026-06-18. Update this file when integration milestones land.*
