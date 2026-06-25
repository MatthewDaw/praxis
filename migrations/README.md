# Migrations

**This directory is the single source of truth for the database schema.** There is
no separate `schema.sql` baseline — `0000_initial.sql` creates the full schema, and
every change after it is a new ordered migration in this directory.

- A **fresh** database gets the whole schema by replaying every migration in order
  (`0000_initial` first).
- An **existing** database only gets what's new: yoyo records applied migrations in
  its `_yoyo_migration` ledger and skips them on the next run.

`bootstrap()` (`knowledge/serve/db.py`, also `python -m knowledge.serve.db`) *is*
`yoyo apply` over this directory, and the `migrate-on-main` workflow runs the same on
merge to `main`. So there is exactly one place to change the schema: add a migration
here. (`0000_initial.sql` stays idempotent — `CREATE … IF NOT EXISTS` — so applying it
to the pre-existing prod DB, which predates this squash, is a safe no-op.)

To change the schema: **add the next `NNNN_*` migration** — a new table/column, a drop,
a type change, a backfill, all the same way. Never edit `0000_initial.sql` for an
ongoing change (it's history); append a migration instead.

### What does *not* belong here

Backfills that derive new data with an **LLM** are deliberately kept out of the
deploy-time migrate workflow — they need `OPENROUTER_API_KEY` and must not run
unattended. The structural-contradiction claims backfill lives at
[`scripts/claims_backfill.py`](../scripts/claims_backfill.py) and is run by hand
once per database (`OPENROUTER_API_KEY=… python -m scripts.claims_backfill`).

## File convention

- `NNNN_short_name.sql` — pure SQL. Statements separated by `;`. Declare order
  with a `-- depends: <other_id> …` comment when it matters.
- `NNNN_short_name.py` — when the migration needs application code (e.g.
  embeddings). Define `steps = [step(fn)]`; `fn(conn)` receives the psycopg3
  backend connection.

Keep migrations idempotent/guarded where practical (`IF EXISTS`,
`ON CONFLICT DO NOTHING`) so a re-run is harmless even before yoyo's ledger
records them.

## Running locally

yoyo picks its backend from the DSN scheme; this project uses psycopg v3, which
yoyo exposes as `postgresql+psycopg`:

```bash
# PRAXIS_DB_URL is a normal postgresql:// DSN; swap the scheme for yoyo.
YOYO_DB="${PRAXIS_DB_URL/postgresql:\/\//postgresql+psycopg://}"

uv run yoyo list  --batch --database "$YOYO_DB" ./migrations   # see status
uv run yoyo apply --batch --database "$YOYO_DB" ./migrations   # apply pending
```
