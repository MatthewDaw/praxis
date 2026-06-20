"""Integration tests for the password-gated OrgsStore.

Skipped unless a database is reachable (PRAXIS_DB_URL or a resolvable Secrets
Manager DSN). Each test uses a unique org_id so runs never collide.
"""

from __future__ import annotations

import uuid

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)


def _store():
    from knowledge.serve.orgs_store import OrgsStore

    return OrgsStore(db.connect())


@pytest.fixture
def unique_org():
    # A fresh org id per run so create_org never collides with leftover rows.
    return "test_org_" + uuid.uuid4().hex[:12]


def test_create_lists_and_membership(unique_org):
    s = _store()
    s.create_org(unique_org, "Acme", "s3cret", "user-a")
    assert s.is_member(unique_org, "user-a")
    orgs = s.list_orgs("user-a")
    assert any(o["org_id"] == unique_org and o["role"] == "owner" for o in orgs)


def test_join_with_correct_password(unique_org):
    s = _store()
    s.create_org(unique_org, "Acme", "s3cret", "user-a")
    s.join_org(unique_org, "s3cret", "user-b")
    assert s.is_member(unique_org, "user-b")


def test_join_wrong_password_rejected(unique_org):
    s = _store()
    s.create_org(unique_org, "Acme", "s3cret", "user-a")
    with pytest.raises(ValueError):
        s.join_org(unique_org, "wrong", "user-b")
    assert not s.is_member(unique_org, "user-b")


def test_create_duplicate_rejected(unique_org):
    s = _store()
    s.create_org(unique_org, "Acme", "s3cret", "user-a")
    with pytest.raises(ValueError):
        s.create_org(unique_org, "Acme2", "other", "user-c")


def test_membership_isolation(unique_org):
    s = _store()
    s.create_org(unique_org, "Acme", "s3cret", "user-a")
    assert not s.is_member(unique_org, "user-z")
