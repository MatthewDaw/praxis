-- Runs once, on first cluster init, before any application connection.
--
-- The app's db.connect() registers the psycopg pgvector adapter only if the
-- `vector` type already exists; on a brand-new database the extension is created
-- by the 0000_initial migration during bootstrap(), which is *after* the first
-- connect — so the adapter would silently fail to register and the first Vector
-- write would error. Creating the extension here guarantees the type exists up
-- front. The 0000_initial migration's own `CREATE EXTENSION IF NOT EXISTS vector`
-- remains the source of truth and is idempotent with this.
CREATE EXTENSION IF NOT EXISTS vector;
