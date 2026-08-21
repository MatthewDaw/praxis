from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import time
from typing import Any

from knowledge.ml_registry.contracts import CodeRef, LegacyCodeRef
from knowledge.ml_registry.domain.run import RunMetricError, RunMetrics
from knowledge.ml_registry.file_lock import exclusive_file_lock

from .blobs import BlobStore
from .events import EventLog, RegistryEvent


class RegistryError(ValueError):
    pass


DDL = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS experiments(
 experiment_id TEXT PRIMARY KEY, spec_digest TEXT NOT NULL, stages TEXT NOT NULL, metric TEXT NOT NULL,
 direction TEXT NOT NULL CHECK(direction IN ('maximize','minimize')), win_condition TEXT NOT NULL,
 noise_floor REAL NOT NULL CHECK(noise_floor>=0), baseline_throughput REAL NOT NULL CHECK(baseline_throughput>=0));
CREATE TABLE IF NOT EXISTS runs(
 run_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id), idea_id TEXT NOT NULL,
 stage TEXT NOT NULL, family TEXT NOT NULL, params TEXT NOT NULL, metrics TEXT NOT NULL, code_ref TEXT NOT NULL,
 device_fingerprint TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN
 ('running','complete','succeeded','failed','voided','superseded')), verdict TEXT CHECK(verdict IS NULL OR verdict IN
 ('adopted','rejected','parked','voided')), started_at REAL NOT NULL, finished_at REAL,
 claim_owner TEXT NOT NULL, heartbeat_at REAL NOT NULL, CHECK(COALESCE(
 (status IN ('running','complete','failed','superseded') AND verdict IS NULL) OR
 (status='succeeded' AND verdict IN ('adopted','rejected','parked')) OR
 (status='voided' AND verdict='voided'),0)));
CREATE TABLE IF NOT EXISTS artifacts(
 artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id), kind TEXT NOT NULL CHECK(kind IN
 ('checkpoint','oof_predictions','split_manifest','dataset_manifest','report')), uri TEXT NOT NULL,
 bytes INTEGER NOT NULL CHECK(bytes>=0), schema_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS registered_models(
 model_id TEXT PRIMARY KEY, family TEXT NOT NULL, sport_scope TEXT NOT NULL, axis TEXT NOT NULL,
 protocol TEXT NOT NULL, extends_json TEXT);
CREATE TABLE IF NOT EXISTS model_versions(
 model_id TEXT NOT NULL REFERENCES registered_models(model_id), version INTEGER NOT NULL CHECK(version>=1),
 run_id TEXT NOT NULL REFERENCES runs(run_id), artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
 checksum TEXT NOT NULL, family_version TEXT NOT NULL, code_sha TEXT NOT NULL,
 preprocessing_hash TEXT NOT NULL, calibration TEXT NOT NULL, thresholds TEXT NOT NULL,
 compat_result TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','incompatible','superseded')),
 PRIMARY KEY(model_id,version));
CREATE TABLE IF NOT EXISTS lineage(
 child_model_id TEXT NOT NULL, child_version INTEGER NOT NULL, parent_model_id TEXT NOT NULL,
 parent_version INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('input_artifact','derived_from','backbone')),
 PRIMARY KEY(child_model_id,child_version,parent_model_id,parent_version,kind),
 FOREIGN KEY(child_model_id,child_version) REFERENCES model_versions(model_id,version),
 FOREIGN KEY(parent_model_id,parent_version) REFERENCES model_versions(model_id,version));
CREATE TABLE IF NOT EXISTS aliases(
 model_id TEXT NOT NULL REFERENCES registered_models(model_id), alias TEXT NOT NULL CHECK(alias IN
 ('champion','candidate','production')), version INTEGER NOT NULL, set_by TEXT NOT NULL, reason TEXT NOT NULL,
 at REAL NOT NULL, PRIMARY KEY(model_id,alias),
 FOREIGN KEY(model_id,version) REFERENCES model_versions(model_id,version));
CREATE TABLE IF NOT EXISTS events(
 sequence INTEGER PRIMARY KEY, schema_version INTEGER NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL, at REAL NOT NULL,
 previous_hash TEXT, event_hash TEXT NOT NULL UNIQUE);
