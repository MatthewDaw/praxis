"""Postgres connection + schema bootstrap for the knowledge-graph store.

Resolves a DSN from the environment or AWS Secrets Manager, hands out
autocommit psycopg (v3) connections, and applies the schema by running the
yoyo migrations under ``migrations/`` — which are the **single source of truth**
for the schema (``0000_initial.sql`` creates everything; later changes are
ordered migrations after it). Run ``python -m knowledge.serve.db`` to migrate.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import boto3
import psycopg

# The migrations directory is the schema source of truth (repo-root/migrations).
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

# RDS-managed secret holding the master DB credentials.
DEFAULT_SECRET = "praxis/knowledge-graph/db"
DEFAULT_DBNAME = "praxis_kg"
DEFAULT_REGION = "us-east-1"


def resolve_dsn() -> str | None:
    """Resolve a Postgres DSN, preferring an explicit URL over Secrets Manager.

    Returns ``None`` when no source is configured or the secret can't be
    fetched (so offline / no-creds environments degrade gracefully).
    """
    # 0) Explicit opt-out: force the JSON candidate store (no Postgres, no org
    #    membership checks). Handy for local single-tenant dev / demos.
    if os.environ.get("PRAXIS_DB_DISABLED") == "1":
        return None
    # 1) Explicit full DSN/URL wins.
    url = os.environ.get("PRAXIS_DB_URL")
    if url:
        return url

    # 2) Fall back to an RDS-managed secret in AWS Secrets Manager — but ONLY
    #    when explicitly allowed. This closes a footgun: a script that forgets to
    #    load the repo .env (so PRAXIS_DB_URL is unset) would otherwise silently
    #    connect to PRODUCTION RDS if AWS creds happen to be on the machine.
    #    Prod (App Runner) and CI set PRAXIS_DB_ALLOW_REMOTE=1 on purpose.
    if os.environ.get("PRAXIS_DB_ALLOW_REMOTE") != "1":
        return None
    secret_name = os.environ.get("PRAXIS_DB_SECRET", DEFAULT_SECRET)
    region = os.environ.get("AWS_REGION", DEFAULT_REGION)
    try:
        client = boto3.client("secretsmanager", region_name=region)
        raw = client.get_secret_value(SecretId=secret_name)["SecretString"]
        s = json.loads(raw)
        dbname = s.get("dbname") or DEFAULT_DBNAME
        # Loud, unmissable: surface exactly which (remote) host we resolved to.
        print(
            f"[db] PRAXIS_DB_URL unset — resolving REMOTE DSN via Secrets Manager "
            f"-> {s['host']}:{s['port']}/{dbname}",
            file=sys.stderr,
        )
        return (
            f"postgresql://{s['username']}:{s['password']}"
            f"@{s['host']}:{s['port']}/{dbname}"
        )
    except Exception:
        # No creds, no network, missing/malformed secret — caller handles None.
        return None


def connect(dsn: str | None = None) -> psycopg.Connection:
    """Open an autocommit connection, resolving the DSN if none is given."""
    dsn = dsn or resolve_dsn()
    if dsn is None:
        raise RuntimeError(
            "No Postgres DSN available: set PRAXIS_DB_URL, or configure "
            f"PRAXIS_DB_SECRET (default {DEFAULT_SECRET!r}) with AWS credentials."
        )
    conn = psycopg.connect(dsn, autocommit=True)
    # Register the pgvector adapter so embeddings round-trip as python lists.
    # Best-effort: offline/no-vector paths must still get a usable connection.
    try:
        from pgvector.psycopg import register_vector

        register_vector(conn)
    except Exception:
        # pgvector not installed, or the `vector` type isn't present — ignore.
        pass
    return conn


def _yoyo_dsn(dsn: str) -> str:
    """Rewrite a libpq DSN to the scheme yoyo uses for psycopg v3.

    yoyo picks its backend from the URL scheme; this project ships psycopg v3,
    which yoyo exposes as ``postgresql+psycopg://``. ``postgres://`` /
    ``postgresql://`` are normalized; an already-``+psycopg`` DSN is left alone.
    """
    for prefix in ("postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return "postgresql+psycopg://" + dsn[len(prefix):]
    return dsn


def bootstrap(dsn: str | None = None) -> None:
    """Apply the yoyo migrations under ``migrations/`` (the schema source of truth).

    Idempotent: yoyo records applied migrations in its ``_yoyo_migration`` ledger
    and only applies what's new, so a fresh DB gets ``0000_initial`` (the full
    schema) plus any later migrations, and an up-to-date DB is a no-op. Replaces
    the former ``schema.sql`` bootstrap.
    """
    dsn = dsn or resolve_dsn()
    if dsn is None:
        raise RuntimeError(
            "No Postgres DSN available: set PRAXIS_DB_URL, or configure "
            f"PRAXIS_DB_SECRET (default {DEFAULT_SECRET!r}) with AWS credentials."
        )
    from yoyo import get_backend, read_migrations

    backend = get_backend(_yoyo_dsn(dsn))
    migrations = read_migrations(str(MIGRATIONS_DIR))
    with backend.lock():
        to_apply = backend.to_apply(migrations)
        backend.apply_migrations(to_apply)
    print(f"bootstrap: applied {len(to_apply)} migration(s) from {MIGRATIONS_DIR.name}/")


if __name__ == "__main__":
    # Mirror the server entrypoints: load the repo .env so PRAXIS_DB_URL is
    # resolved the same way `just backend` resolves it (no manual export step).
    from dotenv import load_dotenv

    load_dotenv()
    bootstrap()
