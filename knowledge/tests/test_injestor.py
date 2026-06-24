"""Tests for the PromptIngestor variant of Ingestor."""

from knowledge.injestion.injestor_variants.prompt_injestor import (
    PromptIngestor,
    segment_passthrough,
)
from knowledge.knowledge_graph.knowledge_graph_variants.in_memory_graph import (
    InMemoryGraph,
)


def test_ingest_without_llm_writes_raw_input():
    graph = InMemoryGraph()
    ingestor = PromptIngestor(graph)  # no llm -> passthrough
    out = ingestor.ingest("prefer pathlib")
    assert "prefer pathlib" in out
    assert "prefer pathlib" in graph.read()


def test_passthrough_strips_doc_noise_and_segments_sentences():
    # A wiki-style chunk: a multi-sentence paragraph, a Markdown header, and a
    # trailing apparatus section with nav links and a citation.
    article = (
        "Volta invented the voltaic pile in 1799. He demonstrated it to Napoleon.\n"
        "\n"
        "== See also ==\n"
        "History of the battery\n"
        "Lemon battery\n"
        "\n"
        "== References ==\n"
        'Chisholm, Hugh, ed. (1911). "Volta, Alessandro". Encyclopaedia Britannica.\n'
    )
    facts = segment_passthrough(article)
    # The paragraph becomes two atomic sentences...
    assert facts == [
        "Volta invented the voltaic pile in 1799.",
        "He demonstrated it to Napoleon.",
    ]
    # ...and no header, nav-link, or citation line survives.
    joined = "\n".join(facts)
    assert "==" not in joined
    assert "History of the battery" not in joined
    assert "Chisholm" not in joined


def test_passthrough_keeps_short_standalone_insight():
    # A short insight with no sentence punctuation must NOT be dropped as noise.
    assert segment_passthrough("prefer pathlib") == ["prefer pathlib"]


def test_synthesis_splits_llm_response_into_insights():
    graph = InMemoryGraph()
    # Fake LLM returns two lines -> two insights.
    ingestor = PromptIngestor(graph, llm=lambda prompt: "insight A\n\ninsight B\n")
    insights = ingestor.synthesis("raw")
    assert [i.raw_text for i in insights] == ["insight A", "insight B"]


def test_ingest_loops_write_over_all_insights():
    graph = InMemoryGraph()
    ingestor = PromptIngestor(graph, llm=lambda prompt: "one\ntwo\nthree")
    ingestor.ingest("raw")
    content = graph.read()
    assert "one" in content and "two" in content and "three" in content
