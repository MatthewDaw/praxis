"""Integration tests for the org-shared SpacesStore.

Mirrors test_orgs_store.py: skipped unless a database is reachable (PRAXIS_DB_URL
or a resolvable Secrets Manager DSN). Each test uses a unique org_id so runs never
collide. ``spaces`` has a foreign key to ``orgs``, so every test first creates the
owning org via :class:`OrgsStore` before inserting spaces.

Under the tenancy redesign a *space* is an org-shared project folder keyed
``(org_id, space_id)`` — there is no owner axis, and every member of the org sees
every space (see specs/005-praxis-tenancy-redesign/design.md §1.7).
"""

from __future__ import annotations

import uuid

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

OWNER = "owner-a"


@pytest.fixture
def unique_org():
    # A fresh org id per run so the orgs FK insert never collides with leftovers.
    return "test_org_" + uuid.uuid4().hex[:12]


def _stores(org_id: str):
    """Create the owning org and return a SpacesStore over the same connection."""
    from knowledge.serve.orgs_store import OrgsStore
    from knowledge.serve.spaces_store import SpacesStore

    conn = db.connect()
    # spaces.org_id REFERENCES orgs(org_id): the org must exist first.
    OrgsStore(conn).create_org(org_id, "Acme", "s3cret", OWNER)
    return SpacesStore(conn)


def test_create_lists_and_exists(unique_org):
    s = _stores(unique_org)
    s.create_space(unique_org, "alpha", "Alpha space")
    assert s.exists(unique_org, "alpha")
    # exists is exact: a never-created space is absent.
    assert not s.exists(unique_org, "beta")
    spaces = s.list_spaces(unique_org)
    assert [x["space_id"] for x in spaces] == ["alpha"]
    assert spaces[0]["name"] == "Alpha space"
    assert spaces[0]["created_at"] is not None


def test_create_duplicate_raises(unique_org):
    s = _stores(unique_org)
    s.create_space(unique_org, "alpha", None)
    with pytest.raises(ValueError):
        s.create_space(unique_org, "alpha", "again")


def test_ensure_space_is_idempotent(unique_org):
    # ensure_space registers a space implied by a snapshot write; a pre-existing
    # row is a no-op, never an error (unlike create_space).
    s = _stores(unique_org)
    s.ensure_space(unique_org, "alpha", "Alpha")
    s.ensure_space(unique_org, "alpha", "ignored second name")
    assert s.exists(unique_org, "alpha")
    assert [x["space_id"] for x in s.list_spaces(unique_org)] == ["alpha"]


def test_list_spaces_ordered_by_space_id(unique_org):
    s = _stores(unique_org)
    for sid in ("zebra", "alpha", "mango"):
        s.create_space(unique_org, sid, None)
    assert [x["space_id"] for x in s.list_spaces(unique_org)] == [
        "alpha",
        "mango",
        "zebra",
    ]


def test_spaces_are_org_shared(unique_org):
    # A space is org-shared, not per-login: once created it exists for the whole
    # org and appears in the org-wide list (there is no owner axis to hide it).
    s = _stores(unique_org)
    s.create_space(unique_org, "alpha", None)
    assert s.exists(unique_org, "alpha")
    assert [x["space_id"] for x in s.list_spaces(unique_org)] == ["alpha"]


def test_same_slug_distinct_orgs_coexist(unique_org):
    # The primary key is (org_id, space_id): the same slug in two different orgs
    # never collides, but spaces do not leak across orgs.
    from knowledge.serve.orgs_store import OrgsStore

    s = _stores(unique_org)
    other_org = "test_org_" + uuid.uuid4().hex[:12]
    OrgsStore(s._conn).create_org(other_org, "Other", "s3cret", OWNER)

    s.create_space(unique_org, "alpha", "org-1 alpha")
    s.create_space(other_org, "alpha", "org-2 alpha")
    assert s.exists(unique_org, "alpha")
    assert s.exists(other_org, "alpha")
    assert s.list_spaces(unique_org)[0]["name"] == "org-1 alpha"
    assert s.list_spaces(other_org)[0]["name"] == "org-2 alpha"
