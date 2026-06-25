# Bulk write throughput + confirmable read-your-writes (H8)

**Status:** implemented
**Gap:** H8 (`praxis-gaps.md`) — *Bulk write throughput / synchronous read-your-writes — PARTIAL*

## Problem

The local factory loop accumulates many confirmed learnings per session and
writes them back at the end. Today the only shaped-fact write surface is
`POST /insights` (and MCP `praxis_add_insight`) — **one fact per call**. Writing N
learnings means N HTTP/auth round-trips, N graph/embedder/connection setups, and —
if the loop fires them concurrently to go faster — the conflict-checked write path
can buckle under the burst (the failure mode tracked in H13.2). The doc's interim
guidance is exactly "serial conflict-checked writes, never parallel bursts," which
is awkward to honour from N separate calls.

Separately, callers want a **confirmable** write: did the fact actually land and is
it retrievable *now*, without polling? Connections are autocommit, so a write is
visible the moment the call returns — but nothing in the response said so.

This is explicitly scoped to **throughput + confirmability**, not async writes.
Making the conflict check asynchronous (so writes return before it runs) is H13's
stretch item and is deliberately left alone here.

## What shipped

1. **`POST /insights/batch`** — accepts `{"insights": [ <same shape as /insights> ],
   "onConflict": "auto_resolve"|"surface"}`. Builds the policy graph + ingestor
   **once** and writes the items **serially** within the single request. One
   round-trip, one setup, no concurrent burst. Episodic items (H4) and writer
   metadata (H12) are honoured per item. Returns one result per item, in order.
2. **Confirmable read-your-writes** — every per-item result (and now the single
   `POST /insights` response too) carries `retrievable: bool`, set from an
   immediate read-back of the just-written fact. A caller can trust the write
   without a follow-up query.
3. **Clean per-item failure** — a malformed/empty item yields
   `{"ok": false, "error": ...}` for that slot and does **not** abort the rest of
   the batch (the good items still land).
4. **MCP `praxis_add_insights`** — bulk sibling of `praxis_add_insight`, same
   per-item shape, batch-level `on_conflict`, structured per-item results.

## Deliberately out of scope

- **Async / job-queue writes** and making conflict-checking non-blocking — H13.
- **Batched embedding** (one embedder call for all items in a batch) — a further
  throughput win, but it requires threading precomputed embeddings through the
  write policy; left as a follow-up so this change stays additive and low-risk.
- `POST /ingest` (raw-document distillation) is already a batch surface; H8 targets
  the **shaped-fact** lane.

## Tests

- `knowledge/serve/tests/test_server.py`:
  `test_insights_batch_writes_all_and_confirms_retrievable`,
  `test_insights_batch_bad_item_does_not_abort_batch`,
  `test_insights_batch_requires_nonempty_list`.
- `knowledge/mcp/tests/test_server.py`:
  `test_add_insights_batch_posts_list_and_summarizes`,
  `test_add_insights_batch_rejects_empty_list`.
