"""Unit tests for the FactsCandidates facade over the facts spine.

These build the facade directly with a deterministic FakeEmbedder and a no-LLM
write policy ([Redactor(), Deduper()]), so they need a Postgres DSN but make no
network/LLM calls. Each test uses a unique throwaway org so runs never collide.
"""

from __future__ import annotations

import pytest

from knowledge.knowledge_graph.knowledge_graph_def import Claim
from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder
from knowledge.serve import db
from knowledge.serve.facts_candidates import DeletionError, FactsCandidates, PromotionError

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"


def _edge_pairs(facade, kind):
    """Undirected ``{frozenset(src, dst)}`` set of ``kind`` edges for the graph."""
    return {frozenset((s, d)) for s, d, _k in facade.graph.all_edges(kind)}


def _contra_status(candidate):
    """``{rival_id: status}`` from a candidate's rich ``contradictions`` field."""
    return {c["id"]: c["status"] for c in candidate.get("contradictions", [])}


def _pair(a, b):
    return f"{min(a, b)}__{max(a, b)}"


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


def test_reject_rejects(facade):
    cid = facade.create({"title": "T", "content": "Some noisy candidate text."})["id"]
    rejected = facade.reject(cid, reason="noise")
    assert rejected["state"] == "rejected"
    assert facade.get(cid)["state"] == "rejected"


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

    clusters = facade.contradictions()
    assert len(clusters) == 1
    cluster = clusters[0]
    assert {m["id"] for m in cluster["members"]} == {a, b}
    pair = cluster["pairs"][0]
    assert pair["id"] == f"{min(a, b)}__{max(a, b)}"
    assert {pair["a"]["id"], pair["b"]["id"]} == {a, b}

    kept = facade.resolve(pair["id"], a)
    assert kept["id"] == a
    assert kept["state"] == "active"
    loser = facade.get(b)
    assert loser["state"] == "rejected"
    assert loser["content"] == "Use spaces for indentation."  # text intact (FR-004/SC-001)
    # Resolved, not deleted: the pending list drops it, but the link survives
    # flipped to contradicted_by so the resolution stays reversible (FR-004).
    assert facade.contradictions() == []
    assert _edge_pairs(facade, "contradicted_by") == {frozenset((a, b))}
    assert _edge_pairs(facade, "contradiction") == set()


def _slot_claim(subject: str, attribute: str, value: str) -> Claim:
    return Claim(subject=subject, attribute=attribute, value=value, functional=True)


def test_auto_resolve_settles_high_confidence_numeric_clash(facade):
    # A deterministic numeric clash on a shared functional slot is high-confidence:
    # auto-resolution supersedes the older fact (b) in favor of the newer (a).
    b = facade.create({"title": "B", "content": "Voltaic pile invented in 1800."})["id"]
    a = facade.create({"title": "A", "content": "Voltaic pile invented in 1799."})["id"]
    facade.graph._persist_claims(b, [_slot_claim("Voltaic pile", "invention year", "1800")])
    facade.graph._persist_claims(a, [_slot_claim("Voltaic pile", "invention year", "1799")])
    facade.graph.add_edge(a, b, "contradiction")

    resolved = facade.auto_resolve_high_confidence(prefer_id=a)
    assert resolved == [_pair(a, b)]
    assert facade.get(a)["state"] == "active"  # newest add wins
    assert facade.get(b)["state"] == "rejected"  # loser superseded, text intact
    assert facade.get(b)["content"] == "Voltaic pile invented in 1800."
    assert facade.contradictions() == []  # no longer pending
    assert _edge_pairs(facade, "contradicted_by") == {frozenset((a, b))}


def test_auto_resolve_leaves_gray_zone_contradiction_pending(facade):
    # A free-text (non-numeric, non-stance) clash is gray-zone / low-confidence:
    # auto-resolution must NOT settle it; it stays pending for manual review.
    a = facade.create({"title": "A", "content": "Use tabs for indentation."})["id"]
    b = facade.create({"title": "B", "content": "Use spaces for indentation."})["id"]
    facade.graph._persist_claims(a, [_slot_claim("indentation", "style", "tabs")])
    facade.graph._persist_claims(b, [_slot_claim("indentation", "style", "spaces")])
    facade.graph.add_edge(a, b, "contradiction")

    resolved = facade.auto_resolve_high_confidence(prefer_id=a)
    assert resolved == []
    assert len(facade.contradictions()) == 1  # still pending
    assert _edge_pairs(facade, "contradiction") == {frozenset((a, b))}


