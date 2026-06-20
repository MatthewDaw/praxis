# Praxis MCP Server — knowledge graph from your coding sessions

Use the PRAXIS knowledge graph **for real** from a coding session. The local MCP server is a thin authenticated HTTP client over the FastAPI backend: it exposes two tools — `praxis_get_context` (read active facts) and `praxis_add_insight` (write a fully-approved fact) — and resolves your tenant `(org_id, user_id)` from a cached Cognito login.

**Owner:** Matthew Daw (`knowledge/mcp`, `knowledge/serve`) · The ingestion/retrieval pipeline runs **in the backend**; the MCP process holds no DB creds — just your login token + HTTP.

---

## Architecture primer

```text
Claude Code / Desktop
        │  (MCP stdio)
   knowledge.mcp server  ── Authorization: Bearer <id token>
        │                   X-Praxis-Org: <org>
        ▼  httpx
   FastAPI backend (knowledge/serve)  ── current_user (JWT sub) + active_org (org_members)
        │  build_trio(graph=PostgresVectorGraph(...))
        ▼
   RDS praxis_kg → facts (pgvector)
```

Tenancy is enforced **server-side** from the verified JWT + org membership — identical to the dashboard — so MCP writes can never land in the wrong tenant.

---

## Prerequisites

- **Python 3.12+** and project deps from repo root: `uv sync`
- **`OPENROUTER_API_KEY`** in `.env` — the backend embeds insights/queries via `OpenRouterEmbedder`/`OpenRouterLlm`.
- **`COGNITO_USER_POOL_ID` / `COGNITO_CLIENT_ID` / `COGNITO_REGION`** in `.env` — already present; used by `login` (SRP auth) and by the backend (JWT verification).
- **`PRAXIS_API_BASE_URL`** — the backend the MCP tools call. Tenant is derived from login, **not** this var. Use either:
  - the deployed App Runner backend: `https://bdsikf2bc8.us-east-1.awsapprunner.com`
  - local dev: `http://localhost:8000` (run `uv run python -m knowledge.serve` with `COGNITO_*` + a DSN).

  > **Deployed backend caveat:** the `/insights` + `/context` endpoints are new backend surface. To use them against App Runner you must ship them first — redeploy with `npm run deploy:web`. Until then, point `PRAXIS_API_BASE_URL` at `http://localhost:8000`.

The MCP server loads `.env` from the repo on startup, so commands below are run from the repo root.

---

## 1. Register with Claude Code

That's the only setup step — login happens **through the MCP tools**, so there's no separate CLI command.

```powershell
claude mcp add praxis -- uv run python -m knowledge.mcp
```

Run this **in the repo** so `uv` resolves the project venv and `.env` loads (for `PRAXIS_API_BASE_URL` + `COGNITO_*`). Verify with `claude mcp list`.

---

## 2. Log in (in a session, no CLI)

Just tell Claude to log you in — it calls the `praxis_login` tool:

> "Log me into Praxis: email me@example.com, password ……"

`praxis_login(email, password, org_id?)` authenticates against the Cognito pool and caches a refresh token + your selected org to `~/.praxis/mcp.json` (mode 600). If you belong to exactly one org it's auto-selected; if several, Claude lists them and you pick with `praxis_select_org`. No org yet? Use `praxis_create_org` (you set its join password) or `praxis_join_org`. Check state any time with `praxis_whoami`.

The served process only **mints** fresh ID tokens from the cached refresh token — you stay logged in across sessions; just ask Claude to log in again if it expires. (Your password is passed to the local tool only to authenticate; only a refresh token is cached, never the password.)

---

## 3. Register with Claude Desktop

Add to `claude_desktop_config.json` (set `cwd` to your repo path so `.env` loads):

```json
{
  "mcpServers": {
    "praxis": {
      "command": "uv",
      "args": ["run", "python", "-m", "knowledge.mcp"],
      "cwd": "C:/Users/mattd/Documents/gauntlet/praxis"
    }
  }
}
```

