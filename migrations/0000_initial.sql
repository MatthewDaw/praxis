-- 0000_initial — the full Praxis schema. SINGLE SOURCE OF TRUTH for structure.
--
-- This is the first yoyo migration: `bootstrap()` (knowledge/serve/db.py) runs
-- `yoyo apply` over this directory, so a fresh DB gets the whole schema here and
-- later structural changes are added as ordered migrations after this one
-- (NNNN_*.sql / NNNN_*.py). There is no separate schema.sql baseline anymore.
--
-- Target: AWS RDS PostgreSQL 16 with the pgvector extension available.
-- Every statement uses IF NOT EXISTS, so applying it to an existing database
-- (e.g. prod, which predates this squash) is a safe no-op.

CREATE EXTENSION IF NOT EXISTS vector;

-- Multi-tenancy model: every row is owned by a (org_id, user_id) pair.
--   * org_id  -- the tenant. Rows are always partitioned by org first.
--   * user_id -- the owning user within the org.
--   * shared  -- when true the row is visible to the whole org (a shared
--                graph); when false it is private to user_id.
-- Read predicate for a requester (org O, user U):
--     WHERE org_id = O AND (shared OR user_id = U)
-- This gives org-shared graphs, user-private graphs, and optional sharing.
--
-- Record ids (e.g. dashboard "cand_1", fact ids) are only unique WITHIN a
-- tenant graph, never globally -- so the primary key is composite
-- (org_id, user_id, id). A bare id PK would let one tenant's seed clobber
-- another's via ON CONFLICT.

-- Knowledge-graph foundation (facts + edges + embeddings).
CREATE TABLE IF NOT EXISTS facts (
    id                text NOT NULL,
    org_id            text NOT NULL DEFAULT 'default',
    user_id           text NOT NULL DEFAULT 'default',
    shared            boolean NOT NULL DEFAULT false,
    text              text NOT NULL,
    source            text,
    confidence        double precision,
    scope             text,
    category          text,
    observation_count integer NOT NULL DEFAULT 1,
    -- Outcome / trust feedback (verification results fed back into the fact).
    -- Retrieval folds these into a utility multiplier so a fact whose suggested
    -- action keeps failing sinks and a proven one holds. 0/0 => neutral (no change).
    success_count     integer NOT NULL DEFAULT 0,
    failure_count     integer NOT NULL DEFAULT 0,
    -- Most recent verification outcome ('succeeded'|'failed'|NULL). The counts above
    -- are cumulative; this carries the *latest* signal so derived-completeness queries
    -- can tell a regressed (succeeded-then-failed) requirement from a still-passing one.
    last_outcome      text,
    -- Lifecycle state: 'proposed' (passive system add, staged), 'active' (user
    -- directly approved -- live knowledge), 'rejected' (superseded/retired;
    -- formerly 'decayed', renamed in specs/003-fact-rejection-lifecycle).
    state             text NOT NULL DEFAULT 'proposed',
    embedding         vector(1536),
    -- Navigation-only topic clustering (HDBSCAN over embeddings, c-TF-IDF/LLM
    -- label). Assigned by a periodic write-time "define" pass, NEVER read by
    -- retrieval — purely so the dashboard can collapse the graph into labeled
    -- super-nodes. NULL == unclustered (HDBSCAN noise, or not yet clustered).
    cluster_id        integer,
    cluster_label     text,
    -- Bi-temporal world-time validity (Graphiti/Zep model). `valid_at` is when
    -- the fact became true in the world (defaults to insert time); `invalid_at`
    -- is when it stopped being true. `invalid_at IS NULL` == currently valid.
    -- Invalidated rows are kept (never deleted), enabling point-in-time recall:
    -- a fact is valid "as of" T when valid_at <= T < invalid_at. This is
    -- orthogonal to the proposed/active/rejected lifecycle `state` (a workflow
    -- status), which is retained unchanged.
    valid_at          timestamptz,
    invalid_at        timestamptz,
    meta              jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, user_id, id)
);

-- Backfill for pre-existing `facts` tables created before these columns landed.
ALTER TABLE facts ADD COLUMN IF NOT EXISTS state text NOT NULL DEFAULT 'proposed';
ALTER TABLE facts ADD COLUMN IF NOT EXISTS cluster_id integer;
ALTER TABLE facts ADD COLUMN IF NOT EXISTS cluster_label text;
ALTER TABLE facts ADD COLUMN IF NOT EXISTS last_outcome text;
ALTER TABLE facts ADD COLUMN IF NOT EXISTS valid_at timestamptz;
ALTER TABLE facts ADD COLUMN IF NOT EXISTS invalid_at timestamptz;

CREATE INDEX IF NOT EXISTS facts_tenant ON facts (org_id, shared, user_id, scope);

CREATE INDEX IF NOT EXISTS facts_embedding_hnsw
    ON facts USING hnsw (embedding vector_cosine_ops);

