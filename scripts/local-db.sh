#!/usr/bin/env bash
# Spin up a local Postgres 16 + pgvector for development and the backend test
# suite, matching the credentials in the repo-root .env (PRAXIS_DB_URL).
#
# Why this exists: the server is Postgres-only. With no local DB, resolve_dsn()
# (knowledge/serve/db.py) falls through to AWS Secrets Manager and tests run
# against the deployed RDS. This gives you an isolated local DB instead.
#
# Usage (from anywhere in the repo):
#   scripts/local-db.sh         # create-or-start the container + bootstrap schema
#   scripts/local-db.sh down    # stop the container (keeps data)
#   scripts/local-db.sh reset   # remove the container AND its data volume
#
# Requires: Docker, and PRAXIS_DB_URL=postgresql://USER:PASS@localhost:PORT/DB
# in the repo-root .env (already the case for local dev).
set -euo pipefail

CONTAINER="praxis-pg"
VOLUME="praxis-pg-data"
IMAGE="pgvector/pgvector:pg16"

# Resolve repo root (this script lives in <root>/scripts/).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

cmd="${1:-up}"

if [[ "$cmd" == "down" ]]; then
  docker stop "$CONTAINER" >/dev/null 2>&1 && echo "stopped $CONTAINER" || echo "$CONTAINER not running"
  exit 0
fi

if [[ "$cmd" == "reset" ]]; then
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker volume rm "$VOLUME" >/dev/null 2>&1 || true
  echo "removed $CONTAINER and volume $VOLUME"
  exit 0
fi

# --- parse PRAXIS_DB_URL from .env (postgresql://user:pass@host:port/db) ------
if [[ ! -f .env ]]; then echo "error: no .env at repo root" >&2; exit 1; fi
DB_URL="$(grep -E '^PRAXIS_DB_URL=' .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" )"
if [[ -z "$DB_URL" ]]; then echo "error: PRAXIS_DB_URL not set in .env" >&2; exit 1; fi

rest="${DB_URL#*://}"          # user:pass@host:port/db
creds="${rest%@*}"            # user:pass
after_at="${rest#*@}"        # host:port/db
DB_NAME="${after_at##*/}"     # db
hostport="${after_at%%/*}"    # host:port
PORT="${hostport##*:}"        # port
DB_USER="${creds%%:*}"        # user
DB_PASS="${creds#*:}"         # pass

# --- create-or-start the container --------------------------------------------
if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  docker start "$CONTAINER" >/dev/null
  echo "started existing $CONTAINER"
else
  docker run -d --name "$CONTAINER" \
    -p "${PORT}:5432" \
    -e POSTGRES_USER="$DB_USER" \
    -e POSTGRES_PASSWORD="$DB_PASS" \
    -e POSTGRES_DB="$DB_NAME" \
    -v "${VOLUME}:/var/lib/postgresql/data" \
    "$IMAGE" >/dev/null
  echo "created $CONTAINER ($IMAGE) on port $PORT"
fi

# --- wait for readiness, then bootstrap the schema ----------------------------
echo -n "waiting for postgres"
until docker exec "$CONTAINER" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; do
  echo -n "."; sleep 1
done
echo " ready"

uv run python -m knowledge.serve.db   # applies schema.sql (extension + tables)
echo "local db ready: ${DB_URL%%:*}://${DB_USER}:****@localhost:${PORT}/${DB_NAME}"
