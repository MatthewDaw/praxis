"""Reads must answer about the graph the caller addressed, and say enough to audit it.

Three targeting defects this pins, all in the same family — a read that quietly answers about a
DIFFERENT store, or answers about the right one while hiding the fields that reveal corruption:

* ``GET /candidates`` (the listing) ignored the ``(space, snapshot)`` target while every by-id
  sibling honored it, so an agent told to "find the id via ``praxis_list_graph``" before repairing
  a snapshot-resident fact was handed ids from its own working memory;
* ``GET /spaces/{space}/snapshots/{snapshot}/facts`` dropped ``meta``/``category``, which is where
  ``requirement_id`` / ``build_state`` / ``check_id`` live — so an auditor built on the only
  target-free snapshot read was structurally unable to SEE a merged, mis-stated or NULL-category
  ticket;
* a LONE ``X-Praxis-Space`` on a live read is dropped by design (working memory keys on
  ``(org, principal.sub)``); that is pinned here as a CONTRACT, because it is the reason live
  ``requirement_id`` values collide across projects and it must not drift silently either way.

Postgres-gated (skips without a DSN). No LLM: facts are seeded straight into the tables and the
routes under test are pure reads.
"""

from __future__ import annotations

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"  # the PRAXIS_AUTH_DISABLED dev principal sub
PROJECT = "targetproj"
SNAPSHOT = f"prd-{PROJECT}"
HEADERS = {"X-Praxis-Space": PROJECT, "X-Praxis-Snapshot": SNAPSHOT}

SNAPSHOT_TEXT = "R12 the importer retries a failed row three times"
WORKING_TEXT = "a note that only ever lived in working memory"


@pytest.fixture
def env(unique_org, monkeypatch):
    """(client, conn, org) over a fresh org holding one working-memory fact and one snapshot fact."""
    monkeypatch.setenv("PRAXIS_AUTH_DISABLED", "1")
    from fastapi.testclient import TestClient

    from knowledge.serve.app import create_app
    from knowledge.serve.orgs_store import OrgsStore
    from knowledge.serve.spaces_store import SpacesStore

    org = unique_org
    tables = ("fact_edges", "facts", "snapshot_edges", "snapshots",
              "org_members", "orgs", "spaces")

    db.bootstrap()
    conn = db.connect()

    def _clean() -> None:
        for table in tables:
            conn.execute(f"DELETE FROM {table} WHERE org_id = %s", (org,))

    _clean()
    OrgsStore(conn).create_org(org, org, "pw", USER)
    SpacesStore(conn).create_space(org, PROJECT, PROJECT)
    client = TestClient(create_app(conn), headers={"X-Praxis-Org": org})
    yield client, conn, org
    _clean()
    conn.close()


def _seed_snapshot_fact(conn, org, fid, text, *, scope=None, category=None, meta=None):
    """Insert a fact row directly into the org-shared ``snapshots`` table."""
    import json

    conn.execute(
        """
        INSERT INTO snapshots (id, org_id, text, scope, state, space, snapshot, category, meta)
        VALUES (%s, %s, %s, %s, 'active', %s, %s, %s, %s)
        """,
        (fid, org, text, scope, PROJECT, SNAPSHOT, category, json.dumps(meta or {})),
    )


def _seed_working_fact(conn, org, fid, text):
    """Insert a fact row into the caller's private working memory (``facts``)."""
    conn.execute(
        """
        INSERT INTO facts (id, org_id, user_id, text, state)
        VALUES (%s, %s, %s, %s, 'active')
        """,
        (fid, org, USER, text),
    )


# --- GET /candidates honors the (space, snapshot) target --------------------

def test_candidate_listing_targets_the_snapshot_when_addressed(env):
    """With both headers the listing must show the SNAPSHOT — the graph the by-id siblings edit."""
    client, conn, org = env
    _seed_working_fact(conn, org, "w1", WORKING_TEXT)
    _seed_snapshot_fact(conn, org, "s1", SNAPSHOT_TEXT)

    rows = client.get("/candidates", headers=HEADERS).json()
    assert [r["id"] for r in rows] == ["s1"]

    # The listing and the by-id read must agree, since the whole point of the listing is to hand
    # an agent an id it then acts on through those routes.
    assert client.get("/candidates/s1", headers=HEADERS).status_code == 200


def test_candidate_listing_without_headers_is_unchanged(env):
    """No target -> working memory, exactly as before. Existing callers (the dashboard sends a
    lone space header, ``praxis_list_graph`` sends none) can never reach the new branch."""
    client, conn, org = env
    _seed_working_fact(conn, org, "w1", WORKING_TEXT)
    _seed_snapshot_fact(conn, org, "s1", SNAPSHOT_TEXT)

    assert [r["id"] for r in client.get("/candidates").json()] == ["w1"]
    lone_space = client.get("/candidates", headers={"X-Praxis-Space": PROJECT}).json()
    assert [r["id"] for r in lone_space] == ["w1"]


def test_candidate_listing_rejects_an_unknown_space(env):
    """Same 404 the by-id siblings give: a mistyped space must not silently degrade to working
    memory, which is the failure mode this whole family of bugs is made of."""
    client, conn, org = env
    _seed_working_fact(conn, org, "w1", WORKING_TEXT)
    resp = client.get(
        "/candidates",
        headers={"X-Praxis-Space": "does-not-exist-xyz", "X-Praxis-Snapshot": SNAPSHOT},
    )
    assert resp.status_code == 404


# --- the snapshot browse view carries the identity keys ---------------------

