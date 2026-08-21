from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from knowledge.ml_registry import HistoricalLedgerImporter, Registry, RunsExport
from knowledge.ml_registry.contracts import CodeRef, ContractError, Partition
from knowledge.ml_registry.storage import BlobError, EventLogError, RegistryError


REPO = Path(__file__).resolve().parents[3]
SHA = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], check=True,
                     capture_output=True, text=True).stdout.strip()
BASE = SHA
DIFF = "c" * 64


def _metrics(metric: float = 0.91, *, throughput: float = 2.0,
             validity: str = "valid") -> dict[str, object]:
    return {
        "metric": metric, "validity": validity, "throughput": throughput,
        "throughput_unit": "rows_per_second", "memory_gb": 1.0, "cpu_time": 2.0,
        "load": {"start_1m": 0.25, "end_1m": 0.5},
    }


def _experiment(registry: Registry, name: str = "campaign") -> None:
    registry.create_experiment(
        experiment_id=name, spec_digest="d" * 64, stages=["representation"], metric="score",
        direction="maximize", win_condition={"metric_at_least": 0.9}, noise_floor=0.01,
        baseline_throughput=1.0,
    )


def _run(registry: Registry, name: str = "run-1", *, adjudicated: bool = False) -> None:
    registry.create_run(
        run_id=name, experiment_id="campaign", idea_id="idea-1", stage="representation", family="linear",
        params={"description": "baseline"},
        metrics={},
        code_ref={"schema_version": 1, "repo": str(REPO), "sha": SHA, "base_sha": BASE, "diff_hash": DIFF, "diff_lines": 3},
        device_fingerprint="cpu:test", status="running", verdict=None, started_at=1.0,
        finished_at=None, claim_owner="worker", heartbeat_at=1.0,
    )
    from knowledge.ml_registry.services.registry_runs import complete_run
    complete_run(registry, run_id=name, metrics=_metrics())
    assert not adjudicated, "adoption requires the atomic version/promotion helper"


def _version(registry: Registry) -> str:
    registry.register_model(model_id="model", family="linear", sport_scope="shared", axis="a01",
                            protocol="Detector", extends=None)
    digest = registry.create_artifact(run_id="run-1", kind="checkpoint", content=b"weights", schema_version="1")
    from knowledge.ml_registry.services.registry_aliases import adopt_run_and_promote
    adopt_run_and_promote(registry, run_id="run-1", model_id="model", reason="won", model_version=dict(
        version=1, artifact_id=digest, checksum=digest,
        family_version="linear@1", code_sha=SHA, preprocessing_hash="prep", calibration={}, thresholds={},
        compat_result={"head_sha": SHA, "passed": True, "at": 3.0}, status="active",
    ))
    return digest


