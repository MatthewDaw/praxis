# Integration Smoke - Monica Dashboard & Human Gate

Updated: 2026-06-25

Plan alignment: [PRAXIS_Project_Plan.html](../plans/PRAXIS_Project_Plan.html) names Monica as **Dashboard & Human Gate Lead**. Monica's deliverable is the React human-gate dashboard in `frontend-react/` plus the Python contract layer in `frontend/`: approval workflow, provenance display, confidence review, contradiction resolution UI, and dashboard evidence for the team demo.

Team boundaries from the plan:

| Owner | Pillar | Smoke responsibility |
|---|---|---|
| Matthew | ML & Knowledge Pipeline | Provides candidate API data, ingestion/distillation output, Knowledge Graph persistence, and provenance-bearing candidates. |
| Monica | Dashboard & Human Gate | Verifies the dashboard can review candidates, show evidence, promote human-approved knowledge, reject bad candidates, and resolve contradictions. |
| Dominic | Architecture, Eval & Integration | Provides eval/integration proof, replay metrics, hook/deploy architecture, and compounding-gain evidence. |

Monica's smoke should prove the dashboard gate works. It should not become a backend, database, ingestion, or eval-harness ownership checklist.

## Current Repo Assumptions

- Current dashboard lifecycle is `proposed -> active` plus `rejected`.
- `frontend-react/src/config/dataSource.ts` defaults local startup to Local Postgres at `http://localhost:8000`; mock fixtures exist, but the selected data source must be confirmed before rehearsal.
- `knowledge/serve/app.py` currently exposes `/candidates`, `/contradictions`, `/graph`, `/snapshots`, `/org/sources`, `/fold-in`, `/apikeys`, `/evals/*`, `/insights`, `/ingest`, and `/context`.
- Live server routes require Cognito/API-key auth unless local dev auth is bypassed with `PRAXIS_AUTH_DISABLED=1` on the server and `VITE_PRAXIS_AUTH_DISABLED=1` in React.
- Live mutation smoke must use disposable data only.

## 0. Preflight

From repo root:

```powershell
git status --short
```

Confirm:

- You are on the intended branch.
- You know whether the demo source is mock fixtures, local live API, or remote live API.
- You are not about to mutate a shared production-like store.

## 1. Offline Contract Gate

Run this before recording or demo rehearsal:

```powershell
uv run pytest frontend/tests/ -q
cd frontend-react
npm test
npm run lint
npm run build
```

Pass criteria:

- Python frontend contract tests pass.
- Vitest dashboard tests pass.
- Typecheck/lint pass.
- Production build succeeds.

If this fails in `frontend/` or `frontend-react/`, Monica owns the fix. If the failure is in `knowledge/` or `knowledge/evals/`, tag Matthew or Dominic before changing cross-pillar code.

## 2. Monica 2-Minute Demo Smoke

This is the primary dashboard rehearsal path. Keep it to about 2 minutes so Matthew and Dominic have time for their pillars.

```powershell
cd frontend-react
npm run dev
```

Open `http://localhost:5173`.

Before timing:

- Confirm data source banner/control state.
- Use mock fixtures for the most stable portfolio demo, or a disposable local API store if the team wants live integration.
- Do not spend time explaining Matthew's pipeline internals or Dominic's eval harness.

| Time | Action | Expected proof |
|---|---|---|
| 0:00-0:15 | Show dashboard and source state | Audience sees this is the human approval surface. |
| 0:15-0:40 | Select `cand_1` and open detail | Provenance, confidence, and audit trail are visible. |
| 0:40-1:10 | Promote `cand_1` | Candidate moves `proposed -> active` only after human confirmation. |
| 1:10-1:35 | Resolve `cand_9` / `cand_16` contradiction | Rival candidate becomes rejected or exits active review queue; contradiction state updates. |
| 1:35-2:00 | Handoff | Monica states that Matthew owns candidate generation/KG persistence and Dominic owns eval/integration proof. |

Pass criteria:

- Segment finishes in 2 minutes or less.
- Promotion and contradiction actions are visible.
- Dashboard language stays on Monica's pillar: provenance, confidence, approval, contradiction resolution.
- Handoff explicitly leaves pipeline and eval proof to Matthew and Dominic.

## 3. Live Candidate API Smoke

Run only when Matthew's API target is available and safe to mutate.

Start local server with dev auth bypass when doing local smoke:

```powershell
$env:PRAXIS_AUTH_DISABLED = "1"
uvicorn knowledge.serve.app:app --host 127.0.0.1 --port 8000
```

Create or update `frontend-react/.env.local`:

