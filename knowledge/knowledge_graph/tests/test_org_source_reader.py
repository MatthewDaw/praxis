"""Integration tests for OrgSourceReader (the skill-sharing read path).

Skipped unless a database is reachable (PRAXIS_DB_URL or a resolvable Secrets
Manager DSN). Each test uses a unique org_id so runs never collide. Rows are
inserted raw (no embedder needed) since the reader only does scoped SELECTs.

Sources are snapshots only: every read goes through a ``snapshot:<name>``
cache_key against ``cached_facts``/``cached_fact_edges``.
"""

from __future__ import annotations

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

KEY = "snapshot:s1"


@pytest.fixture
def unique_org(request):
    # Unique per test node so reruns and parallel tenants never collide.
    return "test_" + request.node.name


def _insert_fact(conn, org, user, fid, text, *, key=KEY, scope=None, state="active"):
    conn.execute(
        """
        INSERT INTO cached_facts (id, org_id, user_id, cache_key, text, scope, state)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (fid, org, user, key, text, scope, state),
    )


def _insert_edge(conn, org, user, src, dst, *, key=KEY, kind="contradiction"):
    conn.execute(
        """
        INSERT INTO cached_fact_edges (org_id, user_id, cache_key, src_id, dst_id, kind)
        VALUES (%s,%s,%s,%s,%s,%s)
        """,
        (org, user, key, src, dst, kind),
    )


def _cleanup(conn, org):
    conn.execute("DELETE FROM cached_fact_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM cached_facts WHERE org_id = %s", (org,))


@pytest.fixture
def conn(unique_org):
    c = db.connect()
    _cleanup(c, unique_org)
    yield c
    _cleanup(c, unique_org)


def _reader(conn, org, user, *, cache_key=KEY):
    from knowledge.knowledge_graph.knowledge_graph_variants.org_source_reader import (
        OrgSourceReader,
    )

    return OrgSourceReader(conn, org, user, cache_key=cache_key)


def test_reads_member_snapshot_facts(conn, unique_org):
    _insert_fact(conn, unique_org, "userB", "b1", "B snapshot skill")
    facts = _reader(conn, unique_org, "userB").all_facts()
    assert [f.text for f in facts] == ["B snapshot skill"]


def test_org_isolation_never_returns_other_org_rows(conn, unique_org):
    other_org = unique_org + "_other"
    _cleanup(conn, other_org)
    try:
        _insert_fact(conn, unique_org, "userB", "b1", "in org X")
        _insert_fact(conn, other_org, "userB", "y1", "in org Y")
        facts = _reader(conn, unique_org, "userB").all_facts()
        assert [f.text for f in facts] == ["in org X"]
    finally:
        _cleanup(conn, other_org)


def test_user_isolation_only_target_user(conn, unique_org):
    _insert_fact(conn, unique_org, "userB", "b1", "B fact")
    _insert_fact(conn, unique_org, "userC", "c1", "C fact")
    facts = _reader(conn, unique_org, "userB").all_facts()
    assert [f.text for f in facts] == ["B fact"]


def test_all_facts_filters_by_state(conn, unique_org):
    _insert_fact(conn, unique_org, "userB", "b1", "active fact", state="active")
    _insert_fact(conn, unique_org, "userB", "b2", "proposed fact", state="proposed")
    texts = {f.text for f in _reader(conn, unique_org, "userB").all_facts(state="active")}
    assert texts == {"active fact"}


def test_snapshot_key_scopes_reads(conn, unique_org):
    # Same id under two snapshot keys, with different text — reads are scoped.
    _insert_fact(conn, unique_org, "userB", "b1", "s1 text", key="snapshot:s1")
    _insert_fact(conn, unique_org, "userB", "b1", "s2 text", key="snapshot:s2")
    s1 = _reader(conn, unique_org, "userB", cache_key="snapshot:s1").all_facts()
    s2 = _reader(conn, unique_org, "userB", cache_key="snapshot:s2").all_facts()
    assert [f.text for f in s1] == ["s1 text"]
    assert [f.text for f in s2] == ["s2 text"]


def test_get_facts_filters_by_id(conn, unique_org):
    _insert_fact(conn, unique_org, "userB", "b1", "fact one")
    _insert_fact(conn, unique_org, "userB", "b2", "fact two")
    _insert_fact(conn, unique_org, "userB", "b3", "fact three")
    got = _reader(conn, unique_org, "userB").get_facts(["b1", "b3"])
    assert {f.text for f in got} == {"fact one", "fact three"}


def test_get_facts_empty_list_is_empty(conn, unique_org):
    _insert_fact(conn, unique_org, "userB", "b1", "fact one")
    assert _reader(conn, unique_org, "userB").get_facts([]) == []


def test_edges_among_requires_both_endpoints_selected(conn, unique_org):
    for fid, txt in [("b1", "one"), ("b2", "two"), ("b3", "three")]:
        _insert_fact(conn, unique_org, "userB", fid, txt)
    _insert_edge(conn, unique_org, "userB", "b1", "b2")  # both in selection
    _insert_edge(conn, unique_org, "userB", "b2", "b3")  # b3 outside selection
    edges = _reader(conn, unique_org, "userB").edges_among(["b1", "b2"])
    assert edges == [("b1", "b2", "contradiction")]