-- Keyword (BM25-style) retrieval branch for hybrid search. A generated tsvector
-- of the fact text, GIN-indexed, so search can fuse a full-text keyword ranking
-- (websearch_to_tsquery + ts_rank) with the pgvector cosine ranking via Reciprocal
-- Rank Fusion. STORED + generated keeps it always in sync with `text` on write,
-- with no application code path to forget. English config matches the query side.
ALTER TABLE facts ADD COLUMN IF NOT EXISTS text_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', text)) STORED;

CREATE INDEX IF NOT EXISTS facts_text_tsv_gin ON facts USING gin (text_tsv);

-- Orgs: app-level tenants. A user creates an org (setting its password) or
-- joins an existing one (supplying that password). The password is stored as a
-- pbkdf2_hmac(sha256) hash with a per-org random salt (see orgs_store.py).
CREATE TABLE IF NOT EXISTS orgs (
    org_id        text PRIMARY KEY,
    name          text,
    password_hash text NOT NULL,
    password_salt text NOT NULL,
    created_by    text NOT NULL,
    created_at    timestamptz DEFAULT now()
);

-- Org membership: which users belong to which org, and their role. The org
-- creator is added as 'owner'; subsequent joiners default to 'member'.
CREATE TABLE IF NOT EXISTS org_members (
    org_id    text,
    user_id   text,
    role      text DEFAULT 'member',
    joined_at timestamptz DEFAULT now(),
    PRIMARY KEY (org_id, user_id),
    FOREIGN KEY (org_id) REFERENCES orgs (org_id) ON DELETE CASCADE
);

-- Mounted snapshots: a per-viewer read-only overlay set. Each row says "when
-- (org_id, user_id) does a retrieval read, also expose snapshot
-- 'snapshot:<snapshot_name>' owned by source_user_id". Mounts only affect the
-- read path (see overlay_graph.py): writes/ingest and saving a snapshot operate
-- on the live `facts` table alone, so a mounted overlay is never merged in and
-- never carried over on a save. source_user_id may be the viewer (your own
-- snapshot) or any other org member (read within the org trust boundary).
CREATE TABLE IF NOT EXISTS mounted_snapshots (
    org_id         text NOT NULL,
    user_id        text NOT NULL,
    source_user_id text NOT NULL,
    snapshot_name  text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, user_id, source_user_id, snapshot_name)
);

