# Praxis Tenancy Redesign — Implementation Contract

**Status:** authoritative. This is the single source of truth for the org → space →
snapshot redesign plus per-user working memory. Every implementer (schema, serve,
mcp, client, factory) builds against THIS document. Where the subsystem maps
disagreed, the resolution is stated inline under **RESOLUTION**.

---

## 0. Model in one paragraph

`org` → `space` → `snapshot`, plus a per-user private **working memory**.

- **Working memory** = the live scratch graph, table **`facts`**, keyed
  `(org_id, user_id, id)` where `user_id` is the real authenticated Cognito sub.
  Private to that user. No `shared`, no space, no snapshot ever appears on it.
- **Space** = an org-shared "project folder", table **`spaces`**, keyed
  `(org_id, space_id)`. No owner. Every org member can read every space.
- **Snapshot** = a saved/org-shared named graph inside a space, table
  **`snapshots`** (renamed from `cached_facts`), keyed
  `(org_id, space, snapshot, id)`. Readable/loadable by any org member.

Load = copy `snapshots(org, space=X, snapshot=S)` → `facts(org, user=me)`.
Dump = copy `facts(org, user=me)` → `snapshots(org, space=Y, snapshot=new)`.
Mount = overlay a snapshot onto a viewer's working-memory **reads** only.

---

## 1. Final DDL (post-migration target shape)

Naming resolutions applied throughout:
- `cached_facts` → **`snapshots`**
- `cached_fact_edges` → **`snapshot_edges`**  *(RESOLUTION: task mandates the rename;
  overrides the schema map which kept the old satellite names.)*
- `cached_claims` → **`snapshot_claims`**
- Every generated `text_tsv` column stays `GENERATED ALWAYS AS
  (to_tsvector('english', text)) STORED` and is **never** named in an
  `INSERT ... SELECT`.
- `embedding vector(1536)` round-trips verbatim in pure SQL (no re-embed at migrate time).

### 1.1 `facts` — working memory (per-user, private)

```
Columns (unchanged from 0000 EXCEPT `shared` is dropped):
  id                text NOT NULL
  org_id            text NOT NULL DEFAULT 'default'
  user_id           text NOT NULL DEFAULT 'default'   -- real authenticated sub
  text              text NOT NULL
  source            text
  confidence        double precision
  scope             text
  category          text
  observation_count integer NOT NULL DEFAULT 1
  success_count     integer NOT NULL DEFAULT 0         -- outcome-trust: KEPT
  failure_count     integer NOT NULL DEFAULT 0         -- KEPT
  last_outcome      text                               -- KEPT
  state             text NOT NULL DEFAULT 'proposed'
  embedding         vector(1536)
  cluster_id        integer
  cluster_label     text
  valid_at          timestamptz
  invalid_at        timestamptz
  meta              jsonb NOT NULL DEFAULT '{}'::jsonb
  created_at        timestamptz NOT NULL DEFAULT now()
  text_tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED

PK      (org_id, user_id, id)
Indexes:
  facts_tenant        (org_id, user_id, scope)          -- shared REMOVED from key
  facts_embedding_hnsw USING hnsw (embedding vector_cosine_ops)
  facts_text_tsv_gin   USING gin (text_tsv)
  facts_category       (org_id, user_id, category)
  facts_meta_gin       USING gin (meta)
```

### 1.2 `fact_edges` — working-memory satellite (UNCHANGED)

```
  org_id, user_id, src_id, dst_id, kind
PK  (org_id, user_id, src_id, dst_id, kind)
FK  (org_id,user_id,src_id) -> facts(org_id,user_id,id) ON DELETE CASCADE
FK  (org_id,user_id,dst_id) -> facts(org_id,user_id,id) ON DELETE CASCADE
```

### 1.3 `claims` — working-memory satellite (UNCHANGED)

```
  org_id, user_id, fact_id, seq, subject, attribute, value, functional, created_at
PK  (org_id, user_id, fact_id, seq)
FK  (org_id,user_id,fact_id) -> facts(org_id,user_id,id) ON DELETE CASCADE
Index claims_slot (org_id, user_id, subject, attribute) WHERE functional
```

### 1.4 `snapshots` — was `cached_facts` (org-shared, in a space)

