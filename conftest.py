"""Pytest session bootstrap (repo root): load ``.env``, then refuse remote DBs.

Two jobs, in order.

1. Load the repo-root ``.env`` before any test imports, so the whole suite
   resolves the same ``PRAXIS_DB_URL`` (and API keys) as the app and the
   migrations. Without this, only modules that call ``load_dotenv()`` themselves
   (app, migrations, ``test_server``) saw ``.env``; the facts/graph tests fell
   through to AWS Secrets Manager and ran against the deployed RDS. Loading here
   makes ``.env`` the single source of truth for local test runs.

   ``load_dotenv`` does not override variables already set in the environment, so
   an explicit shell ``export`` still wins (e.g. CI).

2. Gate the whole session on that DSN being LOCAL. ``.env`` currently points
   ``PRAXIS_DB_URL`` at the deployed RDS instance — the same database the live
   backend serves — so step 1 on its own aims ``pytest -q`` at PRODUCTION: the
   suite creates real orgs/facts/snapshots, takes the yoyo migration lock on
   prod (a killed run leaves a stale lock row behind), and the write burst has
   already cascaded 500s onto unrelated live traffic (see the ``_ConnProxy``
   docstring in ``knowledge/serve/app.py``, gaps H13.2/H13.3).

   The gate lives HERE, on the test path, and NOT inside ``db.resolve_dsn()``:
   the MCP server, the hooks and the ``agent_factory`` tooling legitimately talk
   to the deployed database and must keep working.

   It fails the session at configure time — before a single test runs — because a
   warning scrolls past unread during a long run. See ``db.require_local_dsn``
   for the allowlist, the refusal text, and the escape hatch.
"""

import pytest
from dotenv import load_dotenv

load_dotenv()


def pytest_configure(config):
    """Refuse to collect anything if the resolved DSN is not local."""
    from knowledge.serve import db

    dsn = db.resolve_dsn()
    if dsn is None:
        return  # No database at all (PRAXIS_DB_DISABLED / no creds): DB tests skip.
    try:
        db.require_local_dsn(dsn, context="The pytest suite")
    except db.RemoteDatabaseRefused as exc:
        raise pytest.UsageError(f"\n\n{exc}\n") from None
