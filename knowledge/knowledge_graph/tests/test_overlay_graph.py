"""Unit tests for OverlayGraph using a fake live graph (no DB / embeddings).

OverlayGraph is a thin read-only composition layer: it delegates search to the
live graph's ``overlay_search`` (which does the single UNION ALL query), composes
the no-query recent path, refuses writes, and delegates everything else. These
tests stub the live graph so they run offline and assert the wiring.
"""

from __future__ import annotations

import pytest

from knowledge.knowledge_graph.knowledge_graph_def import Fact, SearchHit
from knowledge.knowledge_graph.knowledge_graph_variants.overlay_graph import OverlayGraph


class _FakeLive:
    def __init__(self) -> None:
        self.overlay_calls: list[tuple] = []
        self.delegated = "from-live"

    def overlay_search(self, query, mounts, *, top_k=10):
        self.overlay_calls.append((query, tuple(mounts), top_k))
        return [SearchHit(fact=Fact(id="l1", text="live one"), score=0.9)]

    def _recent(self, limit):
        return [Fact(id="l1", text="live one")]

    def recent_cache(self, *, space, snapshot, limit):
        return [Fact(id="m1", text="mounted one")]


# Mounts are now org-shared snapshots identified by (space, snapshot) — no source
# user, bare snapshot name (see specs/005-praxis-tenancy-redesign/design.md §3.1).
MOUNTS = [{"space": "sp", "snapshot": "snap"}]


def test_search_delegates_to_overlay_search_with_space_snapshot_pairs():
    live = _FakeLive()
    g = OverlayGraph(live, MOUNTS)
    hits = g.search("q", top_k=5)
    # mounts normalized to (space, snapshot) tuples (bare snapshot name).
    assert live.overlay_calls == [("q", (("sp", "snap"),), 5)]
    assert [h.fact.id for h in hits] == ["l1"]


def test_read_with_context_uses_search():
    g = OverlayGraph(_FakeLive(), MOUNTS)
    assert g.read("what do we do") == "live one"


def test_read_without_context_unions_recent_and_tags_mounted():
    g = OverlayGraph(_FakeLive(), MOUNTS)
    out = g.read()
    assert "live one" in out and "mounted one" in out


def test_recent_union_tags_mounted_fact():
    g = OverlayGraph(_FakeLive(), MOUNTS)
    facts = g._recent_union(limit=50)
    by_id = {f.id: f for f in facts}
    assert by_id["m1"].meta["mountedFrom"] == {"space": "sp", "snapshot": "snap"}
    assert not by_id["l1"].meta.get("mountedFrom")  # live fact untagged


def test_write_is_read_only():
    g = OverlayGraph(_FakeLive(), MOUNTS)
    with pytest.raises(NotImplementedError):
        g.write("nope")


def test_unknown_attrs_delegate_to_live():
    g = OverlayGraph(_FakeLive(), MOUNTS)
    assert g.delegated == "from-live"


def test_no_mounts_still_delegates_search():
    live = _FakeLive()
    g = OverlayGraph(live, [])
    g.search("q")
    assert live.overlay_calls == [("q", (), 10)]
