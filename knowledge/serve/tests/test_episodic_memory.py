"""Serve-level red-specs for episodic memory (H4) + query-time exclusion (H2).

These cover the behaviors that live ABOVE the knowledge_graph component layer — the
MCP/HTTP producer and the /context route — and so can't be exercised by component
evals. Like test_server.py they need a Postgres DSN AND an OPENROUTER_API_KEY (the
HTTP write path embeds for real).

The producer honors category+meta on /insights (routing episodes to the store-only
lane), and /context default-excludes category="episodic" with an include_episodic
override (H2) — across the live + mounted overlay union.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None or not os.getenv("OPENROUTER_API_KEY"),
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret) AND OPENROUTER_API_KEY",
)

USER = "dev-user"
_EPISODE = {
    "insight": "Chose reset-to-0 for the daily habit counter because the PRD was silent.",
    "category": "episodic",
    "meta": {"episode": {"decided_at": "2026-06-25T00:00:00Z", "outcome": "pending"}},
}
_SEMANTIC = "The daily habit counter resets to 0 at local midnight."
_QUERY = "How does the daily habit counter reset work?"


@pytest.fixture
def client(unique_org):
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    for tbl in ("fact_edges", "facts", "cached_facts", "org_members", "orgs"):
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", USER)
    app = create_app(conn)
    yield TestClient(app, headers={"X-Praxis-Org": org})
    for tbl in ("fact_edges", "facts", "cached_facts", "org_members", "orgs"):
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    conn.close()


def test_record_episode_via_http_stores_episodic(client):
    """The harness writes episodes over HTTP/MCP; the producer must persist a single
    episodic-category fact carrying the decision text whole and meta.episode intact."""
    res = client.post("/insights", json=_EPISODE)
    assert res.status_code == 200, res.text
    nodes = client.get("/graph", params={"state": "all"}).json()["graph"]["nodes"]
    episodic = [n for n in nodes if n.get("category") == "episodic"]
    assert len(episodic) == 1
    assert _EPISODE["insight"] in episodic[0]["label"]  # stored whole (graph node text = label)


def test_context_excludes_episodic_by_default(client):
    """/context must omit episodes by default and surface them only on opt-in."""
    client.post("/insights", json=_EPISODE)
    client.post("/insights", json={"insight": _SEMANTIC})
    default = client.get("/context", params={"query": _QUERY}).json()
    assert _EPISODE["insight"] not in (default.get("context") or "")
    assert _SEMANTIC in (default.get("context") or "")
    opted_in = client.get(
        "/context", params={"query": _QUERY, "include_episodic": "true"}
    ).json()
    assert _EPISODE["insight"] in (opted_in.get("context") or "")


def _episode(text):
    return {
        "insight": text,
        "category": "episodic",
        "meta": {"episode": {"outcome": "pending"}},
    }


def _episodic_texts(client):
    nodes = client.get("/graph", params={"state": "all"}).json()["graph"]["nodes"]
    return [n["label"] for n in nodes if n.get("category") == "episodic"]


def test_two_episodes_same_topic_survive_unmerged(client):
    """H4 store-only: two episodes on the same topic both persist, never deduped/merged."""
    first = "Chose Redis for the rate-limiter because it was already in the stack."
    second = "Chose Redis again for the session cache to keep ops surface small."
    assert client.post("/insights", json=_episode(first)).status_code == 200
    assert client.post("/insights", json=_episode(second)).status_code == 200
    texts = _episodic_texts(client)
    assert any(first in t for t in texts)
    assert any(second in t for t in texts)
    assert len(texts) == 2  # both rows kept, no merge


def test_contradicting_episode_does_not_supersede_earlier(client):
    """H4: a later contradicting decision never rejects/supersedes the earlier one —
    the decision timeline is append-only and immutable."""
    earlier = "Decided to store timestamps in UTC across all services."
    later = "Decided to store timestamps in local time, reversing the UTC decision."
    assert client.post("/insights", json=_episode(earlier)).status_code == 200
    assert client.post("/insights", json=_episode(later)).status_code == 200
    nodes = client.get("/graph", params={"state": "all"}).json()["graph"]["nodes"]
    episodic = [n for n in nodes if n.get("category") == "episodic"]
    assert len(episodic) == 2
    # The earlier decision is still active (not rejected/superseded).
    earlier_node = next(n for n in episodic if earlier in n["label"])
    assert earlier_node.get("state") == "active"


def test_context_excludes_episodic_from_mounted_overlay(client):
    """A mounted snapshot's episodes must also be excluded from /context (the exclude
    predicate must apply to the live+mounted UNION, not just the live branch)."""
    client.post("/insights", json=_EPISODE)
    assert client.post("/snapshots", json={"name": "snap-ep"}).status_code == 200
    client.post("/mounts", json={"owner": USER, "snapshot": "snap-ep"})
    ctx = client.get("/context", params={"query": _QUERY}).json()
    assert _EPISODE["insight"] not in (ctx.get("context") or "")


def test_episode_preserves_caller_defined_meta_keys(client):
    """A typed payload layered on top of an episode must survive the write.

    Regression: ``_record_episode`` destructured only alternatives/outcome/decided_at and
    ``graph.record_episode`` rebuilt ``meta.episode`` from just those, so ANY other key the
    caller sent was dropped — silently, with a 200 and ``retrievable: true``. That is how the
    agent-factory's signed-contract payload (kind/n_assertions/actions/signer, built by
    ``contract_signature.build_signed_payload``) vanished, which made the plan gate's
    ``R-CONTRACT-SIGNED`` rule unsatisfiable through the documented write path.
    """
    signed = {
        "kind": "contract-signed",
        "n_assertions": 37,
        "actions": {"cut": 0, "merged": 1, "added": 0},
        "signer": "evaluator",
    }
    res = client.post("/insights", json={
        "insight": "contract-signed: the evaluator reviewed and signed the assertion contract.",
        "category": "episodic",
        "meta": {"episode": {**signed, "outcome": "signed"}},
    })
    assert res.status_code == 200, res.text

    facts = client.get("/facts/by", params={"category": "episodic", "state": "any"}).json()
    ep = next(
        (f.get("meta") or {}).get("episode") or {}
        for f in facts["facts"]
        if ((f.get("meta") or {}).get("episode") or {}).get("kind") == "contract-signed"
    )
    # The caller's own keys round-trip verbatim...
    assert ep["signer"] == "evaluator"
    assert ep["n_assertions"] == 37
    assert ep["actions"] == {"cut": 0, "merged": 1, "added": 0}
    # ...alongside the canonical store-only fields the server still owns.
    assert ep["outcome"] == "signed"
    assert ep["decided_at"]


def test_episode_canonical_fields_win_over_caller_keys(client):
    """``extra`` must never let a caller spoof the server-owned canonical fields."""
    res = client.post("/insights", json={
        "insight": "Episode whose payload tries to override the canonical outcome.",
        "category": "episodic",
        "meta": {"episode": {"kind": "probe", "outcome": "succeeded",
                             "alternatives": ["a"], "decided_at": "2020-01-01T00:00:00Z"}},
    })
    assert res.status_code == 200, res.text
    facts = client.get("/facts/by", params={"category": "episodic", "state": "any"}).json()
    ep = next(
        (f.get("meta") or {}).get("episode") or {}
        for f in facts["facts"]
        if ((f.get("meta") or {}).get("episode") or {}).get("kind") == "probe"
    )
    # The caller's values for the canonical trio are honored through the NAMED params, not
    # smuggled in via extra — so they land exactly once, with no duplicate/conflicting copy.
    assert ep["outcome"] == "succeeded"
    assert ep["alternatives"] == ["a"]
    assert ep["decided_at"] == "2020-01-01T00:00:00Z"
    assert ep["kind"] == "probe"
