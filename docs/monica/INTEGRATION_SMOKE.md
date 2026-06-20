# Integration smoke — Dashboard pillar (Monica)

Self-serve validation when Matthew's candidate API and Dominic's eval metrics are available. No pairing call required — follow [wire-up.md](../integration/wire-up.md).

Live smoke assumes Matthew's API is backed by **PostgreSQL** (Matthew owns `PRAXIS_DB_URL` / Secrets Manager and schema — see [RDS_KG_DEPLOY.md](RDS_KG_DEPLOY.md)); dashboard env vars remain API-only (`PRAXIS_API_BASE_URL`, not a DB connection string).

**Status:** Live candidate API at `knowledge/serve` (mock-seeded store + `/metrics` stub). React client aligned on Matthew API v1 (reject → decayed, promote 400 conflict UX). **Local smoke (2026-06-19):** `test_live_api_smoke.py` list ✅ against `127.0.0.1:8000`; promote/reject/resolve skipped when store has no spare proposed/contradiction rows. Render dual-service checklist in §8 — tick after deploy.

**Demo client:** React (`frontend-react/`) — static Render deploy, custom branding, a11y labels.

**Contract layer:** Python (`frontend/`) — typed models, mock fixtures, pytest, and live API smoke tests (no UI runtime).

---

## Prerequisites

```powershell
cd frontend-react
npm install
```

Python contract tests (from repo root):

```powershell
uv run pytest frontend/tests/ -q
```

---

## 1. Contract tests (offline — run anytime)

From repo root:

```powershell
uv run pytest frontend/tests/test_contract_fixtures.py frontend/tests/test_mock_gate_workflow.py -v
cd frontend-react
npm test
```

**Pass criteria:** All pytest + Vitest green (14 tests each pillar — contract payloads + mock gate workflow including reject, promote chain, contradiction resolve).

**Mock data sync** (after editing `frontend/mock_data.py`):

```powershell
python scripts/export-mock-candidates.py
uv run pytest frontend/tests/test_mock_data_contract.py -q
```

Re-exports [`frontend-react/public/mock-candidates.json`](../../frontend-react/public/mock-candidates.json) — Matthew's API seeds from this file when Postgres/JSON store is empty.

---

## 2. React mock smoke (primary demo path)

```powershell
cd frontend-react
# Ensure VITE_PRAXIS_API_BASE_URL is unset in .env.local
npm run dev
```

Open http://localhost:5173 and rehearse Act 2 per [DEMO_SCRIPT.md](DEMO_SCRIPT.md):

| Step | Action | Expected |
|------|--------|----------|
| 1 | Filter **suggested** | List narrows; provenance visible on rows |
| 2 | Select **cand_2** or **cand_1** | Detail shows confidence breakdown + audit trail |
| 3 | Promote **cand_1** | Confirm dialog shows `proposed → suggested`; success banner |
| 4 | Open **cand_9** contradictions | Side-by-side with **cand_16** |
| 5 | Keep **cand_9** | Rival decayed; contradiction IDs cleared |
| 6 | Expand eval metrics embed | Placeholder curve + scoreboard (no `VITE_PRAXIS_EVAL_METRICS_URL`) |
| 7 | Inspect **cand_18** | pathlib eval-aligned lesson with provenance `logs/session_20260616.jsonl:201` |

**Screenshot checklist (add to `docs/monica/screenshots/` when capturing):**

- [ ] Mock mode banner
- [ ] Candidate list with provenance column
- [ ] Detail panel confidence breakdown
- [ ] Promote confirmation (`proposed → suggested` copy)
- [ ] Contradiction resolution (before/after)
- [ ] Eval metrics embed expanded

---

## 3. React live API smoke (when Matthew's server is up)

Create `frontend-react/.env.local`:

```env
VITE_PRAXIS_API_BASE_URL=http://localhost:8000
VITE_PRAXIS_API_TOKEN=
VITE_PRAXIS_CONTRACT_VERSION=1
VITE_PRAXIS_EVAL_METRICS_URL=http://localhost:8000/metrics
```

```powershell
cd frontend-react
npm test
npm run dev
```

| Step | Action | Expected |
|------|--------|----------|
| 1 | Confirm live mode banner shows API URL | Not mock banner |
| 2 | List loads from `GET /candidates` | Same shape as [fixtures/candidates-list.json](../integration/fixtures/candidates-list.json) |
| 3 | Promote a proposed candidate | `POST /candidates/{id}/promote` returns updated state |
| 4 | **Refresh data** after mutation | List reflects server state |
| 5 | Promote same row again (409) | Error message; refresh recovers |
| 6 | Reject with optional reason | `POST /candidates/{id}/reject` with body |
| 7 | Low-confidence promote (&lt;50%) | Warning on confirm step |

Also verify:

- Card view promote/reject matches table behavior
- Defer contradiction shows info banner (no mutation)

**Troubleshooting:** See [wire-up.md](../integration/wire-up.md) table (400/422, 409, empty list).

