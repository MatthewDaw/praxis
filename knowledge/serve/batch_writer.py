"""Parallel-decide / serial-commit batch writer that preserves dedup.

The bulk ``/insights/batch`` write path is dominated by per-item work that is
*read-only*: embed the text, recall similar facts, and run the policy judges
(dedup / conflict LLM calls). Only the final persist mutates the graph. The loop
is serial today solely because item N's recall must see items 1..N-1 already
committed — that is how two near-duplicate items in the same batch get merged
instead of both landing.

This module splits that dependency:

* **Stage 1 (parallel, read-only).** A bounded worker pool runs
  ``graph.decide`` for every item against the committed-at-batch-start state,
  each worker on its OWN connection (a psycopg connection is not safe for
  concurrent cross-thread use). The expensive recalls + judge calls happen here,
  concurrently.

* **Stage 2 (serial, base connection).** Walk the decisions in order, keeping an
  in-memory list of what THIS batch has already committed (each fact's embedding
  and functional claim slots). For each item, a cheap in-memory check
  (cosine >= ``semantic_recall_floor`` — the widest recall net — or a shared
  functional slot) detects the one thing Stage 1 could not see: a collision with
  an earlier item of the *same* batch. On a hit we re-run ``decide`` on the base
  connection (which now sees that earlier commit) so the real policy adjudicates
  it exactly as the serial loop would; otherwise the parallel decision stands.
  Then ``persist``.

Net: dedup against everything committed before the batch is handled by Stage 1's
full recall; dedup against same-batch items is handled by Stage 2's
reconciliation. Their union is exactly what the serial loop catches — no dedup is
lost — while the costly recall+judge work runs in parallel.

The in-memory gate is intentionally conservative: it only decides *whether* to
re-decide, so a false positive merely costs one extra (correct) re-decide, while
its floor/slot coverage guarantees no real collision slips through for the
policies in use (the dedup/augment/conflict steps all act on candidates bounded
by ``semantic_recall_floor`` or a shared functional slot). A future policy step
that recalls on a different key (e.g. aspect tags) would need its key added here.
"""

from __future__ import annotations

import math
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision

# Conservative default: a handful of concurrent workers cuts wall-clock without
# stampeding the embedder/LLM provider (429s retry, but proactively gentle) or
# opening too many Postgres connections. The endpoint may override via env.
DEFAULT_MAX_WORKERS = 4

# Keys on a batch item that map to ``decide`` keyword arguments. ``text`` is the
# positional content and handled separately.
_DECIDE_KEYS = ("state", "source", "scope", "category", "meta", "derived_from")


@dataclass
class BatchOutcome:
    """The result of one batch item, aligned by index with the input list.

    Exactly one shape applies: ``error`` set (the item failed in isolation),
    ``decision`` + ``fact_id`` set (written), or all ``None`` (a policy step
    dropped the write — empty/suppressed)."""

    decision: WriteDecision | None = None
    fact_id: str | None = None
    error: str | None = None


def _decide_kwargs(item: dict) -> dict:
    return {k: item[k] for k in _DECIDE_KEYS if item.get(k) is not None}


def _slots(decision: WriteDecision) -> set[tuple[str, str]]:
    """The functional (subject, attribute) slots this write occupies, if any.

    Empty unless a ClaimExtractor step ran (the ``surface`` policy); the
    ``auto_resolve`` policy has no claims, so the gate is cosine-only there."""
    return {c.slot for c in decision.claims if c.functional}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _collides_with_batch(
    decision: WriteDecision,
    committed: list[tuple[list[float] | None, set[tuple[str, str]]]],
    floor: float,
) -> bool:
    """True if this write could dedup/conflict against an item committed earlier
    in THIS batch — the only collisions Stage 1's pre-batch recall could miss."""
    slots = _slots(decision)
    emb = decision.embedding
    for c_emb, c_slots in committed:
        if slots and c_slots and (slots & c_slots):
            return True
        if emb and c_emb and _cosine(emb, c_emb) >= floor:
            return True
    return False


def write_insights(
    items: list[dict],
    *,
    base: Any,
    connect: Callable[[], Any],
    close: Callable[[Any], None] | None = None,
    policy_factory: Callable[[], list] | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> list[BatchOutcome]:
    """Decide ``items`` in parallel, commit serially; one outcome per item, in order.

    ``base`` is the graph bound to the request's serial connection (used for the
    final ``persist``, the reconciliation floor, and any re-decide). ``connect``
    opens a fresh autocommit connection for a worker thread; ``close`` releases one
    (defaults to ``conn.close()``) — pass a no-op when the connection is shared and
    owned elsewhere (e.g. a single explicit test connection, with ``max_workers=1``).
    ``policy_factory`` yields a fresh policy list per worker when the steps hold
    per-call state (None shares ``base``'s policy). Each ``item`` is a dict with
    ``text`` plus optional ``state``/``source``/``scope``/``category``/
    ``meta``/``derived_from``.
    """
    if not items:
        return []
    n_workers = max(1, min(max_workers, len(items)))

    # --- Stage 1: parallel, read-only decide (each worker on its own connection).
    local = threading.local()  # per-call: one worker graph+conn per pool thread
    conns: list[Any] = []
    conns_lock = threading.Lock()

    def worker_graph() -> Any:
        g = getattr(local, "graph", None)
        if g is None:
            conn = connect()
            with conns_lock:
                conns.append(conn)
            policy = policy_factory() if policy_factory is not None else None
            g = base.sibling(conn, policy=policy)
            local.graph = g
        return g

    def decide_one(item: dict) -> tuple[str, Any]:
        # Isolate per-item failures so one bad item never aborts the batch.
        try:
            return ("ok", worker_graph().decide(item["text"], **_decide_kwargs(item)))
        except Exception as exc:  # noqa: BLE001 - reported per item, not raised
            return ("err", str(exc))

    try:
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            staged = list(pool.map(decide_one, items))
    finally:
        closer = close if close is not None else (lambda c: c.close())
        with conns_lock:
            for conn in conns:
                try:
                    closer(conn)
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass

    # --- Stage 2: serial commit on the base connection, with same-batch reconciliation.
    floor = base.semantic_recall_floor
    committed: list[tuple[list[float] | None, set[tuple[str, str]]]] = []
    outcomes: list[BatchOutcome] = []
    for item, (status, payload) in zip(items, staged):
        if status == "err":
            outcomes.append(BatchOutcome(error=payload))
            continue
        decision: WriteDecision | None = payload
        try:
            # If an earlier same-batch item could collide, the parallel decision
            # is stale — re-decide on the base connection, which now sees that
            # commit, so the real policy adjudicates it just like the serial loop.
            if decision is not None and _collides_with_batch(decision, committed, floor):
                decision = base.decide(item["text"], **_decide_kwargs(item))
            if decision is None:
                outcomes.append(BatchOutcome())  # empty/suppressed
                continue
            fact_id = base.persist(decision)
            committed.append((decision.embedding, _slots(decision)))
            outcomes.append(BatchOutcome(decision=decision, fact_id=fact_id))
        except Exception as exc:  # noqa: BLE001 - per-item isolation
            outcomes.append(BatchOutcome(error=str(exc)))
    return outcomes
