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
from urllib.parse import unquote, urlsplit

import boto3
import psycopg

# The migrations directory is the schema source of truth (repo-root/migrations).
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

# RDS-managed secret holding the master DB credentials.
DEFAULT_SECRET = "praxis/knowledge-graph/db"
DEFAULT_DBNAME = "praxis_kg"
DEFAULT_REGION = "us-east-1"

# --- Local-only allowlist (used by the TEST guard; see require_local_dsn) -----
# An ALLOWLIST, not a denylist of prod hostnames: a denylist is defeated the day
# someone stands up a second RDS instance, and it fails OPEN (unknown host ->
# allowed) which is exactly backwards for a guard whose job is protecting prod.
LOCAL_DB_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", ""})

# The one escape hatch. Deliberately long and self-incriminating: nobody sets
# this by accident, and it reads as a confession in shell history / CI config.
ALLOW_REMOTE_TESTS_ENV = "PRAXIS_TESTS_ALLOW_REMOTE_DB_I_KNOW_THIS_MAY_WRITE_TO_PRODUCTION"


class RemoteDatabaseRefused(RuntimeError):
    """Raised when a local-only caller (the test suite) resolves a remote DSN."""


def dsn_host(dsn: str) -> str | None:
    """Return the lowercased host in ``dsn``, or ``None`` if it can't be parsed.

    Handles both DSN spellings libpq accepts: a URL (``postgresql://u:p@h:5432/db``)
    and the keyword form (``host=h port=5432 dbname=db``). ``None`` means "could
    not tell" and callers must treat it as NOT local (fail closed).
    """
    if not isinstance(dsn, str) or not dsn.strip():
        return None
    text = dsn.strip()
    if "://" in text:
        try:
            host = urlsplit(text).hostname
        except ValueError:
            return None
        # urlsplit strips the brackets from [::1] already; hostname is lowercased.
        return "" if host is None else host
    if "=" in text:
        for token in text.split():
            key, sep, value = token.partition("=")
            if sep and key.strip().lower() == "host":
                return unquote(value.strip().strip("'\"")).lower()
        # Keyword DSN with no host= -> local unix socket.
        return ""
    return None


def is_local_dsn(dsn: str | None) -> bool:
    """True only when ``dsn`` provably points at this machine (loopback/unix socket)."""
    if dsn is None:
        return False
    host = dsn_host(dsn)
    if host is None:
        return False
    host = host.strip("[]").lower()
    return host in LOCAL_DB_HOSTS or host.endswith(".localhost")


def _redact(dsn: str) -> str:
    """``dsn`` with any password removed, safe to print in an error message."""
    if "://" not in dsn:
        return dsn
    scheme, _, rest = dsn.partition("://")
    if "@" in rest:
        userinfo, _, hostpart = rest.rpartition("@")
        user = userinfo.partition(":")[0]
        rest = f"{user}:***@{hostpart}" if user else hostpart
    return f"{scheme}://{rest}"


REMOTE_DSN_REFUSAL = """\
REFUSING TO RUN: {context} resolved a NON-LOCAL database.

    resolved DSN: {dsn}

Only loopback databases (localhost / 127.0.0.1 / ::1 / unix socket) are allowed
here. The repo .env points PRAXIS_DB_URL at the DEPLOYED RDS instance, which is
the same database the live backend serves: running the suite against it creates
real orgs/facts/snapshots, takes the yoyo migration lock on production, and has
already caused 500s for concurrent users of the live API.

Fix -- start the local pgvector Postgres and point the run at it:

    just db-up                 # docker compose up -d --wait db  (pgvector/pgvector:pg16)
    just db-bootstrap          # apply migrations/ to the local DB (first time only)
    PRAXIS_DB_URL=postgresql://praxis:praxis@localhost:5433/praxis_kg pytest -q

Or export that URL for the shell / restore it in .env (the local URL is already
there, commented out, directly above the RDS one).

No Docker? You can still run the DB-free tests:

    PRAXIS_DB_DISABLED=1 pytest -q --ignore=knowledge/serve/tests

(knowledge/serve/tests must be excluded: importing knowledge.serve.app builds
`app = create_app()` at module scope, which needs a DSN.)

If you REALLY need tests to hit a remote database, set
""" + ALLOW_REMOTE_TESTS_ENV + """=1
-- it will announce itself loudly on every run."""


