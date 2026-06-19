"""Integration tests for the Postgres-backed candidate store.

Skipped unless a database is reachable (PRAXIS_DB_URL or a resolvable
Secrets Manager DSN). Each test uses a unique org_id so runs never collide
with real data or each other.
"""

from __future__ import annotations

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)


def _store(org: str, user: str = "u1", shared: bool = True):
    from knowledge.serve.postgres_store import PostgresCandidateStore

    return PostgresCandidateStore(org_id=org, user_id=user, shared=shared)


def test_seed_and_promote_persist(unique_org):
    s = _store(unique_org)
    assert len(s.list()) > 0
    proposed = next(c for c in s.list() if c.get("state") == "proposed")
    s.promote(proposed["id"])
    # A fresh store for the same tenant reads the persisted state.
    assert _store(unique_org).get(proposed["id"])["state"] == "suggested"


def test_tenants_are_isolated(unique_org):
    a = _store(unique_org + "_a")
    b = _store(unique_org + "_b")
    cid = a.list()[0]["id"]
    a.reject(cid, reason="only in tenant a")
    assert a.get(cid)["state"] == "decayed"
    assert b.get(cid)["state"] == "proposed"  # same id, untouched in tenant b


@pytest.fixture
def unique_org(request):
    # Unique per test node so reruns and parallel tenants never collide.
    return "test_" + request.node.name
