"""Force/raw inserts are PERMANENTLY exempt from the distillation/dedup/conflict pipeline.

Regression for the bulk plan-intake footgun: a fact added with ``raw=True`` (documented as
"skips dedup AND the per-item LLM conflict/claim check") was still re-processed by the async
distillation pass — topically-similar ``category="requirement"`` tickets returned ``action="added"``
but were later auto-``rejected`` (contradiction resolved) or silently ``merged`` into a near-duplicate,
so bulk intake lost tickets. A forced insert must honor the contract literally: land ``active``,
never be auto-merged or auto-rejected (on its own write OR any later re-distill), stay embedded
(retrievable) and secret-redacted, and preserve its ``meta`` (including arrays like ``defines``)
verbatim.

Offline via ``FakeEmbedder`` + ``FakeLlm`` (hash-based / scripted), so the pipeline's merge and
contradiction verdicts are deterministic. ``recall_floor=-1.0`` opts every candidate into the recall
set so the judge/detector wiring is actually exercised (the fake embedder alone sits ~0 cosine).
"""

from __future__ import annotations

from knowledge.knowledge_graph.knowledge_graph_def import Claim, Fact
from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import (
    VectorGraph,
    is_forced,
)
from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_step_variants import (
    ClaimConflictDetector,
    Deduper,
    MergeJudge,
    Redactor,
)
from knowledge.llm.llm_variants.fake_llm import FakeLlm

# Two DELIBERATELY topically-similar requirement tickets (the exact shape bulk intake trips on).
_TICKET_A = "the source-discriminator dedups scraped rows by canonical url"
_TICKET_B = "the source-discriminator must also dedup scraped rows by product id"


class _Claims(WriteStep):
    """Test stand-in for ClaimExtractor: assigns claims by exact text match."""

    consumes_candidates = False

    def __init__(self, mapping: dict[str, list[Claim]]) -> None:
        self._m = mapping

    def apply(self, decision) -> None:
        decision.claims = list(self._m.get(decision.text, []))


def _merging_policy() -> list[WriteStep]:
    """A pipeline that WOULD fold two topically-similar facts into one (the hazard).

    The merge judge always says "same lesson", so absent the forced exemption the second
    write dedup-merges into the first (``action="update"``) — exactly the silent drop.
    """
    return [Redactor(), Deduper(judge=MergeJudge(llm=FakeLlm(default='{"same_lesson": true}')))]


def test_normal_insert_still_dedups_unchanged():
    """Control / non-regression: ordinary (non-forced) writes still dedup as before, so the
    forced exemption is a genuine opt-in, not a global disabling of the pipeline."""
    g = VectorGraph(policy=_merging_policy(), recall_floor=-1.0)
    g.write(_TICKET_A, state="active")
    g.write(_TICKET_B, state="active")  # judge says same lesson -> merged into the first
    assert len(g.facts) == 1  # one survivor
    assert g.facts[0].observation_count == 2  # the merge bumped it


def test_forced_insert_survives_the_distillation_pipeline():
    """THE regression: two topically-similar requirement facts force-inserted BOTH land active,
    neither merged, meta (incl. the ``defines`` array) preserved verbatim — on the very pipeline
    that merges the non-forced pair in the control above."""
    g = VectorGraph(policy=_merging_policy(), recall_floor=-1.0)
    meta_a = {"requirement_id": "R30", "build_state": "incomplete", "defines": ["dedup", "url"]}
    meta_b = {"requirement_id": "R42", "build_state": "incomplete", "defines": ["dedup", "product"]}
    g.write(_TICKET_A, category="requirement", meta=dict(meta_a), forced=True)
    g.write(_TICKET_B, category="requirement", meta=dict(meta_b), forced=True)

    # Both survived as DISTINCT active facts — nothing merged, nothing rejected.
    assert len(g.facts) == 2
    assert all(f.state == "active" for f in g.facts)
    assert all(f.observation_count == 1 for f in g.facts)  # no dedup bump

    by_text = {f.text: f for f in g.facts}
    # meta preserved verbatim, including the array; the forced marker is stamped for round-trip.
    assert by_text[_TICKET_A].meta["defines"] == ["dedup", "url"]
    assert by_text[_TICKET_A].meta["requirement_id"] == "R30"
    assert by_text[_TICKET_B].meta["defines"] == ["dedup", "product"]
    assert all(is_forced(f) for f in g.facts)
    # Forced facts stay retrievable (embedded, active) — exemption is not suppression.
    assert len(g.search(_TICKET_A, state="active")) >= 1


