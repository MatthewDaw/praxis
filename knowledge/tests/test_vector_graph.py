"""Tests for the VectorGraph store (offline via FakeEmbedder)."""

from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import VectorGraph
from knowledge.knowledge_graph.parent_knowledge_graph import KnowledgeGraph
from knowledge.knowledge_graph.parent_searchable_graph import SearchableGraph


def test_is_a_searchable_knowledge_graph():
    g = VectorGraph()
    assert isinstance(g, KnowledgeGraph)
    assert isinstance(g, SearchableGraph)


def test_write_then_read_roundtrips():
    g = VectorGraph()
    # Only "active" facts are retrievable; write as a direct approval.
    g.write("prefer composition over inheritance", state="active")
    assert "composition over inheritance" in g.read()


def test_exact_duplicate_is_deduped():
    g = VectorGraph()
    g.write("use uv run pytest", state="active")
    g.write("use uv run pytest", state="active")  # exact dup -> merged, not added
    assert g.read().count("use uv run pytest") == 1
    assert g._facts[0].observation_count == 2


def test_write_redacts_secrets_and_pii():
    g = VectorGraph()
    g.write("the key is sk-live-SECRET123 and email jane.doe@example.com", state="active")
    content = g.read()
    assert "sk-live-SECRET123" not in content
    assert "jane.doe@example.com" not in content


def test_search_returns_best_match_first():
    g = VectorGraph()
    g.write("the deploy script lives at scripts/deploy.sh", state="active")
    g.write("the test command is uv run pytest", state="active")
    hits = g.search("scripts/deploy.sh", top_k=2)
    assert hits
    assert "deploy.sh" in hits[0].fact.text


def test_only_active_facts_are_retrievable():
    g = VectorGraph()
    g.write("proposed staging note", state="proposed")  # passive -> not retrievable
    g.write("active approved note", state="active")  # direct approval -> retrievable
    content = g.read()
    assert "active approved note" in content
    assert "proposed staging note" not in content
    # search is gated the same way (defaults to state="active")
    texts = [h.fact.text for h in g.search("note", top_k=10)]
    assert "active approved note" in texts
    assert "proposed staging note" not in texts
    # ...but state=None opts back in to all states (the dedup/conflict path)
    all_texts = [h.fact.text for h in g.search("note", top_k=10, state=None)]
    assert "proposed staging note" in all_texts


def test_empty_write_is_noop():
    g = VectorGraph()
    g.write("   ")
    assert g.read() == ""


def test_contradictions_exporter_surfaces_flagged_pairs():
    # Structural path: two facts on the same functional slot with different numeric
    # values are flagged (the numeric clash needs no LLM).
    from knowledge.knowledge_graph.knowledge_graph_def import Claim
    from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
    from knowledge.knowledge_graph.write_policy.write_step_variants import ClaimConflictDetector

    class _Claims(WriteStep):
        consumes_candidates = False

        def __init__(self, mapping):
            self._m = mapping

        def apply(self, decision):
            decision.claims = list(self._m.get(decision.text, []))

    mapping = {
        "Use tabs for indentation": [Claim(subject="style", attribute="indent size",
                                           value="4", functional=True)],
        "Use spaces for indentation": [Claim(subject="style", attribute="indent size",
                                             value="2", functional=True)],
    }
    g = VectorGraph(policy=[_Claims(mapping), ClaimConflictDetector()])
    g.write("Use tabs for indentation")
    g.write("Use spaces for indentation")

    pairs = g.contradictions()
    assert len(pairs) == 1
    assert pairs[0].flagged.text == "Use spaces for indentation"
    assert pairs[0].conflicting.text == "Use tabs for indentation"


def test_conflict_detection_is_best_effort_when_llm_unavailable():
    # Extraction failure must not break the write; with no claims, nothing is flagged.
    from knowledge.knowledge_graph.write_policy.write_step_variants import (
        ClaimExtractionJudge,
        ClaimExtractor,
    )

    class _BoomLlm:
        def complete(self, messages, **_):
            raise RuntimeError("no API key")

    g = VectorGraph(policy=[ClaimExtractor(judge=ClaimExtractionJudge(llm=_BoomLlm()))])
    g.write("a fact")
    g.write("another fact")  # must not raise despite the failing LLM
    assert g.contradictions() == []  # error swallowed -> no claims -> nothing flagged
