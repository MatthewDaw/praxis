"""``meta.finished_at`` is written by the SERVER, on every path that finishes a ticket.

It used to have two producers that disagreed on shape: the lease-release path
(``POST /requirements/{cid}/release``) wrote a fixed-format UTC ISO-8601 string, and
agent_factory's own ``_ticket_state.release`` wrote a bare ``time.time()`` float
through ``PATCH /candidates/{cid}``. One plan carried both. Nothing errored — but
``snapshots_finished_at_idx`` (migration 0013) is a TEXT expression index over the ISO
shape, so an epoch row sorts as text somewhere else entirely and silently drops out of
every range query using it. A short answer, not a failure.

These tests lock the contract that replaced it (:mod:`knowledge.finished_at`):

  * whichever path a ticket finishes through, ``finished_at`` parses as ISO-8601 AND
    matches the exact shape the index range-scans — proven by actually range-querying
    it through the index rather than by eyeballing the string;
  * a client-supplied ``finished_at`` never wins, on any path;
  * a ticket that regresses or yields loses it (it is not finished any more);
  * finishing twice keeps the LATEST completion.

Needs a Postgres DSN, like its sibling lease/index tests.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge import finished_at  # noqa: E402
from knowledge.knowledge_graph.knowledge_graph_variants import (  # noqa: E402
    postgres_vector_graph,
)
from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (  # noqa: E402
    PostgresVectorGraph,
)
from knowledge.knowledge_graph.write_policy.write_step_variants import (  # noqa: E402
    Deduper,
    Redactor,
)
from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder  # noqa: E402
from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"
PROJECT = "team-app"
SOURCE = f"prd-{PROJECT}"
TABLES = ("fact_edges", "facts", "snapshot_edges", "snapshots", "org_members", "orgs")

# A backdated value in the right shape — the interesting forgery, because it would
# survive any check that only validates format.
BACKDATED = "2001-01-01T00:00:00.000000+00:00"
EPOCH_SHAPED = "1785205768.1511981"


@pytest.fixture
def env(unique_org, monkeypatch):
    # The app builds its OWN graphs, so the embedder seam is swapped at module level —
    # otherwise the /insights ticket-upsert case reaches for a real OpenRouter key and
    # fails anywhere one is not configured (CI). The lease/patch routes touch meta only
    # and never embed, so this matters solely for the write path.
    monkeypatch.setattr(postgres_vector_graph, "OpenRouterEmbedder", FakeEmbedder)
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    for tbl in TABLES:
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", USER)
    client = TestClient(create_app(conn), headers={"X-Praxis-Org": org})
    graph = PostgresVectorGraph(
        conn, org, USER, embedder=FakeEmbedder(), recall_floor=-1.0,
        policy=[Redactor(), Deduper()],
    )
    yield client, graph, org, conn
    for tbl in TABLES:
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    conn.close()


def _ticket(graph, text="The home screen lists today's tasks.", **extra):
    return graph.write(
        text, state="active", category="requirement", source=SOURCE, **extra
    )


def _meta(graph, rid):
    return graph.get_fact(rid).meta or {}


# release_requirement refuses to finish a ticket nothing gates (an empty ``pinned_checks``
# would let it certify itself), so a ticket finished through that path carries one.
PINNED = [{"validation_id": "v1", "covers": ["c1"], "run": "true", "passed": True}]


def _finish_via_release(client, rid, owner="agent-a", body_extra=None):
    """Finish through the lease-release path (POST /requirements/{cid}/release)."""
    assert client.post(f"/requirements/{rid}/claim", json={"owner": owner}).status_code == 200
    res = client.post(
        f"/requirements/{rid}/release",
        json={"owner": owner, "state": "finished", **(body_extra or {})},
    )
    assert res.status_code == 200, res.text
    return res


def _finish_via_patch(client, rid, meta_extra=None):
    """Finish through the meta-merge path (_praxis.patch_meta -> PATCH /candidates)."""
    res = client.patch(
        f"/candidates/{rid}",
        json={"meta": {"build_state": "finished", **(meta_extra or {})}},
    )
    assert res.status_code == 200, res.text
    return res


def _indexed_range_hits(conn, org, start: datetime, end: datetime) -> list[str]:
    """Ids whose ``finished_at`` falls in ``[start, end]``, compared exactly the way
    ``snapshots_finished_at_idx`` does: as TEXT against two ISO-8601 bounds. A value
    in any other shape sorts outside these bounds and is simply not returned — which
    is the silent failure mode this whole contract exists to prevent."""
    rows = conn.execute(
        "SELECT id FROM facts "
        "WHERE org_id = %s AND meta ->> 'finished_at' BETWEEN %s AND %s",
        (org, finished_at.iso_utc(start), finished_at.iso_utc(end)),
    ).fetchall()
    return [r[0] for r in rows]


def _assert_indexable_now(conn, org, rid, meta):
    """The stamped value is ISO-8601, is the exact shape the index range-scans, is
    recent, and is actually FOUND by a range query over that expression."""
    stamped = meta.get("finished_at")
    assert stamped, "a finished ticket must carry finished_at"
    parsed = datetime.fromisoformat(stamped)      # ISO-8601, unambiguously
    assert parsed.tzinfo is not None
    assert finished_at.is_indexable(stamped), stamped
    now = datetime.now(timezone.utc)
    assert abs((now - parsed).total_seconds()) < 300, stamped
    window = (now - timedelta(minutes=10), now + timedelta(minutes=10))
    assert rid in _indexed_range_hits(conn, org, *window), (
        f"{stamped!r} did not fall inside its own range query — it will silently "
        f"drop out of every finished-by-date report"
    )


# --- every finish path produces an indexable timestamp ---------------------
def test_release_path_stamps_indexable_finished_at(env):
    client, graph, org, conn = env
    rid = _ticket(graph, meta={"requirement_id": "R1", "tags": ["auth"],
                               "pinned_checks": PINNED})

    _finish_via_release(client, rid)

    meta = _meta(graph, rid)
    assert meta["build_state"] == "finished"
    assert meta["requirement_id"] == "R1"          # untouched
    _assert_indexable_now(conn, org, rid, meta)


def test_patch_meta_path_stamps_indexable_finished_at(env):
    """The path agent_factory's release() takes: it sends build_state only, and the
    server dates it. This is the producer that used to write an epoch float."""
    client, graph, org, conn = env
    rid = _ticket(graph, meta={"requirement_id": "R2"})

    _finish_via_patch(client, rid)

    meta = _meta(graph, rid)
    assert meta["build_state"] == "finished"
    _assert_indexable_now(conn, org, rid, meta)


def test_both_finish_paths_agree_on_shape(env):
    """The two producers must be indistinguishable by shape — the drift that started
    this was two writers that each looked fine alone."""
    client, graph, _, _ = env
    via_release = _ticket(graph, meta={"pinned_checks": PINNED})
    via_patch = _ticket(graph, text="A second ticket.")

    _finish_via_release(client, via_release)
    _finish_via_patch(client, via_patch)

    for rid in (via_release, via_patch):
        assert finished_at.is_indexable(_meta(graph, rid)["finished_at"])


# --- a client-supplied value never wins ------------------------------------
def test_client_supplied_finished_at_is_ignored_on_patch(env):
    client, graph, org, conn = env
    rid = _ticket(graph)

    _finish_via_patch(client, rid, meta_extra={"finished_at": BACKDATED})

    meta = _meta(graph, rid)
    assert meta["finished_at"] != BACKDATED, "a caller must not be able to backdate a completion"
    _assert_indexable_now(conn, org, rid, meta)


def test_client_supplied_epoch_finished_at_is_ignored(env):
    """The exact legacy forgery: the epoch shape that broke the index."""
    client, graph, org, conn = env
    rid = _ticket(graph)

    _finish_via_patch(client, rid, meta_extra={"finished_at": EPOCH_SHAPED})

    _assert_indexable_now(conn, org, rid, _meta(graph, rid))


def test_client_cannot_stamp_finished_at_without_finishing(env):
    """No ``build_state`` transition, so no completion — the caller's value is dropped
    rather than quietly creating a finished_at on an unfinished ticket."""
    client, graph, _, _ = env
    rid = _ticket(graph)

    res = client.patch(f"/candidates/{rid}", json={"meta": {"finished_at": BACKDATED}})
    assert res.status_code == 200, res.text

    assert "finished_at" not in _meta(graph, rid)


def test_client_cannot_smuggle_finished_at_through_regress_detail(env):
    """``/requirements/regress`` merges caller-supplied meta (the WHY a ticket came
    back). It must not accept a completion timestamp along with it."""
    client, graph, _, _ = env
    rid = _ticket(graph)

    res = client.post(
        "/requirements/regress",
        json={
            "project": PROJECT,
            "ids": [rid],
            "detail": {rid: {"audit_disposition": "rebuild", "finished_at": BACKDATED}},
        },
    )
    assert res.status_code == 200, res.text

    meta = _meta(graph, rid)
    assert meta["build_state"] == "incomplete"
    assert meta["audit_disposition"] == "rebuild"   # the legitimate detail survives
    assert "finished_at" not in meta


def test_ticket_upsert_path_is_dated_by_the_server_too(env):
    """``/insights`` mints and re-files tickets, and a ticket carries ``build_state`` by
    definition — so it is a finish path, and the caller's value must not win there either."""
    client, graph, org, conn = env

    minted = client.post("/insights", json={
        "insight": "Sessions expire after 30 days.",
        "category": "requirement",
        "meta": {"requirement_id": "R9", "build_state": "finished",
                 "finished_at": BACKDATED},
    })
    assert minted.status_code == 200, minted.text
    rid = minted.json()["id"]

    meta = _meta(graph, rid)
    assert meta["build_state"] == "finished"
    assert meta["finished_at"] != BACKDATED
    _assert_indexable_now(conn, org, rid, meta)


