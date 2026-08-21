from __future__ import annotations

import sqlite3

from .registry import DDL, RegistryError


SCHEMA_VERSION = 2


def migrate_schema(connection: sqlite3.Connection) -> int:
    """Apply only lossless, offline SQLite schema migrations."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RegistryError(f"registry schema version {version} is newer than supported {SCHEMA_VERSION}")
    if version == 0:
        connection.executescript(DDL)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        version = SCHEMA_VERSION
    elif version == 1:
        connection.executescript(DDL)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
        if "schema_version" not in columns:
            connection.execute("ALTER TABLE events ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 0")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        version = SCHEMA_VERSION
    if version != SCHEMA_VERSION:
        raise RegistryError(f"no lossless registry schema migration from {version} to {SCHEMA_VERSION}")
    return version
