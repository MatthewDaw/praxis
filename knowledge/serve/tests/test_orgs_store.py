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


def test_set_password_rotates_and_old_password_fails(unique_org):
    s = _store()
    s.create_org(unique_org, "Acme", "s3cret", "user-a")
    s.set_password(unique_org, "s3cret", "n3wpass", "user-a")
    # New password works, old one no longer does.
    s.join_org(unique_org, "n3wpass", "user-b")
    assert s.is_member(unique_org, "user-b")
    with pytest.raises(ValueError):
        s.join_org(unique_org, "s3cret", "user-c")


def test_set_password_wrong_current_rejected(unique_org):
    s = _store()
    s.create_org(unique_org, "Acme", "s3cret", "user-a")
    with pytest.raises(ValueError):
        s.set_password(unique_org, "wrong", "n3wpass", "user-a")


def test_set_password_non_member_rejected(unique_org):
    s = _store()
    s.create_org(unique_org, "Acme", "s3cret", "user-a")
    with pytest.raises(ValueError):
        s.set_password(unique_org, "s3cret", "n3wpass", "user-z")


def test_is_owner_distinguishes_owner_from_member(unique_org):
    # is_owner is stricter than is_member: only the role='owner' row qualifies, so
    # a plain joined member is a member but NOT an owner.
    s = _store()
    s.create_org(unique_org, "Acme", "s3cret", "user-a")
    s.join_org(unique_org, "s3cret", "user-b")
    assert s.is_owner(unique_org, "user-a")
    assert s.is_member(unique_org, "user-b")
    assert not s.is_owner(unique_org, "user-b")
    # A non-member is neither.
    assert not s.is_owner(unique_org, "user-z")


def test_delete_org_removes_row_and_cascades_members(unique_org):
    # delete_org drops the orgs row (returning True) and its ON DELETE CASCADE FKs
    # remove the membership rows with it.
    s = _store()
    s.create_org(unique_org, "Acme", "s3cret", "user-a")
    s.join_org(unique_org, "s3cret", "user-b")
    assert s.delete_org(unique_org) is True
    # Gone everywhere: no membership, the deleted org is no longer listed, and a
    # re-delete reports nothing. (Assert the deleted org is absent rather than that
    # user-a owns zero orgs — sibling tests share that owner and don't clean up.)
    assert not s.is_member(unique_org, "user-a")
    assert not s.is_member(unique_org, "user-b")
    assert unique_org not in {o["org_id"] for o in s.list_orgs("user-a")}
    assert s.delete_org(unique_org) is False
