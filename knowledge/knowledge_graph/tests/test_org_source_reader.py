"""Integration tests for OrgSourceReader (the snapshot-browsing read path).

Skipped unless a database is reachable (PRAXIS_DB_URL or a resolvable Secrets
Manager DSN). Each test uses a unique org_id so runs never collide. Rows are
inserted raw (no embedder needed) since the reader only does scoped SELECTs.

Sources are org-shared snapshots only: every read goes through a
``(space, snapshot)`` key against ``snapshots``/``snapshot_edges`` — there is no
per-user axis (see specs/005-praxis-tenancy-redesign/design.md §3.1, §4.5).
"""

from __future__ import annotations

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

SPACE = "sp1"
SNAP = "s1"


@pytest.fixture
def unique_org(request):
    # Unique per test node so reruns and parallel tenants never collide.
    return "test_" + request.node.name


def _insert_fact(conn, org, fid, text, *, space=SPACE, snapshot=SNAP, scope=None, state="active"):
    conn.execute(
        """
        INSERT INTO snapshots (id, org_id, space, snapshot, text, scope, state)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (fid, org, space, snapshot, text, scope, state),
    )


def _insert_edge(conn, org, src, dst, *, space=SPACE, snapshot=SNAP, kind="contradiction"):
    conn.execute(
        """
        INSERT INTO snapshot_edges (org_id, space, snapshot, src_id, dst_id, kind)
        VALUES (%s,%s,%s,%s,%s,%s)
        """,
        (org, space, snapshot, src, dst, kind),
    )


def _cleanup(conn, org):
    conn.execute("DELETE FROM snapshot_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM snapshots WHERE org_id = %s", (org,))


@pytest.fixture
def conn(unique_org):
    c = db.connect()
    _cleanup(c, unique_org)
    yield c
    _cleanup(c, unique_org)


def _reader(conn, org, *, space=SPACE, snapshot=SNAP):
    from knowledge.knowledge_graph.knowledge_graph_variants.org_source_reader import (
        OrgSourceReader,
    )

    return OrgSourceReader(conn, org, space=space, snapshot=snapshot)


def test_reads_snapshot_facts(conn, unique_org):
    _insert_fact(conn, unique_org, "b1", "B snapshot skill")
    facts = _reader(conn, unique_org).all_facts()
    assert [f.text for f in facts] == ["B snapshot skill"]


def test_org_isolation_never_returns_other_org_rows(conn, unique_org):
    other_org = unique_org + "_other"
    _cleanup(conn, other_org)
    try:
        _insert_fact(conn, unique_org, "b1", "in org X")
        _insert_fact(conn, other_org, "y1", "in org Y")
        facts = _reader(conn, unique_org).all_facts()
        assert [f.text for f in facts] == ["in org X"]
    finally:
        _cleanup(conn, other_org)


def test_space_isolation_only_target_space(conn, unique_org):
    # Snapshots are keyed by (space, snapshot): a reader bound to one space never
    # sees another space's rows (the redesign's browse axis is space, not member).
    _insert_fact(conn, unique_org, "b1", "space-B fact", space="spB")
    _insert_fact(conn, unique_org, "c1", "space-C fact", space="spC")
    facts = _reader(conn, unique_org, space="spB").all_facts()
    assert [f.text for f in facts] == ["space-B fact"]


def test_all_facts_filters_by_state(conn, unique_org):
    _insert_fact(conn, unique_org, "b1", "active fact", state="active")
    _insert_fact(conn, unique_org, "b2", "proposed fact", state="proposed")
    texts = {f.text for f in _reader(conn, unique_org).all_facts(state="active")}
    assert texts == {"active fact"}


def test_snapshot_name_scopes_reads(conn, unique_org):
    # Same id under two snapshot names, with different text — reads are scoped.
    _insert_fact(conn, unique_org, "b1", "s1 text", snapshot="s1")
    _insert_fact(conn, unique_org, "b1", "s2 text", snapshot="s2")
    s1 = _reader(conn, unique_org, snapshot="s1").all_facts()
    s2 = _reader(conn, unique_org, snapshot="s2").all_facts()
    assert [f.text for f in s1] == ["s1 text"]
    assert [f.text for f in s2] == ["s2 text"]


def test_get_facts_filters_by_id(conn, unique_org):
    _insert_fact(conn, unique_org, "b1", "fact one")
    _insert_fact(conn, unique_org, "b2", "fact two")
    _insert_fact(conn, unique_org, "b3", "fact three")
    got = _reader(conn, unique_org).get_facts(["b1", "b3"])
    assert {f.text for f in got} == {"fact one", "fact three"}


def test_get_facts_empty_list_is_empty(conn, unique_org):
    _insert_fact(conn, unique_org, "b1", "fact one")
    assert _reader(conn, unique_org).get_facts([]) == []


def test_edges_among_requires_both_endpoints_selected(conn, unique_org):
    for fid, txt in [("b1", "one"), ("b2", "two"), ("b3", "three")]:
        _insert_fact(conn, unique_org, fid, txt)
    _insert_edge(conn, unique_org, "b1", "b2")  # both in selection
    _insert_edge(conn, unique_org, "b2", "b3")  # b3 outside selection
    edges = _reader(conn, unique_org).edges_among(["b1", "b2"])
    assert edges == [("b1", "b2", "contradiction")]
