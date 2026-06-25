"""Slot-guard tests for the Deduper (loss point B: over-merging sibling rows).

The deduper's tabular slot-guard sits ahead of the stage-2 MergeJudge loop and
keys on the full functional ``(subject, attribute)`` slot. It makes a three-way
decision per candidate (distinct / contradiction / duplicate) plus a missing-claim
fail-safe. These tests drive the whole write pipeline offline:

* ``recall_floor=-1.0`` forces sibling rows into the shared recall set despite the
  ``FakeEmbedder``'s ~0 cosine (the hash embedder can't surface them otherwise);
* an always-"same lesson" ``MergeJudge`` stands in for the real precision judge —
  without the guard it would merge every sibling, so a green test proves the guard
  is the thing keeping the rows distinct;
* a ``_Claims`` stub stands in for ``ClaimExtractor`` (assigns claims by text), and
  a ``_Tabular`` stub flags the writes as table-distilled.
"""

from __future__ import annotations

from knowledge.knowledge_graph.knowledge_graph_def import Claim
from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import VectorGraph
from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision
from knowledge.knowledge_graph.write_policy.write_step_variants.claim_conflict_detector import (
    ClaimConflictDetector,
    ClaimValueJudge,
)
from knowledge.knowledge_graph.write_policy.write_step_variants.deduper import (
    TABULAR_FLAG,
    Deduper,
)
from knowledge.knowledge_graph.write_policy.write_step_variants.merge_judge import MergeJudge
from knowledge.llm.llm_variants.fake_llm import FakeLlm

_MERGE_YES = '{"same_lesson": true}'  # the judge would fold every sibling without the guard


class _Tabular(WriteStep):
    """Flag every write as distilled from tabular input (engages the slot-guard)."""

    consumes_candidates = False

    def apply(self, decision: WriteDecision) -> None:
        decision.flags.append(TABULAR_FLAG)


class _Claims(WriteStep):
    """Test stand-in for ClaimExtractor: assigns claims by exact text match."""

    consumes_candidates = False

    def __init__(self, mapping: dict[str, list[Claim]]) -> None:
        self._m = mapping

    def apply(self, decision: WriteDecision) -> None:
        decision.claims = list(self._m.get(decision.text, []))


def _graph(mapping, *, value_judge=None, tabular=True):
    """A pipeline with the guard exercised: (flag ->) claims -> deduper -> conflict."""
    steps: list[WriteStep] = []
    if tabular:
        steps.append(_Tabular())
    steps += [
        _Claims(mapping),
        Deduper(judge=MergeJudge(llm=FakeLlm(default=_MERGE_YES))),
        ClaimConflictDetector(judge=value_judge),
    ]
    # recall_floor=-1.0: force the hash-embedded siblings into the recall set so the
    # judge (and thus the guard) is actually consulted.
    return VectorGraph(policy=steps, recall_floor=-1.0)


def _active(g):
    return [f for f in g.facts if f.state == "active"]


# --- Acceptance 1: both table shapes yield one fact per row -------------------

def test_field_required_shape_not_merged():
    # field -> required: the SUBJECT varies per row (subject-only would catch this).
    mapping = {
        "row1": [Claim(subject="daily_prompt", attribute="required", value="yes", functional=True)],
        "row2": [Claim(subject="email", attribute="required", value="yes", functional=True)],
    }
    g = _graph(mapping)
    g.write("row1", state="active")
    g.write("row2", state="active")
    assert len(_active(g)) == 2  # distinct slots -> both stay active, no silent merge


def test_role_permission_shape_not_merged():
    # role x permission: SAME subject (coach), the ATTRIBUTE varies. Subject-only
    # keying never fires here -> the judge would fold them; slot-keying must not.
    mapping = {
        "p1": [Claim(subject="coach", attribute="can edit themes", value="yes", functional=True)],
        "p2": [Claim(subject="coach", attribute="can edit prompts", value="yes", functional=True)],
        "p3": [Claim(subject="coach", attribute="can manage roster", value="yes", functional=True)],
    }
    g = _graph(mapping)
    for row in ("p1", "p2", "p3"):
        g.write(row, state="active")
    assert len(_active(g)) == 3  # one fact per permission row


# --- Acceptance 2: contradiction, not two silent active facts ----------------

def test_same_slot_conflicting_values_is_a_contradiction():
    # Same (subject, attribute) slot, different numeric value -> the guard blocks the
    # merge AND the conflict detector flags it; the newer active write demotes so the
    # pair is NOT two silent active facts.
    mapping = {
        "lo": [Claim(subject="max_retries", attribute="value", value="3", functional=True)],
        "hi": [Claim(subject="max_retries", attribute="value", value="5", functional=True)],
    }
    g = _graph(mapping)  # numeric clash needs no value judge
    g.write("lo", state="active")
    g.write("hi", state="active")
    assert len(g.contradictions()) == 1  # routed to the conflict engine, not merged
    assert len(_active(g)) == 1  # FR-005: the two contradicting facts are not both active


# --- Acceptance 3: idempotency (same value re-ingest merges) ------------------

def test_same_slot_same_value_is_idempotent():
    # Re-ingesting an identical row (same slot, same value) is a genuine duplicate ->
    # allow the merge: N rows ingested twice yield N facts, not 2N.
    mapping = {
        "r1": [Claim(subject="daily_prompt", attribute="required", value="yes", functional=True)],
        "r2": [Claim(subject="email", attribute="required", value="yes", functional=True)],
    }
    g = _graph(mapping)
    for _ in range(2):  # ingest the same two rows twice
        g.write("r1", state="active")
        g.write("r2", state="active")
    assert len(_active(g)) == 2  # idempotent: 2 facts, not 4


def test_same_value_different_text_merges():
    # Identity-folded sibling text differs but the slot+value match -> still a dup.
    mapping = {
        "For the daily_prompt field, required = yes": [
            Claim(subject="daily_prompt", attribute="required", value="yes", functional=True)
        ],
        "daily_prompt is a required field": [
            Claim(subject="daily_prompt", attribute="required", value="yes", functional=True)
        ],
    }
    g = _graph(mapping)
    g.write("For the daily_prompt field, required = yes", state="active")
    g.write("daily_prompt is a required field", state="active")
    assert len(_active(g)) == 1  # same slot + same value -> merged


# --- Acceptance 4: missing-claim fail-safe (demote to proposed) --------------

def test_missing_claim_demotes_to_proposed():
    # A tabular-flagged row whose functional claim is missing (null subject from the
    # extractor) can't be slotted -> don't merge, demote to proposed for review.
    mapping = {
        "seed": [Claim(subject="daily_prompt", attribute="required", value="yes", functional=True)],
        "orphan": [],  # extractor returned no functional claim
    }
    g = _graph(mapping)
    g.write("seed", state="active")
    g.write("orphan", state="active")
    states = {f.text: f.state for f in g.facts}
    assert states["orphan"] == "proposed"  # demoted, not merged away
    assert states["seed"] == "active"  # the seed is untouched
    assert len(g.facts) == 2  # the orphan is kept as a distinct (proposed) row


# --- Regression guard: prose dedup is unchanged ------------------------------

def test_non_tabular_write_still_merges_via_judge():
    # No tabular flag -> the guard is a no-op and the judge merges as before.
    mapping = {
        "a": [Claim(subject="x", attribute="y", value="1", functional=True)],
        "b": [Claim(subject="p", attribute="q", value="2", functional=True)],
    }
    g = _graph(mapping, tabular=False)
    g.write("a", state="active")
    g.write("b", state="active")  # judge says same_lesson -> merged into "a"
    assert len(_active(g)) == 1
