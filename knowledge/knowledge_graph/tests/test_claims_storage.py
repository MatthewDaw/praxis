"""U1: claims data model + storage (in-memory VectorGraph path, no DB)."""

from knowledge.knowledge_graph.knowledge_graph_def import Claim
from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import VectorGraph
from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision


class _ClaimSetter(WriteStep):
    """Test stand-in for the (U2) extractor: stamps fixed claims on the write."""

    consumes_candidates = False

    def __init__(self, claims: list[Claim]) -> None:
        self._claims = claims

    def apply(self, decision: WriteDecision) -> None:
        decision.claims = list(self._claims)


def test_claim_slot_normalizes_subject_and_attribute():
    c = Claim(subject="  Voltaic  Pile ", attribute="Invention YEAR", value="1799", functional=True)
    assert c.slot == ("voltaic pile", "invention year")
    assert c.value == "1799"  # value keeps its raw form


def test_written_fact_carries_its_claims():
    claims = [
        Claim(subject="voltaic pile", attribute="invention year", value="1799", functional=True),
        Claim(subject="Volta", attribute="discovery", value="methane", functional=False),
    ]
    g = VectorGraph(policy=[_ClaimSetter(claims)])
    g.write("Volta invented the voltaic pile in 1799 and discovered methane.", state="active")
    stored = g.facts[0].claims
    assert len(stored) == 2
    assert stored[0].functional is True and stored[0].value == "1799"
    assert stored[1].functional is False  # multi-valued attribute preserved


def test_fact_with_no_claims_is_not_an_error():
    g = VectorGraph(policy=[_ClaimSetter([])])
    g.write("a fact with no extractable claims", state="active")
    assert g.facts[0].claims == []