def test_standard_tables_and_strict_contracts(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    assert registry.table_names() == (
        "aliases", "artifacts", "events", "experiments", "lineage", "model_versions",
        "registered_models", "runs",
    )
    assert Partition.parse("oof") is Partition.OUT_OF_FOLD
    with pytest.raises(ContractError, match="full 40- or 64"):
        CodeRef.from_mapping({"schema_version": 1, "repo": "r", "sha": "abc", "base_sha": BASE,
                              "diff_hash": DIFF, "diff_lines": 0})


def test_trainer_cannot_write_a_verdict(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    with pytest.raises(RegistryError, match="trainer.*cannot write a verdict"):
        registry.create_run(
            run_id="bad", experiment_id="campaign", idea_id="i", stage="s", family="f", params={}, metrics={},
            code_ref={"schema_version": 1, "repo": str(REPO), "sha": SHA, "base_sha": SHA, "diff_hash": DIFF, "diff_lines": 0},
            device_fingerprint="cpu", status="succeeded", verdict="adopted", started_at=1,
            finished_at=1, claim_owner="trainer", heartbeat_at=1,
        )


def test_run_transitions_are_reasoned_idempotent_and_evented(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    registry.create_run(
        run_id="run", experiment_id="campaign", idea_id="i", stage="s", family="f", params={}, metrics={},
        code_ref={"schema_version": 1, "repo": str(REPO), "sha": SHA, "base_sha": SHA, "diff_hash": DIFF, "diff_lines": 0},
        device_fingerprint="cpu", status="running", verdict=None, started_at=1,
        finished_at=None, claim_owner="trainer", heartbeat_at=1,
    )
    from knowledge.ml_registry.services.registry_runs import complete_run
    from knowledge.ml_registry.services.registry_aliases import adjudicate_run
    complete_run(registry, run_id="run", metrics=_metrics(1.0))
    after_complete = registry.list_events()
    complete_run(registry, run_id="run", metrics=_metrics(1.0))
    assert registry.list_events() == after_complete
    adjudicate_run(registry, run_id="run", verdict="rejected", status="succeeded", reason="below floor")
    after_verdict = registry.list_events()
    adjudicate_run(registry, run_id="run", verdict="rejected", status="succeeded", reason="below floor")
    assert registry.list_events() == after_verdict
    assert registry.list_runs(experiment_id="campaign")[0]["verdict"] == "rejected"
    assert registry.list_runs(experiment_id="campaign")[0]["status"] == "succeeded"


def test_reasoned_supersession_is_distinct_and_idempotent(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    registry.create_run(
        run_id="run", experiment_id="campaign", idea_id="i", stage="s", family="f", params={}, metrics={},
        code_ref={"schema_version": 1, "repo": str(REPO), "sha": SHA, "base_sha": SHA, "diff_hash": DIFF, "diff_lines": 0},
        device_fingerprint="cpu", status="running", verdict=None, started_at=1,
        finished_at=None, claim_owner="trainer", heartbeat_at=1,
    )
    from knowledge.ml_registry.services.registry_aliases import supersede_run
    with pytest.raises(RegistryError, match="reason"):
        supersede_run(registry, run_id="run", reason="")
    supersede_run(registry, run_id="run", reason="controller stopped")
    count = len(registry.list_events())
    supersede_run(registry, run_id="run", reason="controller stopped")
    assert len(registry.list_events()) == count
    assert registry.list_runs(experiment_id="campaign")[0]["status"] == "superseded"


def test_fixture_o_immutable_versions_and_alias_authorities(tmp_path: Path) -> None:
    registry = Registry(tmp_path, clock=lambda: 4.0)
    _experiment(registry)
    _run(registry)
    _version(registry)
    with sqlite3.connect(registry.db_path) as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE model_versions SET status='superseded'")
    with pytest.raises(RegistryError, match="service-owned"):
        registry.set_alias(model_id="model", alias="production", version=1, set_by="adjudicate", reason="bad")
    with pytest.raises(RegistryError, match="service-owned"):
        registry.set_alias(model_id="model", alias="champion", version=1, set_by="finalize", reason="bad")
    from knowledge.ml_registry.services.registry_aliases import move_champion
    from knowledge.ml_registry.services.registry_finalize import RegistryFinalizeService
    move_champion(registry, model_id="model", version=1, reason="won")
    RegistryFinalizeService(registry).move_production(model_id="model", version=1, reason="compat")
    aliases = {row["alias"]: row for row in registry.rows("aliases")}
    assert aliases["champion"]["reason"] == "won"
    assert aliases["production"]["set_by"] == "finalize"


def test_model_version_requires_convergence_matching_code_and_blob(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    _run(registry, adjudicated=False)
    registry.register_model(model_id="model", family="f", sport_scope="shared", axis="a", protocol="P", extends=None)
    digest = registry.create_artifact(run_id="run-1", kind="checkpoint", content=b"x", schema_version="1")
    values = dict(model_id="model", version=1, run_id="run-1", artifact_id=digest, checksum=digest,
                  family_version="f@1", code_sha=SHA, preprocessing_hash="p", calibration={}, thresholds={},
                  compat_result={"head_sha": SHA, "passed": True, "at": 1}, status="active")
    with pytest.raises(RegistryError, match="externally adjudicated"):
        registry.create_model_version(**values)


def test_event_before_projection_recovers_after_crash(tmp_path: Path) -> None:
    def crash(_event):
        raise RuntimeError("crash after event")

    registry = Registry(tmp_path, after_event=crash)
    with pytest.raises(RuntimeError, match="crash after event"):
        _experiment(registry)
    recovered = Registry(tmp_path)
    assert recovered.rows("experiments")[0]["experiment_id"] == "campaign"
    assert len(recovered.rows("events")) == 1


def test_refused_constraint_does_not_poison_durable_events(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    before = registry.list_events()
    with pytest.raises(sqlite3.IntegrityError):
        _experiment(registry)
    with pytest.raises(sqlite3.IntegrityError):
        registry.create_run(
            run_id="orphan", experiment_id="missing", idea_id="i", stage="s", family="f",
            params={}, metrics={}, code_ref={"schema_version": 1, "repo": str(REPO), "sha": SHA, "base_sha": SHA,
            "diff_hash": DIFF, "diff_lines": 0}, device_fingerprint="cpu", status="running",
            verdict=None, started_at=1, finished_at=None, claim_owner="x", heartbeat_at=1,
        )
    assert registry.list_events() == before
    assert Registry.open(tmp_path).rows("experiments")[0]["experiment_id"] == "campaign"


def test_event_tamper_and_blob_tamper_are_detected(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    _run(registry)
    digest = registry.create_artifact(run_id="run-1", kind="checkpoint", content=b"weights", schema_version="1")
    registry.blobs.path(digest).write_bytes(b"tampered")
    with pytest.raises(BlobError, match="checksum"):
        registry.blobs.verify(digest)
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    event = json.loads(lines[0])
    event["payload"]["metric"] = "changed"
    lines[0] = json.dumps(event)
    (tmp_path / "events.jsonl").write_text("\n".join(lines) + "\n")
    with pytest.raises(EventLogError, match="hash chain"):
        Registry(tmp_path)


def test_torn_final_event_is_quarantined_and_projection_rebuilt(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    with (tmp_path / "events.jsonl").open("ab") as handle:
        handle.write(b'{"schema_version":1,"sequence":2')
    registry.db_path.unlink()
    recovered = Registry(tmp_path)
    assert recovered.rows("experiments")[0]["experiment_id"] == "campaign"
    quarantines = list(tmp_path.glob("events.jsonl.torn-*"))
    assert len(quarantines) == 1 and quarantines[0].read_bytes().startswith(b'{"schema_version"')


def test_future_sqlite_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(RegistryError, match="newer than supported"):
        Registry(tmp_path)


@pytest.mark.parametrize("table", [
    "experiments", "runs", "artifacts", "registered_models", "model_versions", "lineage", "aliases",
])
def test_external_sql_cannot_mutate_any_projection_table(tmp_path: Path, table: str) -> None:
    registry = Registry(tmp_path)
    before = registry.list_events()
    with sqlite3.connect(registry.db_path) as db, pytest.raises(sqlite3.DatabaseError, match="authority"):
        db.execute(f"INSERT INTO {table} DEFAULT VALUES")
    assert registry.list_events() == before


def test_single_writer_serializes_concurrent_events(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: registry.create_experiment(
            experiment_id=f"c{index}", spec_digest=hashlib.sha256(str(index).encode()).hexdigest(),
            stages=["s"], metric="m", direction="maximize", win_condition={"metric_at_least": 1},
            noise_floor=0, baseline_throughput=0,
        ), range(24)))
    assert len(registry.rows("experiments")) == 24
    assert [event.sequence for event in registry.events.read()] == list(range(1, 25))


def test_compatibility_changes_are_append_only_effective_state(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    _run(registry)
    _version(registry)
    original = registry.rows("model_versions")[0]
    registry.record_compatibility(model_id="model", version=1, head_sha=SHA,
                                  passed=False, reason="current production code cannot load it")
    assert registry.rows("model_versions")[0] == original
    effective = registry.effective_model_version("model", 1)
    assert effective["effective_status"] == "incompatible"
    assert effective["effective_compat_result"]["passed"] is False
    from knowledge.ml_registry.services.registry_finalize import RegistryFinalizeService
    with pytest.raises(ValueError, match="incompatible"):
        RegistryFinalizeService(registry).move_production(
            model_id="model", version=1, reason="must not bypass latest compat"
        )
    with pytest.raises(RegistryError, match="current HEAD"):
        registry.record_compatibility(model_id="model", version=1, head_sha="f" * 40,
                                      passed=True, reason="stale checkout")


def test_production_rechecks_head_after_the_compatibility_pass(tmp_path: Path, monkeypatch) -> None:
    registry = Registry(tmp_path)
    _experiment(registry)
    _run(registry)
    _version(registry)
    monkeypatch.setattr(registry, "_git_head", lambda _repo: "f" * 40)
    from knowledge.ml_registry.services.registry_finalize import RegistryFinalizeService
    with pytest.raises(RegistryError, match="compatibility-passing"):
        RegistryFinalizeService(registry).move_production(model_id="model", version=1, reason="stale pass")


def test_historical_import_and_runs_export_are_byte_exact(tmp_path: Path) -> None:
    content = (
        "commit\tmetric_value\tmemory_gb\tstatus\tdescription\tthroughput\tdiff_lines\n"
        f"{SHA}\t0.910000\t1.500\tok\tbaseline\t2.2500\t3\n"
    )
    registry = Registry(tmp_path)
    count = HistoricalLedgerImporter(registry).import_ledger(
        content, experiment_id="legacy", spec_digest="d" * 64, metric="score",
        direction="maximize", repo=str(REPO),
    )
    assert count == 1
    assert RunsExport.from_registry(registry, "legacy").serialize() == content
    before = registry.list_events()
    HistoricalLedgerImporter(registry).import_ledger(
        content, experiment_id="legacy", spec_digest="d" * 64, metric="score",
        direction="maximize", repo=str(REPO),
    )
    assert registry.list_events() == before
    with pytest.raises(RegistryError, match="full semantic payload"):
        HistoricalLedgerImporter(registry).import_ledger(
            content, experiment_id="legacy", spec_digest="d" * 64, metric="different",
            direction="maximize", repo=str(REPO),
        )


def test_historical_zero_metric_without_disposition_stays_explicitly_unknown(tmp_path: Path) -> None:
    content = (
        "commit\tmetric_value\tmemory_gb\tstatus\tdescription\tthroughput\tdiff_lines\n"
        f"{SHA}\t0.0\t1.0\tok\tbroken\t1.0\t1\n"
    )
    registry = Registry(tmp_path)
    HistoricalLedgerImporter(registry).import_ledger(
        content, experiment_id="legacy", spec_digest="d" * 64, metric="score",
        direction="maximize", repo=str(REPO),
    )
    run = registry.list_runs(experiment_id="legacy")[0]
    assert json.loads(run["metrics"])["validity"] == "unknown"
    assert run["verdict"] is None


def test_historical_invalid_annotation_projects_voided_verdict_without_rewriting_source(
    tmp_path: Path,
) -> None:
    from knowledge.ml_registry.contracts import (
        LedgerAnnotations,
        LedgerRowIdentity,
        LedgerValidity,
        ThroughputUnit,
    )

    content = (
        "commit\tmetric_value\tmemory_gb\tstatus\tdescription\tthroughput\tdiff_lines\n"
        f"{SHA}:invalid\t0.0\t1.0\tok\tprecision gate failed\t1.0\t1\n"
        f"{SHA}:unknown\t0.1\t1.0\tok\tunadjudicated\t1.0\t1\n"
    )
    annotations = LedgerAnnotations(
        {LedgerRowIdentity(f"{SHA}:invalid", 0): LedgerValidity.INVALID},
        {
            LedgerRowIdentity(f"{SHA}:invalid", 0): ThroughputUnit.SAMPLES_PER_SECOND,
            LedgerRowIdentity(f"{SHA}:unknown", 0): ThroughputUnit.SAMPLES_PER_SECOND,
        },
    )
    registry = Registry(tmp_path)
    HistoricalLedgerImporter(registry).import_ledger(
        content,
        experiment_id="legacy",
        spec_digest="d" * 64,
        metric="score",
        direction="maximize",
        annotations=annotations,
    )
    runs = registry.list_runs(experiment_id="legacy")
    assert [(run["status"], run["verdict"]) for run in runs] == [
        ("voided", "voided"),
        ("complete", None),
    ]
    assert RunsExport.from_registry(registry, "legacy").serialize() == content
