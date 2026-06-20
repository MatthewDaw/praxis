# Render.com Deployment — Monica's Dashboard Pillar

Monica-owned deploy; teammates can ignore and run locally.

The human-gate dashboard deploys as a **React static site** (`frontend-react/`). Blueprint at [`frontend-react/render.yaml`](../../frontend-react/render.yaml).

---

## React static site (`praxis-react-human-gate`)

Vite SPA served from Render's CDN — no server process, no cold-start sleep on the static tier.

### Settings

| Field | Value |
|-------|-------|
| **Blueprint path** | `frontend-react/render.yaml` |
| **Git branch** | `monica/dashboard-human-gate` (set in blueprint — not `main`) |
| **Git repo** | `https://github.com/monigarr/praxis` (repo root only — no `/tree/main`) |
| **Root directory** | `frontend-react` |
| **Build command** | `npm ci && npm run build` |
| **Publish directory** | `./dist` |
| **Instance plan** | Omit — static sites do not use `starter`/`free` web-service plans |
| **Auto deploy** | On commit to `monica/dashboard-human-gate` |
| **First deploy env** | **None required** (mock mode) |

### Dashboard setup

1. **Render → New → Blueprint**
2. Connect GitLab repo: `https://labs.gauntletai.com/monicapeters/praxis.git`
3. Set **Blueprint Path** to `frontend-react/render.yaml`
4. Confirm **Branch** is `monica/dashboard-human-gate` (blueprint sets this; override in Dashboard only if you rename the dev branch)
5. Leave `VITE_PRAXIS_API_BASE_URL` unset for portfolio mock demo
6. Deploy and note the `*.onrender.com` URL

**Pro workspace note:** Account Pro billing unlocks workspace features (e.g. preview environments, team access). Static sites are CDN-hosted and do **not** take a `plan: starter` field — omit `plan` in the blueprint (Render's static-site example in the [blueprint spec](https://render.com/docs/blueprint-spec) has none).

**Common blueprint validation errors:**

| Error | Fix |
|-------|-----|
| `branch … could not be found` | Use repo root URL (`https://github.com/monigarr/praxis`), not a GitHub UI path like `…/tree/main`. Confirm the branch is pushed to that remote. |
| `no such plan starter for service type web` | Remove `plan` from static-site services (`runtime: static`). |

### Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `VITE_PRAXIS_API_BASE_URL` | No (mock if unset) | Matthew's candidate API base URL |
| `VITE_PRAXIS_API_TOKEN` | No | Bearer token for API auth |
| `VITE_PRAXIS_EVAL_METRICS_URL` | No | Dominic's eval metrics JSON endpoint |
| `VITE_PRAXIS_CONTRACT_VERSION` | No (default `1`) | `X-Praxis-Contract` header for API requests |
| `NODE_VERSION` | No (blueprint sets `20`) | Node.js version for build |

**Vite build-time note:** `VITE_*` variables are embedded at **build time**, not read at runtime. To switch mock → live API:

1. Set `VITE_PRAXIS_API_BASE_URL` (and optional token/metrics URL) in Render **Environment** for the static site.
2. Trigger **Manual Deploy** (or push a commit) so Render rebuilds with the new values.
3. Ensure Matthew's API allows CORS from your `*.onrender.com` origin (browser calls the API directly — no Vite dev proxy in production).

### Local parity check before deploy

```powershell
cd frontend-react
npm ci
npm run lint
npm run build
npm run preview
```

Open the preview URL; confirm mock candidates load and Act 2 flows work (filter suggested, promote, resolve contradiction).

### Smoke test on Render URL

- Banner shows **Live API** with deployed `praxis-candidate-api` URL (when blueprint includes both services)
- Pipeline-seeded candidates visible (5+ rows from distillation export)
- Promote/reject/contradiction actions mutate persisted store on the API service
- Eval metrics embed loads from `{API_URL}/metrics`

### Live integration blueprint

[`frontend-react/render.yaml`](../../frontend-react/render.yaml) declares **two** services:

| Service | Type | Purpose |
|---------|------|---------|
| `praxis-candidate-api` | Python web | FastAPI candidate + metrics API (`knowledge/serve`) |
| `praxis-react-human-gate` | Static | Vite SPA wired via `fromService` → `VITE_PRAXIS_API_BASE_URL` |

API-only deploy: use [`knowledge/serve/render.yaml`](../../knowledge/serve/render.yaml).

**CORS:** The API allows `localhost` dev origins and `*.onrender.com` by default. Override with `PRAXIS_CORS_ORIGINS` or `PRAXIS_CORS_ORIGIN_REGEX` on the API service.

**Build note:** Static site uses `npm run build:render`, which sets `VITE_PRAXIS_EVAL_METRICS_URL` to `{API_URL}/metrics` when unset.

### Live API + Postgres (persisted candidate store)

By default the Render API blueprint does not set database credentials — the API falls back to a JSON file store. For **persisted** promote/reject/resolve across restarts and hosts:

1. Stand up RDS per [RDS_KG_DEPLOY.md](RDS_KG_DEPLOY.md) (CDK, AWS CLI, Secrets Manager, schema bootstrap).
2. On the **`praxis-candidate-api`** Render service, set **`PRAXIS_DB_URL`** (copy DSN from Secrets Manager JSON).
3. Ensure the RDS security group allows connections from Render (see runbook §2 security notes).
4. Dashboard env vars stay API-only — `VITE_PRAXIS_API_BASE_URL` only.

### Mock-only fallback

Leave `VITE_PRAXIS_API_BASE_URL` unset (or deploy static site without the API service) for portfolio mock demo.

### Files

- [`frontend-react/render.yaml`](../../frontend-react/render.yaml) — Render blueprint
- [`frontend-react/.node-version`](../../frontend-react/.node-version) — Node 20 pin
- [`frontend-react/.env.example`](../../frontend-react/.env.example) — local env template

### Deprecated Streamlit service

The former **`praxis-human-gate`** Streamlit web service (`frontend/render.yaml`) has been removed from the repo. If it still exists in your Render dashboard, delete or disable it to avoid failed deploys.
