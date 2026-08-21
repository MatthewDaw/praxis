from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path
import sqlite3
import subprocess
import time
from typing import Any

from knowledge.ml_registry.contracts import CodeRef
from knowledge.ml_registry.file_lock import exclusive_file_lock

from .blobs import BlobStore
from .events import EventLog, RegistryEvent


class RegistryError(ValueError):
    pass


DDL = """
PRAGMA foreign_keys=ON;
PRAGMA user_version=1;
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
 claim_owner TEXT NOT NULL, heartbeat_at REAL NOT NULL);
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
 sequence INTEGER PRIMARY KEY, event_type TEXT NOT NULL, payload TEXT NOT NULL, at REAL NOT NULL,
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
"""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


_CHAMPION_CAPABILITY = object()
_PRODUCTION_CAPABILITY = object()


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
            db.executescript(DDL)
        self.recover()

    @classmethod
    def open(cls, root: str | Path, **kwargs: Any) -> "Registry":
        return cls(root, **kwargs)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def recover(self) -> None:
        with exclusive_file_lock(self.lock_path), self._connect() as db:
            projected = {row[0] for row in db.execute("SELECT sequence FROM events")}
            for event in self.events.read():
                if event.sequence not in projected:
                    self._project(db, event)
                    self._record_event(db, event)

    def _write(self, event_type: str, payload: Mapping[str, Any]) -> None:
        with exclusive_file_lock(self.lock_path):
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    at = self.clock()
                    pending = RegistryEvent(len(self.events.read()) + 1, event_type, dict(payload), at, None, "")
                    # Apply invisibly first: refused constraints must never poison durable history.
                    self._project(db, pending)
                    event = self.events.append(event_type, payload, at=at)
                    if self.after_event is not None:
                        self.after_event(event)
                    self._record_event(db, event)
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise

    def _record_event(self, db: sqlite3.Connection, event: RegistryEvent) -> None:
        db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (event.sequence, event.event_type,
                   _json(event.payload), event.at, event.previous_hash, event.event_hash))

    def _project(self, db: sqlite3.Connection, event: RegistryEvent) -> None:
        p = event.payload
        op = event.event_type
        if op == "experiment_created":
            db.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?)", (p["experiment_id"], p["spec_digest"],
                       _json(p["stages"]), p["metric"], p["direction"], _json(p["win_condition"]),
                       p["noise_floor"], p["baseline_throughput"]))
        elif op == "run_created":
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (p["run_id"],
                       p["experiment_id"], p["idea_id"], p["stage"], p["family"], _json(p["params"]),
                       _json(p["metrics"]), _json(p["code_ref"]), p["device_fingerprint"], p["status"],
                       p.get("verdict"), p["started_at"], p.get("finished_at"), p["claim_owner"],
                       p["heartbeat_at"]))
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
        else:
            raise RegistryError(f"unknown event type {op!r}")

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
        payload = dict(values)
        payload["code_ref"] = code_ref.to_mapping()
        payload.setdefault("status", "complete")
        payload.setdefault("verdict", None)
        payload.setdefault("finished_at", payload.get("started_at"))
        payload.setdefault("heartbeat_at", payload.get("started_at"))
        self._write("run_created", payload)

    def create_artifact(self, *, run_id: str, kind: str, content: bytes, schema_version: str) -> str:
        digest, path = self.blobs.put(content)
        self._write("artifact_created", {"artifact_id": digest, "run_id": run_id, "kind": kind,
                    "uri": str(path), "bytes": len(content), "schema_version": schema_version})
        return digest

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
        if json.loads(run["code_ref"])["sha"] != code_sha:
            raise RegistryError("model version code_sha differs from its run code_ref")
        if json.loads(run["params"]).get("convergence") is not True:
            raise RegistryError("model versions may be created only by convergence runs")
        compat = values.get("compat_result")
        if not isinstance(compat, Mapping) or set(compat) != {"head_sha", "passed", "at"}:
            raise RegistryError("compat_result requires exactly head_sha, passed, and at")
        self._write("model_version_created", values)

    def create_lineage(self, **values: Any) -> None:
        self._write("lineage_created", values)

    def set_alias(self, **_values: Any) -> None:
        raise RegistryError("aliases are service-owned; use adjudication/ratchet or finalize")

    def _set_alias(self, *, model_id: str, alias: str, version: int, set_by: str, reason: str,
                   capability: object) -> None:
        if alias == "production" and capability is not _PRODUCTION_CAPABILITY:
            raise RegistryError("production alias requires finalize authority")
        if alias == "champion" and capability is not _CHAMPION_CAPABILITY:
            raise RegistryError("champion alias requires adjudication authority")
        if alias == "production":
            effective = self.effective_model_version(model_id, version)
            compat = effective["effective_compat_result"]
            if compat.get("passed") is not True or effective["effective_status"] != "active":
                raise RegistryError("production alias requires an active, compatibility-passing version")
        self._write("alias_set", {"model_id": model_id, "alias": alias, "version": version,
                    "set_by": set_by, "reason": reason, "at": self.clock()})

    def record_compatibility(self, *, model_id: str, version: int, head_sha: str,
                             passed: bool, reason: str) -> None:
        if not isinstance(passed, bool) or not reason:
            raise RegistryError("compatibility result requires boolean passed and a reason")
        if not any(row["model_id"] == model_id and row["version"] == version
                   for row in self.rows("model_versions")):
            raise RegistryError("compatibility result references an unknown model version")
        self._write("compatibility_recorded", {"model_id": model_id, "version": version,
                    "head_sha": head_sha, "passed": passed, "reason": reason, "at": self.clock()})

    def effective_model_version(self, model_id: str, version: int) -> dict[str, Any]:
        matches = [row for row in self.rows("model_versions")
                   if row["model_id"] == model_id and row["version"] == version]
        if not matches:
            raise RegistryError("unknown model version")
        result = dict(matches[0])
        compat = json.loads(result["compat_result"])
        for event in self.events.read():
            if (event.event_type == "compatibility_recorded" and event.payload["model_id"] == model_id
                    and event.payload["version"] == version):
                compat = {key: event.payload[key] for key in ("head_sha", "passed", "at")}
        result["effective_compat_result"] = compat
        result["effective_status"] = result["status"] if compat["passed"] else "incompatible"
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
