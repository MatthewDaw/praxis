# PRAXIS candidate API — App Runner container.
# Run locally: docker build -t praxis-api . && docker run -p 8080:8080 praxis-api
FROM python:3.12-slim

# uv: fast, reproducible installs from the committed uv.lock.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Lockfile + project metadata first so dependency layers cache across source edits.
COPY pyproject.toml uv.lock README.md ./

# Source the package build needs (setuptools flat layout discovers `knowledge`).
COPY knowledge/ ./knowledge/

# Install deps + the praxis package itself, exactly as locked, without dev tools.
RUN uv sync --frozen --no-dev

# App Runner sends traffic to 8080 and health-checks /health; uvicorn binds 0.0.0.0.
ENV PRAXIS_API_HOST=0.0.0.0 \
    PORT=8080 \
    PATH="/app/.venv/bin:$PATH"
EXPOSE 8080

CMD ["uv", "run", "python", "-m", "knowledge.serve"]
