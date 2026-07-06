-- 0010_facts_working_memory — collapse `facts` to a per-user private working
-- memory and retire the `{sub}::space:<sid>` named-graph hack.
--
-- depends: 0008_snapshots_rekey
--
-- Part of the org -> space -> snapshot tenancy redesign
-- (specs/005-praxis-tenancy-redesign). Working memory is now the LIVE scratch
-- graph keyed strictly (org_id, user_id) where user_id is the real authenticated
-- sub — no `shared`, and space/snapshot NEVER appear on it.
--
-- RESOLUTION (data-loss judgment call): a mangled `{sub}::space:<sid>` LIVE graph
-- is NOT merged into the real user's private working memory (that would pollute
-- the authenticated scratch graph, and the model forbids space on working
-- memory). Instead it becomes a snapshot named `working` inside space `<sid>`.
-- Raw-sub rows (real subs) are left untouched = that user's working memory.
--
-- Depends on 0008 for the `snapshots` / `snapshot_edges` / `snapshot_claims`
-- target tables. Guarded by the presence of `facts.shared`; no-ops once migrated.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'facts' AND column_name = 'shared'
    ) THEN
        RETURN;  -- already migrated
    END IF;

    -- 1. Move mangled named-graph LIVE facts into snapshots(space=<sid>,
    --    snapshot='working'). text_tsv is GENERATED, so it is NOT listed.
    --    Working memory keeps outcome-trust columns; snapshots omit them.
    INSERT INTO snapshots
        (id, org_id, space, snapshot, text, source, confidence, scope, category,
         observation_count, state, embedding, cluster_id, cluster_label,
         valid_at, invalid_at, meta, created_at)
    SELECT id, org_id, split_part(user_id, '::space:', 2), 'working',
           text, source, confidence, scope, category, observation_count, state,
           embedding, cluster_id, cluster_label, valid_at, invalid_at, meta, created_at
    FROM facts WHERE user_id LIKE '%::space:%'
    ON CONFLICT DO NOTHING;

    INSERT INTO snapshot_edges (org_id, space, snapshot, src_id, dst_id, kind)
    SELECT org_id, split_part(user_id, '::space:', 2), 'working', src_id, dst_id, kind
    FROM fact_edges WHERE user_id LIKE '%::space:%'
    ON CONFLICT DO NOTHING;

    INSERT INTO snapshot_claims
        (org_id, space, snapshot, fact_id, seq, subject, attribute, value, functional, created_at)
    SELECT org_id, split_part(user_id, '::space:', 2), 'working',
           fact_id, seq, subject, attribute, value, functional, created_at
    FROM claims WHERE user_id LIKE '%::space:%'
    ON CONFLICT DO NOTHING;

    -- 2. Drop the mangled facts (fact_edges/claims cascade via their FKs). The
    --    raw-sub rows stay = each user's private working memory.
    DELETE FROM facts WHERE user_id LIKE '%::space:%';

    -- 3. Drop `facts.shared` (same data-loss guard as 0008).
    IF EXISTS (SELECT 1 FROM facts WHERE shared) THEN
        RAISE EXCEPTION 'shared=true facts rows exist; visibility intent would be lost';
    END IF;
    ALTER TABLE facts DROP COLUMN shared;

    -- 4. Rebuild facts_tenant without the `shared` column.
    DROP INDEX IF EXISTS facts_tenant;
    CREATE INDEX facts_tenant ON facts (org_id, user_id, scope);

    -- 5. coding-validation -> building-validation rename (design point 7) across
    --    snapshots + satellites + the spaces registry. The satellite FKs lack
    --    ON UPDATE CASCADE, so drop them around the rename and re-add after (all
    --    children reference valid parents again once the four UPDATEs complete).
    ALTER TABLE snapshot_edges  DROP CONSTRAINT snapshot_edges_src_fkey;
    ALTER TABLE snapshot_edges  DROP CONSTRAINT snapshot_edges_dst_fkey;
    ALTER TABLE snapshot_claims DROP CONSTRAINT snapshot_claims_fact_fkey;

    UPDATE snapshots       SET space = 'building-validation' WHERE space = 'coding-validation';
    UPDATE snapshot_edges  SET space = 'building-validation' WHERE space = 'coding-validation';
    UPDATE snapshot_claims SET space = 'building-validation' WHERE space = 'coding-validation';
    UPDATE spaces SET space_id = 'building-validation'
     WHERE space_id = 'coding-validation'
       AND NOT EXISTS (
           SELECT 1 FROM spaces b
           WHERE b.org_id = spaces.org_id AND b.space_id = 'building-validation'
       );

    ALTER TABLE snapshot_edges
        ADD CONSTRAINT snapshot_edges_src_fkey
            FOREIGN KEY (org_id, space, snapshot, src_id)
            REFERENCES snapshots (org_id, space, snapshot, id) ON DELETE CASCADE,
        ADD CONSTRAINT snapshot_edges_dst_fkey
            FOREIGN KEY (org_id, space, snapshot, dst_id)
            REFERENCES snapshots (org_id, space, snapshot, id) ON DELETE CASCADE;
    ALTER TABLE snapshot_claims
        ADD CONSTRAINT snapshot_claims_fact_fkey
            FOREIGN KEY (org_id, space, snapshot, fact_id)
            REFERENCES snapshots (org_id, space, snapshot, id) ON DELETE CASCADE;

    -- 6. Backfill spaces registry rows for the snapshots created above (the
    --    `working` graphs and any building-validation rename). Idempotent; keeps
    --    the space list consistent regardless of 0009 ordering.
    --    Skip orphan tenants (snapshot org with no `orgs` row) so the
    --    `spaces.org_id` -> `orgs` FK can't abort the re-key (see 0009).
    INSERT INTO spaces (org_id, space_id)
    SELECT DISTINCT org_id, space FROM snapshots
    WHERE space <> '__evals__'
      AND EXISTS (SELECT 1 FROM orgs o WHERE o.org_id = snapshots.org_id)
    ON CONFLICT DO NOTHING;
END $$;
