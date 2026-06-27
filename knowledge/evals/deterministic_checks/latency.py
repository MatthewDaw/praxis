"""Latency / throughput checks for the bulk write+mutate paths.

Unlike the correctness checks in :mod:`graph` (which assert *what* lands in the
graph), these assert the MCP server's bulk operations are *fast enough*. Each
builds a fresh isolated Postgres tenant, drives the REAL production path (the
candidate CRUD facade :class:`FactsCandidates` and the parallel
``batch_writer``) for a batch of N items, measures wall-clock, and asserts an
amortized per-op ceiling -- while ALWAYS reporting the measured timing in the
evidence string, so a passing case still doubles as a benchmark.

Two tiers, matching the user-facing split:

* **LLM-free ops** (insert candidate, edit meta, delete) must be *wicked fast* --
  they are pure DB round-trips with no model call. Driven with a deterministic
  offline ``FakeEmbedder`` + a no-LLM ``[Redactor(), Deduper()]`` policy so the
  measurement isolates the store/round-trip cost (no network, reproducible).
  An edit that does NOT change ``content`` must not re-embed (the fast path).

* **The bulk add path** (parallel-decide / serial-commit) is measured for
  orchestration overhead and compared head-to-head with the naive serial
  ``write`` loop over the same items, so a regression in the batch machinery
  shows up as a worse speedup ratio.

DB gating: the cases declare ``embedder: cached`` so the harness SKIPs them when
no committed embedding cache / live key is present -- the same proxy the other
DSN-backed checks use for "a Postgres store is reachable here". The checks
themselves use a ``FakeEmbedder`` internally (deterministic, no vectors needed),
so no real embedding is consumed.

Thresholds are deliberately generous defaults (a dev box talking to RDS pays real
network RTT per round-trip) and every check takes ``per_op_ms`` / ``ratio``
params so a case can tighten them. The number that matters is the one printed in
the evidence; the ceiling is the regression gate.
"""

from __future__ import annotations

import uuid
from time import perf_counter

from knowledge.evals.eval_def import CheckResult, EvalContext


# --------------------------------------------------------------------------- #
# Tenant builders -- each opens an isolated throwaway (org_id, user_id) on the
# live store, wired with the deterministic offline FakeEmbedder so timings are
# reproducible and never touch the network. Mirror the cleanup pattern of the
# correctness checks (DELETE the tenant's fact_edges + facts in a finally).
# --------------------------------------------------------------------------- #
def _counting_embedder():
    """A deterministic FakeEmbedder that counts how many times it embeds.

    Lets a check assert *whether* a code path embeds at all (e.g. a meta-only edit
    should NOT), which is invisible to wall-clock alone offline (FakeEmbedder is
    instant) but is the exact prod cost when the embedder is a network call.
    """
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder

    class _Counting(FakeEmbedder):
        def __init__(self) -> None:
            self.calls = 0
            self.vectors = 0

        def embed(self, texts: list[str]):
            self.calls += 1
            self.vectors += len(texts)
            return super().embed(texts)

    return _Counting()


def _candidates(policy, embedder=None):
    """A FactsCandidates facade (the candidate CRUD path) on a fresh tenant."""
    from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder
    from knowledge.serve import db
    from knowledge.serve.facts_candidates import FactsCandidates

    conn = db.connect()
    db.bootstrap()
    org = "eval_lat_" + uuid.uuid4().hex[:12]
    facade = FactsCandidates(
        conn,
        org,
        "u1",
        embedder=embedder if embedder is not None else FakeEmbedder(),
        policy=policy or [Redactor(), Deduper()],
    )
    return facade, conn, org


def _graph():
    """A bare PostgresVectorGraph (no-LLM policy, FakeEmbedder) on a fresh tenant."""
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder
    from knowledge.serve import db

    conn = db.connect()
    db.bootstrap()
    org = "eval_lat_" + uuid.uuid4().hex[:12]
    graph = PostgresVectorGraph(
        conn, org, "u1", embedder=FakeEmbedder(), policy=[Redactor(), Deduper()]
    )
    return graph, conn, org


def _cleanup(conn, *orgs) -> None:
    for org in orgs:
        try:
            conn.execute("DELETE FROM fact_edges WHERE org_id = %s", (org,))
            conn.execute("DELETE FROM facts WHERE org_id = %s", (org,))
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass


def _probe(i: int) -> str:
    """A distinct probe fact so the Deduper keeps each as its own row (no merge)."""
    return f"Latency probe fact number {i} concerning unique subject token zeta-{i}."