def test_auto_resolve_is_off_by_default_in_create(facade, monkeypatch):
    # The create() hook only auto-resolves when the opt-in flag is set; default off
    # means a wired contradiction edge stays pending.
    monkeypatch.delenv("PRAXIS_AUTO_RESOLVE_CONTRADICTIONS", raising=False)
    from knowledge.serve.facts_candidates import auto_resolve_enabled

    assert auto_resolve_enabled() is False
    monkeypatch.setenv("PRAXIS_AUTO_RESOLVE_CONTRADICTIONS", "1")
    assert auto_resolve_enabled() is True


def test_three_facts_on_one_slot_form_one_cluster(facade):
    a = facade.create({"title": "A", "content": "Voltaic pile invented in 1799."})["id"]
    b = facade.create({"title": "B", "content": "Voltaic pile invented in 1800."})["id"]
    c = facade.create({"title": "C", "content": "Voltaic pile invented in 1801."})["id"]
    # Same functional slot for all three -> they belong in one cluster.
    for fid, year in ((a, "1799"), (b, "1800"), (c, "1801")):
        facade.graph._persist_claims(fid, [_slot_claim("Voltaic pile", "invention year", year)])
    # Pairwise contradiction edges among the three.
    facade.graph.add_edge(a, b, "contradiction")
    facade.graph.add_edge(a, c, "contradiction")
    facade.graph.add_edge(b, c, "contradiction")

    clusters = facade.contradictions()
    assert len(clusters) == 1
    cluster = clusters[0]
    assert {m["id"] for m in cluster["members"]} == {a, b, c}
    assert cluster["slot"] == {"subject": "voltaic pile", "attribute": "invention year"}
    assert {m["value"] for m in cluster["members"]} == {"1799", "1800", "1801"}
    # Resolving one member keeps it via the existing per-pair resolve endpoint.
    pair = cluster["pairs"][0]
    kept_id = pair["a"]["id"]
    facade.resolve(pair["id"], kept_id)
    assert facade.get(kept_id)["state"] == "active"


def test_two_slots_form_two_clusters(facade):
    a = facade.create({"title": "A", "content": "Pile invented 1799."})["id"]
    b = facade.create({"title": "B", "content": "Pile invented 1800."})["id"]
    c = facade.create({"title": "C", "content": "Use tabs."})["id"]
    d = facade.create({"title": "D", "content": "Use spaces."})["id"]
    facade.graph._persist_claims(a, [_slot_claim("pile", "year", "1799")])
    facade.graph._persist_claims(b, [_slot_claim("pile", "year", "1800")])
    facade.graph._persist_claims(c, [_slot_claim("indentation", "style", "tabs")])
    facade.graph._persist_claims(d, [_slot_claim("indentation", "style", "spaces")])
    facade.graph.add_edge(a, b, "contradiction")
    facade.graph.add_edge(c, d, "contradiction")

    clusters = facade.contradictions()
    assert len(clusters) == 2
    member_sets = sorted([{m["id"] for m in cl["members"]} for cl in clusters], key=sorted)
    assert {a, b} in member_sets
    assert {c, d} in member_sets


def test_compound_fact_splits_into_two_slot_clusters(facade):
    """Contradiction is not transitive across slots. B competes on two slots; A
    clashes with B on one, C clashes with B on the other, and A and C share no
    slot — so the chain A-B-C must split into two clusters (with B in both),
    never collapse into one 'A, B, C all conflict' cluster."""
    a = facade.create({"title": "A", "content": "Prod deploys need manual approval."})["id"]
    b = facade.create({"title": "B", "content": "Prod deploys run automatically every Friday."})["id"]
    c = facade.create({"title": "C", "content": "Prod deploys run every Tuesday."})["id"]
    facade.graph._persist_claims(a, [_slot_claim("prod deploy", "approval", "manual")])
    facade.graph._persist_claims(
        b,
        [
            _slot_claim("prod deploy", "approval", "automatic"),
            _slot_claim("prod deploy", "day", "friday"),
        ],
    )
    facade.graph._persist_claims(c, [_slot_claim("prod deploy", "day", "tuesday")])
    facade.graph.add_edge(a, b, "contradiction")  # approval slot
    facade.graph.add_edge(b, c, "contradiction")  # day slot

    clusters = facade.contradictions()
    member_sets = [{m["id"] for m in cl["members"]} for cl in clusters]
    assert len(clusters) == 2
    assert {a, b} in member_sets
    assert {b, c} in member_sets
    # A and C, which never share a slot, never land in the same cluster.
    assert not any({a, c} <= s for s in member_sets)


