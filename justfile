# Praxis dev tasks — run `just` to list, or `just <recipe>`.
# Backend and frontend are long-running; start each in its own terminal.

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

# Quick health check that the backend is up
health:
    curl -s http://localhost:8000/health

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
