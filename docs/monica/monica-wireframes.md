# Monica Peters — Dashboard As-Built Spec

**Author:** Monica Peters <monigarr@MoniGarr.com>  
**Branch:** `monica/dashboard-human-gate`  
**Created:** 2026-06-17  
**Last updated:** 2026-06-18  
**Status:** As-built through Day 2 — Streamlit mock-complete; React client shipped (`frontend-react/`); live API when Matthew publishes endpoints.

Architecture source of truth: [PRAXIS_Project_Plan.html](../PRAXIS_Project_Plan.html).

Pillar architecture: [ARCHITECTURE_MONICA.md](ARCHITECTURE_MONICA.md).

## Overview

The human-gate dashboard is a **modular Streamlit app** under `frontend/`. Entry point `app.py` wires a `DataProvider` to UI components; it does not contain presentation logic.

```text
frontend/app.py
  → components/candidate_list.py      (table + card views, confirmations)
  → components/candidate_detail.py    (detail + audit trail)
  → components/contradiction_panel.py (side-by-side + resolve actions)
  → components/eval_metrics_embed.py  (Dominic metrics URL or placeholder)
  → services/data_provider.py         (mock or API factory)
  → services/api_client.py            (HTTP client — Matthew's API)
```

Lifecycle states: `proposed → suggested → active` (plus `decayed` and unrecognized API values preserved for display).

## Screen 1: Dashboard shell + candidate list (shipped)

**File:** `frontend/app.py`, `frontend/components/candidate_list.py`

| Element | Implementation |
|---------|----------------|
| Header | `st.title("Candidate Review Gate")` + subtitle markdown |
| Mode banner | Mock vs live API caption from `PRAXIS_API_BASE_URL` |
| Search | `st.text_input` — filters title and content (case-insensitive) |
| State filter | `st.selectbox` — All / proposed / suggested / active / decayed |
| Global selection | Shared selectbox drives detail view + table actions |
| Table tab | `st.dataframe` with `ProgressColumn`; promote/reject with confirmations |
| Card tab | `st.columns(3)` grid; **Inspect in detail** sets global selection |
| State badge | `confidence_badge.render_state_badge` — orange/blue/green/gray |
| Confidence | `st.progress` on cards; `ProgressColumn` in table |
| Provenance | `st.caption` with `` `logs/<file>.jsonl:<line>` `` |
| Actions | Confirm dialogs; success banner; decayed candidates blocked from promote |
| Error states | Empty filter message; API load failure banner in `app.py` |
| Footer | Pillar + integration note |

**Mock data:** 17 candidates in `frontend/mock_data.py`. Includes `confidenceBreakdown` on cand_1–3, contradiction pair cand_9 ↔ cand_16, and decayed cand_12.

## Screen 2: Candidate detail (Day 3 — shipped)

**File:** `frontend/components/candidate_detail.py`

| Element | Implementation |
|---------|----------------|
| Container | `st.expander("Candidate detail")`, expanded when list non-empty |
| Selector | Synced with global selection + local selectbox |
| Content | Full title, state, provenance, body |
| Confidence | `render_confidence_breakdown` — frequency/recency/breadth metrics + tooltips |
| Audit trail | Renders `auditTrail` entries with JSONL provenance links |
| Extra fields | Pipeline-only keys (excludes auditTrail duplicate) |
| Contradictions | `contradiction_panel.py` with keep-A / keep-B / defer actions |

## Screen 3: Eval metrics embed (Day 8 — shipped)

**File:** `frontend/components/eval_metrics_embed.py`

- Collapsed expander with correction-rate line chart.
- `PRAXIS_EVAL_METRICS_URL` → Dominic JSON endpoint; placeholder when unset.
- Optional before/after correction scoreboard when API returns those fields.

## Data contract (forward-compatible)

`frontend/models/candidate.py` — `Candidate.from_mapping()` is the integration surface. **Canonical contract:** [candidate-api-v1.md](../integration/candidate-api-v1.md).

