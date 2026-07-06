-- 0008_snapshots_rekey — rename `cached_facts` -> `snapshots` and re-key the graph
-- cache from (org_id, user_id, cache_key) to (org_id, space, snapshot).
--
-- Part of the org -> space -> snapshot tenancy redesign
-- (specs/005-praxis-tenancy-redesign). A snapshot is an ORG-SHARED named graph
-- that lives inside a space, so it no longer carries `user_id` or `shared`. The
-- old mangled user_id (`{sub}::space:<sid>`) yields the space (`<sid>`, else
-- `default`); the old cache_key (minus its `snapshot:` type prefix) yields the
-- bare snapshot name. Eval caches (`eval:<case_id>`) move to the reserved space
-- `__evals__`, keeping the bare case id as their snapshot name. The satellites
-- `cached_fact_edges` / `cached_claims` are renamed to `snapshot_edges` /
-- `snapshot_claims` and re-keyed with the SAME derivation so their FKs line up.
--
-- Guarded by `to_regclass('cached_facts')`: on a fresh DB, 0000 first creates the
-- old cached_facts shape and this rewrites it into the new baseline; on an
-- already-migrated DB (cached_facts gone) the whole block no-ops. Runs inside a
-- single DO block so a mid-transform failure rolls the table set back intact.

DO $$
DECLARE
    r record;
