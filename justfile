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
# The database local dev and the test suite should use. NOTE: the repo .env
# currently points PRAXIS_DB_URL at the DEPLOYED RDS instance (the local URL is
# there, commented out, directly above it) — so anything that just loads .env is
# talking to production. `just test` below pins the local URL instead, and the
# suite refuses outright to run against a non-local database.
local_db_url := "postgresql://praxis:praxis@localhost:5433/praxis_kg"

# Start the local pgvector Postgres (idempotent; waits until it accepts connections).
db-up:
    docker compose up -d --wait db
    @echo "Local DB ready at postgresql://praxis:praxis@localhost:5433/praxis_kg"
    @echo "Run 'just db-bootstrap' once to apply the schema, then 'just backend'."

# Apply the yoyo migrations under migrations/ to the local DB. Idempotent.
# Pins the local DSN: bare `python -m knowledge.serve.db` loads .env, which
# currently resolves to prod RDS — this recipe must never migrate production.
db-bootstrap:
    PRAXIS_DB_URL={{local_db_url}} uv run python -m knowledge.serve.db

# Print the local DB connection string.
db-url:
    @echo "{{local_db_url}}"

# Open a psql shell in the running local DB.
db-shell:
    docker compose exec db psql -U praxis -d praxis_kg

# Stop and remove the local Postgres container and its data volume.
db-down:
    docker compose down -v

# --- Tests -------------------------------------------------------------------

# Run the Python suite against the LOCAL Postgres (run `just db-up` first).
test *ARGS:
    PRAXIS_DB_URL={{local_db_url}} uv run pytest {{ARGS}}

# Run only the DB-free tests (no Docker needed). knowledge/serve/tests is
# excluded because importing knowledge.serve.app builds `app = create_app()` at
# module scope, which errors at collection with no DSN.
test-nodb *ARGS:
    PRAXIS_DB_DISABLED=1 uv run pytest --ignore=knowledge/serve/tests {{ARGS}}

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