```env
VITE_PRAXIS_API_BASE_URL=http://localhost:8000
VITE_PRAXIS_API_TOKEN=
VITE_PRAXIS_CONTRACT_VERSION=1
VITE_PRAXIS_AUTH_DISABLED=1
```

Then:

```powershell
cd frontend-react
npm test
npm run dev
```

Manual live checks:

| Step | Action | Expected |
|---|---|---|
| 1 | Confirm live source | Banner/control shows local API URL, not an accidental source. |
| 2 | Load candidates | `GET /candidates` returns rows with provenance-compatible shape. |
| 3 | Promote disposable proposed row | `POST /candidates/{id}/promote` returns or refreshes to `active`. |
| 4 | Refresh one candidate | Updated row reflects server state without full data reload. |
| 5 | Reject disposable row | `POST /candidates/{id}/reject` marks row `rejected`. |
| 6 | Resolve disposable contradiction | `POST /contradictions/{id}/resolve` updates kept/rival state. |

Pass criteria:

- Dashboard handles API success and recoverable errors clearly.
- Mutations persist after refresh.
- Monica does not edit backend persistence or pipeline code to force this smoke green.

## 4. Eval / Integration Visibility Smoke

This is a UI visibility check for Dominic's pillar, not a Monica-owned eval proof.

Use when an eval endpoint or fixture-backed eval panel is available:

```powershell
cd frontend-react
npm run dev
```

Check:

- Eval/evidence panel renders without layout break.
- Empty/unavailable state is clear if Dominic's endpoint is not configured.
- Monica can point to the eval area during handoff without claiming ownership of the metrics.

Pass criteria:

- Dashboard does not block the demo when eval data is unavailable.
- Any missing eval proof is recorded as Dominic-owned follow-up, not a Monica blocker.

## 5. Render / Hosted Smoke

Use after the team decides which branch and deploy target are authoritative.

Reference: [RENDER_DEPLOY.md](RENDER_DEPLOY.md).

Hosted pass criteria:

- Render branch/repo values match the current GitHub repo and intended deploy branch.
- React static site loads.
- API URL/auth variables are configured for live deploy, or the app clearly uses an intentional mock/demo source.
- `GET /candidates` loads candidate rows.
- Promote/reject/resolve actions work against disposable data.
- Eval/evidence panel renders or shows a clear unavailable state.

Screenshots to capture under `docs/monica/screenshots/`:

- Data-source banner/control state.
- Candidate detail with provenance/confidence.
- Promote confirmation or post-promote active state.
- Contradiction before/after.
- Eval/evidence panel or unavailable state.

## 6. Team Integration Smoke - Optional

These are not Monica's offline gate and should not block her 2-minute dashboard demo.

Run only when the team wants an end-to-end proof:

```powershell
$env:PRAXIS_API_BASE_URL = "http://127.0.0.1:8000"
$env:PRAXIS_INGEST_SMOKE = "1"
$env:PYTHONPATH = "frontend"
uv run pytest frontend/tests/test_ingest_promote_smoke.py -v
```

Expected ownership:

| Area | Owner | Monica action |
|---|---|---|
| `/ingest` and candidate creation | Matthew | Verify resulting candidates are reviewable in UI. |
| Promote to graph/store behavior | Matthew | Verify dashboard reflects the mutation. |
| Eval replay / compounding curve | Dominic | Verify dashboard can show or link the evidence. |
| Human approval UX | Monica | Verify provenance, confidence, promote, reject, contradiction flow. |

## Reporting Template

Use this after each smoke run:

```text
Monica dashboard smoke:
- Source: mock fixtures / local API / hosted API
- Offline gate: pass/fail/not run
- 2-minute demo path: pass/fail, duration
- Candidate review: pass/fail
- Promote proposed -> active: pass/fail
- Reject behavior: pass/fail
- Contradiction resolution: pass/fail
- Eval/evidence panel: pass/fail/unavailable
- Cross-pillar blockers:
  - Matthew:
  - Dominic:
```

## Related

| Doc | Purpose |
|---|---|
| [PRAXIS_Project_Plan.html](../plans/PRAXIS_Project_Plan.html) | Team pillar ownership and demo plan |
| [wire-up.md](../integration/wire-up.md) | Full local wire-up commands |
| [candidate-api-v1.md](../integration/candidate-api-v1.md) | Dashboard/API contract |
| [DEMO_SCRIPT.md](DEMO_SCRIPT.md) | Monica spoken beats |
| [REHEARSAL_LOG.md](REHEARSAL_LOG.md) | 2-minute Monica rehearsal plan |
| [RENDER_DEPLOY.md](RENDER_DEPLOY.md) | Hosted dashboard/API deploy notes |
