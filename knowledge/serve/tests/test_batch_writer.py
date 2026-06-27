"""Orchestration tests for the parallel batch writer (no DB needed).

A ``FakeGraph`` stands in for ``PostgresVectorGraph``: it dedups by exact text
against a shared in-memory store that only mutates on ``persist`` (serial), so it
faithfully models the property that matters — Stage 1 decides against the
pre-batch state, and only the serial commit makes a write visible to the next
item. These tests pin the orchestration contract: same-batch dedup is preserved,
decides actually run concurrently, and per-item failures stay isolated.
"""

from __future__ import annotations

import threading

from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision
from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder
from knowledge.serve import batch_writer

_EMB = FakeEmbedder()

# floor 0.95: only (near-)identical text (FakeEmbedder cosine 1.0) trips the
# same-batch gate; distinct texts sit well below and never falsely merge.
_FLOOR = 0.95

_RAISE = "<<boom>>"  # decide() raises on this text to exercise error isolation


class FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeGraph:
    """Exact-text dedup against a shared store; mutates only on persist()."""

    def __init__(self, store: dict, ids: list, *, barrier: threading.Barrier | None = None) -> None:
        self.store = store  # text -> fact_id (the committed state)
        self.ids = ids  # shared id counter (list holding one int)
        self.semantic_recall_floor = _FLOOR
        self.barrier = barrier
        self.decide_calls: list[str] = []

    def decide(self, text: str, **_: object) -> WriteDecision | None:
        text = text.strip()
        if not text:
            return None
        if text == _RAISE:
            raise ValueError("reserved")
        if self.barrier is not None:
            self.barrier.wait(timeout=5)  # deadlocks unless decides run concurrently
        self.decide_calls.append(text)
        d = WriteDecision(text=text)
        d.embedding = _EMB.embed_one(text)
        if text in self.store:  # dedup against currently-committed facts
            d.action = "update"
            d.update_target_id = self.store[text]
        return d

    def persist(self, decision: WriteDecision) -> str:
        if decision.action == "update" and decision.update_target_id:
            return decision.update_target_id
        self.ids[0] += 1
        fid = f"f{self.ids[0]}"
        self.store[decision.text] = fid
        return fid

    def sibling(self, conn: object, *, policy: object = None) -> "FakeGraph":
        # Workers share the same committed store but are distinct graph instances.
        return FakeGraph(self.store, self.ids, barrier=self.barrier)


def _run(items, *, base, max_workers=4):
    conns: list[FakeConn] = []

    def connect():
        c = FakeConn()
        conns.append(c)
        return c

    outcomes = batch_writer.write_insights(
        items, base=base, connect=connect, max_workers=max_workers
    )
    return outcomes, conns


def test_same_batch_duplicates_still_dedup():
    base = FakeGraph({}, [0])
    items = [{"text": "use uv not pip"}, {"text": "use uv not pip"}, {"text": "prefer ruff"}]

    outcomes, conns = _run(items, base=base)

    # The duplicate merged into the first instead of both landing: 2 facts, not 3.
    assert len(base.store) == 2
    assert outcomes[0].fact_id == outcomes[1].fact_id  # second merged into first
    assert outcomes[2].fact_id not in (None, outcomes[0].fact_id)
    assert all(o.error is None for o in outcomes)
    assert all(c.closed for c in conns)  # every worker connection was closed


def test_dedup_against_pre_existing_fact():
    base = FakeGraph({"use uv not pip": "f0"}, [0])
    outcomes, _ = _run([{"text": "use uv not pip"}], base=base)

    # Matches a fact committed before the batch — Stage 1 recall already saw it.
    assert outcomes[0].fact_id == "f0"
    assert len(base.store) == 1


def test_distinct_items_all_added_no_false_merge():
    base = FakeGraph({}, [0])
    items = [{"text": f"distinct fact number {i}"} for i in range(6)]

    outcomes, _ = _run(items, base=base)

    assert len({o.fact_id for o in outcomes}) == 6
    assert len(base.store) == 6


def test_decides_run_concurrently():
    # A 2-party barrier inside decide() only releases if two decides are in flight
    # at once; with a serial executor this would time out and raise.
    base = FakeGraph({}, [0], barrier=threading.Barrier(2))
    items = [{"text": "alpha"}, {"text": "beta"}]

    outcomes, _ = _run(items, base=base, max_workers=2)

    assert {o.fact_id for o in outcomes} == {"f1", "f2"}


def test_per_item_failure_is_isolated():
    base = FakeGraph({}, [0])
    items = [{"text": "good one"}, {"text": _RAISE}, {"text": "good two"}]

    outcomes, _ = _run(items, base=base)

    assert outcomes[0].error is None and outcomes[0].fact_id
    assert outcomes[1].error == "reserved" and outcomes[1].fact_id is None
    assert outcomes[2].error is None and outcomes[2].fact_id
    assert len(base.store) == 2  # the bad item wrote nothing


def test_empty_input_returns_empty():
    assert batch_writer.write_insights([], base=FakeGraph({}, [0]), connect=lambda: None) == []
