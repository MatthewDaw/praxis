# Integration smoke — Dashboard pillar (Monica)

Self-serve validation when Matthew's candidate API and Dominic's eval metrics are available. No pairing call required — follow [wire-up.md](../integration/wire-up.md).

**Status:** Client ready on `monica/dashboard-human-gate`. Server endpoints not in repo yet (2026-06-19). Use mock path for demo until smoke below passes.

---

## Prerequisites

```powershell
cd frontend
.\venv\Scripts\pip install -e ..
```

React (optional second client):

```powershell
cd frontend-react
npm install
```

---

## 1. Contract tests (offline — run anytime)

From repo root:

```powershell
uv run pytest frontend/tests/test_contract_fixtures.py frontend/tests/test_mock_gate_workflow.py -v
```

**Pass criteria:** All tests green (contract payloads + mock gate workflow including reject, promote chain, contradiction resolve).

---

## 2. Streamlit mock smoke (demo path)

```powershell
cd frontend
Remove-Item Env:PRAXIS_API_BASE_URL -ErrorAction SilentlyContinue
.\venv\Scripts\streamlit run app.py
```

Rehearse Act 2 per [DEMO_SCRIPT.md](DEMO_SCRIPT.md):

| Step | Action | Expected |
|------|--------|----------|
| 1 | Filter **suggested** | List narrows; provenance visible on rows |
| 2 | Select **cand_2** or **cand_1** | Detail shows confidence breakdown + audit trail |
| 3 | Promote **cand_1** | proposed → suggested; success banner |
| 4 | Open **cand_9** contradictions | Side-by-side with **cand_16** |
| 5 | Keep **cand_9** | Rival removed; contradiction IDs cleared |
| 6 | Expand eval metrics embed | Placeholder curve + scoreboard (no `PRAXIS_EVAL_METRICS_URL`) |

**Screenshot checklist (add to `docs/monica/screenshots/` when capturing):**

- [ ] Mock mode banner
- [ ] Candidate list with provenance column
- [ ] Detail panel confidence breakdown
- [ ] Contradiction resolution (before/after)
- [ ] Eval metrics embed expanded

---

## 3. Streamlit live API smoke (when Matthew's server is up)

```powershell
$env:PRAXIS_API_BASE_URL = "http://localhost:8000"
$env:PRAXIS_API_TOKEN = ""   # optional
$env:PYTHONPATH = "frontend"
uv run pytest frontend/tests/test_contract_fixtures.py -v
cd frontend
.\venv\Scripts\streamlit run app.py
```

| Step | Action | Expected |
|------|--------|----------|
| 1 | Confirm live mode banner shows API URL | Not mock banner |
| 2 | List loads from `GET /candidates` | Same shape as [fixtures/candidates-list.json](../integration/fixtures/candidates-list.json) |
| 3 | Promote a proposed candidate | `POST /candidates/{id}/promote` returns updated state |
| 4 | Sidebar **Refresh data** after mutation | List reflects server state |
| 5 | Promote same row again (409) | Error message; refresh recovers |

**Troubleshooting:** See [wire-up.md](../integration/wire-up.md) table (400/422, 409, empty list).

---

## 4. React live API smoke

```powershell
# frontend-react/.env.local
# VITE_PRAXIS_API_BASE_URL=http://localhost:8000
cd frontend-react
npm run dev
```

Repeat Act 2 steps at http://localhost:5173. Verify:

- Reject reason field sends optional body to API
- Low-confidence promote warning (&lt;50%) appears on confirm step
- Card view promote/reject matches table behavior
- Defer contradiction shows info banner (no mutation)

---

## 5. Eval metrics live smoke (when Dominic's endpoint is up)

```powershell
$env:PRAXIS_EVAL_METRICS_URL = "http://localhost:9000/metrics"
cd frontend
.\venv\Scripts\streamlit run app.py
```

React: `VITE_PRAXIS_EVAL_METRICS_URL=http://localhost:9000/metrics`

**Pass criteria:** Live chart + cold/after/reduction metrics per [eval-metrics-v1.md](../integration/eval-metrics-v1.md) and [eval-metrics.json](../integration/fixtures/eval-metrics.json).

---

## 6. Automated rehearsal gate (CI-friendly)

Run before Practice 1 (Wed Jun 25):

```powershell
uv run pytest knowledge/evals/tests/test_cases.py frontend/tests/ -q
cd frontend-react; npm run lint; npm run build
```

All green = code path ready for timed Act 2 rehearsal.

---

## Related

| Doc | Purpose |
|-----|---------|
| [wire-up.md](../integration/wire-up.md) | Full wire-up commands |
| [candidate-api-v1.md](../integration/candidate-api-v1.md) | API contract |
| [DEMO_SCRIPT.md](DEMO_SCRIPT.md) | Act 2 spoken beats |
| [RENDER_DEPLOY.md](RENDER_DEPLOY.md) | Portfolio mock deploy |
