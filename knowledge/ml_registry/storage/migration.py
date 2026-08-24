from __future__ import annotations

import sqlite3

from .registry import DDL, RegistryError


SCHEMA_VERSION = 5


def migrate_schema(connection: sqlite3.Connection) -> int:
    """Apply only lossless, offline SQLite schema migrations."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RegistryError(f"registry schema version {version} is newer than supported {SCHEMA_VERSION}")
    if version == 0:
        connection.executescript(DDL)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        version = SCHEMA_VERSION
    elif version in {1, 2, 3, 4}:
        if version == 1:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
            if "schema_version" not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 0")
        for trigger in ("guard_runs_update", "guard_versions_insert", "guard_lineage_insert",
                        "guard_aliases_insert", "guard_aliases_update", "guard_aliases_delete",
                        "valid_run_pair_insert", "valid_run_pair_update"):
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        connection.executescript(DDL)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        version = SCHEMA_VERSION
    if version != SCHEMA_VERSION:
        raise RegistryError(f"no lossless registry schema migration from {version} to {SCHEMA_VERSION}")
    return version