def _verdict(name: str, ok: bool, *, n: int, total_s: float, per_op_ms: float, ceiling_ms: float,
             extra: str = "") -> CheckResult:
    # The <=/> symbol reflects the ACTUAL per-op vs ceiling comparison, not the
    # overall verdict (which may fail on a separate gate, e.g. wasted embeds).
    within = per_op_ms <= ceiling_ms
    return CheckResult(
        name=name,
        passed=ok,
        evidence=(
            f"{n} ops in {total_s * 1000:.0f}ms = {per_op_ms:.1f}ms/op "
            f"({'<=' if within else '>'} {ceiling_ms:.0f}ms/op ceiling)" + (f"; {extra}" if extra else "")
        ),
    )


# --------------------------------------------------------------------------- #
# Tier 1: LLM-free ops -- must be wicked fast.
# --------------------------------------------------------------------------- #
def bulk_insert_latency(
    ctx: EvalContext, *, n: int = 50, per_op_ms: float = 200.0
) -> CheckResult:
    """``n`` candidate inserts (the praxis_insert_fact / POST /candidates path) must
    average under ``per_op_ms`` per op.

    Drives the REAL :meth:`FactsCandidates.create` -- which writes through the policy
    (one recall SELECT + insert) and then re-reads the candidate (a per-op rival-map
    edge scan). No model call (FakeEmbedder + ``[Redactor(), Deduper()]``), so any
    time here is pure DB round-trips: this is the floor the bulk-insert UX rides on.
    """
    facade, conn, org = _candidates(policy=None)
    try:
        t0 = perf_counter()
        for i in range(n):
            facade.create({"title": f"probe {i}", "content": _probe(i)})
        dt = perf_counter() - t0
        per = dt / n * 1000
        return _verdict("bulk_insert_latency", per <= per_op_ms, n=n, total_s=dt,
                        per_op_ms=per, ceiling_ms=per_op_ms)
    finally:
        _cleanup(conn, org)


def bulk_update_latency(
    ctx: EvalContext, *, n: int = 50, per_op_ms: float = 150.0
) -> CheckResult:
    """``n`` metadata-only edits (the praxis_edit_fact path) must be fast AND must not
    re-embed.

    Each edit changes only the title/meta -- NOT ``content`` -- so it should take a
    no-re-embed fast path. Two gates, both required:

      * amortized wall-clock under ``per_op_ms`` per op;
      * ZERO embed calls across the timed edits.

    The embed-count gate catches a real latency trap that wall-clock alone hides
    offline: ``FactsCandidates.update`` reads the fact's existing ``text`` and passes
    it straight back to ``update_fact``, which re-embeds whenever ``text is not None``
    -- so today a pure title rename pays a full embedding (a network round-trip in
    prod) for nothing. RED until the edit path skips embedding when the content is
    unchanged. Seeds ``n`` facts first (not timed), then times the edits alone.
    """
    emb = _counting_embedder()
    facade, conn, org = _candidates(policy=None, embedder=emb)
    try:
        ids = [facade.create({"title": f"probe {i}", "content": _probe(i)})["id"] for i in range(n)]
        before = emb.calls
        t0 = perf_counter()
        for j, cid in enumerate(ids):
            facade.update(cid, {"title": f"renamed {j}"})  # meta-only: no content change
        dt = perf_counter() - t0
        embeds = emb.calls - before
        per = dt / n * 1000
        ok = per <= per_op_ms and embeds == 0
        return _verdict(
            "bulk_update_latency", ok, n=n, total_s=dt, per_op_ms=per, ceiling_ms=per_op_ms,
            extra=(
                "0 embeds on meta-only edits"
                if embeds == 0
                else f"{embeds} wasted embed(s) on {n} meta-only edits "
                "(update re-embeds unchanged content -- a network round-trip/op in prod)"
            ),
        )
    finally:
        _cleanup(conn, org)


def bulk_delete_latency(
    ctx: EvalContext, *, n: int = 50, per_op_ms: float = 100.0
) -> CheckResult:
    """``n`` deletes (the praxis_delete_fact path) must average under ``per_op_ms`` per op.

    Delete is the leanest mutation: a ``get_fact`` guard + a single DELETE (edges
    cascade). Pure DB, no model call -- the tightest "wicked fast" ceiling. Seeds
    ``n`` proposed facts first (not timed; proposed/rejected are deletable), then
    times the deletes alone.
    """
    facade, conn, org = _candidates(policy=None)
    try:
        ids = [facade.create({"title": f"probe {i}", "content": _probe(i)})["id"] for i in range(n)]
        t0 = perf_counter()
        for cid in ids:
            facade.delete(cid)  # proposed facts are deletable without a reject first
        dt = perf_counter() - t0
        per = dt / n * 1000
        return _verdict("bulk_delete_latency", per <= per_op_ms, n=n, total_s=dt,
                        per_op_ms=per, ceiling_ms=per_op_ms)
    finally:
        _cleanup(conn, org)