BEGIN
    IF to_regclass('public.cached_facts') IS NULL THEN
        RETURN;  -- already migrated (or never existed)
    END IF;

    -- 1. Rename the three cache tables to their snapshot names. Indexes keep
    --    their (cached_*) names across the rename and are swapped in step 7.
    ALTER TABLE cached_facts      RENAME TO snapshots;
    ALTER TABLE cached_fact_edges RENAME TO snapshot_edges;
    ALTER TABLE cached_claims     RENAME TO snapshot_claims;

    -- 2. Drop the satellites' FKs + PKs first: they reference the user_id /
    --    cache_key columns about to be dropped/renamed on snapshots. Constraint
    --    names are auto-generated, so drop them by catalog lookup.
    FOR r IN
        SELECT conname, conrelid::regclass AS tbl
        FROM pg_constraint
        WHERE conrelid IN ('snapshot_edges'::regclass, 'snapshot_claims'::regclass)
          AND contype IN ('f', 'p')
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
    END LOOP;

    -- 3. snapshots: derive space, rename cache_key -> snapshot, strip prefixes.
    ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS space text;
    UPDATE snapshots
       SET space = COALESCE(NULLIF(split_part(user_id, '::space:', 2), ''), 'default');
    ALTER TABLE snapshots RENAME COLUMN cache_key TO snapshot;
    UPDATE snapshots SET snapshot = regexp_replace(snapshot, '^snapshot:', '');
    -- Eval caches -> reserved space + bare case id. The 'snapshot:' strip above
    -- leaves 'eval:%' rows untouched, so matching the pre-strip 'eval:' value
    -- still works here.
    UPDATE snapshots SET space = '__evals__' WHERE snapshot LIKE 'eval:%';
    UPDATE snapshots SET snapshot = regexp_replace(snapshot, '^eval:', '')
     WHERE space = '__evals__';

    -- 4. Data-loss guard: no cache row was ever org-shared (all shared=false).
    IF EXISTS (SELECT 1 FROM snapshots WHERE shared) THEN
        RAISE EXCEPTION 'shared=true snapshot rows exist; visibility intent would be lost';
    END IF;

    -- 5. Drop the old PK + user_id/shared; re-key on (org_id, space, snapshot, id).
    FOR r IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'snapshots'::regclass AND contype = 'p'
    LOOP
        EXECUTE format('ALTER TABLE snapshots DROP CONSTRAINT %I', r.conname);
    END LOOP;
    ALTER TABLE snapshots
        DROP COLUMN user_id,
        DROP COLUMN shared,
        ALTER COLUMN space SET NOT NULL,
        ADD CONSTRAINT snapshots_pkey PRIMARY KEY (org_id, space, snapshot, id);

    -- 6. Satellites: identical derivation so the re-keyed FKs match snapshots.
    ALTER TABLE snapshot_edges ADD COLUMN IF NOT EXISTS space text;
    UPDATE snapshot_edges
       SET space = COALESCE(NULLIF(split_part(user_id, '::space:', 2), ''), 'default');
    ALTER TABLE snapshot_edges RENAME COLUMN cache_key TO snapshot;
    UPDATE snapshot_edges SET snapshot = regexp_replace(snapshot, '^snapshot:', '');
    UPDATE snapshot_edges SET space = '__evals__' WHERE snapshot LIKE 'eval:%';
    UPDATE snapshot_edges SET snapshot = regexp_replace(snapshot, '^eval:', '')
     WHERE space = '__evals__';
    ALTER TABLE snapshot_edges
        DROP COLUMN user_id,
        ALTER COLUMN space SET NOT NULL,
        ADD CONSTRAINT snapshot_edges_pkey
            PRIMARY KEY (org_id, space, snapshot, src_id, dst_id, kind),
        ADD CONSTRAINT snapshot_edges_src_fkey
            FOREIGN KEY (org_id, space, snapshot, src_id)
            REFERENCES snapshots (org_id, space, snapshot, id) ON DELETE CASCADE,
        ADD CONSTRAINT snapshot_edges_dst_fkey
            FOREIGN KEY (org_id, space, snapshot, dst_id)
            REFERENCES snapshots (org_id, space, snapshot, id) ON DELETE CASCADE;

    ALTER TABLE snapshot_claims ADD COLUMN IF NOT EXISTS space text;
    UPDATE snapshot_claims
       SET space = COALESCE(NULLIF(split_part(user_id, '::space:', 2), ''), 'default');
    ALTER TABLE snapshot_claims RENAME COLUMN cache_key TO snapshot;
    UPDATE snapshot_claims SET snapshot = regexp_replace(snapshot, '^snapshot:', '');
    UPDATE snapshot_claims SET space = '__evals__' WHERE snapshot LIKE 'eval:%';
    UPDATE snapshot_claims SET snapshot = regexp_replace(snapshot, '^eval:', '')
     WHERE space = '__evals__';
    ALTER TABLE snapshot_claims
        DROP COLUMN user_id,
        ALTER COLUMN space SET NOT NULL,
        ADD CONSTRAINT snapshot_claims_pkey
            PRIMARY KEY (org_id, space, snapshot, fact_id, seq),
        ADD CONSTRAINT snapshot_claims_fact_fkey
            FOREIGN KEY (org_id, space, snapshot, fact_id)
            REFERENCES snapshots (org_id, space, snapshot, id) ON DELETE CASCADE;

    -- 7. Swap the indexes: drop cached_* (they survived the table rename), then
    --    create the snapshots_* / snapshot_claims_slot set (see §1.4–1.6).
    DROP INDEX IF EXISTS cached_facts_tenant;
    DROP INDEX IF EXISTS cached_facts_embedding_hnsw;
    DROP INDEX IF EXISTS cached_facts_text_tsv_gin;
    DROP INDEX IF EXISTS cached_facts_key;
    DROP INDEX IF EXISTS cached_facts_category;
    DROP INDEX IF EXISTS cached_facts_meta_gin;
    DROP INDEX IF EXISTS cached_claims_slot;

    CREATE INDEX IF NOT EXISTS snapshots_tenant   ON snapshots (org_id, space, scope);
    CREATE INDEX IF NOT EXISTS snapshots_key      ON snapshots (org_id, space, snapshot);
    CREATE INDEX IF NOT EXISTS snapshots_category ON snapshots (org_id, space, snapshot, category);
    CREATE INDEX IF NOT EXISTS snapshots_meta_gin ON snapshots USING gin (meta);
    CREATE INDEX IF NOT EXISTS snapshots_embedding_hnsw
        ON snapshots USING hnsw (embedding vector_cosine_ops);
    CREATE INDEX IF NOT EXISTS snapshots_text_tsv_gin ON snapshots USING gin (text_tsv);
    CREATE INDEX IF NOT EXISTS snapshot_claims_slot
        ON snapshot_claims (org_id, space, snapshot, subject, attribute) WHERE functional;
END $$;
