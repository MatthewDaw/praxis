"""A deterministic arm that is re-measured emits the same bytes, and must still be adoptable.

Under schema 5 the `artifacts` row was keyed on the content digest ALONE. Every arm in a
deterministic campaign re-emits byte-identical output when it is re-dispatched, so the second
Run died on `UNIQUE constraint failed: artifacts.artifact_id` before it could be adjudicated;
and working around that by reusing the FIRST run's row then failed promotion, which requires
the adjudicated run's own `(artifact_id, run_id)` pair. Content addressing and ownership
validation could not both hold. Schema 6 keys the row on `(run_id, artifact_id)`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from knowledge.ml_registry import Registry
from knowledge.ml_registry.services.registry_aliases import adopt_run_and_promote
from knowledge.ml_registry.storage import RegistryError
from knowledge.ml_registry.storage.migration import SCHEMA_VERSION

from .test_registry_native_adjudication import SHA, create_run, registry_with_champion


IDENTICAL = b"deterministic-arm-output"


def test_two_runs_emitting_identical_bytes_each_get_their_own_artifact_row(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "arm-first", 0.81)
    create_run(registry, "arm-second", 0.81)

    first = registry.create_artifact(run_id="arm-first", kind="report", content=IDENTICAL,
                                     schema_version="1")
    second = registry.create_artifact(run_id="arm-second", kind="report", content=IDENTICAL,
                                      schema_version="1")

    assert first == second, "the blob stays content-addressed"
    owners = {row["run_id"] for row in registry.rows("artifacts") if row["artifact_id"] == first}
    assert owners == {"arm-first", "arm-second"}


def test_a_re_measured_arm_can_be_promoted_on_its_own_artifact(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "arm-first", 0.81)
    create_run(registry, "arm-second", 0.81)
    registry.create_artifact(run_id="arm-first", kind="checkpoint", content=IDENTICAL,
                             schema_version="1")
    artifact = registry.create_artifact(run_id="arm-second", kind="checkpoint", content=IDENTICAL,
                                        schema_version="1")

    adopt_run_and_promote(
        registry, run_id="arm-second", model_id="model", reason="re-measured arm wins",
        model_version={"version": 2, "artifact_id": artifact, "checksum": artifact,
                       "family_version": "linear@1", "code_sha": SHA, "preprocessing_hash": "prep",
                       "calibration": {}, "thresholds": {},
                       "compat_result": {"head_sha": SHA, "passed": True, "at": 1},
                       "status": "active"})

    version = next(row for row in registry.rows("model_versions") if row["version"] == 2)
    assert version["run_id"] == "arm-second"
    assert version["artifact_id"] == artifact


def test_promotion_still_refuses_an_artifact_another_run_owns(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "arm-first", 0.81)
    create_run(registry, "arm-second", 0.81)
    artifact = registry.create_artifact(run_id="arm-first", kind="checkpoint", content=IDENTICAL,
                                        schema_version="1")

    with pytest.raises(RegistryError, match="adjudicated run's artifact"):
        adopt_run_and_promote(
            registry, run_id="arm-second", model_id="model", reason="borrowed artifact",
            model_version={"version": 2, "artifact_id": artifact, "checksum": artifact,
                           "family_version": "linear@1", "code_sha": SHA,
                           "preprocessing_hash": "prep", "calibration": {}, "thresholds": {},
                           "compat_result": {"head_sha": SHA, "passed": True, "at": 1},
                           "status": "active"})


def test_a_schema_5_projection_is_rebuilt_onto_the_composite_key(tmp_path: Path) -> None:
    """The upgrade is lossless: the events log is the record, SQLite is only its projection."""
    registry = registry_with_champion(tmp_path)
    create_run(registry, "arm-first", 0.81)
    registry.create_artifact(run_id="arm-first", kind="report", content=IDENTICAL,
                             schema_version="1")
    before = registry.canonical_projection_digest()

    with registry._connect() as db:
        db.execute("PRAGMA user_version=5")

    reopened = Registry(tmp_path)
    with reopened._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        key = [row[1] for row in db.execute("PRAGMA table_info(artifacts)") if row[5]]
    assert key == ["artifact_id", "run_id"]
    assert reopened.canonical_projection_digest() == before
