from __future__ import annotations

import sqlite3

from .registry import DDL, RegistryError


SCHEMA_VERSION = 1


def migrate_schema(connection: sqlite3.Connection) -> int:
    """Apply only lossless, offline SQLite schema migrations."""
    connection.executescript(DDL)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RegistryError(f"registry schema version {version} is newer than supported {SCHEMA_VERSION}")
    if version != SCHEMA_VERSION:
        raise RegistryError(f"no lossless registry schema migration from {version} to {SCHEMA_VERSION}")
    return version