```
Columns (same as cached_facts MINUS user_id, MINUS shared, PLUS space + snapshot;
outcome-trust columns are still OMITTED, exactly as cached_facts omitted them):
  id                text NOT NULL
  org_id            text NOT NULL DEFAULT 'default'
  space             text NOT NULL
  snapshot          text NOT NULL          -- bare name, NO 'snapshot:' prefix
  text              text NOT NULL
  source            text
  confidence        double precision
  scope             text
  category          text
  observation_count integer NOT NULL DEFAULT 1
  state             text NOT NULL DEFAULT 'proposed'
  embedding         vector(1536)
  cluster_id        integer
  cluster_label     text
  valid_at          timestamptz
  invalid_at        timestamptz
  meta              jsonb NOT NULL DEFAULT '{}'::jsonb
  created_at        timestamptz NOT NULL DEFAULT now()
  text_tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED

PK      (org_id, space, snapshot, id)
Indexes:
  snapshots_tenant         (org_id, space, scope)              -- no user_id, no shared
  snapshots_key            (org_id, space, snapshot)
  snapshots_category       (org_id, space, snapshot, category)
  snapshots_meta_gin       USING gin (meta)
  snapshots_embedding_hnsw USING hnsw (embedding vector_cosine_ops)
  snapshots_text_tsv_gin   USING gin (text_tsv)
```

### 1.5 `snapshot_edges` — was `cached_fact_edges`

```
  org_id, space, snapshot, src_id, dst_id, kind
PK  (org_id, space, snapshot, src_id, dst_id, kind)
FK  (org_id,space,snapshot,src_id) -> snapshots(org_id,space,snapshot,id) ON DELETE CASCADE
FK  (org_id,space,snapshot,dst_id) -> snapshots(org_id,space,snapshot,id) ON DELETE CASCADE
```

### 1.6 `snapshot_claims` — was `cached_claims`

```
  org_id, space, snapshot, fact_id, seq, subject, attribute, value, functional, created_at
PK  (org_id, space, snapshot, fact_id, seq)
FK  (org_id,space,snapshot,fact_id) -> snapshots(org_id,space,snapshot,id) ON DELETE CASCADE
Index snapshot_claims_slot (org_id, space, snapshot, subject, attribute) WHERE functional
```

### 1.7 `spaces` — org-shared (was owner-keyed)

```
  org_id     text NOT NULL
  space_id   text NOT NULL
  name       text
  created_at timestamptz NOT NULL DEFAULT now()
PK  (org_id, space_id)                                   -- owner_sub DROPPED
FK  (org_id) -> orgs(org_id) ON DELETE CASCADE
```

### 1.8 `mounted_snapshots` — re-keyed to (space, snapshot)

```
  org_id     text NOT NULL           -- viewer org
  user_id    text NOT NULL           -- viewer (real sub)
  space      text NOT NULL           -- referenced snapshot's space
  snapshot   text NOT NULL           -- referenced snapshot (bare name)
  created_at timestamptz NOT NULL DEFAULT now()
PK  (org_id, user_id, space, snapshot)   -- source_user_id/snapshot_name DROPPED
```

Unchanged tables: `orgs`, `org_members`, `api_keys`.

---

## 2. Ordered yoyo migrations + data transform

