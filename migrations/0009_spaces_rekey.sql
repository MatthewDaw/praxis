-- 0009_spaces_rekey — flip `spaces` from owner-private to ORG-SHARED.
--
-- Part of the org -> space -> snapshot tenancy redesign
-- (specs/005-praxis-tenancy-redesign). A space is now a purely organizational,
-- org-shared "project folder": any org member can read it. So the owner axis is
-- dropped and the key collapses from (org_id, owner_sub, space_id) to
-- (org_id, space_id). Where two owners registered the same space_id in one org
-- (owner-a and owner-b both own `alpha`), the earliest `created_at` wins.
--
-- Guarded by the presence of the `owner_sub` column: on a fresh DB, 0006 first
-- creates the owner-keyed shape and this collapses it; on an already-migrated DB
-- the block no-ops.

DO $$
DECLARE
    r record;
BEGIN
    IF to_regclass('public.spaces') IS NULL THEN
        RETURN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'spaces' AND column_name = 'owner_sub'
    ) THEN
        RETURN;  -- already org-shared
    END IF;

    -- 1. Dedupe (org_id, space_id) collisions across owner_sub: keep the earliest
    --    created_at (owner_sub as the deterministic tie-breaker) via ctid.
    DELETE FROM spaces s USING (
        SELECT org_id, space_id,
               (array_agg(ctid ORDER BY created_at, owner_sub))[1] AS keep_ctid
        FROM spaces
        GROUP BY org_id, space_id
        HAVING count(*) > 1
    ) d
    WHERE s.org_id = d.org_id AND s.space_id = d.space_id AND s.ctid <> d.keep_ctid;

    -- 2. Drop owner_sub; re-key on (org_id, space_id). The org FK is preserved.
    FOR r IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'spaces'::regclass AND contype = 'p'
    LOOP
        EXECUTE format('ALTER TABLE spaces DROP CONSTRAINT %I', r.conname);
    END LOOP;
    ALTER TABLE spaces
        DROP COLUMN owner_sub,
        ADD CONSTRAINT spaces_pkey PRIMARY KEY (org_id, space_id);

    -- 3. Backfill a registry row for every non-eval space now referenced by a
    --    snapshot but missing from `spaces`, so the app's space list stays
    --    consistent. Repeat/no-op safe.
    --    `spaces.org_id` FKs to `orgs`, so an orphan tenant (a snapshot whose
    --    org has no `orgs` row — e.g. a leftover test tenant) can't get a
    --    registry row; skip it rather than abort the whole re-key.
    INSERT INTO spaces (org_id, space_id)
    SELECT DISTINCT org_id, space FROM snapshots
    WHERE space <> '__evals__'
      AND EXISTS (SELECT 1 FROM orgs o WHERE o.org_id = snapshots.org_id)
    ON CONFLICT DO NOTHING;
END $$;
