-- PRAXIS knowledge-graph + dashboard schema (canonical).
--
-- Target: AWS RDS PostgreSQL 16 with the pgvector extension available.
-- This file is idempotent: every statement uses IF NOT EXISTS so it can be
-- re-run safely as a migration (see knowledge/serve/db.py :: bootstrap()).

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
    -- Lifecycle state: 'proposed' (passive system add, staged), 'active' (user
    -- directly approved -- live knowledge), 'decayed' (superseded/retired).
    state             text NOT NULL DEFAULT 'proposed',
    embedding         vector(1536),
    meta              jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, user_id, id)
);

-- Backfill for pre-existing `facts` tables created before `state` landed.
ALTER TABLE facts ADD COLUMN IF NOT EXISTS state text NOT NULL DEFAULT 'proposed';

CREATE INDEX IF NOT EXISTS facts_tenant ON facts (org_id, shared, user_id, scope);

CREATE INDEX IF NOT EXISTS facts_embedding_hnsw
    ON facts USING hnsw (embedding vector_cosine_ops);

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
    meta              jsonb NOT NULL DEFAULT '{}'::jsonb,
    cache_key         text NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    -- cache_key is part of the PK so the same fact id can live under multiple
    -- saved states (e.g. two snapshots) without colliding.
    PRIMARY KEY (org_id, user_id, cache_key, id)
);

CREATE INDEX IF NOT EXISTS cached_facts_tenant ON cached_facts (org_id, shared, user_id, scope);

CREATE INDEX IF NOT EXISTS cached_facts_embedding_hnsw
    ON cached_facts USING hnsw (embedding vector_cosine_ops);

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
