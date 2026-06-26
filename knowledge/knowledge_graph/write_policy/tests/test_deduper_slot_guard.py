"""Slot-guard tests for the Deduper (loss point B: over-merging sibling rows).

The deduper's slot-guard sits ahead of the stage-2 MergeJudge loop and keys on the
full functional ``(subject, attribute)`` slot. It makes a three-way decision per
candidate (distinct / contradiction / duplicate) plus a tabular missing-claim
fail-safe, and engages for any write with a functional claim (prose and tabular).
These tests drive the whole write pipeline offline:

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


# --- Prose loss-point B: distinct functional slots must not merge ------------

def test_non_tabular_distinct_slots_not_merged():
    # Loss point B (prose): two notes on DIFFERENT functional slots share enough
    # vocabulary that the (always-yes) judge would fold them — the guard now engages
    # for prose too (any write with a functional claim), so they stay distinct.
    mapping = {
        "a": [Claim(subject="x", attribute="y", value="1", functional=True)],
        "b": [Claim(subject="p", attribute="q", value="2", functional=True)],
    }
    g = _graph(mapping, tabular=False)
    g.write("a", state="active")
    g.write("b", state="active")  # judge would merge; guard blocks (different slot)
    assert len(_active(g)) == 2


def test_non_tabular_no_functional_claim_still_merges_via_judge():
    # A prose write with NO functional claim (the additive-preference case) does not
    # engage the guard -> the judge merges as before. This is what keeps the Mem0-style
    # additive-merge path working (the guard targets distinct *functional* facts only).
    mapping = {"a": [], "b": []}  # extractor found no functional claim on either
    g = _graph(mapping, tabular=False)
    g.write("a", state="active")
    g.write("b", state="active")  # judge says same_lesson -> merged into "a"
    assert len(_active(g)) == 1


# --- Live repro (2026-06-25): two distinct requirements sharing vocabulary ----

def test_distinct_requirements_sharing_vocabulary_not_merged():
    # Live repro: two distinct requirements written as separate prose insights came
    # back action:"merged" (a Deduper same-lesson over-merge) because they share heavy
    # vocabulary (participation %, threshold, streak, day) and sit at high cosine —
    # collapsing two requirements into one fact and losing R3's identity. They are
    # about DIFFERENT subjects (a day's team-participation-percentage vs the team
    # streak), so their functional claims occupy different slots and the slot-guard
    # keeps them as two distinct facts even though the same-lesson judge says "merge".
    r2 = (
        "Team participation percentage for a date = active athletes who completed "
        "divided by the active roster, times 100."
    )
    r3 = (
        "Team streak is the consecutive run of days where participation percentage "
        "meets the threshold; a miss resets the streak to 0."
    )
    mapping = {
        r2: [Claim(subject="team participation percentage", attribute="definition",
                   value="completed / roster * 100", functional=True)],
        r3: [Claim(subject="team streak", attribute="definition",
                   value="consecutive days at or above threshold; miss resets to 0",
                   functional=True)],
    }
    g = _graph(mapping, tabular=False)  # prose path, no tabular flag (like add_insight)
    g.write(r2, state="active")
    g.write(r3, state="active")  # same-lesson judge says merge; guard blocks (distinct slot)
    actives = _active(g)
    assert len(actives) == 2  # both requirements survive as distinct facts
    texts = " || ".join(f.text for f in actives)
    assert "participation percentage for a date" in texts  # R2's identity preserved
    assert "consecutive run of days" in texts  # R3's identity preserved


# --- Filing-status identity guard (tax-bracket cross-status collapse) ---------

def test_same_value_across_filing_statuses_not_merged():
    # The killer for tax brackets: Single 22% and MFS 22% are the SAME range
    # ($48,475-$103,350), so their functional claims share slot AND value — the
    # same-value branch would rule them a genuine duplicate and merge, dropping one
    # status's row. The filing-status guard rules them distinct facts (different
    # dominant status in the text) before the slot logic runs.
    single = "Single filers are taxed at a 22% rate on income between $48,475 and $103,350."
    mfs = "For Married filing separately (MFS), the 22% rate covers $48,475 to $103,350."
    claim = Claim(subject="income", attribute="22% bracket range", value="48475-103350", functional=True)
    mapping = {single: [claim], mfs: [claim]}
    g = _graph(mapping, tabular=False)
    g.write(single, state="active")
    g.write(mfs, state="active")
    assert len(_active(g)) == 2  # same slot+value but different status -> both survive


def test_same_rate_different_range_across_statuses_not_a_contradiction():
    # Single 22% ($48,475-$103,350) vs MFJ 22% ($96,950-$206,700): same rate, different
    # range. Status-blind, the shared slot + different numeric value reads as a clash and
    # the conflict detector would reject the loser. The guard rules the pair cross-status,
    # so it is neither merged nor flagged — both ladders keep their 22% row.
    single = "Single filers: 22% rate on income between $48,475 and $103,350."
    mfj = "Married filing jointly: 22% rate on income between $96,950 and $206,700."
    mapping = {
        single: [Claim(subject="income", attribute="22% bracket upper bound", value="103350", functional=True)],
        mfj: [Claim(subject="income", attribute="22% bracket upper bound", value="206700", functional=True)],
    }
    g = _graph(mapping, tabular=False)
    g.write(single, state="active")
    g.write(mfj, state="active")
    assert len(g.contradictions()) == 0  # different status -> not a contradiction
    assert len(_active(g)) == 2  # both 22% rows survive
