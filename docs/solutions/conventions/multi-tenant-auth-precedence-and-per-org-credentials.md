---
title: Multi-tenant Praxis auth — precedence, per-project org override, and which backend hosts which org
date: 2026-07-22
category: conventions
module: knowledge/serve (auth + api keys), agent_factory/hooks (_praxis)
problem_type: convention
component: authentication
severity: high
related_components: [api_keys, cognito, multi_tenancy, agent_factory]
applies_when:
  - Running two orgs (e.g. bestie + sotos) side by side, each with its own durable credential
  - A validly-minted pxk_ key or Cognito bearer is rejected and you need to know WHY
  - Deciding where to set an org/credential so it does not leak into another org's build
  - Wiring a new backend and choosing its auth mode (API-key-only vs Cognito-enabled)
tags: [multi-tenancy, auth, api-keys, cognito, bearer, praxis-org, agent-factory, credentials, isolation]
---

# Multi-tenant Praxis auth

Two orgs run concurrently, each with its own durable credential, and neither can
disturb the other. This documents the rules that make that true.

## Auth precedence (per request)

A request authenticates by EXACTLY ONE mode. Precedence, highest first:

1. **`PRAXIS_AUTH_DISABLED=1`** — dev seam; returns a fixed dev principal. Local/test only.
2. **`X-Praxis-Key: pxk_...`** (env `PRAXIS_API_KEY`) — a scoped API key. Preferred for
   automated agents. Resolves to a Principal pinned to the key's org (`api_key_org`).
3. **`Authorization: Bearer <IdToken>`** — a Cognito JWT, minted from the refresh token in
   the identity cache (`PRAXIS_MCP_CACHE`, default `~/.praxis/mcp.json`).

If an API key is present it is used and a bearer is never consulted (see
`agent_factory/hooks/_praxis.py:_auth_headers` and `knowledge/serve/auth.py:make_current_user`).

## Org precedence (which tenant the request targets)

`X-Praxis-Org` selects the org. The hook resolves it as: **`PRAXIS_ORG` pin > org
selected in the identity cache > `agent-factory` default** (`_praxis._resolve_org`,
mirrored in `knowledge/mcp/identity.resolve_org`). For a **key** request, the selected
org MUST equal the key's org or the server returns **403** — that match IS the membership
for a key. For a **bearer**, the user must be a member of the org (403 otherwise).

## The per-project override rule (NEVER edit the shared file)

Each repo pins its own org + credential via `<repo>/.claude/settings.local.json`:

```json
{ "env": {
    "PRAXIS_ORG": "bestie",
    "PRAXIS_API_KEY": "pxk_...bestie-scoped...",
    "PRAXIS_MCP_CACHE": "~/.praxis/bestie.json"
} }
```

A real env var **always wins** over the shared `agent_factory/.env` (the dotenv loader
never overrides an already-set var — `_praxis._load_dotenv`). So a project overrides its
org WITHOUT any edit inside the praxis repo. **Never edit `agent_factory/.env` to repoint
an org** — that is the shared default (`sotos`), and editing it silently moves every
project that relies on the default.

- The `bestie` checkout operates in org `bestie` by default; the agent-factory `sotos`
  build operates in org `sotos`. Both credentials are valid simultaneously; using one
  never affects the other.

## Which backend hosts which org

Backends are distinguished by `PRAXIS_API_BASE_URL`. A backend can be **Cognito-enabled**
(has `COGNITO_USER_POOL_ID` + `COGNITO_CLIENT_ID`) or **API-key-only** (those unset). A
bearer sent to an API-key-only backend now fails with a 401 that NAMES the mode
("this backend is API-key-only … send an X-Praxis-Key"), and a bearer from the wrong pool
fails with "bearer pool mismatch" — not a generic "invalid token".

> The `.env` the hook actually loaded (and the backend it resolved) is now logged to
> stderr on every run: `[praxis-hook] env=<path> backend=<url>`. If two `.env` files at
> different inodes disagree (e.g. a stale plugin-cache copy), that line shows which one won.
> Make ONE file authoritative — symlink the plugin-cache copy to `praxis/agent_factory/.env`
> rather than maintaining two.

## Durability of credentials across restarts

- **API keys** are stored (hash only) in the `api_keys` Postgres table — durable by
  construction. The hash is HMAC-SHA256 under `PRAXIS_API_KEY_SECRET` (a stable,
  env-provided pepper) or plain sha256 when unset. The secret is read fresh from the
  environment and **never generated at boot** — a boot-generated secret would orphan every
  previously minted key on the next restart. Keys minted before a pepper was configured
  still resolve (the resolver also tries the legacy sha256). Keys are **non-expiring**;
  they live until revoked.
- **Bearers** are Cognito-signed (RS256) and verified via JWKS. Praxis mints no JWTs, so
  there is no local signing secret to persist — the only server-side auth secret is the
  API-key pepper above.

## Preflight — `af whoami`

`python -m agent_factory.tools.whoami` prints one line —
`backend=… resolved_org=… (via …) principal=… auth_mode=key|bearer|dev [key_org=…]` — and
on a mismatch appends a crisp `MISMATCH: key scoped to org 'sotos' but PRAXIS_ORG='bestie'`,
exiting non-zero. It hits `GET /whoami` with the same headers a real hook request sends, so
what it reports is what the gates will see. Run it before an af-build to fail fast on a
wrong-org/wrong-backend setup.