def test_ticket_upsert_to_incomplete_clears_finished_at(env):
    client, graph, _, _ = env
    body = {
        "insight": "Sessions expire after 30 days.",
        "category": "requirement",
        "meta": {"requirement_id": "R10", "build_state": "finished"},
    }
    assert client.post("/insights", json=body).status_code == 200
    rid = client.post("/insights", json={
        **body, "meta": {"requirement_id": "R10", "build_state": "incomplete"},
    }).json()["id"]

    meta = _meta(graph, rid)
    assert meta["build_state"] == "incomplete"
    assert "finished_at" not in meta


# --- unfinishing clears it -------------------------------------------------
def test_regress_clears_finished_at(env):
    client, graph, _, _ = env
    rid = _ticket(graph)
    _finish_via_patch(client, rid)
    assert _meta(graph, rid)["finished_at"]

    res = client.post("/requirements/regress", json={"project": PROJECT, "ids": [rid]})
    assert res.status_code == 200, res.text

    meta = _meta(graph, rid)
    assert meta["build_state"] == "incomplete"
    assert "finished_at" not in meta, (
        "a regressed ticket that keeps its completion timestamp reads as done work "
        "it did not complete"
    )


def test_release_incomplete_clears_a_previous_finished_at(env):
    client, graph, _, _ = env
    rid = _ticket(graph)
    _finish_via_patch(client, rid)
    assert _meta(graph, rid)["finished_at"]

    assert client.post(f"/requirements/{rid}/claim", json={"owner": "agent-a"}).status_code == 200
    res = client.post(
        f"/requirements/{rid}/release", json={"owner": "agent-a", "state": "incomplete"}
    )
    assert res.status_code == 200, res.text

    meta = _meta(graph, rid)
    assert meta["build_state"] == "incomplete"
    assert "finished_at" not in meta


