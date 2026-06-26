"""MemoizingEmbedder collapses repeated/prefetched embeds into one inner call."""

from __future__ import annotations

from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder
from knowledge.llm.embedder_variants.memoizing_embedder import MemoizingEmbedder
from knowledge.llm.llm_def import Vector


class CountingEmbedder(FakeEmbedder):
    """Wraps the deterministic fake, recording every batch handed to ``embed``."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[Vector]:
        self.calls.append(list(texts))
        return super().embed(texts)


def test_prefetch_then_embed_one_makes_a_single_inner_call() -> None:
    inner = CountingEmbedder()
    emb = MemoizingEmbedder(inner)

    texts = ["alpha", "beta", "gamma"]
    emb.prefetch(texts)
    # Each per-insight embed now resolves from the memo — no further inner calls.
    vecs = [emb.embed_one(t) for t in texts]

    assert len(inner.calls) == 1
    assert inner.calls[0] == texts
    # Memoized vectors match what the inner embedder would have produced.
    assert vecs == FakeEmbedder().embed(texts)


def test_memo_dedups_repeats_and_passes_misses_through() -> None:
    inner = CountingEmbedder()
    emb = MemoizingEmbedder(inner)

    emb.prefetch(["a", "a", "b", "", "b"])  # repeats + empty collapse to {a, b}
    assert inner.calls == [["a", "b"]]

    # A text never prefetched falls through to the inner embedder once.
    emb.embed_one("c")
    assert inner.calls == [["a", "b"], ["c"]]
    # And is then itself memoized.
    emb.embed_one("c")
    assert inner.calls == [["a", "b"], ["c"]]
