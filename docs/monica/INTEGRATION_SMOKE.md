# Integration smoke — Dashboard pillar (Monica)

Self-serve validation when Matthew's candidate API and Dominic's eval metrics are available. No pairing call required — follow [wire-up.md](../integration/wire-up.md).

**Status:** Both clients ready on `monica/dashboard-human-gate`. Server endpoints not in repo yet (2026-06-19). Use mock path for demo until smoke below passes.

**Primary demo client:** React (`frontend-react/`) — static Render deploy, custom branding, a11y labels.  
**Reference client:** Streamlit (`frontend/`) — Python contract tests and Matthew wire-up parity.

---

## Prerequisites

```powershell
cd frontend-react
npm install
```

Streamlit (reference / pytest):

```powershell
cd frontend
.\venv\Scripts\pip install -e ..
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
```

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
VITE_PRAXIS_EVAL_METRICS_URL=http://localhost:9000/metrics
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

## 4. Streamlit mock smoke (reference client)

```powershell
cd frontend
Remove-Item Env:PRAXIS_API_BASE_URL -ErrorAction SilentlyContinue
.\venv\Scripts\streamlit run app.py
```

Repeat Act 2 steps — useful for Matthew's Python client validation and pytest parity.

---

## 5. Streamlit live API smoke (reference client)

```powershell
$env:PRAXIS_API_BASE_URL = "http://localhost:8000"
$env:PRAXIS_API_TOKEN = ""   # optional
$env:PYTHONPATH = "frontend"
uv run pytest frontend/tests/test_contract_fixtures.py -v
cd frontend
.\venv\Scripts\streamlit run app.py
```

Same pass criteria as §3; use sidebar **Refresh data** after mutations.

---

## 6. Eval metrics live smoke (when Dominic's endpoint is up)

**React (primary):**

```powershell
# frontend-react/.env.local
# VITE_PRAXIS_EVAL_METRICS_URL=http://localhost:9000/metrics
cd frontend-react
npm run dev
```

**Streamlit (reference):**

```powershell
$env:PRAXIS_EVAL_METRICS_URL = "http://localhost:9000/metrics"
cd frontend
.\venv\Scripts\streamlit run app.py
```

**Pass criteria:** Live chart + cold/after/reduction metrics per [eval-metrics-v1.md](../integration/eval-metrics-v1.md) and [eval-metrics.json](../integration/fixtures/eval-metrics.json).

---

## 7. Automated rehearsal gate (CI-friendly)

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

## Related

| Doc | Purpose |
|-----|---------|
| [wire-up.md](../integration/wire-up.md) | Full wire-up commands |
| [candidate-api-v1.md](../integration/candidate-api-v1.md) | API contract |
| [DEMO_SCRIPT.md](DEMO_SCRIPT.md) | Act 2 spoken beats |
| [RENDER_DEPLOY.md](RENDER_DEPLOY.md) | Portfolio mock deploy |
| [DAYS_9_10_REMAINING.md](DAYS_9_10_REMAINING.md) | Manual rehearsal + video checklist |