def bulk_insert_scales_linearly(
    ctx: EvalContext, *, n: int = 40, ratio: float = 2.6
) -> CheckResult:
    """Doubling the batch must not more-than-``ratio`` the wall-clock (catches O(n^2)).

    Times ``n`` inserts and ``2n`` inserts in two fresh tenants and asserts
    ``time(2n) <= ratio * time(n)``. A per-op rival-map/full-table scan that grows
    with the tenant's size would push the ratio toward 4x (quadratic); a healthy
    linear path sits near 2x (``ratio`` leaves headroom for fixed per-call overhead).
    """
    facade_a, conn_a, org_a = _candidates(policy=None)
    facade_b, conn_b, org_b = _candidates(policy=None)
    try:
        t0 = perf_counter()
        for i in range(n):
            facade_a.create({"title": f"a{i}", "content": _probe(i)})
        t_n = perf_counter() - t0

        t0 = perf_counter()
        for i in range(2 * n):
            facade_b.create({"title": f"b{i}", "content": _probe(i)})
        t_2n = perf_counter() - t0

        observed = (t_2n / t_n) if t_n > 0 else float("inf")
        ok = observed <= ratio
        return CheckResult(
            name="bulk_insert_scales_linearly",
            passed=ok,
            evidence=(
                f"{n} inserts={t_n * 1000:.0f}ms, {2 * n} inserts={t_2n * 1000:.0f}ms; "
                f"2x-batch ratio={observed:.2f} ({'<=' if ok else '>'} {ratio:.2f} cap)"
                + ("" if ok else " -- super-linear growth suggests a per-op full-scan")
            ),
        )
    finally:
        _cleanup(conn_a, org_a)
        _cleanup(conn_b, org_b)


# --------------------------------------------------------------------------- #
# Tier 2: the bulk add path -- LLM-backed in production, measured here for the
# orchestration overhead of parallel-decide / serial-commit vs a serial loop.
# --------------------------------------------------------------------------- #
def bulk_add_insights_throughput(
    ctx: EvalContext,
    *,
    n: int = 40,
    per_op_ms: float = 250.0,
    max_workers: int = 4,
    min_speedup: float = 0.0,
) -> CheckResult:
    """The parallel ``batch_writer`` must commit ``n`` insights under ``per_op_ms``/op
    and be no slower than the serial loop (``min_speedup`` ratio gate).

    Drives the REAL :func:`batch_writer.write_insights` (parallel-decide /
    serial-commit) against one tenant, then the naive ``for item: graph.write``
    serial loop against a second tenant, over the same ``n`` distinct items. Reports
    both wall-clocks and the speedup. With a FakeEmbedder + no-LLM policy there is no
    network work to overlap, so this measures the *orchestration floor* (worker
    connections + reconciliation) -- in production the parallelised embed+recall+judge
    is where the win lands. ``min_speedup`` defaults to 0 (ceiling-gated only) so the
    case does not fail on a box where thread/connection overhead dominates the
    offline path; set it >1 to assert a real speedup where the embedder is networked.
    """
    from knowledge.serve import batch_writer, db

    base, base_conn, org = _graph()
    serial, serial_conn, org2 = _graph()
    items = [{"text": _probe(i), "state": "active"} for i in range(n)]
    try:
        from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor

        t0 = perf_counter()
        outcomes = batch_writer.write_insights(
            items,
            base=base,
            connect=db.connect,
            policy_factory=lambda: [Redactor(), Deduper()],
            max_workers=max_workers,
        )
        dt_batch = perf_counter() - t0
        written = sum(1 for o in outcomes if o.fact_id)

        t0 = perf_counter()
        for it in items:
            serial.write(it["text"], state="active")
        dt_serial = perf_counter() - t0

        per = dt_batch / max(written, 1) * 1000
        speedup = (dt_serial / dt_batch) if dt_batch > 0 else float("inf")
        all_landed = written == n  # distinct items: none should dedup away
        ok = all_landed and per <= per_op_ms and speedup >= min_speedup
        return CheckResult(
            name="bulk_add_insights_throughput",
            passed=ok,
            evidence=(
                f"batch: {written}/{n} written in {dt_batch * 1000:.0f}ms = {per:.1f}ms/op "
                f"({'<=' if per <= per_op_ms else '>'} {per_op_ms:.0f}ms/op); "
                f"serial: {dt_serial * 1000:.0f}ms; speedup={speedup:.2f}x "
                f"(>= {min_speedup:.2f} gate; workers={max_workers})"
                + ("" if all_landed else f" -- only {written}/{n} landed (dedup collapse?)")
            ),
        )
    finally:
        _cleanup(base_conn, org)
        _cleanup(serial_conn, org2)
