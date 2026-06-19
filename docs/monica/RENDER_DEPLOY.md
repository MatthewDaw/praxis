# Render.com Deployment — Monica's Dashboard Pillar

Monica-owned deploy; teammates can ignore and run locally.

Two independent Render services: **Streamlit** (`frontend/`) and **React static site** (`frontend-react/`). Each has its own blueprint path in the repo.

---

## Streamlit web service (`praxis-human-gate`)

### Settings

| Field | Value |
|-------|-------|
| **Root directory** | `frontend` |
| **Build command** | `pip install -r requirements.txt` |
| **Start command** | `streamlit run app.py --server.port=$PORT --server.address=0.0.0.0` |
| **Health check** | HTTP `/` (Streamlit root) |

### Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `PRAXIS_API_BASE_URL` | No (mock if unset) | Matthew's candidate API base URL |
| `PRAXIS_API_TOKEN` | No | Bearer token for API auth |
| `PRAXIS_EVAL_METRICS_URL` | No | Dominic's eval metrics JSON endpoint |
| `PRAXIS_CONTRACT_VERSION` | No (default `1`) | `X-Praxis-Contract` header for API requests |

### Startup expectations

- **Cold start:** Render free/starter tiers may take 30–60s on first request after idle — acceptable for capstone demo; mention during live presentation if spinning up fresh.
- **Mock demo mode:** Deploy without `PRAXIS_API_BASE_URL` for portfolio-safe public demo (fixtures only).

### Local parity check before deploy

```powershell
cd frontend
pip install -r requirements.txt
streamlit run app.py
```

### Files

- [`frontend/render.yaml`](../../frontend/render.yaml) — Render blueprint
- [`frontend/.streamlit/config.toml`](../../frontend/.streamlit/config.toml) — light theme defaults (committed via `!.streamlit/config.toml` in `frontend/.gitignore`)

---

## React static site (`praxis-react-human-gate`)

Vite SPA served from Render's CDN — no server process, no cold-start sleep on the static tier.

### Settings

| Field | Value |
|-------|-------|
| **Blueprint path** | `frontend-react/render.yaml` |
| **Git branch** | `monica/dashboard-human-gate` (set in blueprint — not `main`) |
| **Root directory** | `frontend-react` |
| **Build command** | `npm ci && npm run build` |
| **Publish directory** | `./dist` |
| **Instance plan** | `starter` (static site; Pro workspace billing is separate from service plan) |
| **Auto deploy** | On commit to `monica/dashboard-human-gate` |
| **First deploy env** | **None required** (mock mode) |

### Dashboard setup

1. **Render → New → Blueprint**
2. Connect GitLab repo: `https://labs.gauntletai.com/monicapeters/praxis.git`
3. Set **Blueprint Path** to `frontend-react/render.yaml` (Streamlit uses `frontend/render.yaml` separately — do not overwrite the existing Streamlit service)
4. Confirm **Branch** is `monica/dashboard-human-gate` (blueprint sets this; override in Dashboard only if you rename the dev branch)
5. Leave `VITE_PRAXIS_API_BASE_URL` unset for portfolio mock demo
6. Deploy and note the `*.onrender.com` URL

**Pro workspace note:** Account Pro billing unlocks workspace features (e.g. preview environments, team access). This static site uses the `starter` service plan in the blueprint — appropriate for a Vite CDN deploy. Do not set `plan: free` if you want Render's default production static-site tier.

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

- Banner shows mock mode (no live API URL)
- 17 candidates visible
- Promote/reject/contradiction actions update UI (mock provider — no backend needed)

### Files

- [`frontend-react/render.yaml`](../../frontend-react/render.yaml) — Render blueprint
- [`frontend-react/.node-version`](../../frontend-react/.node-version) — Node 20 pin
- [`frontend-react/.env.example`](../../frontend-react/.env.example) — local env template