### Required for display (defaults if absent)

| Field | Aliases accepted | Notes |
|-------|------------------|-------|
| `id` | — | Stable identifier |
| `title` | — | Distilled lesson title |
| `content` | — | Full lesson body |
| `state` | — | Known: `proposed`, `suggested`, `active`, `decayed`; unknown values shown as-is (gray badge) |
| `confidence` | — | Float 0–1; defaults to `0.0` |
| `provenance` | `source`, `source_log`, `sourceLog` | Canonical display: `logs/<file>.jsonl:<line>` |
| `createdAt` | `created_at`, `updatedAt`, `updated_at` | ISO 8601 |

### Optional (pipeline extensions)

| Field | Aliases | Notes |
|-------|---------|-------|
| `confidenceBreakdown` | `confidence_breakdown` | `{ frequency, recency, breadth }` + optional rationale strings |
| `contradictions` | `contradiction_ids` | List of ids or `{ id }` objects |
| `auditTrail` | `audit_trail` | List of `{ action, timestamp, provenance, actor, note? }` |
| *any other key* | — | Preserved in `Candidate.extra` and shown in detail view |

**Versioning:** HTTP client sends `X-Praxis-Contract: 1`. Matthew/Dominic may extend the schema; Monica's pillar must not break on unknown fields.

### Mutations (canonical v1 — `docs/integration/candidate-api-v1.md`)

| Action | Endpoint | Body |
|--------|----------|------|
| Promote | `POST /candidates/{id}/promote` | `{ "targetState": "suggested" \| "active" }` → updated candidate |
| Reject | `POST /candidates/{id}/reject` | `{ "reason"?: string }` |
| Resolve contradiction | `POST /contradictions/{id}/resolve` | `{ "resolution": "keep_a" \| "keep_b", "keepId": string }` → kept candidate |

Contradiction id: `{primaryId}__{rivalId}`. UI maps "Keep this candidate" → `keep_a`, rival → `keep_b` in `contract_v1.py`.

409 responses surface as user-visible conflict messages (refresh + retry). Promote retries with `{}` if server rejects explicit `targetState`.

## React client (`frontend-react/`) — shipped 2026-06-18

Parallel **Vite + React + TypeScript** app for Matthew's API validation. Same contract, same mock fixtures (`public/mock-candidates.json` exported from `mock_data.py`), same Act 2 demo steps.

| Element | Implementation |
|---------|----------------|
| Entry | `src/App.tsx` — sidebar refresh, filters, table/card tabs |
| API | `src/api/apiClient.ts` + `mockProvider.ts` — contract v1 headers, promote 400/422 retry |
| List | `CandidateTable.tsx`, `CandidateCards.tsx` |
| Detail | `CandidateDetail.tsx` + `ConfidenceBreakdown.tsx` |
| Contradictions | `ContradictionPanel.tsx` — keep primary / keep rival / defer |
| Eval embed | `EvalMetricsEmbed.tsx` — `VITE_PRAXIS_EVAL_METRICS_URL` or placeholder |
| Env vars | `VITE_PRAXIS_API_BASE_URL`, `VITE_PRAXIS_API_TOKEN`, `VITE_PRAXIS_CONTRACT_VERSION` |

Matthew runs `npm run dev` in `frontend-react/`; Monica's Streamlit path unchanged in `frontend/`.

## Design notes

- Streamlit-native layout — no custom CSS framework
- Theme: `frontend/.streamlit/config.toml` (light, high-contrast defaults)
- Keyboard: Tab to selection controls; button `help` text on promote/reject/inspect
- Deploy: `frontend/render.yaml` — see [RENDER_DEPLOY.md](RENDER_DEPLOY.md)

## Remaining (Days 9–10)

See [DAYS_9_10_REMAINING.md](DAYS_9_10_REMAINING.md) — demo rehearsal checklist, user-flow video, screen-reader pass.
