-- Indexes for the exhaustive, filtered fact enumeration (`facts_by` /
-- `checks_for_surface`, the agent-factory coverage spine).
--
-- `facts_by` runs a single server-side query filtered by the `category` column and
-- the JSONB `meta` column (e.g. category='check', meta.scope='validation',
-- meta.applies_to ∋ 'auth'). The existing `facts_tenant` index covers
-- (org_id, shared, user_id, scope) but NOT category, and there is no index on meta.
-- These two add the missing coverage so the enumeration stays an indexed scan of the
-- matching partition rather than a tenant-wide sequential filter as the fact set grows.
--
-- 1. A btree on (org_id, user_id, category) — the category-driven enumeration path.
-- 2. A GIN on meta (jsonb_ops) — serves the `meta->key @> [value]` containment used
--    for array-membership (applies_to lists); scalar `meta->>key = value` is then a
--    cheap filter over the already category-narrowed rows.
--
-- Purely additive and idempotent (IF NOT EXISTS); mirrored on `cached_facts` so
-- snapshot/eval reads via the cache tables are indexed identically.

CREATE INDEX IF NOT EXISTS facts_category
    ON facts (org_id, user_id, category);

CREATE INDEX IF NOT EXISTS facts_meta_gin
    ON facts USING gin (meta);

CREATE INDEX IF NOT EXISTS cached_facts_category
    ON cached_facts (org_id, user_id, category);

CREATE INDEX IF NOT EXISTS cached_facts_meta_gin
    ON cached_facts USING gin (meta);
