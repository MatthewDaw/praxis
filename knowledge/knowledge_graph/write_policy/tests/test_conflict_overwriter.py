"""Unit tests for the ConflictOverwriter write step.

Drives the step directly with a stub StoreView: an LLM "yes" turns the decision
into an ``overwrite`` targeting the conflicting fact; a "no" leaves it a plain
``add``. No network, no DB.
"""

from __future__ import annotations

from knowledge.knowledge_graph.knowledge_graph_def import Fact, SearchHit
from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision
from knowledge.knowledge_graph.write_policy.write_step_variants import ConflictOverwriter
from knowledge.llm.llm_variants.fake_llm import FakeLlm


class _StubStore:
    """A StoreView returning one similar hit above the similarity floor."""

    def __init__(self, fact: Fact, score: float = 0.9) -> None:
        self._hit = SearchHit(fact=fact, score=score)

    def most_similar(self, text: str, k: int = 5) -> list[SearchHit]:
        return [self._hit]


def _existing() -> Fact:
    return Fact(id="f1", text="use uv, not pip, in this repo")


def test_yes_overwrites_nearest():
    step = ConflictOverwriter(llm=FakeLlm(default="yes"))
    decision = WriteDecision(text="use pip, not uv, in this repo")
    step.apply(decision, _StubStore(_existing()))
    assert decision.action == "overwrite"
    assert decision.update_target_id == "f1"
    assert decision.supersede_ids == []


def test_no_stays_add():
    step = ConflictOverwriter(llm=FakeLlm(default="no"))
    decision = WriteDecision(text="use pip, not uv, in this repo")
    step.apply(decision, _StubStore(_existing()))
    assert decision.action == "add"
    assert decision.update_target_id is None


def test_no_llm_is_inert():
    step = ConflictOverwriter(llm=None)
    decision = WriteDecision(text="anything")
    step.apply(decision, _StubStore(_existing()))
    assert decision.action == "add"