New files, applied after `0007_facts_meta_category_idx.sql`, each **idempotent /
guard-safe** (`to_regclass`/`IF EXISTS`/`ON CONFLICT`) so they run correctly both on
a fresh DB (which first gets 0000's OLD shape) and on existing prod. `db.py:bootstrap`
auto-discovers them via `read_migrations(MIGRATIONS_DIR)`; no code change there.

| # | file | depends |
|---|------|---------|
| 0008 | `0008_snapshots_rekey.sql` | — |
| 0009 | `0009_spaces_rekey.sql` | — |
| 0010 | `0010_facts_working_memory.sql` | `-- depends: 0008_snapshots_rekey` |
| 0011 | `0011_mounted_rekey.sql` | — |

> Ordering note: the date-named `m2026_06_23_reject_rename.py` sorts lexically AFTER
> `0011`. On a fresh DB it runs last, after `cached_facts` no longer exists; its
> `_table_exists` guard must no-op on the missing table. Confirm that guard before
> landing. On prod it is already in the `_yoyo_migration` ledger and does not re-run.

### 0008 — `cached_facts` → `snapshots` (+ satellites)

Order of operations, all guarded by `to_regclass('cached_facts') IS NOT NULL`:

1. `ALTER TABLE cached_facts RENAME TO snapshots;`
2. `ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS space text;`
3. Populate space from the OLD mangled user_id BEFORE it is dropped:
   `UPDATE snapshots SET space = COALESCE(NULLIF(split_part(user_id,'::space:',2),''),'default');`
   (mangled `{sub}::space:<sid>` → `<sid>`; raw-sub eval/snapshot rows → `'default'`.)
4. `ALTER TABLE snapshots RENAME COLUMN cache_key TO snapshot;`
   then strip the type prefix: `UPDATE snapshots SET snapshot = regexp_replace(snapshot,'^snapshot:','');`
   — **RESOLUTION for evals:** rows whose old `cache_key` began `eval:` keep the
   `eval:` text as their snapshot name AND get `space='__evals__'`:
   `UPDATE snapshots SET space='__evals__' WHERE snapshot LIKE 'eval:%';`
   `UPDATE snapshots SET snapshot=regexp_replace(snapshot,'^eval:','') WHERE space='__evals__';`
   (Do the eval reassignment BEFORE stripping, or match on the pre-strip value.)
5. Pre-drop guard (data-loss assert): `DO $$ BEGIN IF EXISTS (SELECT 1 FROM snapshots
   WHERE shared) THEN RAISE EXCEPTION 'shared=true snapshot rows exist; visibility
   intent would be lost'; END IF; END $$;` (All 230 current rows are `shared=false`.)
6. `ALTER TABLE snapshots DROP CONSTRAINT cached_facts_pkey, DROP COLUMN user_id,
   DROP COLUMN shared, ALTER COLUMN space SET NOT NULL,
   ADD CONSTRAINT snapshots_pkey PRIMARY KEY (org_id, space, snapshot, id);`
7. Satellites (same derivation, keys stay consistent because they share the source
   `(user_id,cache_key)`):
   - `cached_fact_edges` → `snapshot_edges`: rename table; add `space`; populate
     `space` via the same `split_part`/`'__evals__'` rules keyed off `user_id`+`cache_key`;
     rename `cache_key`→`snapshot`, strip prefixes; drop old PK + both cache_key FKs;
     drop `user_id`; add PK `(org_id,space,snapshot,src_id,dst_id,kind)` + FKs to
     `snapshots(org_id,space,snapshot,id)`.
   - `cached_claims` → `snapshot_claims`: same pattern; PK
     `(org_id,space,snapshot,fact_id,seq)`; FK to `snapshots`.
8. Drop OLD indexes (`cached_facts_*`, `cached_claims_slot`) and create the new
   `snapshots_*` / `snapshot_claims_slot` indexes from §1.4–1.6.

### 0009 — `spaces` → org-shared

1. Dedupe real `(org_id, space_id)` collisions across `owner_sub` (owner-a and
   owner-b both own `alpha` in several `test_org*`): keep earliest by `created_at`,
   coalescing a non-null `name`:
   ```
   DELETE FROM spaces s USING (
     SELECT org_id, space_id,
            (array_agg(ctid ORDER BY created_at, owner_sub))[1] AS keep_ctid
     FROM spaces GROUP BY org_id, space_id HAVING count(*) > 1
   ) d
   WHERE s.org_id=d.org_id AND s.space_id=d.space_id AND s.ctid<>d.keep_ctid;
   ```
2. `ALTER TABLE spaces DROP CONSTRAINT spaces_pkey, DROP COLUMN owner_sub,
   ADD CONSTRAINT spaces_pkey PRIMARY KEY (org_id, space_id);` (org FK preserved.)
3. Backfill a registry row for every space now referenced by a snapshot but missing
   from `spaces` (keeps the app's space list consistent; runs AFTER 0010 would also
   be fine, but do it here as `INSERT ... SELECT DISTINCT org_id, space FROM snapshots
   WHERE space <> '__evals__' ON CONFLICT DO NOTHING` — repeat/no-op safe).

### 0010 — collapse `facts` to per-user working memory

**RESOLUTION (data-loss judgment call, signed off here):** a mangled
`{sub}::space:<sid>` LIVE graph becomes a snapshot named `working` inside space
`<sid>` — NOT merged into the real user's private working memory (that would pollute
the authenticated scratch graph, and the model forbids space on working memory).

1. Move mangled named-graph facts into `snapshots` (do NOT list `text_tsv`):
   ```
   INSERT INTO snapshots
     (id, org_id, space, snapshot, text, source, confidence, scope, category,
      observation_count, state, embedding, cluster_id, cluster_label,
      valid_at, invalid_at, meta, created_at)
   SELECT id, org_id, split_part(user_id,'::space:',2), 'working',
          text, source, confidence, scope, category, observation_count, state,
          embedding, cluster_id, cluster_label, valid_at, invalid_at, meta, created_at
   FROM facts WHERE user_id LIKE '%::space:%';
   ```
   Then their satellites into `snapshot_edges` / `snapshot_claims` with
   `space=split_part(user_id,'::space:',2), snapshot='working'` from
   `fact_edges` / `claims WHERE user_id LIKE '%::space:%'`.
2. `DELETE FROM facts WHERE user_id LIKE '%::space:%';` (edges/claims cascade).
   Raw-sub rows (real subs like `24782438`, `u1`, `u2`) are untouched = working memory.