def require_local_dsn(dsn: str | None, *, context: str = "This process") -> None:
    """Refuse to continue unless ``dsn`` is local, or the escape hatch is set.

    This is the guard for the TEST path only — it is deliberately NOT called from
    ``resolve_dsn``/``connect``, because the MCP server, the hooks and the
    agent_factory tooling legitimately talk to the deployed database.
    """
    if is_local_dsn(dsn):
        return
    if os.environ.get(ALLOW_REMOTE_TESTS_ENV) == "1":
        print(
            f"\n!!! {ALLOW_REMOTE_TESTS_ENV}=1 !!!\n"
            f"!!! {context} is running against a REMOTE database: "
            f"{_redact(dsn or '<unresolved>')}\n"
            "!!! Writes here are NOT isolated. This may be production.\n",
            file=sys.stderr,
            flush=True,
        )
        return
    raise RemoteDatabaseRefused(REMOTE_DSN_REFUSAL.format(
        context=context, dsn=_redact(dsn or "<unresolved>")
    ))


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


# DSNs this process has already migrated. Test fixtures call bootstrap() per
# fixture across dozens of files; after the first call the schema cannot have
# moved under us, so re-running is pure lock contention.
_BOOTSTRAPPED: set[str] = set()

# Seconds to wait for the yoyo lock row before giving up. yoyo's default is 10s,
# which several concurrent local/CI runs blow through and then fail with
# LockTimeout / duplicate key on yoyo_lock_pkey.
LOCK_TIMEOUT = int(os.environ.get("PRAXIS_DB_LOCK_TIMEOUT", "60"))


def bootstrap(dsn: str | None = None, *, force: bool = False) -> None:
    """Apply the yoyo migrations under ``migrations/`` (the schema source of truth).

    Idempotent, and cheap on the common path: yoyo records applied migrations in
    its ``_yoyo_migration`` ledger, so a fresh DB gets ``0000_initial`` (the full
    schema) plus any later migrations and an up-to-date DB is a no-op.

    Two things keep concurrent runs from fighting over the migration lock:
    the pending set is computed BEFORE taking the lock and the lock is only
    taken when there is something to apply (an up-to-date DB never locks at all),
    and a DSN already migrated by this process is skipped outright (pass
    ``force=True`` to re-check).
    """
    dsn = dsn or resolve_dsn()
    if dsn is None:
        raise RuntimeError(
            "No Postgres DSN available: set PRAXIS_DB_URL, or configure "
            f"PRAXIS_DB_SECRET (default {DEFAULT_SECRET!r}) with AWS credentials."
        )
    if dsn in _BOOTSTRAPPED and not force:
        return
    from yoyo import get_backend, read_migrations

    backend = get_backend(_yoyo_dsn(dsn))
    migrations = read_migrations(str(MIGRATIONS_DIR))
    pending = backend.to_apply(migrations)
    if pending:
        with backend.lock(timeout=LOCK_TIMEOUT):
            # Re-check under the lock: another process may have applied them
            # while we waited.
            pending = backend.to_apply(migrations)
            backend.apply_migrations(pending)
    print(f"bootstrap: applied {len(pending)} migration(s) from {MIGRATIONS_DIR.name}/")
    _BOOTSTRAPPED.add(dsn)


if __name__ == "__main__":
    # Mirror the server entrypoints: load the repo .env so PRAXIS_DB_URL is
    # resolved the same way `just backend` resolves it (no manual export step).
    from dotenv import load_dotenv

    load_dotenv()
    bootstrap()
