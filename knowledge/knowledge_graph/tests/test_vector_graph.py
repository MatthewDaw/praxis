"""US3 write-path tests: embed once, one shared recall pass.

These pin SC-007: a single ``write`` embeds the incoming text exactly once (the
merge judge and persistence share that vector and the one candidate-recall pass),
and a merged duplicate triggers zero conflict checks (merge runs before the
structural conflict detector; a merge short-circuits it).

Offline via ``FakeEmbedder`` (hash-based, so non-identical texts sit at ~0
cosine). ``recall_floor=-1.0`` opts the candidate into the recall set despite the
fake embedder's low similarity, so the judge wiring is exercised deterministically.
"""

from __future__ import annotations

from knowledge.knowledge_graph.knowledge_graph_def import Claim
from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import VectorGraph
from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_step_variants import (
    ClaimConflictDetector,
    Deduper,
    MergeJudge,
)
from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder
from knowledge.llm.llm_def import Vector
from knowledge.llm.llm_variants.fake_llm import FakeLlm
from knowledge.llm.parent_embedder import Embedder


class _CountingEmbedder(Embedder):
    """Wraps FakeEmbedder, recording every text it embeds (to count re-embeds)."""

    def __init__(self) -> None:
        self._inner = FakeEmbedder()
        self.embedded: list[str] = []

    def embed(self, texts: list[str]) -> list[Vector]:
        self.embedded.extend(texts)
        return self._inner.embed(texts)


class _Claims(WriteStep):
    """Test stand-in for ClaimExtractor: assigns claims by exact text match."""

    consumes_candidates = False

    def __init__(self, mapping: dict[str, list[Claim]]) -> None:
        self._m = mapping

    def apply(self, decision) -> None:
        decision.claims = list(self._m.get(decision.text, []))


def test_single_write_embeds_incoming_text_once():
    # One write into a non-empty graph must embed the incoming text exactly once,
    # shared by the recall pass and persistence (no per-judge or store-time re-embed).
    emb = _CountingEmbedder()
    g = VectorGraph(embedder=emb)
    g.write("first fact about deployment scripts", state="active")
    emb.embedded.clear()
    g.write("second fact about the test command", state="active")
    assert emb.embedded.count("second fact about the test command") == 1


def test_one_recall_pass_feeds_the_merge_judge():
    # A single recall pass (one embed) feeds the merge judge: it is consulted exactly
    # once on the recalled candidate.
    emb = _CountingEmbedder()
    merge_llm = FakeLlm(default='{"same_lesson": false}')  # not a duplicate
    policy = [Deduper(judge=MergeJudge(llm=merge_llm))]
    g = VectorGraph(embedder=emb, policy=policy, recall_floor=-1.0)
    g.write("alpha fact", state="active")
    emb.embedded.clear()
    g.write("beta fact", state="active")
    assert emb.embedded.count("beta fact") == 1  # one embed powers the shared recall
    assert len(merge_llm.calls) == 1  # merge judge saw the candidate


def test_below_floor_candidate_is_not_judged():
    # The store's single recall gate filters a below-floor candidate out before the
    # judge — the merge judge is never consulted (FakeEmbedder pairs sit ~0 cosine,
    # below the default recall_floor of 0.45).
    merge_llm = FakeLlm(default='{"same_lesson": true}')
    g = VectorGraph(policy=[Deduper(judge=MergeJudge(llm=merge_llm))])
    g.write("alpha unrelated fact", state="active")
    g.write("beta unrelated fact", state="active")
    assert len(g._facts) == 2  # not merged
    assert merge_llm.calls == []  # below the recall floor -> judge never consulted


def test_merged_dup_triggers_zero_conflict_checks():
    # Merge runs before the conflict detector: when the merge judge collapses the
    # write into an existing fact (action == "update"), the structural detector is
    # skipped, so no contradiction is recorded even with conflicting claims.
    merge_llm = FakeLlm(default='{"same_lesson": true}')
    mapping = {
        "use uv run pytest": [Claim(subject="cmd", attribute="version", value="1", functional=True)],
        "run the suite with uv run pytest": [
            Claim(subject="cmd", attribute="version", value="2", functional=True)
        ],
    }
    policy = [_Claims(mapping), Deduper(judge=MergeJudge(llm=merge_llm)), ClaimConflictDetector()]
    g = VectorGraph(policy=policy, recall_floor=-1.0)
    g.write("use uv run pytest", state="active")
    g.write("run the suite with uv run pytest", state="active")  # paraphrase -> merge
    assert any(f.observation_count == 2 for f in g._facts)  # merged into the survivor
    assert g.contradictions() == []  # detector skipped on a merge
