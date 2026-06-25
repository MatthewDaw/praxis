# PRAXIS React — Knowledge Graph Dashboard

**For:** Matthew Daw (ML & Knowledge Pipeline) — server owner of [candidate-api-v1](../docs/integration/candidate-api-v1.md)  
**From:** Monica Peters — human-gate dashboard UI for the PRAXIS knowledge loop

This is the **React review dashboard** described in [PRAXIS_Project_Plan.html](../docs/PRAXIS_Project_Plan.html): human approval workflow (`proposed → active`), provenance on every candidate, confidence breakdown, contradiction resolution, and Dominic's compounding-curve embed.

It targets the **same REST contract** as `frontend/services/api_client.py` (Python reference client) so Matthew can validate his FastAPI server without pairing sessions.

---

## Quick start (mock mode — no backend)

```powershell
cd frontend-react
npm install
npm run dev
```

Open http://localhost:5173 — loads mock candidates from `public/mock-candidates.json` (exported from `frontend/mock_data.py`).

**Sync mock JSON after editing Python fixtures:**

```powershell
# from repo root
python scripts/export-mock-candidates.py
```

Exports both `public/mock-candidates.json` and `public/mock-graph.json` from [`frontend/mock_data.py`](../frontend/mock_data.py).

**Demo rehearsal (Act 2):**

1. Filter **active** candidates
2. Inspect provenance on **cand_2**
3. Promote **cand_1** proposed → active
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
| Promote | `POST /candidates/{id}/promote` with `{ "targetState": "active" }` |
| Reject | `POST /candidates/{id}/reject` |
| Resolve | `POST /contradictions/{primary}__{rival}/resolve` |

Client retries promote with `{}` if the server returns 400/422 on explicit `targetState` (same as the Python reference client).

---

## Graph view (mock + stubbed `GET /graph`)

The **Graph** tab visualizes knowledge topology: candidate nodes, contradiction/support edges, a lifecycle funnel, and a scope tree. Mock mode loads fixtures from:

| File | Purpose |
|------|---------|
| `public/mock-candidates.json` | Candidate list (human gate) |
| `public/mock-graph.json` | Nodes, edges, `scopeGroups` for graph layout |

**Proposed server endpoint (Matthew — not required for this UI):**

```http
GET /graph
```

Response shape mirrors `mock-graph.json`:

```json
{
  "nodes": [
    { "id": "cand_1", "label": "...", "state": "proposed", "confidence": 0.85, "scope": "frontend/typescript", "category": "pattern" }
  ],
  "edges": [
    { "src": "cand_9", "dst": "cand_16", "kind": "contradiction" },
    { "src": "cand_1", "dst": "cand_2", "kind": "support" }
  ],
  "scopeGroups": [
    { "id": "frontend", "label": "Frontend", "parentId": null, "memberIds": [] },
    { "id": "react", "label": "React", "parentId": "frontend", "memberIds": ["cand_2", "cand_5"] }
  ]
}
```

Edge `kind` values: `contradiction`, `support`, `similarity`. Persistence target for edges: [`migrations/0000_initial.sql`](../migrations/0000_initial.sql) `fact_edges` table.

**Live API fallback:** If `GET /graph` is missing (404) or fails, the client derives a minimal graph from `GET /candidates` (nodes + contradiction edges only). Support/similarity edges appear when the server or mock fixture supplies them.

Promote, reject, and resolve actions update the mock graph in-memory so the Graph tab stays aligned with table/card views.

---

## Local Claude logs (browser upload + stub ingest)

The **Local Claude logs** data source lets reviewers upload `.jsonl` session files in the browser without touching Matthew's distillation pipeline:

| Step | Behavior |
|------|----------|
| Upload | Pick one or more `.jsonl` files (e.g. from `~/.claude/projects/…`) |
| Transcript | Session transcript panel with search and kind filters |
| Candidates | Heuristic preview candidates (`heuristicDistiller.ts`) — labeled as **not** pipeline distillation |
| Human gate | Promote / reject / graph views work on heuristic candidates in-memory |

Files stay in memory until the tab closes — raw JSONL is **not** stored in `localStorage`.

**Optional distillation (Matthew — not required for this UI):**

```http
POST /ingest/jsonl
Content-Type: application/json
```

Request body mirrors the browser upload payload:

```json
{
  "files": [
    { "name": "session.jsonl", "content": "{\"type\":\"user\",...}\n" }
  ]
}
```

The **Send to API for distillation** button is enabled when `VITE_PRAXIS_API_BASE_URL` is set at build time (or defaults to `http://localhost:8000`). On 404/405 the client shows *"Distillation endpoint not available yet"* until Matthew ships the endpoint.

---

## Project layout

```text
frontend-react/
├── public/mock-candidates.json   # Exported from frontend/mock_data.py
├── src/
│   ├── api/                      # contract v1 client + mock provider
│   ├── components/               # list, detail, contradictions, eval embed
│   ├── hooks/useCandidates.ts
│   └── types/candidate.ts
└── vite.config.ts
```

---

## Build & test

```powershell
npm test          # Vitest — contract fixtures + mock gate workflow
npm run lint
npm run build
npm run preview
```

Static output in `dist/` — deploy beside Matthew's API or serve from any static host.

**Render deploy (portfolio mock demo):** [docs/monica/RENDER_DEPLOY.md](../docs/monica/RENDER_DEPLOY.md) — blueprint at [`render.yaml`](render.yaml).

---

## Related docs

- [candidate-api-v1.md](../docs/integration/candidate-api-v1.md) — Matthew ↔ dashboard contract
- [wire-up.md](../docs/integration/wire-up.md) — self-serve validation
- [INTEGRATION_SMOKE.md](../docs/monica/INTEGRATION_SMOKE.md) — smoke checklist
- [Matthew-Daw-ML-Pipeline-PlanDRAFT.md](../docs/Matthew-Daw-ML-Pipeline-PlanDRAFT.md) — pipeline pillar plan
- [docs/matt/future-work/](../docs/matt/future-work/) — post-MVP knowledge-graph eval design (measurement spine)

---

## Notes for Matthew

- **You own the server** — this app only consumes your API; it does not import `knowledge/` Python modules.
- **Provenance is mandatory** — every candidate must include `logs/<file>.jsonl:<line>` for audit storytelling.
- **Extend safely** — unknown JSON fields are preserved in `Candidate.extra` and shown in the detail panel.
- The in-memory knowledge graph in `knowledge/knowledge_graph/` is separate from this UI; wire promoted `active` candidates into your graph store when the API is live.
