from __future__ import annotations

import sqlite3

from .registry import DDL, RegistryError


SCHEMA_VERSION = 6

# Schema 6 re-keys `artifacts` on (run_id, artifact_id). Under schema 5 the row was keyed
# on the content digest ALONE, so two Runs emitting byte-identical output collided on
# `UNIQUE constraint failed: artifacts.artifact_id` -- which every deterministic arm does
# the moment it is re-measured. The blob stays content-addressed; only the ROW, which is a
# run's reference to that blob, gains its owning run. `model_versions` follows with a
# composite foreign key, because a single-column reference to a non-unique parent is not
# one SQLite will accept.
_REKEYED_TABLES = ("artifacts", "model_versions")

# Triggers whose bodies or guards changed after the schema that first created them.
# `CREATE TRIGGER IF NOT EXISTS` cannot update one in place, so an upgrade drops them first.
_STALE_TRIGGERS = ("guard_runs_update", "guard_versions_insert", "guard_lineage_insert",
                   "guard_aliases_insert", "guard_aliases_update", "guard_aliases_delete",
                   "valid_run_pair_insert", "valid_run_pair_update")


def migrate_schema(connection: sqlite3.Connection) -> int:
    """Apply only lossless, offline SQLite schema migrations."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RegistryError(f"registry schema version {version} is newer than supported {SCHEMA_VERSION}")
    if version == 0:
        connection.executescript(DDL)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        version = SCHEMA_VERSION
    elif version in {1, 2, 3, 4, 5}:
        if version == 1:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
            if "schema_version" not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 0")
        for trigger in _STALE_TRIGGERS:
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        _drop_rekeyed_tables(connection)
        connection.executescript(DDL)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        version = SCHEMA_VERSION
    if version != SCHEMA_VERSION:
        raise RegistryError(f"no lossless registry schema migration from {version} to {SCHEMA_VERSION}")
    return version


def _drop_rekeyed_tables(connection: sqlite3.Connection) -> None:
    """Drop the tables schema 6 re-keys, so `CREATE TABLE IF NOT EXISTS` rebuilds them.

    This is lossless because `events.jsonl` is the durable record and the SQLite file is
    only its projection: emptying two tables makes the projection disagree with the log,
    and `Registry.recover` then replays every event into a fresh database. Dropping is the
    only way to change a key here -- `IF NOT EXISTS` silently keeps the old definition, so
    an upgrade that merely re-ran the DDL would report success and change nothing.
    """
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        for table in _REKEYED_TABLES:
            # Dropping the table drops its triggers with it; foreign keys are off because
            # the implicit DELETE FROM would otherwise trip `lineage`'s reference.
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.commit()
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