---

## 4. Eval metrics live smoke (when Dominic's endpoint is up)

```powershell
# frontend-react/.env.local
# VITE_PRAXIS_EVAL_METRICS_URL=http://localhost:8000/metrics
cd frontend-react
npm run dev
```

**Pass criteria:** Live chart + cold/after/reduction metrics per [eval-metrics-v1.md](../integration/eval-metrics-v1.md) and [eval-metrics.json](../integration/fixtures/eval-metrics.json).

---

## 5. Automated rehearsal gate (CI-friendly)

Run before Practice 1 (Wed Jun 25):

```powershell
uv run pytest knowledge/evals/tests/test_cases.py frontend/tests/ -q
cd frontend-react
npm test
npm run lint
npm run build
```

All green = code path ready for timed Act 2 rehearsal.

---

## 6. Render dual-service smoke (live API + React static)

Blueprint: [`frontend-react/render.yaml`](../../frontend-react/render.yaml) — `praxis-candidate-api` + `praxis-react-human-gate` with `VITE_PRAXIS_API_BASE_URL` wired via `fromService`.

**Local parity check** (before or after Render deploy):

```powershell
# Terminal 1 — Matthew API
uvicorn knowledge.serve.app:app --host 127.0.0.1 --port 8000

# Terminal 2 — automated client smoke
$env:PRAXIS_API_BASE_URL = "http://127.0.0.1:8000"
$env:PYTHONPATH = "frontend"
uv run pytest frontend/tests/test_live_api_smoke.py -v

# Terminal 3 — React against local API
cd frontend-react
# .env.local: VITE_PRAXIS_API_BASE_URL=http://127.0.0.1:8000
npm run dev
```

**Render pass criteria** (tick when verified on deployed URLs):

- [ ] React shows live API badge (not mock fixtures banner)
- [ ] `GET /candidates` loads seeded mock rows with provenance
- [ ] Promote persists after browser refresh
- [ ] Reject sets **decayed** (row remains, badge updates)
- [ ] Resolve **cand_9** ↔ **cand_16** decays rival
- [ ] Eval embed loads from `{API_URL}/metrics` (Matthew fixture until Dominic ships)

**Screenshot checklist** (add to `docs/monica/screenshots/` when capturing on Render):

- [ ] Live API mode banner with Render API URL
- [ ] Post-promote refresh showing updated state
- [ ] Eval metrics from `/metrics` endpoint

---

## 7. Ingest + promote→graph smoke (opt-in, non-blocking)

These tests are **not** part of the offline merge gate. They skip when endpoints are missing or the store has no spare rows.

**Contract fixtures (offline — always run):**

```powershell
uv run pytest frontend/tests/test_contract_fixtures.py -q
cd frontend-react
npm test -- src/api/ingestClient.test.ts src/api/contractFixtures.test.ts
```

Fixtures: [`ingest-jsonl-request.json`](../integration/fixtures/ingest-jsonl-request.json), [`ingest-jsonl-response.json`](../integration/fixtures/ingest-jsonl-response.json) (proposed response shape for Matthew review).

**Live ingest smoke (manual opt-in):**

```powershell
# Terminal 1 — Matthew API
uvicorn knowledge.serve.app:app --host 127.0.0.1 --port 8000

# Terminal 2 — opt-in smoke (skips on 404 until POST /ingest/jsonl lands)
$env:PRAXIS_API_BASE_URL = "http://127.0.0.1:8000"
$env:PRAXIS_INGEST_SMOKE = "1"
$env:PYTHONPATH = "frontend"
uv run pytest frontend/tests/test_ingest_promote_smoke.py -v
```

| Step | Action | Expected |
|------|--------|----------|
| 1 | Run contract fixture tests | All green offline |
| 2 | Set `PRAXIS_INGEST_SMOKE=1` + live API URL | Tests run (not skipped for missing env) |
| 3 | `POST /ingest/jsonl` with fixture payload | **Skip** if 404/405; **200** when Matthew ships endpoint |
| 4 | Promote suggested → active | **Skip** if no spare rows; **200** when store has candidates |
| 5 | Re-run eval case `promote_then_rerun` with `--fake` | Injected arm passes (simulates post-promote graph) |

**Pass criteria:** Offline contract tests always green. Live steps skip gracefully until Matthew wires `POST /ingest/jsonl` and promote→graph. Never add these live tests to CI merge gate without team agreement.

---

## Related

| Doc | Purpose |
|-----|---------|
| [wire-up.md](../integration/wire-up.md) | Full wire-up commands |
| [candidate-api-v1.md](../integration/candidate-api-v1.md) | API contract |
| [DEMO_SCRIPT.md](DEMO_SCRIPT.md) | Act 2 spoken beats |
| [RENDER_DEPLOY.md](RENDER_DEPLOY.md) | Portfolio mock deploy |
| [DAYS_9_10_REMAINING.md](DAYS_9_10_REMAINING.md) | Manual rehearsal + video checklist |
