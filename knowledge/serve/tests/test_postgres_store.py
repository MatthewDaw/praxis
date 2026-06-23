"""Integration tests for the Postgres-backed candidate store.

Skipped unless a database is reachable (PRAXIS_DB_URL or a resolvable
Secrets Manager DSN). Each test uses a unique org_id so runs never collide
with real data or each other. Tenancy is supplied per call.
"""

from __future__ import annotations

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)


def _store():
    from knowledge.serve.postgres_store import PostgresCandidateStore

    return PostgresCandidateStore()


def test_seed_and_promote_persist(unique_org):
    s = _store()
    s._seed_if_empty(unique_org, "u1", shared=True)
    assert len(s.list(unique_org, "u1")) > 0
    proposed = next(c for c in s.list(unique_org, "u1") if c.get("state") == "proposed")
    s.promote(unique_org, "u1", proposed["id"])
    # A fresh store for the same tenant reads the persisted state.
    # The funnel is now proposed -> active (no intermediate "suggested").
    assert _store().get(unique_org, "u1", proposed["id"])["state"] == "active"


def test_tenants_are_isolated(unique_org):
    s = _store()
    org_a, org_b = unique_org + "_a", unique_org + "_b"
    s._seed_if_empty(org_a, "u1", shared=True)
    s._seed_if_empty(org_b, "u1", shared=True)
    cid = s.list(org_a, "u1")[0]["id"]
    s.reject(org_a, "u1", cid, reason="only in tenant a")
    assert s.get(org_a, "u1", cid)["state"] == "decayed"
    assert s.get(org_b, "u1", cid)["state"] == "proposed"  # same id, untouched in tenant b


@pytest.fixture
def unique_org(request):
    # Unique per test node so reruns and parallel tenants never collide.
    return "test_" + request.node.name