3. `ALTER TABLE facts DROP COLUMN shared;` (guarded by the same shared-assert as 0008).
4. `DROP INDEX IF EXISTS facts_tenant; CREATE INDEX facts_tenant ON facts (org_id, user_id, scope);`
5. **coding-validation → building-validation rename** (design point 7), applied to
   the moved rows AND the registry, across all three tables + `spaces`:
   ```
   UPDATE snapshots      SET space='building-validation' WHERE space='coding-validation';
   UPDATE snapshot_edges SET space='building-validation' WHERE space='coding-validation';
   UPDATE snapshot_claims SET space='building-validation' WHERE space='coding-validation';
   UPDATE spaces SET space_id='building-validation' WHERE space_id='coding-validation'
     AND NOT EXISTS (SELECT 1 FROM spaces b WHERE b.org_id=spaces.org_id AND b.space_id='building-validation');
   ```
   **RESOLUTION (global→per-project checks):** legacy GLOBAL check graphs
   (`planning-validation`, `coding-validation`) land mechanically as spaces
   `planning-validation` / `building-validation`, snapshot `working`. They are legacy
   fixtures. The NEW baseline is per-project: the factory writes/reads checks at
   `space=<project>, snapshot=planning-validation|building-validation` (§7). Existing
   global checks are NOT retro-fitted into per-project spaces (old data carries no
   reliable project association); teams re-seed per project. This is an accepted
   behavior change, documented in the factory skills.

### 0011 — re-key `mounted_snapshots`

Only one test row exists, so collisions are nil; still guard.
```
ALTER TABLE mounted_snapshots ADD COLUMN IF NOT EXISTS space text;
UPDATE mounted_snapshots SET space =
  COALESCE(NULLIF(split_part(source_user_id,'::space:',2),''),'default');
ALTER TABLE mounted_snapshots RENAME COLUMN snapshot_name TO snapshot;
UPDATE mounted_snapshots SET snapshot = regexp_replace(snapshot,'^snapshot:','');
ALTER TABLE mounted_snapshots
  DROP CONSTRAINT mounted_snapshots_pkey,
  DROP COLUMN source_user_id,
  ALTER COLUMN space SET NOT NULL,
  ADD CONSTRAINT mounted_snapshots_pkey PRIMARY KEY (org_id, user_id, space, snapshot);
```

---

## 3. Read predicates (canonical)

| Store | Predicate |
|-------|-----------|
| Working memory (`facts` + satellites) | `WHERE org_id=%s AND user_id=%s` |
| Snapshot (`snapshots` + satellites) | `WHERE org_id=%s AND space=%s AND snapshot=%s` |
| Snapshots in a space (list) | `WHERE org_id=%s AND space=%s` |
| Mounted overlay (viewer) | live: `org_id=%s AND user_id=%s`; mounted branch: `org_id=%s AND (space,snapshot) ∈ mounts` |
| Spaces list | `WHERE org_id=%s` |

The old `shared OR user_id` disjunction is **deleted everywhere** — including
`PostgresVectorGraph._where`, `overlay_search`, `_search_vec`, `_search_keyword`,
`OrgSourceReader._where`, and both copy-column constants.

### 3.1 `PostgresVectorGraph` binding change (store contract)

Generalize the existing `facts_table` / `cache_key` seam:

- `facts_table='facts'` → working memory. Bind `(org_id, user_id)`. No `shared` col.
- `facts_table='snapshots'` → snapshot-bound. Bind `(org_id, space, snapshot)` via new
  constructor kwargs `space: str, snapshot: str` (replacing `cache_key` for this table).
  `user_id` is unused for this table.

`_FACT_COPY_COLS` splits into two lists:
```
_FACT_COPY_COLS       = "id, org_id, user_id, text, source, confidence, scope, category, " \
                        "observation_count, state, embedding, cluster_id, cluster_label, " \
                        "valid_at, invalid_at, meta, created_at"   # working memory (shared REMOVED)
_SNAPSHOT_COPY_COLS   = "id, org_id, text, source, confidence, scope, category, " \
                        "observation_count, state, embedding, cluster_id, cluster_label, " \
                        "valid_at, invalid_at, meta, created_at"   # NO user_id, NO shared
```
`save_cache`/`load_caches`/`merge_caches_into_live`/`copy_snapshot_from`/`rename_cache`/
`delete_cache`/`cache_count`/`list_caches`/`recent_cache`/`overlay_search` all re-key
from `(user_id, cache_key)` to `(space, snapshot)` on the snapshot side. The working
side of save/load selects/omits `user_id` per the two column lists above (never `shared`).

- **save (dump):** `INSERT INTO snapshots(_SNAPSHOT_COPY_COLS, space, snapshot)
  SELECT <cols minus user_id>, %s, %s FROM facts WHERE org_id=%s AND user_id=%s`.
- **load:** `INSERT INTO facts(_FACT_COPY_COLS) SELECT id, %s(org), %s(user), <rest>
  FROM snapshots WHERE org_id=%s AND space=%s AND snapshot=%s` — stamp the loader's
  `user_id` (snapshots carry no user_id).
- `overlay_search` mounted branch: `WHERE org_id=%s AND ((space=%s AND snapshot=%s) OR …)`;
  mounts list becomes `list[tuple[space, snapshot]]`; `mountedFrom` tag becomes
  `{"space", "snapshot"}`.
