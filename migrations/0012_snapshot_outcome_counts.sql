-- 0012_snapshot_outcome_counts — carry outcome-trust onto `snapshots`.
--
-- Under the canonical project-space layout the `prd-<project>` snapshot is the LIVE
-- project graph the factory reads/writes ticket state against directly (not a
-- point-in-time save loaded into working memory first). Completeness is DERIVED from
-- the outcome-trust counters, which until now lived only on `facts` (working memory) —
-- so a snapshot-bound read classified every ticket "never-built" and `record_outcome`
-- could not persist there. Add the same three columns `facts` carries so state lives
-- on the snapshot. Purely additive + idempotent; existing rows default to neutral
-- (0/0/NULL == never-built), the correct starting point.

ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS success_count integer NOT NULL DEFAULT 0;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS failure_count integer NOT NULL DEFAULT 0;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS last_outcome text;