def test_forced_insert_is_still_secret_redacted():
    """The forced fast lane keeps the cheap regex Redactor: secrets are still scrubbed."""
    g = VectorGraph(policy=_merging_policy(), recall_floor=-1.0)
    g.write("contact the on-call at ops-secret@example.com for the runbook", forced=True)
    stored = g.facts[0].text
    assert "ops-secret@example.com" not in stored
    assert "[REDACTED]" in stored


def test_forced_incumbent_is_never_merged_or_rejected_by_a_later_write():
    """A forced fact is exempt as an INCUMBENT too: a later non-forced write that would normally
    fold into it (or contradict-reject it) leaves it untouched and active — it is excluded from
    the write-time recall, so nothing can merge into or reject it."""
    g = VectorGraph(policy=_merging_policy(), recall_floor=-1.0)
    forced_fact = g.write(_TICKET_A, forced=True)
    assert forced_fact is not None
    forced_id = forced_fact.added_fact_id

    # A later ordinary write on the very same topic: the merge judge says "same lesson", but the
    # forced incumbent is not even a candidate, so this lands as its OWN new active fact.
    later = g.write(_TICKET_B, state="active")
    assert later is not None
    assert later.action == "add"  # not "update" -> did NOT merge into the forced incumbent
    incumbent = next(f for f in g.facts if f.id == forced_id)
    assert incumbent.state == "active"  # never rejected
    assert incumbent.observation_count == 1  # never absorbed a merge


def test_forced_insert_survives_a_save_reload_cycle():
    """The exemption round-trips: after serializing + reloading the facts (a save/snapshot cycle),
    forced facts are still active with meta intact, and a re-distill (re-writing the persisted
    text+meta) re-bypasses the pipeline because ``meta['forced']`` is honored on read."""
    g = VectorGraph(policy=_merging_policy(), recall_floor=-1.0)
    g.write(_TICKET_A, category="requirement",
            meta={"requirement_id": "R30", "defines": ["dedup", "url"]}, forced=True)
    g.write(_TICKET_B, category="requirement",
            meta={"requirement_id": "R42", "defines": ["dedup", "product"]}, forced=True)

    # Save -> reload: rebuild a fresh graph from the serialized facts (round-trips meta['forced']).
    dumped = [f.model_dump() for f in g.facts]
    reloaded = VectorGraph(policy=_merging_policy(), recall_floor=-1.0)
    reloaded._facts = [Fact.model_validate(d) for d in dumped]

    assert all(f.state == "active" for f in reloaded._facts)
    assert all(is_forced(f) for f in reloaded._facts)
    by_text = {f.text: f for f in reloaded._facts}
    assert by_text[_TICKET_A].meta["defines"] == ["dedup", "url"]
    assert by_text[_TICKET_B].meta["defines"] == ["dedup", "product"]

    # Re-distill: re-submit each forced fact's own text WITH its persisted meta. Because
    # meta['forced'] is set, write() re-bypasses the pipeline (no merge/reject) even though the
    # forced arg isn't re-passed — the exemption is durable, not a one-shot at first insert.
    before = len(reloaded._facts)
    for f in list(reloaded._facts):
        d = reloaded.write(f.text, category="requirement", meta=dict(f.meta))
        assert d is not None
        assert d.action == "add"  # never merged/rejected on re-distill
        assert d.state == "active"
    # The originals are all still active after the re-distill pass.
    assert all(f.state == "active" for f in reloaded._facts[:before])


def test_forced_insert_bypasses_the_contradiction_detector():
    """A forced insert whose functional claim clashes with an existing fact is NOT flagged or
    demoted — the conflict/claim pipeline is skipped entirely for it (so it can never be
    auto-rejected as a 'resolved' contradiction)."""
    mapping = {
        "the deploy timeout is 30 seconds": [
            Claim(subject="deploy", attribute="timeout", value="30", functional=True)
        ],
        "the deploy timeout is 60 seconds": [
            Claim(subject="deploy", attribute="timeout", value="60", functional=True)
        ],
    }
    policy = [
        _Claims(mapping),
        Deduper(judge=MergeJudge(llm=FakeLlm(default='{"same_lesson": false}'))),
        ClaimConflictDetector(),
    ]
    g = VectorGraph(policy=policy, recall_floor=-1.0)
    g.write("the deploy timeout is 30 seconds", state="active")
    # A clashing value, but forced -> no contradiction is detected and it lands active.
    d = g.write("the deploy timeout is 60 seconds", forced=True)
    assert d is not None
    assert d.state == "active"  # not demoted to "proposed" (FR-005 never triggers)
    assert g.contradictions() == []  # detector skipped for the forced write
    assert {f.state for f in g.facts} == {"active"}  # both live
