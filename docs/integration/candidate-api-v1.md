# Candidate API — Contract v1

**Owner (client):** Monica Peters — React (`frontend-react/`) + Python reference (`frontend/services/`)  
**Owner (server):** Matthew Daw — ML & Knowledge Pipeline  
**Version:** `X-Praxis-Contract: 1` (override via `PRAXIS_CONTRACT_VERSION`)

This document is the **async integration handshake**. Matthew implements the server to these shapes; the Python reference client in `frontend/services/api_client.py` and the React client in `frontend-react/src/api/` target them without a pairing session.

**Fixtures:** [`fixtures/`](fixtures/) — copy-paste examples for server tests and client contract tests.

---

## Base URL

Set `PRAXIS_API_BASE_URL` for Python contract smoke tests (e.g. `http://localhost:8000`). React uses build-time `VITE_PRAXIS_API_BASE_URL` (same URL). Bearer token: `PRAXIS_API_TOKEN` / `VITE_PRAXIS_API_TOKEN`. Active org: `PRAXIS_ORG_ID` / `VITE_PRAXIS_ORG_ID` (default `default`).

---

## Authentication & tenancy

The server (`knowledge/serve/app.py`) hard-requires authentication and tenant context on **every data route** (`/health` stays open):

- **Bearer JWT** — a valid Cognito ID/access token in `Authorization: Bearer <token>`. Missing or invalid token → **401**. The token's `sub` is the tenant `user_id`.
- **`X-Praxis-Org`** — selects the active org. The server defaults to `default` when the header is absent. On the Postgres path the caller must be a member of that org, else **403**; on the in-memory/JSON path (offline/dev) the membership check is skipped and any org string is accepted.
- **Dev seam** — set `PRAXIS_AUTH_DISABLED=1` on the server to bypass JWT verification and return a fixed `dev-user` principal. This is how the contract smoke tests run without minting real tokens.

Clients send these via `PRAXIS_API_TOKEN` → `Authorization` and `PRAXIS_ORG_ID` → `X-Praxis-Org` (`frontend/services/contract_v1.py` `contract_headers`; React `frontend-react/src/api/contract.ts` `contractHeaders`). The Python client surfaces 401/403 as an `ApiClientError` with a hint to set the token/org.

---

## Headers (all requests)

| Header | Value |
|--------|-------|
| `Accept` | `application/json` |
| `Content-Type` | `application/json` (mutations) |
| `X-Praxis-Contract` | `1` |
| `Authorization` | `Bearer <cognito-jwt>` (data routes; omit only when server has `PRAXIS_AUTH_DISABLED=1`) |
| `X-Praxis-Org` | active org id (data routes; defaults to `default`) |

---

## Read endpoints

### `GET /candidates`

Optional query: `?state=proposed|active|decayed`

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
| `state` | string | yes | `proposed`, `active`, `decayed` (unknown values displayed as-is) |
| `confidence` | float | yes | 0–1 aggregate |
| `provenance` | string | yes | `logs/<file>.jsonl:<line>` |
| `createdAt` | ISO 8601 | yes | Aliases: `created_at`, `updatedAt` |
| `confidenceBreakdown` | object | no | `{ frequency, recency, breadth }` + optional `*Rationale` strings |
| `contradictions` | array | no | Rival candidate ids or `{ id }` objects |
| `auditTrail` | array | no | `{ action, timestamp, provenance, actor, note? }` |
| *other keys* | any | no | Preserved in dashboard `Candidate.extra` |

Client parser: `frontend/models/candidate.py` → `Candidate.from_mapping()` (Python); `frontend-react/src/api/candidateModel.ts` → `candidateFromMapping()` (React).

---

## Mutation endpoints

### `POST /candidates/{id}/promote`

**Request body (canonical v1):**

```json
{ "targetState": "active" }
```

See [`fixtures/promote-request.json`](fixtures/promote-request.json).

The dashboard computes `targetState` from the current candidate state (`proposed` → `active`).

**Fallback:** If the server returns **400** or **422** on explicit `targetState`, the client retries once with `{}` (server-side auto-advance).

**Response:** updated Candidate object.

**409 / stale promote:** Contract documents **409** for state conflict. Matthew's server (`knowledge/serve`) returns **400** with `cannot promote` for invalid/stale promote; clients map that to the same refresh-and-retry UX as 409.

### `POST /candidates/{id}/reject`

**Request body:**

```json
{ "reason": "optional human note" }
```

**Response:** empty body or updated candidate. Reject sets `state: "decayed"` (Matthew's store); clients refresh the list to show the decayed badge.

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

Implementation:

- Python: `frontend/services/contract_v1.py`, `frontend/services/api_client.py`
- React: `frontend-react/src/api/contract.ts`, `frontend-react/src/api/apiClient.ts`
- Server: `knowledge/serve/app.py` (Matthew)

---

## Self-serve validation (no meeting)

```powershell
$env:PYTHONPATH = "frontend"
uv run pytest frontend/tests/test_contract_fixtures.py frontend/tests/test_mock_gate_workflow.py -q
$env:PRAXIS_API_BASE_URL = "http://localhost:8000"
uv run pytest frontend/tests/test_live_api_smoke.py -q   # when Matthew's server is up
cd frontend-react
npm test
```

See also [`wire-up.md`](wire-up.md).

---

## Related docs

- [monica-wireframes.md](../monica/monica-wireframes.md) — as-built UI spec
- [ARCHITECTURE_MONICA.md](../monica/ARCHITECTURE_MONICA.md) §17 — pillar integration architecture
