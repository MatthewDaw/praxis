# Candidate API — Contract v1

**Owner (client):** Monica Peters — Streamlit (`frontend/`) and React (`frontend-react/`)  
**Owner (server):** Matthew Daw — ML & Knowledge Pipeline  
**Version:** `X-Praxis-Contract: 1` (override via `PRAXIS_CONTRACT_VERSION`)

This document is the **async integration handshake**. Matthew implements the server to these shapes; the Streamlit dashboard client in `frontend/services/api_client.py` targets them without a pairing session.

**Fixtures:** [`fixtures/`](fixtures/) — copy-paste examples for server tests and client contract tests.

---

## Base URL

Set `PRAXIS_API_BASE_URL` on the dashboard (e.g. `https://api.example.com` or `http://localhost:8000`). Optional `PRAXIS_API_TOKEN` for Bearer auth.

---

## Headers (all requests)

| Header | Value |
|--------|-------|
| `Accept` | `application/json` |
| `Content-Type` | `application/json` (mutations) |
| `X-Praxis-Contract` | `1` |

---

## Read endpoints

### `GET /candidates`

Optional query: `?state=proposed|suggested|active|decayed`

**Response** — JSON array **or** wrapped object:

```json
{ "candidates": [ /* Candidate objects */ ] }
```

See [`fixtures/candidates-list.json`](fixtures/candidates-list.json).

### `GET /candidates/{id}`

**Response:** single Candidate object. **404** if unknown.

### Candidate object (read model)

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | string | yes | Stable identifier |
| `title` | string | yes | Distilled lesson title |
| `content` | string | yes | Full lesson body |
| `state` | string | yes | `proposed`, `suggested`, `active`, `decayed` (unknown values displayed as-is) |
| `confidence` | float | yes | 0–1 aggregate |
| `provenance` | string | yes | `logs/<file>.jsonl:<line>` |
| `createdAt` | ISO 8601 | yes | Aliases: `created_at`, `updatedAt` |
| `confidenceBreakdown` | object | no | `{ frequency, recency, breadth }` + optional `*Rationale` strings |
| `contradictions` | array | no | Rival candidate ids or `{ id }` objects |
| `auditTrail` | array | no | `{ action, timestamp, provenance, actor, note? }` |
| *other keys* | any | no | Preserved in dashboard `Candidate.extra` |

Client parser: `frontend/models/candidate.py` → `Candidate.from_mapping()`.

---

## Mutation endpoints

### `POST /candidates/{id}/promote`

**Request body (canonical v1):**

```json
{ "targetState": "suggested" }
```

or

```json
{ "targetState": "active" }
```

See [`fixtures/promote-request.json`](fixtures/promote-request.json).

The dashboard computes `targetState` from the current candidate state (`proposed` → `suggested` → `active`).

**Fallback:** If the server returns **400** or **422** on explicit `targetState`, the client retries once with `{}` (server-side auto-advance).

**Response:** updated Candidate object.

**409:** State conflict (e.g. stale promote). Dashboard shows message; user refreshes and retries.

### `POST /candidates/{id}/reject`

**Request body:**

```json
{ "reason": "optional human note" }
```

**Response:** empty body or updated candidate (client ignores body).

### `POST /contradictions/{id}/resolve`

**Contradiction id format:** `{primaryId}__{rivalId}` (e.g. `cand_9__cand_16`).

**Request body:**

```json
{
  "resolution": "keep_a",
  "keepId": "cand_9"
}
```

| `resolution` | Meaning |
|--------------|---------|
| `keep_a` | Keep the primary (left) candidate in the pair |
| `keep_b` | Keep the rival (right) candidate |

`merge` is stretch — not required for MVP.

See [`fixtures/resolve-request.json`](fixtures/resolve-request.json).

**Response:** the kept Candidate object.

---

## Dashboard client mapping

| UI action | HTTP |
|-----------|------|
| Promote | `POST /candidates/{id}/promote` with `{ targetState }` |
| Reject | `POST /candidates/{id}/reject` with optional `reason` |
| Keep this candidate | `resolution: keep_a`, `keepId` = primary id |
| Keep rival | `resolution: keep_b`, `keepId` = rival id |
| Defer | No API call (UI-only) |

Implementation: `frontend/services/contract_v1.py`, `frontend/services/api_client.py`.

---

## Self-serve validation (no meeting)

```powershell
cd frontend
$env:PYTHONPATH = "."
..\frontend\venv\Scripts\pytest tests/test_contract_fixtures.py -q
$env:PRAXIS_API_BASE_URL = "http://localhost:8000"
..\frontend\venv\Scripts\pytest tests/test_contract_fixtures.py -q  # when server up
```

See also [`wire-up.md`](wire-up.md).

---

## Related docs

- [monica-wireframes.md](../monica/monica-wireframes.md) — as-built UI spec
- [ARCHITECTURE_MONICA.md](../monica/ARCHITECTURE_MONICA.md) §17 — pillar integration architecture
- [eval-metrics-v1.md](eval-metrics-v1.md) — Dominic eval JSON contract