def test_patch_to_non_finished_state_clears_finished_at(env):
    client, graph, _, _ = env
    rid = _ticket(graph)
    _finish_via_patch(client, rid)

    res = client.patch(f"/candidates/{rid}", json={"meta": {"build_state": "blocked"}})
    assert res.status_code == 200, res.text

    assert "finished_at" not in _meta(graph, rid)


# --- re-finishing / unrelated writes ---------------------------------------
def test_finishing_twice_keeps_the_latest_timestamp(env):
    client, graph, _, _ = env
    rid = _ticket(graph)

    _finish_via_patch(client, rid)
    first = _meta(graph, rid)["finished_at"]
    time.sleep(0.01)
    _finish_via_patch(client, rid)
    second = _meta(graph, rid)["finished_at"]

    assert second > first, "a re-finish records the latest completion"
    assert finished_at.is_indexable(second)


def test_unrelated_write_does_not_move_a_completion(env):
    """A bookkeeping patch that does not touch ``build_state`` (clearing a run marker,
    recording an audit note) must leave the completion where it was."""
    client, graph, _, _ = env
    rid = _ticket(graph)
    _finish_via_patch(client, rid)
    stamped = _meta(graph, rid)["finished_at"]

    time.sleep(0.01)
    res = client.patch(f"/candidates/{rid}", json={"meta": {"run_owner": None}})
    assert res.status_code == 200, res.text

    assert _meta(graph, rid)["finished_at"] == stamped
