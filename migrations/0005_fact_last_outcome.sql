-- Track the most-recent verification outcome per fact.
--
-- `success_count`/`failure_count` (migration 0004) are cumulative and cannot tell a
-- requirement that passed once and later regressed (succeeded-then-failed) from one
-- that is still passing. Derived-completeness queries (incomplete_requirements /
-- completeness_summary) need that distinction, so we record the *latest* outcome.
--
-- 'succeeded' | 'failed' | NULL (never verified). Purely additive and idempotent
-- (IF NOT EXISTS); fresh databases get this from the 0000 baseline. Re-running is
-- harmless and existing rows keep NULL (= never verified), which reads as
-- "never-built" — the correct default for a requirement with no outcome yet.

ALTER TABLE facts ADD COLUMN IF NOT EXISTS last_outcome text;
