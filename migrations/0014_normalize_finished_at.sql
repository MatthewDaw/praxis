-- 0014_normalize_finished_at — make every stored `finished_at` the one indexable shape.
--
-- `meta.finished_at` used to have two producers: the backend's lease-release path
-- (`postgres_vector_graph.release_requirement`), which writes a fixed-format UTC
-- ISO-8601 string, and agent_factory's ticket loop (`_ticket_state.release`), which
-- wrote a bare `time.time()` float. One plan carried both shapes. Nothing errored —
-- but `snapshots_finished_at_idx` (migration 0013) is a TEXT expression index over
-- the ISO shape, so an epoch row sorts as text somewhere else entirely and silently
-- drops out of every range query that uses it. A short answer, not a failure.
--
-- The server is now the sole writer (`knowledge/finished_at.py`), so no new epoch
-- rows can appear. This migration fixes the rows that already exist:
--
--   1. Epoch -> ISO. The epoch value IS the real completion instant, so this is a
--      pure reformat: no timestamp is invented, moved, or re-released. Rows are
--      matched by shape (`^digits(.digits)?$`), so an already-ISO value is untouched
--      and a re-run is a no-op.
--   2. Drop `finished_at` from tickets that are not finished. A regressed or yielded
--      ticket that kept a stale completion timestamp reads as done work it did not
--      complete; the write paths now clear it on every non-finished transition, and
--      this brings existing rows in line with that invariant.
--   3. Backfill `finished_at` = `created_at` for tickets that ARE finished but carry
--      none — every ticket completed before stamping existed. Reports already fell
--      back to `created_at` for these (the D33 COALESCE), so this changes no number;
--      it moves that fallback into the data, so `build_state = 'finished'` now
--      implies a non-null, indexable `finished_at` with no exceptions, and the
--      index serves the range query for every finished ticket rather than most.
--
-- Both tables carry ticket meta (`facts` = working memory, `snapshots` = the
-- `prd-<project>` plans the factory actually builds against), so both are normalized.

UPDATE snapshots
   SET meta = meta || jsonb_build_object(
         'finished_at',
         to_char(to_timestamp((meta ->> 'finished_at')::double precision) AT TIME ZONE 'utc',
                 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00')
 WHERE meta ->> 'finished_at' ~ '^[0-9]+(\.[0-9]+)?$';

UPDATE facts
   SET meta = meta || jsonb_build_object(
         'finished_at',
         to_char(to_timestamp((meta ->> 'finished_at')::double precision) AT TIME ZONE 'utc',
                 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00')
 WHERE meta ->> 'finished_at' ~ '^[0-9]+(\.[0-9]+)?$';

UPDATE snapshots
   SET meta = meta - 'finished_at'
 WHERE meta ? 'finished_at'
   AND COALESCE(meta ->> 'build_state', '') <> 'finished';

UPDATE facts
   SET meta = meta - 'finished_at'
 WHERE meta ? 'finished_at'
   AND COALESCE(meta ->> 'build_state', '') <> 'finished';

UPDATE snapshots
   SET meta = COALESCE(meta, '{}'::jsonb) || jsonb_build_object(
         'finished_at',
         to_char(created_at AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00')
 WHERE meta ->> 'build_state' = 'finished'
   AND NOT (meta ? 'finished_at');

UPDATE facts
   SET meta = COALESCE(meta, '{}'::jsonb) || jsonb_build_object(
         'finished_at',
         to_char(created_at AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00')
 WHERE meta ->> 'build_state' = 'finished'
   AND NOT (meta ? 'finished_at');
