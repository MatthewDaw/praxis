# Render.com Deployment — Monica's Dashboard Pillar

Monica-owned deploy; teammates can ignore and run locally.

## Settings

| Field | Value |
|-------|-------|
| **Root directory** | `frontend` |
| **Build command** | `pip install -r requirements.txt` |
| **Start command** | `streamlit run app.py --server.port=$PORT --server.address=0.0.0.0` |
| **Health check** | HTTP `/` (Streamlit root) |

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `PRAXIS_API_BASE_URL` | No (mock if unset) | Matthew's candidate API base URL |
| `PRAXIS_API_TOKEN` | No | Bearer token for API auth |
| `PRAXIS_EVAL_METRICS_URL` | No | Dominic's eval metrics JSON endpoint |
| `PRAXIS_CONTRACT_VERSION` | No (default `1`) | `X-Praxis-Contract` header for API requests |

## Startup expectations

- **Cold start:** Render free/starter tiers may take 30–60s on first request after idle — acceptable for capstone demo; mention during live presentation if spinning up fresh.
- **Mock demo mode:** Deploy without `PRAXIS_API_BASE_URL` for portfolio-safe public demo (fixtures only).

## Local parity check before deploy

```powershell
cd frontend
pip install -r requirements.txt
streamlit run app.py
```

## Files

- [`frontend/render.yaml`](../frontend/render.yaml) — Render blueprint
- [`frontend/.streamlit/config.toml`](../frontend/.streamlit/config.toml) — light theme defaults (committed via `!.streamlit/config.toml` in `frontend/.gitignore`)
