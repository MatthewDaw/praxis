"""Integration tests for the destructive deletes + the raw insert fast lane.

Three features share this file because they share a test harness (TestClient over
``create_app(conn)`` with auth bypassed via conftest -> dev principal sub
``dev-user``):

* DELETE /spaces/{id}  — tears down one of a login's private working graphs and
  hard-purges its facts so a re-created same-id space starts EMPTY (not orphaned).
* DELETE /orgs/{id}    — owner-only org wipe: purges every member's facts + the
  org's api keys, then drops the org (cascading members + spaces).
* POST /insights/batch with ``raw=true`` — the trusted fast lane that skips the
  Deduper + LLM conflict pipeline, so near-duplicate items all land as distinct
  facts instead of being collapsed.

Like test_spaces.py, the server writes through the facade's REAL embedder (no fakes
injected by create_app), so POST /insights embeds for real — these tests need both
a Postgres DSN and an OPENROUTER_API_KEY. (The raw path skips the LLM *conflict*
steps but still embeds, which is exactly the throughput win being exercised.)
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

# Load the repo-root .env so PRAXIS_DB_URL / OPENROUTER_API_KEY resolve at import
# time (the module-level skipif below reads them).
load_dotenv()

from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None or not os.getenv("OPENROUTER_API_KEY"),
    reason=(
        "needs a Postgres DSN (PRAXIS_DB_URL / AWS secret) AND OPENROUTER_API_KEY — "
        "the HTTP write path embeds insights via the real embedder"
    ),
)

USER = "dev-user"  # the PRAXIS_AUTH_DISABLED dev principal sub


def _wipe(conn, org):
    """Remove every trace of ``org`` so a test starts (and ends) clean.

    Covers the whole facts spine plus the org-scoped storage a delete is supposed
    to purge (``api_keys``/``mounted_snapshots``) — so teardown leaves nothing
    even when a test under exercise FAILS before its own delete runs.
    """
    conn.execute("DELETE FROM fact_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM cached_fact_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM cached_facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM mounted_snapshots WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM api_keys WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM spaces WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))


def _fact_count(conn, org, user_id):
    return conn.execute(
        "SELECT count(*) FROM facts WHERE org_id = %s AND user_id = %s",
        (org, user_id),
    ).fetchone()[0]


@pytest.fixture
def env():
    """A connection + app, with a factory for throwaway orgs (auto-cleaned).

    ``make_org(owner=...)`` creates a fresh, uniquely-named org owned by ``owner``
    (default the dev principal) and registers it for teardown; ``client_for(org)``
    returns a TestClient that sends that org in ``X-Praxis-Org``. Several tests
    need MORE than one org (e.g. a non-owner case where the dev principal is only a
    plain member), so the org is not baked into a single fixture value.
    """
    db.bootstrap()
    conn = db.connect()
    created: list[str] = []

    def make_org(owner: str = USER, org_id: str | None = None) -> str:
        org_id = org_id or ("test_sod_" + uuid.uuid4().hex[:12])
        _wipe(conn, org_id)
        OrgsStore(conn).create_org(org_id, org_id, "pw", owner)
        created.append(org_id)
        return org_id

    app = create_app(conn)

    def client_for(org_id: str) -> TestClient:
        return TestClient(app, headers={"X-Praxis-Org": org_id})

    yield SimpleNamespace(
        conn=conn, app=app, make_org=make_org, client_for=client_for, created=created
    )

    for org_id in created:
        _wipe(conn, org_id)
    conn.close()


def _candidate_texts(client, *, space=None):
    headers = {"X-Praxis-Space": space} if space is not None else {}
    return {c["content"] for c in client.get("/candidates", headers=headers).json()}


# --- (1) DELETE A SPACE ----------------------------------------------------
def test_delete_space_unlists_and_purges_graph(env):
    """A deleted space stops listing AND a re-created same-id space starts empty:
    the facts under ``<sub>::space:<id>`` are hard-purged, not orphaned."""
    org = env.make_org()
    client = env.client_for(org)
    assert client.post("/spaces", json={"spaceId": "alpha"}).status_code == 200

    fact = "the alpha space deploy target is the zebra cluster"
    assert client.post(
        "/insights", json={"insight": fact}, headers={"X-Praxis-Space": "alpha"}
    ).status_code == 200
    assert fact in _candidate_texts(client, space="alpha")
    space_uid = f"{USER}::space:alpha"
    assert _fact_count(env.conn, org, space_uid) >= 1

    # Delete the space.
    res = client.delete("/spaces/alpha")
    assert res.status_code == 200, res.text
    assert res.json() == {"deleted": "alpha"}

    # (a) it no longer lists.
    spaces = client.get("/spaces").json()["spaces"]
    assert "alpha" not in [s["space_id"] for s in spaces]
    # The graph rows are gone at the DB level.
    assert _fact_count(env.conn, org, space_uid) == 0

    # (b) re-creating a same-id space starts EMPTY (purged, not resurrected).
    assert client.post("/spaces", json={"spaceId": "alpha"}).status_code == 200
    assert _candidate_texts(client, space="alpha") == set()


def test_delete_space_leaves_default_graph_intact(env):
    """Deleting a named space never touches the login's bare default graph (a
    different ``user_id``)."""
    org = env.make_org()
    client = env.client_for(org)
    client.post("/spaces", json={"spaceId": "alpha"})

    default_fact = "the default graph deploy target is the lion cluster"
    space_fact = "the alpha space deploy target is the zebra cluster"
    client.post("/insights", json={"insight": default_fact})
    client.post(
        "/insights", json={"insight": space_fact}, headers={"X-Praxis-Space": "alpha"}
    )

    assert client.delete("/spaces/alpha").status_code == 200
    # Default graph survives untouched.
    assert default_fact in _candidate_texts(client)
    assert _fact_count(env.conn, org, USER) >= 1


def test_delete_unknown_space_is_404(env):
    org = env.make_org()
    client = env.client_for(org)
    assert client.delete("/spaces/ghost").status_code == 404


# --- (2) DELETE AN ORG (owner-only) ----------------------------------------
def test_delete_org_non_owner_member_forbidden(env):
    """A plain member (not the owner) gets 403 and the org survives."""
    org = env.make_org(owner="owner-x")
    # The dev principal joins as a plain member (role=member, not owner).
    OrgsStore(env.conn).join_org(org, "pw", USER)
    assert OrgsStore(env.conn).is_member(org, USER)
    assert not OrgsStore(env.conn).is_owner(org, USER)

    res = env.client_for(org).delete(f"/orgs/{org}")
    assert res.status_code == 403, res.text
    # The org row is still there.
    assert env.conn.execute(
        "SELECT 1 FROM orgs WHERE org_id = %s", (org,)
    ).fetchone() is not None


def test_delete_org_owner_purges_facts_keys_and_membership(env):
    """The owner succeeds; afterwards the org is gone from /me and its facts +
    api keys are purged from storage."""
    org = env.make_org()  # dev principal is owner
    client = env.client_for(org)

    assert client.post(
        "/insights", json={"insight": "a fact that belongs to this doomed org"}
    ).status_code == 200
    assert client.post("/apikeys", json={"label": "k"}).status_code == 200
    # Preconditions: there IS data to purge.
    assert _fact_count(env.conn, org, USER) >= 1
    assert env.conn.execute(
        "SELECT count(*) FROM api_keys WHERE org_id = %s", (org,)
    ).fetchone()[0] >= 1

    res = client.delete(f"/orgs/{org}")
    assert res.status_code == 200, res.text
    assert res.json() == {"deleted": org}

    # Gone from /me (membership cascaded away).
    me = client.get("/me").json()
    assert org not in [o["org_id"] for o in me["orgs"]]
    # Storage purged: facts, api keys, and the org row itself.
    assert _fact_count(env.conn, org, USER) == 0
    assert env.conn.execute(
        "SELECT count(*) FROM api_keys WHERE org_id = %s", (org,)
    ).fetchone()[0] == 0
    assert env.conn.execute(
        "SELECT 1 FROM orgs WHERE org_id = %s", (org,)
    ).fetchone() is None


def test_delete_unknown_org_is_404(env):
    """A non-member can't tell an org apart from a non-existent one: both 404."""
    ghost = "test_sod_ghost_" + uuid.uuid4().hex[:12]
    res = env.client_for(ghost).delete(f"/orgs/{ghost}")
    assert res.status_code == 404


# --- (3) RAW INSERT flag ---------------------------------------------------
def test_raw_batch_keeps_all_near_duplicates(env):
    """raw=true skips the Deduper/LLM-conflict pipeline, so near-duplicate items
    that the normal path would collapse all land as DISTINCT facts."""
    org = env.make_org()
    client = env.client_for(org)

    # Three near-duplicate statements of the same fact: the Deduper/conflict path
    # would fold these into one, the raw lane keeps all three.
    texts = [
        "the primary deploy target is the zebra cluster",
        "the primary deploy target is the zebra cluster.",
        "primary deploy target: the zebra cluster",
    ]
    res = client.post(
        "/insights/batch",
        json={"insights": [{"insight": t} for t in texts], "raw": True},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["count"] == 3
    assert all(r["ok"] for r in data["results"])
    assert all(r.get("retrievable") for r in data["results"])

    # No collapsing: one stored fact per input item.
    assert _fact_count(env.conn, org, USER) == 3
