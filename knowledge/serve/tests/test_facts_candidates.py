"""Unit tests for the FactsCandidates facade over the facts spine.

These build the facade directly with a deterministic FakeEmbedder and a no-LLM
write policy ([Redactor(), Deduper()]), so they need a Postgres DSN but make no
network/LLM calls. Each test uses a unique throwaway org so runs never collide.
"""

from __future__ import annotations

import pytest

from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder
from knowledge.serve import db
from knowledge.serve.facts_candidates import FactsCandidates, PromotionError

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"


@pytest.fixture
def facade(unique_org):
    """A FactsCandidates bound to a fresh throwaway tenant (no LLM, fake embed)."""
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    # Clean any prior run so the tenant starts empty and reruns stay isolated.
    conn.execute("DELETE FROM fact_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM facts WHERE org_id = %s", (org,))
    f = FactsCandidates(
        conn, org, USER, embedder=FakeEmbedder(), policy=[Redactor(), Deduper()]
    )
    yield f
    conn.execute("DELETE FROM fact_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM facts WHERE org_id = %s", (org,))
    conn.close()


def test_create_get_list_round_trip(facade):
    created = facade.create({"title": "Use uv", "content": "Use uv, not pip, in this repo."})
    cid = created["id"]
    assert created["state"] == "proposed"
    assert created["title"] == "Use uv"

    got = facade.get(cid)
    assert got is not None and got["id"] == cid
    assert got["content"] == "Use uv, not pip, in this repo."

    assert any(c["id"] == cid for c in facade.list())
    # State filter: proposed includes it, active does not.
    assert any(c["id"] == cid for c in facade.list("proposed"))
    assert not any(c["id"] == cid for c in facade.list("active"))


def test_get_unknown_returns_none(facade):
    assert facade.get("does-not-exist") is None


def test_promote_advances_proposed_to_active(facade):
    cid = facade.create({"title": "T", "content": "Prefer pytest over unittest."})["id"]
    promoted = facade.promote(cid)
    assert promoted["state"] == "active"
    assert facade.get(cid)["state"] == "active"


def test_promote_from_terminal_state_raises(facade):
    cid = facade.create({"title": "T", "content": "Deploy via CDK."})["id"]
    facade.promote(cid)  # proposed -> active (terminal)
    with pytest.raises(PromotionError):
        facade.promote(cid)


def test_promote_unknown_raises_keyerror(facade):
    with pytest.raises(KeyError):
        facade.promote("nope")


def test_reject_decays(facade):
    cid = facade.create({"title": "T", "content": "Some noisy candidate text."})["id"]
    rejected = facade.reject(cid, reason="noise")
    assert rejected["state"] == "decayed"
    assert facade.get(cid)["state"] == "decayed"


def test_update_changes_title_and_content(facade):
    cid = facade.create({"title": "Old", "content": "Original content."})["id"]
    updated = facade.update(cid, {"title": "New", "content": "Edited content."})
    assert updated["title"] == "New"
    assert updated["content"] == "Edited content."
    assert facade.get(cid)["content"] == "Edited content."


def test_delete_then_get_and_keyerror(facade):
    cid = facade.create({"title": "T", "content": "Disposable candidate."})["id"]
    facade.delete(cid)
    assert facade.get(cid) is None
    with pytest.raises(KeyError):
        facade.delete(cid)


def test_contradiction_edge_surfaces_pair_and_resolves(facade):
    a = facade.create({"title": "A", "content": "Use tabs for indentation."})["id"]
    b = facade.create({"title": "B", "content": "Use spaces for indentation."})["id"]
    # Persist a contradiction edge directly on the spine (the policy here is
    # no-LLM, so we wire the edge ourselves to exercise the pair path).
    facade.graph.add_edge(a, b, "contradiction")

    pairs = facade.contradictions()
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair["id"] == f"{min(a, b)}__{max(a, b)}"
    assert {pair["a"]["id"], pair["b"]["id"]} == {a, b}

    kept = facade.resolve(pair["id"], a)
    assert kept["id"] == a
    assert kept["state"] == "active"
    assert facade.get(b)["state"] == "decayed"
    # The edge is gone, so no pair remains.
    assert facade.contradictions() == []


def test_resolve_custom_decays_both_and_creates_active(facade):
    a = facade.create({"title": "A", "content": "Store timestamps in UTC."})["id"]
    b = facade.create({"title": "B", "content": "Store timestamps in local time."})["id"]
    facade.graph.add_edge(a, b, "contradiction")
    pair_id = f"{min(a, b)}__{max(a, b)}"

    new = facade.resolve_custom(pair_id, "Store timestamps in UTC, render in local time.")
    assert new["state"] == "active"
    assert new["id"] not in (a, b)
    assert new["content"] == "Store timestamps in UTC, render in local time."
    assert facade.get(a)["state"] == "decayed"
    assert facade.get(b)["state"] == "decayed"
    assert facade.contradictions() == []