- `OrgSourceReader.__init__(conn, org_id, *, space, snapshot)` (drops `target_user_id`);
  `_where` → `org_id=%s AND space=%s AND snapshot=%s` over `snapshots`/`snapshot_edges`.

---

## 4. Serve route signature changes (`knowledge/serve/app.py`)

### 4.1 Resolver (design point 4)

`active_user_id` dependency: **delete the space-mangling**. Body becomes
`return principal.sub` (drop the `X-Praxis-Space` header param and the
`spaces_store.owns` / `{sub}::space:{sid}` branch). Name kept to minimize the diff
across ~45 call sites.

**New dependency** `snapshot_target(x_praxis_space, x_praxis_snapshot) -> (space, snapshot) | None`
reads headers `X-Praxis-Space` + `X-Praxis-Snapshot`. When BOTH are present the
generic read/write routes operate on a snapshot-bound graph; when absent they use
working memory. Helper:
```
def graph_for(org, uid, target):        # target = snapshot_target result or None
    if target is None:
        return live_or_overlay(org, uid)          # working memory (+ mounts on reads)
    space, snap = target
    if not spaces_store.exists(org, space):        # 404 unknown space
        raise HTTPException(404, f"unknown space {space!r}")
    return PostgresVectorGraph(conn, org, facts_table='snapshots', space=space, snapshot=snap, ...)
```
Applies to (add `target = Depends(snapshot_target)`, route reads/writes via
`graph_for(org, uid, target)`): `/facts/by`, `/context`, `/surfaces/{screen_id}/checks`,
`/candidates/{cid}` (GET), `PATCH /candidates/{cid}`, `POST /facts/{fact_id}/outcome`,
`/requirements/incomplete`, `/insights`, `/ingest`, `/graph`, `/surfaces/bindings`,
`/surfaces/coverage`, `/facts/{fact_id}/surfaces`. This is the seam that lets the
factory read/mutate a project snapshot (checks + `prd-<project>` tickets) without any
`{sub}::space:` mangling.

### 4.2 Snapshot routes (re-keyed space+snapshot)

