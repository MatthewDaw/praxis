-- 0011_mounted_rekey — re-key `mounted_snapshots` from (source_user_id,
-- snapshot_name) to (space, snapshot).
--
-- Part of the org -> space -> snapshot tenancy redesign
-- (specs/005-praxis-tenancy-redesign). A mount is a per-viewer read-only overlay
-- of a snapshot onto the viewer's working-memory reads. The snapshot is now
-- addressed by (space, snapshot) instead of by its owning user; the viewer stays
-- (org_id, user_id). Same derivation as 0008: the old mangled source_user_id
-- yields the space, the snapshot_name (minus its `snapshot:` prefix, if any)
-- yields the bare snapshot name.
--
-- Guarded by the presence of the `source_user_id` column; no-ops once migrated.

DO $$
DECLARE
    r record;
BEGIN
    IF to_regclass('public.mounted_snapshots') IS NULL THEN
        RETURN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'mounted_snapshots' AND column_name = 'source_user_id'
    ) THEN
        RETURN;  -- already re-keyed
    END IF;

    ALTER TABLE mounted_snapshots ADD COLUMN IF NOT EXISTS space text;
    UPDATE mounted_snapshots
       SET space = COALESCE(NULLIF(split_part(source_user_id, '::space:', 2), ''), 'default');
    ALTER TABLE mounted_snapshots RENAME COLUMN snapshot_name TO snapshot;
    UPDATE mounted_snapshots SET snapshot = regexp_replace(snapshot, '^snapshot:', '');

    -- Two source_user_ids can collapse onto the same (space, snapshot) for one
    -- viewer; keep the earliest by created_at so the new PK never collides.
    DELETE FROM mounted_snapshots m USING (
        SELECT org_id, user_id, space, snapshot,
               (array_agg(ctid ORDER BY created_at))[1] AS keep_ctid
        FROM mounted_snapshots
        GROUP BY org_id, user_id, space, snapshot
        HAVING count(*) > 1
    ) d
    WHERE m.org_id = d.org_id AND m.user_id = d.user_id
      AND m.space = d.space AND m.snapshot = d.snapshot
      AND m.ctid <> d.keep_ctid;

    FOR r IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'mounted_snapshots'::regclass AND contype = 'p'
    LOOP
        EXECUTE format('ALTER TABLE mounted_snapshots DROP CONSTRAINT %I', r.conname);
    END LOOP;
    ALTER TABLE mounted_snapshots
        DROP COLUMN source_user_id,
        ALTER COLUMN space SET NOT NULL,
        ADD CONSTRAINT mounted_snapshots_pkey PRIMARY KEY (org_id, user_id, space, snapshot);
END $$;