Restart Claude Desktop. The `praxis_*` tools should appear in the tool list.

---

## 4. The tools

### `praxis_get_context(query, top_k=8) -> str`

`GET /context?query=…&top_k=…`. Returns a context string built from the **active facts** most similar to `query` for your tenant (score-ordered, token-bounded). An empty query returns recent facts. Use it to pull your standing constraints/decisions into a session — e.g. *"how do I install deps in this repo?"*.

### `praxis_add_insight(insight, scope=None, category=None, source=None) -> str`

`POST /insights`. Stores a single, self-contained insight and returns the backend summary (added / merged with bumped `observation_count` / **overwrote a conflicting fact**).

Semantics — read before calling:

- **Fully approved, not proposed.** Your in-chat confirmation *is* the human gate, so the insight lands directly in the active `facts` store at full credibility (`confidence = 1.0`). It does **not** enter the `proposed → suggested → active` lifecycle (that remains only for log-distilled dashboard candidates).
- **Confirm exact wording first.** State a specific, self-contained insight and confirm the precise text in chat before calling.
- **Re-adds merge.** A near-duplicate bumps `observation_count` instead of creating a second row.
- **Force-overwrite on conflict.** A contradicting insight **supersedes** the nearest conflicting fact in place (and decays any other contradictions) — no contradictory pair lingers, and the newest approved truth wins.

`scope` (e.g. `global`), `category` (e.g. `constraint`), and `source` are optional metadata stored on the fact.

### Auth / org tools

- `praxis_login(email, password, org_id=None)` — authenticate and cache; auto-selects a single org.
- `praxis_select_org(org_id)` — set the active org for subsequent calls.
- `praxis_create_org(org_id, password, name=None)` / `praxis_join_org(org_id, password)` — bootstrap membership, then select.
- `praxis_whoami()` — show current login, active org, and your memberships.

The data tools fail soft: if you're not logged in (or no org is selected), they return a short hint telling Claude to call `praxis_login` / `praxis_select_org` rather than erroring.

---

## 5. Verify end to end

```powershell
# In a Claude session (ask Claude to log you in first; it calls praxis_login):
praxis_add_insight("use uv, not pip, in this repo", scope="global", category="constraint")
praxis_get_context("how do I install deps?")          # returns the insight
praxis_add_insight("use uv, not pip in this repo")    # near-dup → merge, obs_count bumps
praxis_add_insight("use pip, not uv, in this repo")   # contradiction → overwrites (one fact, new text)
```

Cross-check persistence/tenancy: the row appears in RDS under your Cognito `sub` (`SELECT … FROM facts WHERE org_id=<org> AND user_id=<sub>`) and in the deployed dashboard graph when logged in as that user.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Tool says "not logged in" | `~/.praxis/mcp.json` missing/expired | Ask Claude to log in (`praxis_login`) |
| 401 from the backend | Token expired or invalid | Ask Claude to run `praxis_login` again |
| 403 from the backend | Not a member of the selected org | `praxis_select_org` / `praxis_join_org` for an org you belong to |
| 503 from the backend | Backend has no DB (DSN) | Start the API with a DSN, or point at a backend that has one |
| Tools missing in Claude | Registered outside the repo / `.env` not loaded | Re-run `claude mcp add` from the repo root; set `cwd` in Desktop config |
| Insights never persist remotely | `/insights`+`/context` not deployed | Redeploy App Runner (`npm run deploy:web`) or use `http://localhost:8000` |

---

## Related docs

| Doc | Purpose |
|-----|---------|
| [RDS_KG_DEPLOY.md](../monica/RDS_KG_DEPLOY.md) | RDS Postgres + pgvector store of record |
| [ARCHITECTURE_MONICA.md](../monica/ARCHITECTURE_MONICA.md) | Pillar boundaries (dashboard is API-only) |
