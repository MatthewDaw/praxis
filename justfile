# Praxis dev tasks — run `just` to list, or `just <recipe>`.
# Backend and frontend are long-running; start each in its own terminal.
#
# First-time / from-scratch local dev:
#   just db-up            # start local Postgres (pgvector)
#   just db-bootstrap     # apply migrations (only needed once, or after db-down)
#   just backend          # terminal 1 — http://localhost:8000
#   just frontend         # terminal 2 — http://localhost:5173
#
# The backend auto-loads .env (PRAXIS_DB_URL -> the local DB below), so there is
# no manual `export` step: `just db-up` and `just backend` just work together.

# List available recipes (default).
default:
    @just --list

# Start the FastAPI backend (knowledge/serve) on http://localhost:8000
backend:
    uv run python -m knowledge.serve

# Start the React dashboard (Vite) on http://localhost:5173
frontend:
    cd frontend-react && npm run dev

# Install frontend dependencies
install-frontend:
    cd frontend-react && npm install

# Quick health check that the backend is up (expects {"status":"ok",...}).
health:
    curl -s http://localhost:8000/health

# --- Local Postgres (pgvector) -----------------------------------------------
# The database the backend uses for local dev. The repo .env points
# PRAXIS_DB_URL at this container (localhost:5433/praxis_kg) and the backend
# loads .env on startup, so once it's up + bootstrapped nothing reaches AWS
# Secrets Manager / prod RDS.

# Start the local pgvector Postgres (idempotent; waits until it accepts connections).
db-up:
    docker compose up -d --wait db
    @echo "Local DB ready at postgresql://praxis:praxis@localhost:5433/praxis_kg"
    @echo "Run 'just db-bootstrap' once to apply the schema, then 'just backend'."

# Apply the yoyo migrations under migrations/ to the local DB. Idempotent.
db-bootstrap:
    uv run python -m knowledge.serve.db

# Print the local DB connection string (already set as PRAXIS_DB_URL in .env).
db-url:
    @echo "postgresql://praxis:praxis@localhost:5433/praxis_kg"

# Open a psql shell in the running local DB.
db-shell:
    docker compose exec db psql -U praxis -d praxis_kg

# Stop and remove the local Postgres container and its data volume.
db-down:
    docker compose down -v

# Start the local observability UI (Arize Phoenix) on http://localhost:6006 (Docker)
observability:
    docker start phoenix 2>/dev/null || docker run -d --name phoenix -p 6006:6006 arizephoenix/phoenix:version-17.9.0
    @echo "Phoenix UI: http://localhost:6006"
    @echo "To send traces: run the backend with PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006"

# Stop the local Phoenix container
observability-stop:
    docker stop phoenix

# Start the Phoenix proxy on http://localhost:8800 (dashboard trace links)
observability-proxy:
    @echo "Set VITE_PRAXIS_PHOENIX_PROXY_URL=http://localhost:8800 in frontend-react/.env.local"
    uv run uvicorn frontend.phoenix_proxy.app:app --port 8800
