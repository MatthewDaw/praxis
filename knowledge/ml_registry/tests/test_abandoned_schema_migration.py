"""(succeeded, abandoned) is legal on an already-versioned schema-6 database.

The live registry is schema 6. Bumping user_version would refuse peers still compiled
against 6, so the CHECK rewrite is a lossless in-place rebuild, same pattern as the
experiment-amend trigger.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from knowledge.ml_registry.storage.migration import SCHEMA_VERSION, migrate_schema


_OLD_RUNS = """
CREATE TABLE experiments(
 experiment_id TEXT PRIMARY KEY, spec_digest TEXT NOT NULL, stages TEXT NOT NULL, metric TEXT NOT NULL,
 direction TEXT NOT NULL, win_condition TEXT NOT NULL, rope REAL NOT NULL, baseline_throughput REAL NOT NULL);
CREATE TABLE runs(
 run_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id), idea_id TEXT NOT NULL,
 stage TEXT NOT NULL, family TEXT NOT NULL, params TEXT NOT NULL, metrics TEXT NOT NULL, code_ref TEXT NOT NULL,
 device_fingerprint TEXT NOT NULL, status TEXT NOT NULL, verdict TEXT CHECK(verdict IS NULL OR verdict IN
 ('adopted','rejected','parked','voided')), started_at REAL NOT NULL, finished_at REAL,
 claim_owner TEXT NOT NULL, heartbeat_at REAL NOT NULL, CHECK(COALESCE(
 (status IN ('running','complete','failed','superseded') AND verdict IS NULL) OR
 (status='succeeded' AND verdict IN ('adopted','rejected','parked')) OR
 (status='voided' AND verdict='voided'),0)));
CREATE TRIGGER valid_run_pair_update BEFORE UPDATE ON runs WHEN NOT COALESCE(
 (NEW.status IN ('running','complete','failed','superseded') AND NEW.verdict IS NULL) OR
 (NEW.status='succeeded' AND NEW.verdict IN ('adopted','rejected','parked')) OR
 (NEW.status='voided' AND NEW.verdict='voided'),0)
 BEGIN SELECT RAISE(ABORT,'invalid run status/verdict pair'); END;
"""


def test_schema_6_runs_table_gains_abandoned_without_bumping_user_version(tmp_path: Path) -> None:
    db_path = tmp_path / "registry.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript(_OLD_RUNS)
    connection.execute("PRAGMA user_version=6")
    connection.execute(
        "INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?)",
        ("c", "d" * 64, "[]", "f1", "maximize", "{}", 0.01, 1.0),
    )
    connection.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "mute", "c", "idea", "representation", "f", "{}", "{}", "{}", "cpu",
            "succeeded", "rejected", 1.0, 2.0, "w", 2.0,
        ),
    )
    connection.commit()
    assert migrate_schema(connection) == SCHEMA_VERSION
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
    sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'"
    ).fetchone()[0]
    assert "abandoned" in sql
    connection.create_function("registry_authority", 0, lambda: "run_abandoned")
    connection.execute("UPDATE runs SET verdict='abandoned' WHERE run_id='mute'")
    row = connection.execute("SELECT verdict FROM runs WHERE run_id='mute'").fetchone()
    assert row[0] == "abandoned"
    # Existing rejected rows survive the copy-swap, and remain a legal pair.
    connection.create_function("registry_authority", 0, lambda: "run_created")
    connection.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "other", "c", "idea2", "representation", "f", "{}", "{}", "{}", "cpu",
            "succeeded", "rejected", 1.0, 2.0, "w", 2.0,
        ),
    )
    assert connection.execute(
        "SELECT verdict FROM runs WHERE run_id='other'"
    ).fetchone()[0] == "rejected"
    connection.close()
