# Dashboard wire-up — self-serve (no pairing)

Validate Matthew's candidate API and Dominic's eval metrics without a sync call.

## Prerequisites

```powershell
uv sync
```

## 1. Contract tests (offline)

Confirms fixtures and payload builders match canonical v1. From **repo root**:

```powershell
$env:PYTHONPATH = "frontend"
uv run pytest frontend/tests/test_contract_fixtures.py -v
```

## 2. Mock dashboard (no backend)

```powershell
cd frontend-react
npm install
npm run dev
```

Rehearse Act 2 per [`../monica/DEMO_SCRIPT.md`](../monica/DEMO_SCRIPT.md):

1. Filter **suggested** candidates
2. Inspect provenance on **cand_2**
3. Promote **cand_1** proposed → suggested
4. Resolve contradiction **cand_9** ↔ **cand_16**

## 3. Live API (when Matthew's server is up)

```powershell
$env:PRAXIS_API_BASE_URL = "http://localhost:8000"
$env:PRAXIS_API_TOKEN = ""   # optional
$env:PYTHONPATH = "frontend"
uv run pytest frontend/tests/test_contract_fixtures.py frontend/tests/test_live_api_smoke.py -v
```

React dashboard with live API:

```powershell
# frontend-react/.env.local
# VITE_PRAXIS_API_BASE_URL=http://localhost:8000
cd frontend-react
npm run dev
```

Use **Refresh** in the dashboard after mutations if the list looks stale.

## 4. Eval metrics (when Dominic's endpoint is up)

```powershell
# frontend-react/.env.local
# VITE_PRAXIS_EVAL_METRICS_URL=http://localhost:9000/metrics
cd frontend-react
npm run dev
```

Expand **Eval metrics — compounding curve** and confirm live chart + before/after scoreboard.

## 5. Render deploy (mock-only, portfolio)

No `VITE_PRAXIS_API_BASE_URL` in Render env vars. See [`../monica/RENDER_DEPLOY.md`](../monica/RENDER_DEPLOY.md).

## 6. Postgres-backed API setup (Matthew)

Stand up RDS PostgreSQL 16 + pgvector, bootstrap schema, and configure the candidate API to use `PostgresCandidateStore` instead of the JSON file store: [`../monica/RDS_KG_DEPLOY.md`](../monica/RDS_KG_DEPLOY.md).

Requires AWS CLI credentials to pull DB secrets from Secrets Manager (or set `PRAXIS_DB_URL` explicitly).

## 7. Integration smoke checklist

Full pass/fail tables for mock + live API + eval metrics: [`../monica/INTEGRATION_SMOKE.md`](../monica/INTEGRATION_SMOKE.md).

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Promote 400/422 | Server may expect `{}` only — client retries automatically |
| Promote 409 | Refresh data; another reviewer changed state |
| Empty list | `GET /candidates` must return array or `{ "candidates": [] }` |
| Field missing in UI | Compare response to [`fixtures/candidates-list.json`](fixtures/candidates-list.json) |