| Old | New |
|-----|-----|
| `GET /snapshots` (caller's) | `GET /snapshots?space=<space>` → list snapshots in the space `[{snapshot,count,createdAt}]` |
| `POST /snapshots {name}` | `POST /snapshots {space, snapshot}` → dump working memory into `snapshots(org,space,snapshot)` |
| `POST /snapshots/{name}/load {mode}` | `POST /snapshots/load {space, snapshot, mode}` → load into working memory |
| `DELETE /snapshots/{name}` | `DELETE /snapshots {space, snapshot}` (body) — delete keyed `(org,space,snapshot)`; also unmount any `mounted_snapshots` rows referencing it (all viewers) |
| `PATCH /snapshots/{name} {name}` | `PATCH /snapshots {space, snapshot, newSnapshot}` → rename within the same space; **RESOLUTION:** drop the self-mount fixup (self-mount concept is gone); re-point any `mounted_snapshots(space,snapshot)` rows to `newSnapshot` |
| `POST /snapshots/{name}/copy-to-org {targetOrg,targetName}` | `POST /snapshots/copy-to-org {space, snapshot, targetOrg, targetSpace, targetSnapshot}` → cross-org snapshot copy re-keyed to `(space,snapshot)` |

### 4.3 Mount routes

| Old | New |
|-----|-----|
| `GET /mounts` → `{sourceUser,snapshot,isSelf,count}` | `GET /mounts` → `{space, snapshot, count}` (no sourceUser/isSelf) |
| `POST /mounts {sourceUser?,snapshot}` | `POST /mounts {space, snapshot}` — viewer implicit; validate space+snapshot exist |
| `DELETE /mounts {sourceUser?,snapshot}` | `DELETE /mounts {space, snapshot}` |

`_validate_mount_target(org, space, snapshot)` checks the snapshot has rows in
`snapshots`. Self-mount / rename-fixup logic removed.

### 4.4 Space routes (org-shared semantics flip)

| Old | New |
|-----|-----|
| `POST /spaces {spaceId,name}` (private, auto-activates) | `POST /spaces {spaceId,name}` → org-shared row `(org_id,space_id)`, no owner, no active-graph side effect |
| `GET /spaces` (caller's) | `GET /spaces` → ALL spaces in the org |
| `DELETE /spaces/{space_id}` (purges a working graph) | `DELETE /spaces/{space_id}` → delete the space row + ALL its snapshots (cascade) + any `mounted_snapshots` referencing them; **touches NO `facts`** |
| `PATCH /spaces/{space_id} {name}` | unchanged shape; `SpacesStore.rename_space(org_id, space_id, name)` (no owner) |
| `POST /spaces/copy-to-org {targetOrg,targetSpace}` | **RESOLUTION:** re-scope to `POST /spaces/copy-to-org {space, targetOrg, targetSpace}` = copy ALL snapshots of source `space` into `targetSpace` in `targetOrg` (was "copy my working graph"). |

### 4.5 Org-sources / fold-in (browse axis: member → space)

**RESOLUTION:** the browse axis changes from `(member, snapshot)` to `(space, snapshot)`;
this is a re-model, not a mechanical re-key.

| Old | New |
|-----|-----|
| `GET /org/sources` (members + their snapshots) | `GET /org/sources` → the org's SPACES + their snapshots `[{space, snapshots:[{snapshot,count}]}]` |
| `GET /org/sources/{user_id}/snapshots/{name}/facts` | `GET /spaces/{space}/snapshots/{snapshot}/facts` → `{space, snapshot, groups}` via `OrgSourceReader(conn, org, space=space, snapshot=snapshot)` |
| `POST /fold-in {sourceUser,snapshot,factIds,mode}` | `POST /fold-in {space, snapshot, factIds, mode}` → fold a space snapshot's facts into caller's working memory; `foldedFrom` meta becomes `{"space","snapshot"}` |

### 4.6 Evals (RESOLUTION: reserved space)

Eval cache lives in `snapshots` under reserved `space='__evals__'`, `snapshot=<case_id>`.
`_ensure_cached` / `regenerate_evals` / `load_evals` / `cached_eval_cases` build
`PostgresVectorGraph(conn, org, facts_table='snapshots', space='__evals__', snapshot=case_id)`.
`GET /evals/cached` lists `snapshots WHERE org_id=%s AND space='__evals__'`. Evals stay
org-scoped (acceptable for fixtures; they were demo data). `'__evals__'` is a reserved
space id the app rejects for user-created spaces. Not surfaced by `GET /spaces` /
`GET /org/sources` (filter it out).

---

## 5. MCP tool signature changes (`knowledge/mcp/server.py`)

`_headers()`: **DELETE** the `X-Praxis-Space` emission block. Working-memory tools send
no space header. Snapshot tools set `X-Praxis-Space` + `X-Praxis-Snapshot` (or explicit
body/URL params) only for the specific op.

| Tool | Old sig | New sig / behavior |
|------|---------|--------------------|
| `praxis_save_snapshot` | `(name)` | `(space, snapshot)` — dump working memory into `snapshots(org,space,snapshot)` |
| `praxis_load_snapshot` | `(name, mode='replace')` | `(space, snapshot, mode='replace')` — load into working memory |
| `praxis_list_snapshots` | `()` | `(space)` — list a space's snapshots |
| `praxis_delete_snapshot` | `(name)` | `(space, snapshot)` |
| `praxis_copy_snapshot_to_org` | `(name, target_org, target_name=None)` | `(space, snapshot, target_org, target_space, target_snapshot=None)` |
| `praxis_mount_snapshot` | `(snapshot, source_user=None)` | `(space, snapshot)` — body `{space,snapshot}` |
| `praxis_unmount_snapshot` | `(snapshot, source_user=None)` | `(space, snapshot)` |
| `praxis_list_mounts` | `()` | render `(space, snapshot, count)` (no sourceUser/isSelf) |
| `praxis_fold_in` | `(source_user, snapshot, fact_ids, mode)` | `(space, snapshot, fact_ids, mode)` — body key `space` not `sourceUser` |
| `praxis_browse_snapshot` | `(user_id, name)` | `(space, snapshot)` → `GET /spaces/{space}/snapshots/{snapshot}/facts` |
| `praxis_list_org_sources` | `()` members+snapshots | `()` → iterate SPACES + snapshots |
| `praxis_create_space` | `(space_id, name=None)` + `set_space` side effect | `(space_id, name=None)` — org-shared; REMOVE `identity.set_space` auto-activate |
| `praxis_list_space` | private spaces | ALL org spaces |
| `praxis_select_space` | selects working graph via header | **RESOLUTION:** repurpose to a purely LOCAL client default feeding the `space` param of snapshot/mount ops (no header). (Keep the tool; drop all working-graph language.) |
| `praxis_delete_space` | deletes private space + working graph | delete org-shared space + ALL its snapshots; touches NO working memory |
| `praxis_copy_space_to_org` | `(target_org, target_space)` copy working graph | `(space, target_org, target_space)` — copy all snapshots of `space` (matches §4.4) |

Working-memory tools (`praxis_get_context`, `praxis_add_insight(s)`, `praxis_ingest*`,
`praxis_list_graph`, `praxis_insert_fact`, `praxis_edit_fact`, `praxis_record_outcome`,
`praxis_get_fact`, `praxis_clear_graph`, contradictions, derivations): **no signature
change** — they always resolve to the authenticated `user_id` (no space header).

`identity.py`: remove the `X-Praxis-Space`-as-working-graph pathway. Drop the
`PRAXIS_SPACE` env → header emission. `active_space()`/`set_space()`/`Tenant.space_id`
either removed or repurposed as a client-side default for the `space` PARAM (never a
header driving the working graph). `save_identity`/`load_identity` stop
serializing a working-graph space. `auth.py` is **unchanged** (asserted): `principal.sub`
is already the working-memory `user_id`.

---

## 6. Client method signature changes

### 6.1 `agent_factory/hooks/_praxis.py` (stdlib hooks client)

- `_auth_headers`: **DELETE** the `PRAXIS_SPACE → x-praxis-space` block. Keep
  `x-praxis-org` + api-key/Cognito auth. Working-memory reads resolve to
  `(org, authenticated user)` with no space header.
- `_request(method, path, *, params=None, body=None, not_found_ok=False,
  space=None, snapshot=None)` — replace the single `space` override with a
  `(space, snapshot)` pair; when BOTH given, emit `x-praxis-space` + `x-praxis-snapshot`.
  Fail-closed + `not_found_ok` semantics preserved. **A `(space, snapshot)` that is
  required but missing must raise, never fall back to working memory** (a mis-defaulted
  checks read returning empty would fail a Stop gate OPEN).
- `facts_by(category=None, meta=None, state='active', space=None, snapshot=None)` —
  thread both.
- `surface_checks(project, screen_id, scope=None, space=None, snapshot=None)`.
- `context(query, *, top_k=10, as_of=None, space=None, snapshot=None)`.
- `incomplete_requirements(project, *, exclude_leased=False, space=None, snapshot=None)` —
  reads the `prd-<project>` tickets. **RESOLUTION (mutable tickets):** the
  `prd-<project>` ticket graph is a SNAPSHOT in the project space
  (`space=<project>, snapshot=prd-<project>`) and is MUTABLE through the serve
  snapshot-bound write path (§4.1). "Read-only" applies to MOUNTS and to load/dump
  copy semantics, NOT to snapshot rows addressed directly by `(space, snapshot)`.
  So ticket ops below thread the ticket `(space, snapshot)` reference explicitly.
- `get_fact(cid, *, space=None, snapshot=None)`, `patch_meta(cid, meta_dict, *,
  space=None, snapshot=None)`, `record_outcome(cid, success, *, space=None,
  snapshot=None)` — thread the ticket reference (do NOT silently fall through to
  working memory). Keep the bare-`prd-` prefix-stripping guard in
  `incomplete_requirements`.
- `ping()` — issues with no snapshot (working-memory/default probe).

### 6.2 `praxis_client/client.py` (public SDK) — additive only

The SDK never sent a space header, so working-memory parity is already correct.
`_headers` stays org+key only.

- `get_context(query, top_k=8, *, category=None, categories=None, scope=None,
  meta=None, space=None, snapshot=None)` — when `space`+`snapshot` given, serialize
  them into the query string (`&space=&snapshot=`); when omitted, byte-for-byte
  unchanged (`{query, top_k}` only) so the existing no-filter parity test holds.
- **New methods** (the explicit `(space,snapshot)` ops the SDK lacks):
  - `load_snapshot(space, snapshot, mode='replace') -> dict`  (copy snapshot → working memory)
  - `save_snapshot(space, snapshot) -> dict`  (dump working memory → snapshot)
  - `list_snapshots(space) -> list[dict]`
  - `delete_snapshot(space, snapshot) -> dict`
  - `mount_snapshot(space, snapshot) -> dict` / `unmount_snapshot(space, snapshot) -> dict`
  - `list_mounts() -> list[dict]`
- `__init__.py` re-exports unchanged unless new types are added. `README.md`: document
  that working-memory ops are principal-scoped (no space header) and `(space,snapshot)`
  are explicit params on the snapshot/load/dump/mount methods.

---

## 7. Factory check-resolution model

**Project → space derivation (RESOLUTION):** `space == the bare project name`. The
factory already carries the bare project (the `prd-<project>` prefix + the
`/requirements/incomplete?project=` arg). Document this in
`_ticket_state.py`, `build_completeness_gate.py:_active_project`, and both skills.

Inside a project space `<project>`:
- snapshot `prd-<project>` — plan + tickets (mutable via §4.1 snapshot-bound writes).
- snapshot `planning-validation` — planning-scope checks (read by af-intake-plan).
- snapshot `building-validation` — validation-scope checks (read by af-build). **Renamed
  from `coding-validation` everywhere.**

### 7.1 Seam functions (`agent_factory/hooks/_ticket_state.py`)

- `DEFAULT_VALIDATION_CHECKS_SPACE = "coding-validation"` → rename constant to
  `DEFAULT_VALIDATION_CHECKS_SNAPSHOT = "building-validation"` (now a SNAPSHOT name).
- `DEFAULT_PLANNING_CHECKS_SPACE = "planning-validation"` → `DEFAULT_PLANNING_CHECKS_SNAPSHOT
  = "planning-validation"` (now a SNAPSHOT name).
- `default_checks_space(scope)` → `default_checks_snapshot(scope)`:
  `validation → 'building-validation'`, `planning → 'planning-validation'`, else `None`.
  The SPACE half is the project's space (`<project>`), not a global string. Keep a
  back-compat alias `default_checks_space = default_checks_snapshot` if any external
  symbol needs it, but update all in-repo call sites.
- `_CHECKS_SPACE_UNSET` sentinel → semantics "unset snapshot override".
- `resolve_validation_requirements(ticket, project='', scope=None,
  checks_ref=_CHECKS_SPACE_UNSET)`: compute
  `snapshot = default_checks_snapshot(scope)` (overridable by `checks_ref`),
  `space = project`. Pass `(space=project, snapshot=snapshot)` to every check read:
  `_praxis.facts_by(..., space=project, snapshot=snapshot)` (the tag, `"*"`, and
  planning lanes) and `_praxis.surface_checks(project, screen, space=project,
  snapshot=snapshot)`.
- `retrieve_advisory_checks(...)`: same `(space=project, snapshot=snapshot)` rework of
  the `_praxis.context(...)` call.
- `start_ticket(cid, owner, project='', ..., checks_ref=_CHECKS_SPACE_UNSET)`: thread the
  `(space=project, snapshot)` reference into `resolve_validation_requirements(...,
  scope='validation')`. Docstring `coding-validation` → `building-validation`.

The `checks_ref` override is a `(space, snapshot)` pair (or a bare snapshot name that
defaults space to `project`). Explicit `None` forces the ticket/default reference.

### 7.2 Slash overrides

- `/af-intake-plan --checks-space=<...>` and `/af-build --checks-space=<...>` become a
  `(space, snapshot)` override threaded as `checks_ref`. Default resolution:
  `space=<project>`, `snapshot=planning-validation` (intake) / `building-validation`
  (build).
- af-build workflow `CHECKS_SPACE=coding-validation` constant → `CHECKS_SNAPSHOT=building-validation`
  (+ `space=<project>`); §8 per-ticket worker contract `start_ticket(checks_ref=...)`.
- af-intake-plan Part C writes checks INTO the project space's snapshot:
  planning checks (`source=planning-checklist`, `scope=planning`) →
  `space=<project>, snapshot=planning-validation`; validation checks
  (`scope=validation`) → `space=<project>, snapshot=building-validation`. The old global
  `planning-checklist` shared library is reconciled to per-project snapshots (checks are
  no longer global — re-seed per project or drop the shared-checklist concept).

### 7.3 Docs + tests to update (per-project snapshot + building-validation rename)

`af-intake-plan/SKILL.md` (B3, Part C intro, C1, C2), `af-build/SKILL.md` (Validation source,
workflow WORKER const, §8 worker contract), `agent_factory/docs/af-memory-policy.md` (§1
seam + signatures), `agent_factory/docs/factory-state-contract.md` (seam + signatures),
`agent_factory/docs/coverage-spine/02-planner.md` (planning-snapshot wording),
`agent_factory/tests/test_checks_space_seam.py` (assert `building-validation` snapshot +
project space; capture snapshot as well as space), `agent_factory/tests/test_check_resolution_lanes.py`
(`_DBSpy.facts_by/surface_checks/context` add `snapshot`; advisory assertion
`context_calls[0]['space']=='coding-validation'` → `snapshot=='building-validation'`,
`space=='<project>'`).

---

## 8. Landing / atomicity constraints

1. The MCP `_headers` space-header removal MUST land together with the serve resolver
   drop (§4.1) and the factory `_praxis` header change (§6.1) — otherwise the backend
   reverts to the wrong graph. Single coordinated change set.
2. The factory `(space, snapshot)` read mechanism (headers on generic routes, §4.1) MUST
   land in lockstep with `_ticket_state` threading, or check reads return empty and
   af-build fails OPEN.
3. Snapshot store rename (`snapshots`/`snapshot_edges`/`snapshot_claims`) and the store's
   `_SNAPSHOT_COPY_COLS` must match the 0008 DDL exactly (column parity).
4. `seed_snapshots.py`: rewrite writers to `snapshots(org, space, snapshot)` (drop
   `--user`, add `--space`; snapshot = bare name, no `snapshot:` prefix; no `shared`).
5. `spaces_store.py` / `mounted_store.py`: re-key all SQL to `(org_id, space_id)` /
   `(org_id, user_id, space, snapshot)` per §4.4 / §4.3.
6. Verify a fresh `yoyo apply` against an empty DB produces the NEW shape (0000 old
   shape → 0008–0011 transform → new baseline), and that `m2026_06_23_reject_rename.py`
   no-ops on the now-missing `cached_facts`.