CREATE TRIGGER IF NOT EXISTS immutable_artifacts_update BEFORE UPDATE ON artifacts BEGIN
 SELECT RAISE(ABORT,'artifacts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_artifacts_delete BEFORE DELETE ON artifacts BEGIN
 SELECT RAISE(ABORT,'artifacts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_versions_update BEFORE UPDATE ON model_versions BEGIN
 SELECT RAISE(ABORT,'model_versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_versions_delete BEFORE DELETE ON model_versions BEGIN
 SELECT RAISE(ABORT,'model_versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS production_authority_insert BEFORE INSERT ON aliases
 WHEN NEW.alias='production' AND NEW.set_by!='finalize' BEGIN SELECT RAISE(ABORT,'production alias requires finalize'); END;
CREATE TRIGGER IF NOT EXISTS production_authority_update BEFORE UPDATE ON aliases
 WHEN NEW.alias='production' AND NEW.set_by!='finalize' BEGIN SELECT RAISE(ABORT,'production alias requires finalize'); END;
CREATE TRIGGER IF NOT EXISTS champion_authority_insert BEFORE INSERT ON aliases
 WHEN NEW.alias='champion' AND NEW.set_by NOT IN ('adjudicate','ratchet') BEGIN SELECT RAISE(ABORT,'champion alias requires adjudicate or ratchet'); END;
CREATE TRIGGER IF NOT EXISTS champion_authority_update BEFORE UPDATE ON aliases
WHEN NEW.alias='champion' AND NEW.set_by NOT IN ('adjudicate','ratchet') BEGIN SELECT RAISE(ABORT,'champion alias requires adjudicate or ratchet'); END;
CREATE TRIGGER IF NOT EXISTS guard_experiments_insert BEFORE INSERT ON experiments
 WHEN registry_authority() NOT IN ('experiment_created','historical_ledger_imported','historical_archive_imported') BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_experiments_update BEFORE UPDATE ON experiments BEGIN SELECT RAISE(ABORT,'experiments are immutable'); END;
CREATE TRIGGER IF NOT EXISTS guard_experiments_delete BEFORE DELETE ON experiments BEGIN SELECT RAISE(ABORT,'experiments are immutable'); END;
CREATE TRIGGER IF NOT EXISTS guard_runs_insert BEFORE INSERT ON runs
 WHEN registry_authority() NOT IN ('run_created','historical_ledger_imported','historical_archive_imported') BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_runs_update BEFORE UPDATE ON runs
 WHEN registry_authority() NOT IN ('run_completed','run_adjudicated','run_adopted','run_superseded','adoption_invalidated') BEGIN SELECT RAISE(ABORT,'run write authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_runs_delete BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT,'runs cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS valid_run_pair_insert BEFORE INSERT ON runs WHEN NOT COALESCE(
 (NEW.status IN ('running','complete','failed','superseded') AND NEW.verdict IS NULL) OR
 (NEW.status='succeeded' AND NEW.verdict IN ('adopted','rejected','parked')) OR
 (NEW.status='voided' AND NEW.verdict='voided'),0)
 BEGIN SELECT RAISE(ABORT,'invalid run status/verdict pair'); END;
CREATE TRIGGER IF NOT EXISTS valid_run_pair_update BEFORE UPDATE ON runs WHEN NOT COALESCE(
 (NEW.status IN ('running','complete','failed','superseded') AND NEW.verdict IS NULL) OR
 (NEW.status='succeeded' AND NEW.verdict IN ('adopted','rejected','parked')) OR
 (NEW.status='voided' AND NEW.verdict='voided'),0)
 BEGIN SELECT RAISE(ABORT,'invalid run status/verdict pair'); END;
CREATE TRIGGER IF NOT EXISTS guard_artifacts_insert BEFORE INSERT ON artifacts
 WHEN registry_authority()!='artifact_created' BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_models_insert BEFORE INSERT ON registered_models
 WHEN registry_authority() NOT IN ('registered_model_created','historical_archive_imported') BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_models_update BEFORE UPDATE ON registered_models BEGIN SELECT RAISE(ABORT,'registered_models are immutable'); END;
CREATE TRIGGER IF NOT EXISTS guard_models_delete BEFORE DELETE ON registered_models BEGIN SELECT RAISE(ABORT,'registered_models are immutable'); END;
CREATE TRIGGER IF NOT EXISTS guard_versions_insert BEFORE INSERT ON model_versions
 WHEN registry_authority() NOT IN ('model_version_created','run_adopted') BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_lineage_insert BEFORE INSERT ON lineage
 WHEN registry_authority() NOT IN ('lineage_created','run_adopted') BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_lineage_update BEFORE UPDATE ON lineage BEGIN SELECT RAISE(ABORT,'lineage is immutable'); END;
CREATE TRIGGER IF NOT EXISTS guard_lineage_delete BEFORE DELETE ON lineage BEGIN SELECT RAISE(ABORT,'lineage is immutable'); END;
CREATE TRIGGER IF NOT EXISTS guard_aliases_insert BEFORE INSERT ON aliases
 WHEN registry_authority() NOT IN ('alias_set','run_adopted','registry_finalized') BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_aliases_update BEFORE UPDATE ON aliases
 WHEN registry_authority() NOT IN ('alias_set','run_adopted','adoption_invalidated','registry_finalized') BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_aliases_delete BEFORE DELETE ON aliases BEGIN SELECT RAISE(ABORT,'aliases cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS guard_events_insert BEFORE INSERT ON events
 WHEN registry_authority()='' BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_events_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS guard_events_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events are immutable'); END;
"""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


_CHAMPION_CAPABILITY = object()
_PRODUCTION_CAPABILITY = object()
_TRAINER_CAPABILITY = object()
_ADJUDICATOR_CAPABILITY = object()


class Registry:
    """Single-writer registry: durable event first, recoverable SQLite projection second."""

    def __init__(self, root: str | Path, *, clock: Callable[[], float] = time.time,
                 after_event: Callable[[RegistryEvent], None] | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "registry.sqlite3"
        self.lock_path = self.root / "writer"
        self.events = EventLog(self.root / "events.jsonl")
        self.blobs = BlobStore(self.root / "blobs")
        self.clock = clock
        self.after_event = after_event
        with self._connect() as db:
            from .migration import migrate_schema
            migrate_schema(db)
        self.recover()

    @classmethod
    def open(cls, root: str | Path, **kwargs: Any) -> "Registry":
        return cls(root, **kwargs)

    is_single_writer = True
    model_versions_are_immutable = True
    imports_results_tsv = False

    @staticmethod
    def alias_writer(alias: str) -> str:
        if alias == "champion":
            return "adjudication"
        if alias == "production":
            return "finalize"
        raise RegistryError(f"alias {alias!r} has no exclusive writer")

    def _connect(self, authority: str = "") -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.create_function("registry_authority", 0, lambda: authority)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def recover(self) -> None:
        with exclusive_file_lock(self.lock_path):
            events = self.events.read()
            with self._connect() as db:
                if self._projection_matches(db, events):
                    return
            self._rebuild_projection(events)

    def _projection_matches(self, db: sqlite3.Connection,
                            events: tuple[RegistryEvent, ...]) -> bool:
        expected = sqlite3.connect(":memory:")
        expected.row_factory = sqlite3.Row
        expected.execute("PRAGMA foreign_keys=ON")
        authority = {"value": ""}
        expected.create_function("registry_authority", 0, lambda: authority["value"])
        expected.executescript(DDL)
        for event in events:
            authority["value"] = event.event_type
            self._project(expected, event)
            self._record_event(expected, event)
        expected.commit()
        try:
            for table in self.table_names():
                columns = [row[1] for row in expected.execute(f"PRAGMA table_info({table})")]
                select = ",".join(columns)
                actual = [tuple(row) for row in db.execute(f"SELECT {select} FROM {table} ORDER BY rowid")]
                wanted = [tuple(row) for row in expected.execute(f"SELECT {select} FROM {table} ORDER BY rowid")]
                if actual != wanted:
                    return False
            return True
        finally:
            expected.close()

    def _rebuild_projection(self, events: tuple[RegistryEvent, ...]) -> None:
        quarantine = self.db_path.with_name(f"{self.db_path.name}.projection-quarantine")
        quarantine.unlink(missing_ok=True)
        if self.db_path.exists():
            self.db_path.replace(quarantine)
        for suffix in ("-wal", "-shm"):
            Path(f"{self.db_path}{suffix}").unlink(missing_ok=True)
        with self._connect() as bootstrap:
            from .migration import migrate_schema
            migrate_schema(bootstrap)
        for event in events:
            with self._connect(event.event_type) as db:
                self._project(db, event)
                self._record_event(db, event)

    def _write(self, event_type: str, payload: Mapping[str, Any]) -> None:
        with exclusive_file_lock(self.lock_path):
            with self._connect(event_type) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    at = self.clock()
                    pending = RegistryEvent(1, len(self.events.read()) + 1, event_type, dict(payload), at, None, "")
                    # Apply invisibly first: refused constraints must never poison durable history.
                    changed = self._project(db, pending)
                    if changed is False:
                        db.rollback()
                        return
                    event = self.events.append(event_type, payload, at=at)
                    if self.after_event is not None:
                        self.after_event(event)
                    self._record_event(db, event)
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise

    def _record_event(self, db: sqlite3.Connection, event: RegistryEvent) -> None:
        db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)", (event.sequence, event.schema_version,
                   event.event_type, _json(event.payload), event.at, event.previous_hash, event.event_hash))

    def _project(self, db: sqlite3.Connection, event: RegistryEvent) -> bool:
        p = event.payload
        op = event.event_type
        if op == "experiment_created":
            db.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?)", (p["experiment_id"], p["spec_digest"],
                       _json(p["stages"]), p["metric"], p["direction"], _json(p["win_condition"]),
                       p["noise_floor"], p["baseline_throughput"]))
        elif op == "run_created":
            self._insert_run(db, p)
        elif op == "run_adjudicated":
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (p["run_id"],)).fetchone()
            if row is None:
                raise RegistryError("unknown run")
            if row["status"] == p["status"] and row["verdict"] == p["verdict"]:
                return False
            if row["status"] != "complete" or row["verdict"] is not None:
                raise RegistryError("adjudication requires one complete, unadjudicated run")
            db.execute("UPDATE runs SET status=?,verdict=?,finished_at=?,heartbeat_at=? WHERE run_id=?",
                       (p["status"], p["verdict"], p["at"], p["at"], p["run_id"]))
        elif op == "run_adopted":
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (p["run_id"],)).fetchone()
            if row is None or row["status"] != "complete" or row["verdict"] is not None:
                raise RegistryError("atomic adoption requires one complete, unadjudicated run")
            version = p["model_version"]
            db.execute("UPDATE runs SET status='succeeded',verdict='adopted',finished_at=?,heartbeat_at=? "
                       "WHERE run_id=?", (event.at, event.at, p["run_id"]))
            db.execute("INSERT INTO model_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                version["model_id"], version["version"], p["run_id"], version["artifact_id"],
                version["checksum"], version["family_version"], version["code_sha"],
                version["preprocessing_hash"], _json(version["calibration"]), _json(version["thresholds"]),
                _json(version["compat_result"]), version["status"],
            ))
            parent_version = p.get("parent_version")
            if parent_version is not None:
                db.execute("INSERT INTO lineage VALUES(?,?,?,?,?)", (
                    version["model_id"], version["version"], version["model_id"],
                    parent_version, "derived_from",
                ))
            db.execute("INSERT INTO aliases VALUES(?,?,?,?,?,?) ON CONFLICT(model_id,alias) DO UPDATE SET "
                       "version=excluded.version,set_by=excluded.set_by,reason=excluded.reason,at=excluded.at",
                       (version["model_id"], "champion", version["version"], "adjudicate", p["reason"], event.at))
        elif op == "ratchet_evidence_recorded":
            # Evidence is canonical in the append-only event stream.  Keeping it out of
            # mutable model metadata preserves the eight-table registry contract.
            return
        elif op == "trial_refused":
            return
        elif op == "campaign_spec_registered":
            # CampaignSpec is a versioned control-plane contract, not a ninth
            # model-registry entity. Its canonical copy lives in the event log.
            return
        elif op == "adoption_invalidated":
            run = db.execute("SELECT * FROM runs WHERE run_id=?", (p["adoption_run_id"],)).fetchone()
            if run is None or run["status"] != "succeeded" or run["verdict"] != "adopted":
                raise RegistryError("ratchet rollback requires the active adopted run")
            alias = db.execute("SELECT * FROM aliases WHERE model_id=? AND alias='champion'",
                               (p["model_id"],)).fetchone()
            if alias is None or alias["version"] != p["invalidated_version"]:
                raise RegistryError("ratchet rollback requires the invalidated version to be champion")
            db.execute("UPDATE runs SET status='superseded',verdict=NULL,finished_at=?,heartbeat_at=? "
                       "WHERE run_id=?", (event.at, event.at, p["adoption_run_id"]))
            db.execute("UPDATE aliases SET version=?,set_by='ratchet',reason=?,at=? "
                       "WHERE model_id=? AND alias='champion'", (
                           p["parent_version"], p["reason"], event.at, p["model_id"],
                       ))
        elif op == "run_completed":
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (p["run_id"],)).fetchone()
            if row is None:
                raise RegistryError("unknown run")
            encoded = _json(p["metrics"])
            if row["status"] == "complete" and row["metrics"] == encoded:
                return False
            if row["status"] != "running" or row["verdict"] is not None:
                raise RegistryError("trainer completion requires one running, unadjudicated run")
            db.execute("UPDATE runs SET status='complete',metrics=?,finished_at=?,heartbeat_at=? WHERE run_id=?",
                       (encoded, p["at"], p["at"], p["run_id"]))
        elif op == "run_superseded":
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (p["run_id"],)).fetchone()
            if row is None:
                raise RegistryError("unknown run")
            if row["status"] == "superseded" and row["verdict"] is None:
                return False
            if row["status"] not in {"running", "complete"}:
                raise RegistryError("only a non-terminal run can be superseded")
            db.execute("UPDATE runs SET status='superseded',verdict=NULL,finished_at=?,heartbeat_at=? WHERE run_id=?",
                       (p["at"], p["at"], p["run_id"]))
        elif op == "historical_ledger_imported":
            experiment = p["experiment"]
            db.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?)", (
                experiment["experiment_id"], experiment["spec_digest"], _json(experiment["stages"]),
                experiment["metric"], experiment["direction"], _json(experiment["win_condition"]),
                experiment["noise_floor"], experiment["baseline_throughput"],
            ))
            for run in p["runs"]:
                LegacyCodeRef.from_mapping(run["code_ref"])
                self._insert_run(db, run)
        elif op == "historical_archive_imported":
            experiment = p["experiment"]
            db.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?)", (
                experiment["experiment_id"], experiment["spec_digest"], _json(experiment["stages"]),
                experiment["metric"], experiment["direction"], _json(experiment["win_condition"]),
                experiment["noise_floor"], experiment["baseline_throughput"],
            ))
            for run in p["runs"]:
                LegacyCodeRef.from_mapping(run["code_ref"])
                self._insert_run(db, run)
            model = p.get("model")
            if model is not None:
                db.execute("INSERT INTO registered_models VALUES(?,?,?,?,?,?)", (
                    model["model_id"], model["family"], model["sport_scope"], model["axis"],
                    model["protocol"], _json(model.get("extends")) if model.get("extends") else None,
                ))
        elif op == "historical_evidence_freeze_imported":
            # Evidence is content-addressed in BlobStore and described by this event;
            # it deliberately has no canonical SQL projection.
            return
        elif op == "artifact_created":
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?)", tuple(p[k] for k in
                       ("artifact_id", "run_id", "kind", "uri", "bytes", "schema_version")))
        elif op == "registered_model_created":
            db.execute("INSERT INTO registered_models VALUES(?,?,?,?,?,?)", (p["model_id"], p["family"],
                       p["sport_scope"], p["axis"], p["protocol"], _json(p.get("extends")) if p.get("extends") else None))
        elif op == "model_version_created":
            db.execute("INSERT INTO model_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (p["model_id"], p["version"],
                       p["run_id"], p["artifact_id"], p["checksum"], p["family_version"], p["code_sha"],
                       p["preprocessing_hash"], _json(p["calibration"]), _json(p["thresholds"]),
                       _json(p["compat_result"]), p["status"]))
        elif op == "lineage_created":
            db.execute("INSERT INTO lineage VALUES(?,?,?,?,?)", tuple(p[k] for k in
                       ("child_model_id", "child_version", "parent_model_id", "parent_version", "kind")))
        elif op == "alias_set":
            db.execute("INSERT INTO aliases VALUES(?,?,?,?,?,?) ON CONFLICT(model_id,alias) DO UPDATE SET "
                       "version=excluded.version,set_by=excluded.set_by,reason=excluded.reason,at=excluded.at",
                       tuple(p[k] for k in ("model_id", "alias", "version", "set_by", "reason", "at")))
        elif op == "compatibility_recorded":
            return
        elif op == "registry_finalized":
            champion = db.execute(
                "SELECT version FROM aliases WHERE model_id=? AND alias='champion'", (p["model_id"],),
            ).fetchone()
            if champion is None or champion["version"] != p["version"]:
                raise RegistryError("finalization target is no longer the current champion")
            for upstream in p["upstreams"]:
                production = db.execute(
                    "SELECT version FROM aliases WHERE model_id=? AND alias='production'",
                    (upstream["model_id"],),
                ).fetchone()
                if production is None or production["version"] != upstream["version"]:
                    raise RegistryError("finalization upstream production alias moved before commit")
            db.execute("INSERT INTO aliases VALUES(?,?,?,?,?,?) ON CONFLICT(model_id,alias) DO UPDATE SET "
                       "version=excluded.version,set_by=excluded.set_by,reason=excluded.reason,at=excluded.at",
                       (p["model_id"], "production", p["version"], "finalize", p["reason"], event.at))
        else:
            raise RegistryError(f"unknown event type {op!r}")
        return True

    @staticmethod
    def _insert_run(db: sqlite3.Connection, p: Mapping[str, Any]) -> None:
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (p["run_id"],
                   p["experiment_id"], p["idea_id"], p["stage"], p["family"], _json(p["params"]),
                   _json(p["metrics"]), _json(p["code_ref"]), p["device_fingerprint"], p["status"],
                   p.get("verdict"), p["started_at"], p.get("finished_at"), p["claim_owner"],
                   p["heartbeat_at"]))

    def create_experiment(self, **values: Any) -> None:
        self._write("experiment_created", values)

    def create_run(self, **values: Any) -> None:
        code_ref = CodeRef.from_mapping(values["code_ref"])
        try:
            subprocess.run(
                ["git", "-C", code_ref.repo, "cat-file", "-e", f"{code_ref.sha}^{{commit}}"],
                check=True, capture_output=True, text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RegistryError("code_ref sha does not exist as a commit in its declared repo") from exc
        if values.get("verdict") is not None:
            raise RegistryError("trainer run creation cannot write a verdict")
        if values.get("status", "running") != "running" or values.get("finished_at") is not None:
            raise RegistryError("new runs must start running with no finished_at")
        if values.get("metrics") not in (None, {}):
            raise RegistryError("trainer records metrics only when completing a run")
        running = next((row for row in self.rows("runs")
                        if row["experiment_id"] == values.get("experiment_id")
                        and row["idea_id"] == values.get("idea_id")
                        and row["status"] == "running"), None)
        if running is not None:
            self._write("trial_refused", {
                "experiment_id": values.get("experiment_id"),
                "idea_id": values.get("idea_id"),
                "existing_run_id": running["run_id"],
                "refused_run_id": values.get("run_id"),
                "reason_code": "trial_already_in_flight",
            })
            raise RegistryError("trial_already_in_flight")
        payload = dict(values)
        payload["code_ref"] = code_ref.to_mapping()
        payload["metrics"] = {}
        payload.setdefault("status", "running")
        payload.setdefault("verdict", None)
        payload.setdefault("finished_at", None)
        payload.setdefault("heartbeat_at", payload.get("started_at"))
        self._write("run_created", payload)

    def _complete_run(self, *, run_id: str, metrics: Mapping[str, Any], capability: object) -> None:
        if capability is not _TRAINER_CAPABILITY:
            raise RegistryError("run completion requires trainer authority")
        try:
            typed = RunMetrics.from_mapping(metrics)
        except RunMetricError as exc:
            raise RegistryError(str(exc)) from exc
        self._write("run_completed", {"run_id": run_id, "metrics": dict(typed.to_mapping()),
                    "at": self.clock()})

    def _adjudicate_run(self, *, run_id: str, verdict: str, status: str, reason: str,
                        capability: object) -> None:
        if capability is not _ADJUDICATOR_CAPABILITY:
            raise RegistryError("run verdict requires adjudication authority")
        if verdict == "adopted":
            raise RegistryError("adoption requires the atomic model-version and champion promotion path")
        expected = {"rejected": "succeeded", "parked": "succeeded", "voided": "voided"}
        if expected.get(verdict) != status or not reason:
            raise RegistryError("run verdict and terminal status are inconsistent")
        self._write("run_adjudicated", {"run_id": run_id, "verdict": verdict, "status": status,
                    "reason": reason, "at": self.clock()})

    def _adopt_run_and_promote(self, *, run_id: str, model_id: str, reason: str,
                               model_version: Mapping[str, Any], capability: object) -> bool:
        if capability is not _ADJUDICATOR_CAPABILITY:
            raise RegistryError("atomic adoption requires adjudication authority")
        version = dict(model_version)
        version["model_id"] = model_id
        version["run_id"] = run_id
        prior = [event for event in self.events.read()
                 if event.event_type == "run_adopted" and event.payload.get("run_id") == run_id]
        champion = next((row for row in self.rows("aliases")
                         if row["model_id"] == model_id and row["alias"] == "champion"), None)
        parent_version = (prior[-1].payload.get("parent_version") if prior
                          else champion["version"] if champion is not None else None)
        payload = {"run_id": run_id, "reason": reason, "model_version": version,
                   "parent_version": parent_version}
        if prior:
            if prior[-1].payload != payload:
                raise RegistryError("atomic adoption retry drifted from its full semantic payload")
            self.recover()
            return False
        self._validate_adoption_payload(run_id=run_id, model_id=model_id, reason=reason,
                                        model_version=version)
        self._write("run_adopted", payload)
        return True

    def _record_ratchet_evidence(self, payload: Mapping[str, Any], *, capability: object) -> None:
        if capability is not _ADJUDICATOR_CAPABILITY:
            raise RegistryError("ratchet evidence requires adjudication authority")
        pair = (payload.get("run_id"), payload.get("counterfactual_run_id"))
        prior = [event for event in self.events.read()
                 if event.event_type == "ratchet_evidence_recorded"
                 and (event.payload.get("run_id"), event.payload.get("counterfactual_run_id")) == pair]
        if prior:
            if prior[-1].payload != payload:
                raise RegistryError("ratchet evidence retry drifted from its full semantic payload")
            self.recover()
            return
        self._write("ratchet_evidence_recorded", payload)

    def _invalidate_adoption(self, payload: Mapping[str, Any], *, capability: object) -> None:
        if capability is not _ADJUDICATOR_CAPABILITY:
            raise RegistryError("ratchet rollback requires adjudication authority")
        self._write("adoption_invalidated", payload)

    def _validate_adoption_payload(self, *, run_id: str, model_id: str, reason: str,
                                    model_version: Mapping[str, Any]) -> None:
        if not reason.strip():
            raise RegistryError("adoption requires a reason")
        run = next((row for row in self.rows("runs") if row["run_id"] == run_id), None)
        if run is None or run["status"] != "complete" or run["verdict"] is not None:
            raise RegistryError("atomic adoption requires one complete, unadjudicated run")
        if not any(row["model_id"] == model_id for row in self.rows("registered_models")):
            raise RegistryError("atomic adoption references an unknown registered model")
        artifact_id = str(model_version.get("artifact_id", ""))
        artifact = next((row for row in self.rows("artifacts") if row["artifact_id"] == artifact_id), None)
        if artifact is None or artifact["run_id"] != run_id:
            raise RegistryError("atomic adoption requires the adjudicated run's artifact")
        self.blobs.verify(artifact_id)
        if model_version.get("checksum") != artifact_id:
            raise RegistryError("model version checksum must equal its content-addressed artifact id")
        code_ref = json.loads(run["code_ref"])
        if model_version.get("code_sha") != code_ref.get("sha"):
            raise RegistryError("model version code_sha differs from its run code_ref")
        compat = model_version.get("compat_result")
        if not isinstance(compat, Mapping) or set(compat) != {"head_sha", "passed", "at"}:
            raise RegistryError("compat_result requires exactly head_sha, passed, and at")
        if compat["passed"] is not True or compat["head_sha"] != self._git_head(code_ref["repo"]):
            raise RegistryError("initial compatibility must pass against the declared repo's current HEAD")
        if model_version.get("status") != "active":
            raise RegistryError("an adopted model version must start active")
        required = {"model_id", "run_id", "version", "artifact_id", "checksum", "family_version",
                    "code_sha", "preprocessing_hash", "calibration", "thresholds", "compat_result", "status"}
        if set(model_version) != required:
            raise RegistryError("atomic adoption model version payload has missing or unknown fields")

    def _supersede_run(self, *, run_id: str, reason: str, capability: object) -> None:
        if capability is not _ADJUDICATOR_CAPABILITY:
            raise RegistryError("supersession requires adjudication authority")
        if not reason.strip():
            raise RegistryError("supersession requires a reason")
        self._write("run_superseded", {"run_id": run_id, "reason": reason, "at": self.clock()})

    def create_artifact(self, *, run_id: str, kind: str, content: bytes, schema_version: str) -> str:
        digest, path = self.blobs.put(content)
        self._write("artifact_created", {"artifact_id": digest, "run_id": run_id, "kind": kind,
                    "uri": str(path), "bytes": len(content), "schema_version": schema_version})
        return digest

    def import_historical_ledger(self, *, import_id: str, experiment: Mapping[str, Any],
                                 runs: list[Mapping[str, Any]], source_blob_sha256: str) -> bool:
        payload = {"import_id": import_id, "experiment": dict(experiment),
                   "runs": [dict(run) for run in runs], "source_blob_sha256": source_blob_sha256}
        for event in self.events.read():
            if event.event_type == "historical_ledger_imported" and event.payload.get("import_id") == import_id:
                if event.payload != payload:
                    raise RegistryError("historical import id drifted from its full semantic payload")
                return False
        self.blobs.verify(source_blob_sha256)
        self._write("historical_ledger_imported", payload)
        return True

    def import_historical_archive(self, payload: Mapping[str, Any]) -> bool:
        """Commit one fully validated archive projection as a single durable event."""
        import_id = str(payload["import_id"])
        for event in self.events.read():
            if event.event_type == "historical_archive_imported" and event.payload.get("import_id") == import_id:
                if event.payload != payload:
                    raise RegistryError("historical import id drifted from its full semantic payload")
                return False
        for item in payload["evidence"]:
            self.blobs.verify(item["blob_sha256"])
        self._write("historical_archive_imported", payload)
        return True

    def import_historical_evidence_freeze(self, payload: Mapping[str, Any]) -> bool:
        import_id = str(payload["import_id"])
        for event in self.events.read():
            if event.event_type == "historical_evidence_freeze_imported" and event.payload.get("import_id") == import_id:
                if event.payload != payload:
                    raise RegistryError("historical evidence import id drifted")
                return False
        for item in payload["evidence"]:
            self.blobs.verify(item["blob_sha256"])
        self._write("historical_evidence_freeze_imported", payload)
        return True

    def register_campaign_spec(self, spec: Mapping[str, Any]) -> bool:
        """Persist a validated project-owned CampaignSpec in the canonical event log.

        Campaign specifications are control-plane inputs rather than registry entities,
        so they deliberately have no ninth projection table.  The latest event for a
        campaign is the durable portfolio-manifest snapshot used by readiness.
        """
        from knowledge.ml_registry.contracts import CampaignSpec

        canonical = CampaignSpec.from_mapping(spec).to_mapping()
        campaign_id = canonical["campaign_id"]
        prior = [event for event in self.events.read()
                 if event.event_type == "campaign_spec_registered"
                 and event.payload.get("campaign_id") == campaign_id]
        if prior and prior[-1].payload == canonical:
            return False
        self._write("campaign_spec_registered", canonical)
        return True

    def register_model(self, **values: Any) -> None:
        self._write("registered_model_created", values)

    def create_model_version(self, **values: Any) -> None:
        code_sha = str(values.get("code_sha", ""))
        if len(code_sha) not in {40, 64} or any(ch not in "0123456789abcdef" for ch in code_sha.lower()):
            raise RegistryError("code_sha must be a full 40- or 64-character hexadecimal digest")
        artifact_id = str(values["artifact_id"])
        self.blobs.verify(artifact_id)
        if values.get("checksum") != artifact_id:
            raise RegistryError("model version checksum must equal its content-addressed artifact id")
        runs = {row["run_id"]: row for row in self.rows("runs")}
        run = runs.get(str(values.get("run_id")))
        if run is None:
            raise RegistryError("model version references an unknown run")
        run_code_ref = json.loads(run["code_ref"])
        if run_code_ref.get("sha") != code_sha:
            raise RegistryError("model version code_sha differs from its run code_ref")
        if run["status"] != "succeeded" or run["verdict"] != "adopted":
            raise RegistryError("model versions require an externally adjudicated adopted run")
        compat = values.get("compat_result")
        if not isinstance(compat, Mapping) or set(compat) != {"head_sha", "passed", "at"}:
            raise RegistryError("compat_result requires exactly head_sha, passed, and at")
        head = self._git_head(run_code_ref["repo"])
        if compat["head_sha"] != head or compat["passed"] is not True:
            raise RegistryError("initial compatibility must pass against the declared repo's current HEAD")
        self._write("model_version_created", values)

    def create_lineage(self, **values: Any) -> None:
        self._write("lineage_created", values)

    def set_alias(self, **_values: Any) -> None:
        raise RegistryError("aliases are service-owned; use adjudication/ratchet or finalize")

    def _set_alias(self, *, model_id: str, alias: str, version: int, set_by: str, reason: str,
                   capability: object) -> None:
        if not reason.strip():
            raise RegistryError("alias move requires a non-empty reason")
        if alias == "production" and capability is not _PRODUCTION_CAPABILITY:
            raise RegistryError("production alias requires finalize authority")
        if alias == "champion" and capability is not _CHAMPION_CAPABILITY:
            raise RegistryError("champion alias requires adjudication authority")
        if alias == "production":
            effective = self.effective_model_version(model_id, version)
            compat = effective["effective_compat_result"]
            run = next(row for row in self.rows("runs") if row["run_id"] == effective["run_id"])
            code_ref = json.loads(run["code_ref"])
            if "repo" not in code_ref:
                raise RegistryError("production alias requires known git provenance")
            if (compat.get("passed") is not True or effective["effective_status"] != "active"
                    or compat.get("head_sha") != self._git_head(code_ref["repo"])):
                raise RegistryError("production alias requires an active, compatibility-passing version")
        self._write("alias_set", {"model_id": model_id, "alias": alias, "version": version,
                    "set_by": set_by, "reason": reason, "at": self.clock()})

    def _finalize_registry_version(self, payload: Mapping[str, Any], *, capability: object) -> bool:
        """Commit the canonical production move and its evidence as one durable event."""
        if capability is not _PRODUCTION_CAPABILITY:
            raise RegistryError("registry finalization requires finalize authority")
        identity = (payload.get("model_id"), payload.get("version"))
        prior = [event for event in self.events.read()
                 if event.event_type == "registry_finalized"
                 and (event.payload.get("model_id"), event.payload.get("version")) == identity]
        if prior:
            if prior[-1].payload != payload:
                raise RegistryError("registry finalization retry drifted from its full semantic payload")
            self.recover()
            return False
        self._write("registry_finalized", payload)
        return True

    def record_compatibility(self, *, model_id: str, version: int, head_sha: str,
                             passed: bool, reason: str) -> None:
        if not isinstance(passed, bool) or not reason:
            raise RegistryError("compatibility result requires boolean passed and a reason")
        if not any(row["model_id"] == model_id and row["version"] == version
                   for row in self.rows("model_versions")):
            raise RegistryError("compatibility result references an unknown model version")
        effective = self.effective_model_version(model_id, version)
        run = next(row for row in self.rows("runs") if row["run_id"] == effective["run_id"])
        code_ref = json.loads(run["code_ref"])
        if "repo" not in code_ref:
            raise RegistryError("legacy unknown code provenance cannot receive a compatibility result")
        current_head = self._git_head(code_ref["repo"])
        if head_sha != current_head:
            raise RegistryError("compatibility result must name the declared repo's current HEAD")
        self._write("compatibility_recorded", {"model_id": model_id, "version": version,
                    "head_sha": head_sha, "passed": passed, "reason": reason, "at": self.clock()})

    @staticmethod
    def _git_head(repo: str) -> str:
        return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()

    def effective_model_version(self, model_id: str, version: int) -> dict[str, Any]:
        matches = [row for row in self.rows("model_versions")
                   if row["model_id"] == model_id and row["version"] == version]
        if not matches:
            raise RegistryError("unknown model version")
        result = dict(matches[0])
        compat = json.loads(result["compat_result"])
        invalidated = False
        for event in self.events.read():
            if (event.event_type == "compatibility_recorded" and event.payload["model_id"] == model_id
                    and event.payload["version"] == version):
                compat = {key: event.payload[key] for key in ("head_sha", "passed", "at")}
            elif (event.event_type == "registry_finalized" and event.payload["model_id"] == model_id
                  and event.payload["version"] == version):
                compat = {"head_sha": event.payload["head_sha"], "passed": True, "at": event.at}
            elif (event.event_type == "adoption_invalidated"
                  and event.payload.get("model_id") == model_id
                  and event.payload.get("invalidated_version") == version):
                invalidated = True
        result["effective_compat_result"] = compat
        result["effective_status"] = (
            "superseded" if invalidated else result["status"] if compat["passed"] else "incompatible"
        )
        return result

    def rows(self, table: str) -> list[dict[str, Any]]:
        allowed = {"experiments", "runs", "artifacts", "registered_models", "model_versions",
                   "lineage", "aliases", "events"}
        if table not in allowed:
            raise RegistryError(f"unknown table {table!r}")
        with self._connect() as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]

    def list_runs(self, *, experiment_id: str | None = None) -> tuple[dict[str, Any], ...]:
        rows = self.rows("runs")
        if experiment_id is not None:
            rows = [row for row in rows if row["experiment_id"] == experiment_id]
        return tuple(sorted(rows, key=lambda row: (row["started_at"], row["run_id"])))

    def list_events(self) -> tuple[RegistryEvent, ...]:
        return self.events.read()

    def table_names(self) -> tuple[str, ...]:
        with self._connect() as db:
            return tuple(row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ))

    @staticmethod
    def alias_authorities() -> Mapping[str, frozenset[str]]:
        return {"champion": frozenset({"adjudicate", "ratchet"}),
                "production": frozenset({"finalize"})}

    def snapshot_digest(self) -> str:
        payload = {table: self.rows(table) for table in self.table_names()}
        return hashlib.sha256(_json(payload).encode()).hexdigest()

    def canonical_projection_digest(self) -> str:
        tables = ("experiments", "runs", "artifacts", "registered_models", "model_versions", "lineage", "aliases")
        return hashlib.sha256(_json({table: self.rows(table) for table in tables}).encode()).hexdigest()

    def verify_event_chain(self) -> bool:
        self.events.read()
        return True

    def projection_is_empty(self) -> bool:
        return not any(self.rows(table) for table in self.table_names())

    def is_empty(self) -> bool:
        """Return whether the registry has no projection, events, or CAS bytes."""
        return (self.projection_is_empty() and not self.events.read()
                and not any(path.is_file() for path in self.blobs.root.rglob("*")))

    def model_versions(self) -> list[dict[str, Any]]:
        return self.rows("model_versions")

    def aliases(self) -> list[dict[str, Any]]:
        return self.rows("aliases")

    def verdicts(self) -> list[dict[str, Any]]:
        return [row for row in self.rows("runs") if row["verdict"] is not None]