def test_single_pair_is_cluster_of_two(facade):
    a = facade.create({"title": "A", "content": "Use tabs for indentation."})["id"]
    b = facade.create({"title": "B", "content": "Use spaces for indentation."})["id"]
    facade.graph.add_edge(a, b, "contradiction")

    clusters = facade.contradictions()
    assert len(clusters) == 1
    cluster = clusters[0]
    assert {m["id"] for m in cluster["members"]} == {a, b}
    # No claims stored -> no slot, degrades to a per-pair cluster.
    assert cluster["slot"] is None
    assert len(cluster["pairs"]) == 1

    # Resolving the single underlying pair uses the existing endpoint.
    kept = facade.resolve(cluster["pairs"][0]["id"], a)
    assert kept["id"] == a and kept["state"] == "active"
    assert facade.get(b)["state"] == "rejected"
    assert facade.contradictions() == []


def test_resolve_custom_rejects_both_and_creates_active(facade):
    a = facade.create({"title": "A", "content": "Store timestamps in UTC."})["id"]
    b = facade.create({"title": "B", "content": "Store timestamps in local time."})["id"]
    facade.graph.add_edge(a, b, "contradiction")
    pair_id = f"{min(a, b)}__{max(a, b)}"

    new = facade.resolve_custom(pair_id, "Store timestamps in UTC, render in local time.")
    assert new["state"] == "active"
    assert new["id"] not in (a, b)
    assert new["content"] == "Store timestamps in UTC, render in local time."
    assert facade.get(a)["state"] == "rejected"
    assert facade.get(b)["state"] == "rejected"
    assert facade.get(a)["content"] == "Store timestamps in UTC."  # text intact
    assert facade.get(b)["content"] == "Store timestamps in local time."  # text intact
    assert facade.contradictions() == []
    # The new fact *supersedes* each disputed fact (directional, discoverable,
    # reversible) — it is NOT asserted to contradict them, so no contradicted_by
    # edges are fabricated and the old pending pair is gone.
    assert _edge_pairs(facade, "supersedes") == {
        frozenset((new["id"], a)),
        frozenset((new["id"], b)),
    }
    assert _edge_pairs(facade, "contradicted_by") == set()
    assert _edge_pairs(facade, "contradiction") == set()


def test_resolve_custom_supersedes_every_cluster_member(facade):
    """A 3-way clique on one slot, settled by a single user-authored fact: all
    three members are rejected and superseded — not just the first pair (the bug
    this fixes) — and none is marked as contradicting the new fact."""
    a = facade.create({"title": "A", "content": "Deploy on Friday."})["id"]
    b = facade.create({"title": "B", "content": "Deploy on Tuesday."})["id"]
    c = facade.create({"title": "C", "content": "Deploy on Monday."})["id"]
    for fid, day in ((a, "friday"), (b, "tuesday"), (c, "monday")):
        facade.graph._persist_claims(fid, [_slot_claim("deploy", "day", day)])
    facade.graph.add_edge(a, b, "contradiction")
    facade.graph.add_edge(a, c, "contradiction")
    facade.graph.add_edge(b, c, "contradiction")

    cluster = facade.contradictions()[0]
    assert {m["id"] for m in cluster["members"]} == {a, b, c}

    new = facade.resolve_custom(cluster["id"], "Deploy on the first weekday of each sprint.")
    assert new["state"] == "active"
    for fid in (a, b, c):
        assert facade.get(fid)["state"] == "rejected"
    assert facade.contradictions() == []
    assert _edge_pairs(facade, "supersedes") == {
        frozenset((new["id"], a)),
        frozenset((new["id"], b)),
        frozenset((new["id"], c)),
    }
    assert _edge_pairs(facade, "contradiction") == set()


# --- US2: review by state + contradictions ---------------------------------


def test_per_fact_contradictions_carry_status(facade):
    """FR-012: a fact's contradictions list both pending and resolved rivals, each
    annotated with its status."""
    a = facade.create({"title": "A", "content": "Use tabs."})["id"]
    b = facade.create({"title": "B", "content": "Use spaces."})["id"]
    c = facade.create({"title": "C", "content": "Use two spaces."})["id"]
    facade.graph.add_edge(a, b, "contradiction")  # stays pending
    facade.graph.add_edge(a, c, "contradiction")
    facade.resolve(_pair(a, c), a)  # a wins over c -> a<->c becomes resolved

    assert _contra_status(facade.get(a)) == {b: "pending", c: "resolved"}


def test_global_contradictions_lists_pending_only(facade):
    """FR-013a: the global view lists only pending pairs; resolved ones drop out."""
    a = facade.create({"title": "A", "content": "Use tabs."})["id"]
    b = facade.create({"title": "B", "content": "Use spaces."})["id"]
    c = facade.create({"title": "C", "content": "Use two spaces."})["id"]
    facade.graph.add_edge(a, b, "contradiction")  # pending
    facade.graph.add_edge(a, c, "contradiction")
    facade.resolve(_pair(a, c), a)  # resolved -> excluded from the pending view

    pairs = facade.contradictions()
    assert len(pairs) == 1
    assert pairs[0]["id"] == _pair(a, b)
    assert pairs[0]["status"] == "pending"


