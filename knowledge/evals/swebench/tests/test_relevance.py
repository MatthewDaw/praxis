"""Offline U4 tests: the pre-treatment R_exist relevance oracle.

Runs fully offline — a small fake HTTP client (same shape as U3's ``FakeClient``)
returns canned ``GET /context`` responses with per-hit scores, keyed by the instance's
SPACE. No real HTTP.

    uv run pytest knowledge/evals/swebench/tests/test_relevance.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

from knowledge.evals.swebench.instances import Instance, load_candidates
from knowledge.evals.swebench.ingest import space_id_for
from knowledge.evals.swebench.relevance import (
    ABS_FLOOR,
    RelevanceResult,
    build_query,
    r_exist,
)

FIX = Path(__file__).parent / "fixtures"


def _instance() -> Instance:
    records = json.loads((FIX / "rebench_sample.json").read_text(encoding="utf-8"))
    return {i.instance_id: i for i in load_candidates(records)}["sympy__sympy-fake-0001"]


class FakeContextClient:
    """Returns canned /context hits per space; records queries for determinism checks."""

    org = "swebench_eval"

    def __init__(self, context_hits: dict | None = None):
        self.context_hits = context_hits or {}  # space -> list[hit]
        self.calls: list[tuple[str, str, int]] = []  # (space, query, top_k)

    def get_context(self, space: str, query: str, top_k: int) -> dict:
        self.calls.append((space, query, top_k))
        return {"context": "", "hits": self.context_hits.get(space, [])}


def _hit(score: float, text: str = "fact", hid: str = "f1") -> dict:
    return {"id": hid, "text": text, "score": score, "source": "git/pr:1"}


# ---------------------------------------------------------------------------
# Relevant vs empty/off-topic space.
# ---------------------------------------------------------------------------
def test_relevant_space_sets_r_exist():
    inst = _instance()
    space = space_id_for(inst)
    # A fact about a gold-changed file, comfortably above the floor.
    hits = {space: [_hit(0.72, "Matrix.multiply has an empty-cols edge case", "rel")]}
    client = FakeContextClient(context_hits=hits)

    res = r_exist(inst, client)
    assert isinstance(res, RelevanceResult)
    assert res.r_exist is True
    assert res.top_score == 0.72
    assert res.top_hit is not None and res.top_hit["id"] == "rel"


def test_empty_space_clears_nothing():
    inst = _instance()
    client = FakeContextClient(context_hits={})  # no hits for this space

    res = r_exist(inst, client)
    assert res.r_exist is False
    assert res.top_score is None
    assert res.top_hit is None


def test_off_topic_space_below_floor_is_not_relevant():
    inst = _instance()
    space = space_id_for(inst)
    # An unrelated fact that the embedder scored well below the existence floor.
    hits = {space: [_hit(0.18, "CI cache config for the docs build")]}
    client = FakeContextClient(context_hits=hits)

    res = r_exist(inst, client)
    assert res.r_exist is False
    assert res.top_score == 0.18  # surfaced for case studies, but no triggering hit
    assert res.top_hit is None


# ---------------------------------------------------------------------------
# Floor behavior — pins the comparison to ABS_FLOOR, not a magic number.
# ---------------------------------------------------------------------------
def test_hit_just_below_floor_does_not_set_r_exist():
    inst = _instance()
    space = space_id_for(inst)
    client = FakeContextClient(context_hits={space: [_hit(ABS_FLOOR - 0.01)]})

    assert r_exist(inst, client).r_exist is False


def test_hit_at_or_above_floor_sets_r_exist():
    inst = _instance()
    space = space_id_for(inst)
    # Exactly at the floor (>= is inclusive, matching the reader's abs_floor).
    at_floor = FakeContextClient(context_hits={space: [_hit(ABS_FLOOR)]})
    assert r_exist(inst, at_floor).r_exist is True

    above = FakeContextClient(context_hits={space: [_hit(ABS_FLOOR + 0.01)]})
    assert r_exist(inst, above).r_exist is True


def test_best_hit_is_chosen_among_many():
    inst = _instance()
    space = space_id_for(inst)
    hits = {space: [_hit(0.20, "low", "lo"), _hit(0.55, "best", "hi"), _hit(0.40, "mid", "md")]}
    client = FakeContextClient(context_hits=hits)

    res = r_exist(inst, client)
    assert res.r_exist is True
    assert res.top_score == 0.55
    assert res.top_hit["id"] == "hi"


# ---------------------------------------------------------------------------
# Determinism.
# ---------------------------------------------------------------------------
def test_same_space_and_instance_give_same_verdict_and_score():
    inst = _instance()
    space = space_id_for(inst)
    client = FakeContextClient(context_hits={space: [_hit(0.61)]})

    first = r_exist(inst, client)
    second = r_exist(inst, client)
    assert (first.r_exist, first.top_score) == (second.r_exist, second.top_score)
    # The oracle queried the instance's own space both times with the same query.
    assert {c[0] for c in client.calls} == {space}
    assert {c[1] for c in client.calls} == {build_query(inst)}


# ---------------------------------------------------------------------------
# Query construction.
# ---------------------------------------------------------------------------
def test_build_query_includes_gold_files_and_issue():
    inst = _instance()
    query = build_query(inst)
    for path in inst.gold_files:
        assert path in query
    assert inst.problem_statement in query
