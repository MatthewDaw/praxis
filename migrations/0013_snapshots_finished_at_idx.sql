-- 0013_snapshots_finished_at_idx — a range-queryable index on the ticket finish timestamp.
--
-- The S4 (ticket-completion-series) query filters `snapshots` by the ticket finish
-- timestamp, stored as `meta->>'finished_at'` (a fixed-format, zero-padded UTC
-- ISO-8601 string set when a ticket lands — e.g. "2026-07-25T03:50:06.740712+00:00").
-- `snapshots_meta_gin` is a GIN index over the whole `meta` jsonb column: it serves
-- containment/key-existence lookups, not a `<`/`>`/`BETWEEN` predicate on an
-- extracted key. Without a dedicated index, any range scan over `finished_at` falls
-- back to a full Seq Scan of every snapshot row.
--
-- Index the extracted key as TEXT rather than casting to timestamptz: that cast
-- (`timestamptz_in`) is STABLE, not IMMUTABLE (its result can depend on the session
-- `TimeZone` GUC), so Postgres refuses it in an expression index. A fixed-format
-- UTC ISO-8601 string sorts lexicographically identically to its chronological
-- order, so a plain (immutable) text expression index serves the same `BETWEEN`
-- range predicate, compared as text against two ISO-8601 bounds.

CREATE INDEX IF NOT EXISTS snapshots_finished_at_idx
    ON snapshots (((meta ->> 'finished_at')));