def test_snapshot_browse_exposes_meta_and_category(env):
    """An auditor on this endpoint must be able to see a ticket's identity and lifecycle."""
    client, conn, org = env
    _seed_snapshot_fact(
        conn, org, "s1", SNAPSHOT_TEXT,
        scope="build",
        category="requirement",
        meta={"requirement_id": "R12", "build_state": "finished"},
    )
    body = client.get(f"/spaces/{PROJECT}/snapshots/{SNAPSHOT}/facts").json()
    fact = body["groups"][0]["facts"][0]
    assert fact["category"] == "requirement"
    assert fact["meta"] == {"requirement_id": "R12", "build_state": "finished"}
    # The pre-existing keys are untouched — this widened the brief, it did not reshape it.
    assert fact["id"] == "s1"
    assert fact["text"] == SNAPSHOT_TEXT
    assert fact["scope"] == "build"
    assert fact["state"] == "active"


def test_snapshot_browse_reports_a_null_category_ticket(env):
    """The signature of a ticket that landed on the wrong write path: identity keys present,
    ``category`` NULL. Before ``meta`` was in the brief this fact was indistinguishable from any
    other prose in the snapshot, so no tool built here could report the corruption."""
    client, conn, org = env
    _seed_snapshot_fact(
        conn, org, "s1", SNAPSHOT_TEXT,
        meta={"requirement_id": "R12", "build_state": "incomplete"},
    )
    fact = client.get(
        f"/spaces/{PROJECT}/snapshots/{SNAPSHOT}/facts"
    ).json()["groups"][0]["facts"][0]
    assert fact["category"] is None
    assert fact["meta"]["requirement_id"] == "R12"


def test_snapshot_browse_gives_an_empty_meta_not_null(env):
    """A fact with no metadata reports ``{}``, so a consumer can iterate keys unconditionally."""
    client, conn, org = env
    _seed_snapshot_fact(conn, org, "s1", SNAPSHOT_TEXT)
    fact = client.get(
        f"/spaces/{PROJECT}/snapshots/{SNAPSHOT}/facts"
    ).json()["groups"][0]["facts"][0]
    assert fact["meta"] == {}
    assert fact["category"] is None


# --- the live-read scoping contract ----------------------------------------

def test_live_facts_by_ignores_a_lone_space_header(env):
    """PINNED CONTRACT, not an endorsement: working memory is one pool per (org, principal.sub),
    so a live ``/facts/by`` read is NOT space-scoped and a lone ``X-Praxis-Space`` — including a
    bogus one — changes nothing. It is accepted rather than rejected because the dashboard sends
    the active space id on every request by contract; a 400 here would break every live read.

    The consequence is the one to remember: several projects' tickets share this pool, so
    ``meta.requirement_id`` is only unique WITHIN a plan snapshot. Anything keyed on it must
    address the snapshot (both headers), which the assertions below hold the line on.
    """
    client, conn, org = env
    _seed_working_fact(conn, org, "w1", WORKING_TEXT)
    _seed_snapshot_fact(conn, org, "s1", SNAPSHOT_TEXT)

    bare = client.get("/facts/by").json()["facts"]
    bogus = client.get("/facts/by", headers={"X-Praxis-Space": "does-not-exist-xyz"}).json()["facts"]
    assert [f["id"] for f in bare] == ["w1"]
    assert [f["id"] for f in bogus] == [f["id"] for f in bare]

    # Both headers, and only both, reach the snapshot.
    targeted = client.get("/facts/by", headers=HEADERS).json()["facts"]
    assert [f["id"] for f in targeted] == ["s1"]


def test_graph_clear_refuses_a_snapshot_target_instead_of_wiping_working_memory(env):
    """A destructive route may not silently redirect the caller's blast radius.

    ``POST /graph/clear`` can only truncate working memory. It used to accept the header pair
    and ignore it, so the plan_repro eval passed a space believing it cleared "the eval space"
    and instead wiped the caller's own working memory — irreversibly, on a shared key. The pair
    is a deliberate act everywhere else in this API, so its presence here means the caller is
    wrong about what is about to be destroyed: refuse, and name the verb that does the job.
    """
    client, conn, org = env
    _seed_working_fact(conn, org, "w1", WORKING_TEXT)
    _seed_snapshot_fact(conn, org, "s1", SNAPSHOT_TEXT)

    resp = client.post("/graph/clear", headers=HEADERS)
    assert resp.status_code == 400, resp.text
    assert "DELETE /snapshots" in resp.json()["detail"]
    assert [r["id"] for r in client.get("/candidates").json()] == ["w1"], "nothing may be wiped"
    assert [r["id"] for r in client.get("/candidates", headers=HEADERS).json()] == ["s1"]


def test_graph_clear_still_works_bare_and_with_a_lone_space_header(env):
    """The deliberate call still clears working memory, and only working memory.

    A lone space header must keep working: the dashboard's "clear graph" button sends the
    active space id on every request by contract, and it has always meant working memory here.
    """
    client, conn, org = env
    _seed_working_fact(conn, org, "w1", WORKING_TEXT)
    _seed_snapshot_fact(conn, org, "s1", SNAPSHOT_TEXT)

    resp = client.post("/graph/clear", headers={"X-Praxis-Space": PROJECT})
    assert resp.status_code == 200, resp.text
    assert resp.json()["cleared"] == 1
    assert client.get("/candidates").json() == []
    # The org-shared snapshot is untouched — the wipe never reaches it.
    assert [r["id"] for r in client.get("/candidates", headers=HEADERS).json()] == ["s1"]
