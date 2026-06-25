"""U3: real-distiller validation harness over the fd866322 gold pairing.

Runs the real ``SessionIngestor`` over a committed session-narrative fixture, replayed
deterministically through a committed ``CassetteLlm`` cassette (records the live
distillation once when ``OPENROUTER_API_KEY`` is set; replays everywhere else). The
graph is a neutral ``InMemoryGraph`` so the harness measures *extraction only*.

It asserts the known downstream risks **provisionally** — not extraction quality, which
the proxy run already cleared. What this run found on ``gpt-4o-mini`` (recorded in the
cassette):

- The durable convention is recovered (the footgun-validation lesson). GOOD.
- ``scope`` is coarse — every insight came back ``repo`` rather than discriminating
  ``module:knowledge/evals``. We assert scope is *well-formed*, never pin a gold value.
- Experiment-state leakage is HIGH: 3 of 4 insights name the in-flight constructs
  (phoenix / umap / yoyo) rather than durable knowledge. The prompt's durable-vs-
  experiment guidance did not suppress them here. We gate at the observed baseline so a
  future prompt/category improvement can be measured against it — this is the concrete
  motivation for the deferred experiment-state-filtering question, not a clean pass.

Dedup *absorption* of near-duplicates is a live ``PostgresVectorGraph`` property and is
deliberately NOT tested here (the isolated ``InMemoryGraph`` has no dedup) — the harness
simply does not gate on uniqueness.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from knowledge.injestion.injestor_variants.session_injestor import SessionIngestor
from knowledge.knowledge_graph.knowledge_graph_variants.in_memory_graph import (
    InMemoryGraph,
)
from knowledge.llm.llm_cassette import CassetteLlm
from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm

load_dotenv()  # resolve OPENROUTER_API_KEY / OPENROUTER_MODEL when recording

_FIXTURES = Path(__file__).parent / "fixtures"
_NARRATIVE = _FIXTURES / "session_fd866322_narrative.md"
_CASSETTE = _FIXTURES / "session_ingestion" / "session_fd866322.json"
_HAS_KEY = bool(os.getenv("OPENROUTER_API_KEY"))

# Skip cleanly only before the cassette is recorded AND with no key to record it.
# Once committed (the normal state), CI replays it with no key.
pytestmark = pytest.mark.skipif(
    not _CASSETTE.exists() and not _HAS_KEY,
    reason="no committed cassette and no OPENROUTER_API_KEY to record one",
)

# The experiment-state count this run recorded. Gate at the baseline: a future prompt
# or category change that reduces leakage still passes; a regression that adds more
# fails loudly. Lowering this is the deferred experiment-state-filtering work.
_EXPERIMENT_STATE_BASELINE = 3
_EXPERIMENT_MARKERS = ("phoenix", "umap", "yoyo")


@pytest.fixture(scope="module")
def insights():
    llm = CassetteLlm(OpenRouterLlm(), _CASSETTE, allow_compute=_HAS_KEY)
    ingestor = SessionIngestor(InMemoryGraph(), llm)
    return ingestor.synthesis(
        _NARRATIVE.read_text(encoding="utf-8"), source="session/fd866322"
    )


def test_extraction_produces_a_bounded_handful(insights):
    # Sanity: a few durable facts, not zero and not a flood. No uniqueness gate — dedup
    # absorption is a live-graph property, not testable on InMemoryGraph.
    assert 1 <= len(insights) <= 15
    assert all(i.raw_text.strip() for i in insights)
    assert all(i.source == "session/fd866322" for i in insights)


def test_recovers_the_validity_convention(insights):
    # The headline durable lesson surfaces (provisional text match, not an exact gold).
    blob = " ".join(i.raw_text.lower() for i in insights)
    assert any(k in blob for k in ("footgun", "empirical", "validated", "results.md"))
    assert any(i.category in ("convention", "decision") for i in insights)


def test_scope_is_well_formed_not_pinned(insights):
    # scope is the distiller's least-reliable column (this run returned all `repo`):
    # assert only well-formedness, never pin a specific gold scope.
    for i in insights:
        assert i.scope is None or i.scope == "repo" or i.scope.startswith(
            ("file:", "module:")
        )


def test_experiment_state_within_baseline(insights):
    # Counted, not masked. 3/4 here name in-flight constructs rather than durable
    # knowledge — gate against regression at that baseline; reducing it is deferred work.
    n = sum(
        1
        for i in insights
        if any(m in i.raw_text.lower() for m in _EXPERIMENT_MARKERS)
    )
    assert n <= _EXPERIMENT_STATE_BASELINE, (
        f"experiment-state facts ({n}) regressed past baseline "
        f"{_EXPERIMENT_STATE_BASELINE}"
    )
    # The durable lesson must still be present alongside the experiment-state noise.
    assert any(
        i.category in ("convention", "decision")
        and not any(m in i.raw_text.lower() for m in _EXPERIMENT_MARKERS)
        for i in insights
    )