def test_reapprove_rejected_swaps_states_and_keeps_link(facade):
    """FR-010: re-approving a rejected fact flips it to active and demotes its
    active contradictor, keeping the link. FR-009: a fact linked only via a
    separate contradiction is not touched (no auto-cascade)."""
    a = facade.create({"title": "A", "content": "Use tabs."})["id"]
    b = facade.create({"title": "B", "content": "Use spaces."})["id"]
    facade.graph.add_edge(a, b, "contradiction")
    facade.resolve(_pair(a, b), a)  # a active, b rejected, linked contradicted_by
    assert facade.get(a)["state"] == "active"
    assert facade.get(b)["state"] == "rejected"

    # A separate fact d also contradicts a (pending) — must survive re-approval.
    d = facade.create({"title": "D", "content": "Use four spaces."})["id"]
    facade.graph.add_edge(a, d, "contradiction")

    result = facade.promote(b)  # re-approve the rejected fact
    assert result["state"] == "active"
    assert facade.get(b)["state"] == "active"
    assert facade.get(a)["state"] == "rejected"  # direct contradictor demoted
    assert facade.get(d)["state"] == "proposed"   # FR-009: untouched (created state)
    # The b<->a pair stays linked (resolved).
    assert frozenset((a, b)) in _edge_pairs(facade, "contradicted_by")
    # The action reports the demoted fact, with its other-contradictions flag.
    demoted = {r["id"]: r["hasOtherContradictions"] for r in result.get("rejected", [])}
    assert demoted.get(a) is True  # a also contradicts d


def test_reject_reports_other_contradictions(facade):
    """FR-008: a manual reject reports whether the fact has any contradiction."""
    a = facade.create({"title": "A", "content": "Alpha note."})["id"]
    b = facade.create({"title": "B", "content": "Beta note."})["id"]
    facade.graph.add_edge(a, b, "contradiction")
    assert facade.reject(a, reason="noise")["hasOtherContradictions"] is True

    lone = facade.create({"title": "L", "content": "Lonely note."})["id"]
    assert facade.reject(lone)["hasOtherContradictions"] is False


def test_resolve_loser_other_contradictions_flag(facade):
    """FR-008/SC-007: the resolve response flags whether the rejected loser has a
    contradiction other than the one just resolved."""
    x = facade.create({"title": "X", "content": "X note."})["id"]
    y = facade.create({"title": "Y", "content": "Y note."})["id"]
    facade.graph.add_edge(x, y, "contradiction")
    assert facade.resolve(_pair(x, y), x)["hasOtherContradictions"] is False

    p = facade.create({"title": "P", "content": "P note."})["id"]
    q = facade.create({"title": "Q", "content": "Q note."})["id"]
    r = facade.create({"title": "R", "content": "R note."})["id"]
    facade.graph.add_edge(p, q, "contradiction")
    facade.graph.add_edge(q, r, "contradiction")  # q's other contradiction
    assert facade.resolve(_pair(p, q), p)["hasOtherContradictions"] is True  # loser q


# --- US3: delete gating -----------------------------------------------------


def test_delete_gated_to_proposed_or_rejected(facade):
    """FR-014: an active fact can't be deleted (reject first); proposed/rejected can."""
    active = facade.create({"title": "A", "content": "Active note."})["id"]
    facade.promote(active)  # -> active
    with pytest.raises(DeletionError):
        facade.delete(active)
    assert facade.get(active) is not None  # untouched

    proposed = facade.create({"title": "P", "content": "Proposed note."})["id"]
    facade.delete(proposed)
    assert facade.get(proposed) is None

    rejected = facade.create({"title": "R", "content": "Rejected note."})["id"]
    facade.reject(rejected)
    facade.delete(rejected)
    assert facade.get(rejected) is None


def test_delete_removes_contradiction_links(facade):
    """FR-015/SC-005: deleting a fact removes its edges; the contradictor no longer
    lists it."""
    a = facade.create({"title": "A", "content": "A note."})["id"]
    b = facade.create({"title": "B", "content": "B note."})["id"]
    facade.graph.add_edge(a, b, "contradiction")
    facade.reject(a)  # make it deletable
    facade.delete(a)
    assert facade.get(a) is None
    assert _contra_status(facade.get(b)) == {}  # b no longer linked to a
    assert _edge_pairs(facade, "contradiction") == set()
