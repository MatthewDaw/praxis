"""Postgres connection + schema bootstrap for the knowledge-graph store.

Resolves a DSN from the environment or AWS Secrets Manager, hands out
autocommit psycopg (v3) connections, and applies the canonical schema
(``schema.sql``). Run ``python -m knowledge.serve.db`` to migrate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import boto3
import psycopg

# Canonical DDL lives next to this module.
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

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

    # 2) Fall back to an RDS-managed secret in AWS Secrets Manager.
    secret_name = os.environ.get("PRAXIS_DB_SECRET", DEFAULT_SECRET)
    region = os.environ.get("AWS_REGION", DEFAULT_REGION)
    try:
        client = boto3.client("secretsmanager", region_name=region)
        raw = client.get_secret_value(SecretId=secret_name)["SecretString"]
        s = json.loads(raw)
        dbname = s.get("dbname") or DEFAULT_DBNAME
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


def bootstrap(dsn: str | None = None) -> None:
    """Apply ``schema.sql`` (extension + tables) idempotently."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect(dsn) as conn:
        conn.execute(sql)
    print(f"bootstrap: applied {SCHEMA_PATH.name}")


if __name__ == "__main__":
    bootstrap()