-- Scoped API keys: a long-lived, org-scoped service token an automated agent
-- uses instead of the Cognito SRP + per-request token mint. Only the sha256 hex
-- of the raw key (`pxk_<random>`) is stored; the raw key is shown once at mint.
-- A key is scoped to one `org_id` (and optionally a specific `user_id`); a
-- request authenticating with it must select that same org via X-Praxis-Org.
CREATE TABLE IF NOT EXISTS api_keys (
    id           text PRIMARY KEY,
    key_hash     text NOT NULL UNIQUE,
    org_id       text NOT NULL,
    user_id      text,
    label        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked      boolean NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS api_keys_hash ON api_keys (key_hash) WHERE NOT revoked;

-- Edges connect two facts within the same tenant graph.
CREATE TABLE IF NOT EXISTS fact_edges (
    org_id text NOT NULL DEFAULT 'default',
    user_id text NOT NULL DEFAULT 'default',
    src_id text NOT NULL,
    dst_id text NOT NULL,
    kind   text NOT NULL DEFAULT 'contradiction',
    PRIMARY KEY (org_id, user_id, src_id, dst_id, kind),
    FOREIGN KEY (org_id, user_id, src_id)
        REFERENCES facts (org_id, user_id, id) ON DELETE CASCADE,
    FOREIGN KEY (org_id, user_id, dst_id)
        REFERENCES facts (org_id, user_id, id) ON DELETE CASCADE
);

-- Atomic claims extracted from a fact's text at write time, in the form
-- (subject, attribute, value). `functional` marks single-valued attributes (an
-- event's year, a person's birth year) where two differing values for the same
-- (subject, attribute) slot is a contradiction; multi-valued attributes (a
-- person's discoveries) never conflict on value difference. `subject` and
-- `attribute` are stored normalized (lowercased, whitespace-collapsed) so the
-- slot index can match across surface variation; `value` keeps its raw form.
-- `seq` distinguishes the several claims a single fact yields.
CREATE TABLE IF NOT EXISTS claims (
    org_id     text NOT NULL DEFAULT 'default',
    user_id    text NOT NULL DEFAULT 'default',
    fact_id    text NOT NULL,
    seq        integer NOT NULL,
    subject    text NOT NULL,
    attribute  text NOT NULL,
    value      text NOT NULL,
    functional boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, user_id, fact_id, seq),
    FOREIGN KEY (org_id, user_id, fact_id)
        REFERENCES facts (org_id, user_id, id) ON DELETE CASCADE
);

-- Slot lookup for the contradiction path: find other facts asserting the same
-- functional (subject, attribute) slot. Partial index — only functional claims
-- can produce a contradiction, so only they need fast slot recall.
CREATE INDEX IF NOT EXISTS claims_slot
    ON claims (org_id, user_id, subject, attribute) WHERE functional;

-- Graph cache: saved graph states kept strictly separate from the live `facts`
-- retrieval path so cached data can never leak into MCP get_context. Same column
-- shape as `facts` plus a `cache_key` that names the saved state:
--   * 'eval:<case_id>'  -- a distilled eval case (file), cached so re-loads are free
--   * 'snapshot:<name>' -- a user snapshot of the live graph at a moment in time
-- Loading a cache entry truncates `facts` and inserts the entry's rows; saving a
-- snapshot copies `facts` into here. A PostgresVectorGraph pointed at these tables
-- (with a bound cache_key) reuses all graph code for the eval-distillation path.
CREATE TABLE IF NOT EXISTS cached_facts (
    id                text NOT NULL,
    org_id            text NOT NULL DEFAULT 'default',
    user_id           text NOT NULL DEFAULT 'default',
    shared            boolean NOT NULL DEFAULT false,
    text              text NOT NULL,
    source            text,
    confidence        double precision,
    scope             text,
    category          text,
    observation_count integer NOT NULL DEFAULT 1,
    state             text NOT NULL DEFAULT 'proposed',
    embedding         vector(1536),
    -- Mirrors `facts`: cluster assignments are copied verbatim on save/load so a
    -- snapshot or eval cache restores its topic super-nodes without re-clustering.
    cluster_id        integer,
    cluster_label     text,
    -- Mirrors `facts`: bi-temporal validity copied verbatim on save/load so a
    -- snapshot or eval cache restores point-in-time recall losslessly.
    valid_at          timestamptz,
    invalid_at        timestamptz,
    meta              jsonb NOT NULL DEFAULT '{}'::jsonb,
    cache_key         text NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    -- cache_key is part of the PK so the same fact id can live under multiple
    -- saved states (e.g. two snapshots) without colliding.
    PRIMARY KEY (org_id, user_id, cache_key, id)
);

-- Backfill for pre-existing `cached_facts` tables created before clustering landed.
ALTER TABLE cached_facts ADD COLUMN IF NOT EXISTS cluster_id integer;
ALTER TABLE cached_facts ADD COLUMN IF NOT EXISTS cluster_label text;
ALTER TABLE cached_facts ADD COLUMN IF NOT EXISTS valid_at timestamptz;
ALTER TABLE cached_facts ADD COLUMN IF NOT EXISTS invalid_at timestamptz;

CREATE INDEX IF NOT EXISTS cached_facts_tenant ON cached_facts (org_id, shared, user_id, scope);

CREATE INDEX IF NOT EXISTS cached_facts_embedding_hnsw
    ON cached_facts USING hnsw (embedding vector_cosine_ops);

-- Keyword branch twin of `facts.text_tsv` (mirrors the facts/cached_facts split),
-- so a cache-bound graph (snapshots / eval cache) gets the same hybrid retrieval.
ALTER TABLE cached_facts ADD COLUMN IF NOT EXISTS text_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', text)) STORED;

CREATE INDEX IF NOT EXISTS cached_facts_text_tsv_gin ON cached_facts USING gin (text_tsv);

CREATE INDEX IF NOT EXISTS cached_facts_key ON cached_facts (org_id, user_id, cache_key);

CREATE TABLE IF NOT EXISTS cached_fact_edges (
    org_id text NOT NULL DEFAULT 'default',
    user_id text NOT NULL DEFAULT 'default',
    cache_key text NOT NULL,
    src_id text NOT NULL,
    dst_id text NOT NULL,
    kind   text NOT NULL DEFAULT 'contradiction',
    PRIMARY KEY (org_id, user_id, cache_key, src_id, dst_id, kind),
    FOREIGN KEY (org_id, user_id, cache_key, src_id)
        REFERENCES cached_facts (org_id, user_id, cache_key, id) ON DELETE CASCADE,
    FOREIGN KEY (org_id, user_id, cache_key, dst_id)
        REFERENCES cached_facts (org_id, user_id, cache_key, id) ON DELETE CASCADE
);

-- Snapshot twin of `claims` (mirrors the facts/cached_facts split) so saved and
-- eval-cached graphs carry their extracted claims losslessly.
CREATE TABLE IF NOT EXISTS cached_claims (
    org_id     text NOT NULL DEFAULT 'default',
    user_id    text NOT NULL DEFAULT 'default',
    cache_key  text NOT NULL,
    fact_id    text NOT NULL,
    seq        integer NOT NULL,
    subject    text NOT NULL,
    attribute  text NOT NULL,
    value      text NOT NULL,
    functional boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, user_id, cache_key, fact_id, seq),
    FOREIGN KEY (org_id, user_id, cache_key, fact_id)
        REFERENCES cached_facts (org_id, user_id, cache_key, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS cached_claims_slot
    ON cached_claims (org_id, user_id, cache_key, subject, attribute) WHERE functional;
