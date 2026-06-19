# PRAXIS React — Knowledge Graph Dashboard

**For:** Matthew Daw (ML & Knowledge Pipeline) — server owner of [candidate-api-v1](../docs/integration/candidate-api-v1.md)  
**From:** Monica Peters — parallel React client to the Streamlit dashboard in `frontend/`

This is the **React review dashboard** described in [PRAXIS_Project_Plan.html](../docs/PRAXIS_Project_Plan.html): human approval workflow (`proposed → suggested → active`), provenance on every candidate, confidence breakdown, contradiction resolution, and Dominic's compounding-curve embed.

It targets the **same REST contract** as `frontend/services/api_client.py` so Matthew can validate his FastAPI (or other) server without pairing sessions.

---

## Quick start (mock mode — no backend)

```powershell
cd frontend-react
npm install
npm run dev
```

Open http://localhost:5173 — loads 17 mock candidates from `public/mock-candidates.json` (exported from `frontend/mock_data.py`).

**Demo rehearsal (Act 2):**

1. Filter **suggested** candidates
2. Inspect provenance on **cand_2**
3. Promote **cand_1** proposed → suggested
4. Resolve contradiction **cand_9** ↔ **cand_16**

---

## Live API (Matthew's server)

Create `.env.local`:

```env
VITE_PRAXIS_API_BASE_URL=http://localhost:8000
VITE_PRAXIS_API_TOKEN=
VITE_PRAXIS_CONTRACT_VERSION=1
VITE_PRAXIS_EVAL_METRICS_URL=http://localhost:9000/metrics
```

Then:

```powershell
npm run dev
```

| UI action | HTTP |
|-----------|------|
| List | `GET /candidates` |
| Promote | `POST /candidates/{id}/promote` with `{ "targetState": "suggested" \| "active" }` |
| Reject | `POST /candidates/{id}/reject` |
| Resolve | `POST /contradictions/{primary}__{rival}/resolve` |

Client retries promote with `{}` if the server returns 400/422 on explicit `targetState` (same as Streamlit client).

---

## Project layout

```text
frontend-react/
├── public/mock-candidates.json   # Same fixtures as Streamlit mock_data.py
├── src/
│   ├── api/                      # contract v1 client + mock provider
│   ├── components/               # list, detail, contradictions, eval embed
│   ├── hooks/useCandidates.ts
│   └── types/candidate.ts
└── vite.config.ts
```

---

## Build

```powershell
npm run build
npm run preview
```

Static output in `dist/` — deploy beside Matthew's API or serve from any static host.

---

## Related docs

- [candidate-api-v1.md](../docs/integration/candidate-api-v1.md) — Matthew ↔ dashboard contract
- [wire-up.md](../docs/integration/wire-up.md) — Streamlit self-serve validation (same endpoints)
- [Matthew-Daw-ML-Pipeline-PlanDRAFT.md](../docs/Matthew-Daw-ML-Pipeline-PlanDRAFT.md) — pipeline pillar plan
- [docs/matt/future-work/](../docs/matt/future-work/) — post-MVP knowledge-graph eval design (measurement spine)

---

## Notes for Matthew

- **You own the server** — this app only consumes your API; it does not import `knowledge/` Python modules.
- **Provenance is mandatory** — every candidate must include `logs/<file>.jsonl:<line>` for audit storytelling.
- **Extend safely** — unknown JSON fields are preserved in `Candidate.extra` and shown in the detail panel.
- The in-memory knowledge graph in `knowledge/knowledge_graph/` is separate from this UI; wire promoted `active` candidates into your graph store when the API is live.
