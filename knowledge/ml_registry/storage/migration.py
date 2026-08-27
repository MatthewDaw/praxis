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
                   "valid_run_pair_insert", "valid_run_pair_update",
                   "guard_experiments_update")

# Recreated on every open: schema 6 left experiments fully immutable, which trapped any
# campaign that needed to *tighten* a registered win condition. The body lives in DDL;
# dropping first is what makes the recreate take effect on an already-versioned database
# without a schema-version bump that would refuse other live lanes still on 6.
_EXPERIMENT_AMEND_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS guard_experiments_update BEFORE UPDATE ON experiments
 WHEN registry_authority() NOT IN ('experiment_amended') BEGIN SELECT RAISE(ABORT,'experiments are immutable'); END;
"""


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
    _ensure_experiment_amend_trigger(connection)
    _ensure_abandoned_verdict_allowed(connection)
    return version


def _ensure_experiment_amend_trigger(connection: sqlite3.Connection) -> None:
    """Install the amend-aware experiments-update trigger without bumping user_version.

    Schema 6 left the row fully immutable. Replacing that trigger is lossless and
    additive -- a live peer still compiled against SCHEMA_VERSION=6 must not find
    this database "too new". Skip when the body is already the amend-aware one so
    a check-only replay does not rewrite a current projection.
    """
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='guard_experiments_update'"
    ).fetchone()
    sql = row[0] if row else ""
    if "experiment_amended" in sql:
        return
    connection.execute("DROP TRIGGER IF EXISTS guard_experiments_update")
    connection.executescript(_EXPERIMENT_AMEND_TRIGGER)


_ABANDONED_PAIR_SQL = """
 (NEW.status IN ('running','complete','failed','superseded') AND NEW.verdict IS NULL) OR
 (NEW.status='succeeded' AND NEW.verdict IN ('adopted','rejected','parked','abandoned')) OR
 (NEW.status='voided' AND NEW.verdict='voided')
""".strip()

_ABANDONED_PAIR_INSERT = f"""
CREATE TRIGGER IF NOT EXISTS valid_run_pair_insert BEFORE INSERT ON runs WHEN NOT COALESCE(
 {_ABANDONED_PAIR_SQL},0)
 BEGIN SELECT RAISE(ABORT,'invalid run status/verdict pair'); END;
"""

_ABANDONED_PAIR_UPDATE = f"""
CREATE TRIGGER IF NOT EXISTS valid_run_pair_update BEFORE UPDATE ON runs WHEN NOT COALESCE(
 {_ABANDONED_PAIR_SQL},0)
 BEGIN SELECT RAISE(ABORT,'invalid run status/verdict pair'); END;
"""

_ABANDONED_GUARD_RUNS_UPDATE = """
CREATE TRIGGER IF NOT EXISTS guard_runs_update BEFORE UPDATE ON runs
 WHEN registry_authority() NOT IN ('run_completed','run_adjudicated','run_adopted','run_superseded','adoption_invalidated','run_abandoned') BEGIN SELECT RAISE(ABORT,'run write authority required'); END;
"""

_GUARD_RUNS_INSERT = """
CREATE TRIGGER IF NOT EXISTS guard_runs_insert BEFORE INSERT ON runs
 WHEN registry_authority() NOT IN ('run_created','historical_ledger_imported','historical_archive_imported') BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
"""

_GUARD_RUNS_DELETE = """
CREATE TRIGGER IF NOT EXISTS guard_runs_delete BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT,'runs cannot be deleted'); END;
"""


def _ensure_abandoned_verdict_allowed(connection: sqlite3.Connection) -> None:
    """Allow (succeeded, abandoned) on an already-versioned schema-6 database.

    Abandoned is a verdict the judge did not reach, recorded so a rejection it did not
    reach cannot later be cited as proof the approach fails. It is not a new schema:
    bumping ``user_version`` would refuse live peers still compiled against 6. Same
    pattern as :func:`_ensure_experiment_amend_trigger`.
    """
    _rebuild_runs_table_if_abandoned_missing(connection)
    _ensure_trigger_body(connection, "guard_runs_insert", "run_created", _GUARD_RUNS_INSERT)
    _ensure_trigger_body(connection, "guard_runs_delete", "runs cannot be deleted", _GUARD_RUNS_DELETE)
    _ensure_trigger_body(connection, "valid_run_pair_insert", "abandoned", _ABANDONED_PAIR_INSERT)
    _ensure_trigger_body(connection, "valid_run_pair_update", "abandoned", _ABANDONED_PAIR_UPDATE)
    _ensure_trigger_body(connection, "guard_runs_update", "run_abandoned", _ABANDONED_GUARD_RUNS_UPDATE)


def _ensure_trigger_body(
    connection: sqlite3.Connection, name: str, marker: str, create_sql: str
) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
    ).fetchone()
    sql = row[0] if row else ""
    if marker in sql:
        return
    connection.execute(f"DROP TRIGGER IF EXISTS {name}")
    connection.executescript(create_sql)


def _rebuild_runs_table_if_abandoned_missing(connection: sqlite3.Connection) -> None:
    """Recreate ``runs`` so the table CHECK accepts ``abandoned``. Triggers follow separately.

    SQLite cannot ALTER a table CHECK. Copying the rows is lossless: ``events.jsonl`` is
    still the durable record, and the projection's contents do not change. Skip when the
    live SQL already names abandoned so a current projection is not rewritten on every open.
    """
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'"
    ).fetchone()
    if row is None or "abandoned" in row[0]:
        return
    new_sql = row[0]
    new_sql = new_sql.replace(
        "('adopted','rejected','parked','voided')",
        "('adopted','rejected','parked','voided','abandoned')",
    )
    new_sql = new_sql.replace(
        "verdict IN ('adopted','rejected','parked')",
        "verdict IN ('adopted','rejected','parked','abandoned')",
    )
    if "abandoned" not in new_sql:
        raise RegistryError("could not rewrite the runs CHECK to accept abandoned")
    # sqlite_master stores `CREATE TABLE runs(` or `CREATE TABLE "runs"(`
    if new_sql.startswith("CREATE TABLE \"runs\""):
        new_sql = "CREATE TABLE runs_new" + new_sql[len("CREATE TABLE \"runs\""):]
    elif new_sql.startswith("CREATE TABLE runs"):
        new_sql = "CREATE TABLE runs_new" + new_sql[len("CREATE TABLE runs"):]
    else:
        raise RegistryError(f"unexpected runs table SQL: {new_sql[:80]!r}")
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        for trigger in (
            "guard_runs_insert",
            "guard_runs_update",
            "guard_runs_delete",
            "valid_run_pair_insert",
            "valid_run_pair_update",
        ):
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        connection.executescript(new_sql)
        connection.execute("INSERT INTO runs_new SELECT * FROM runs")
        connection.execute("DROP TABLE runs")
        connection.execute("ALTER TABLE runs_new RENAME TO runs")
        connection.commit()
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


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
