from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
from .events import EventLog, EventLogError, RegistryEvent


class RegistryError(ValueError):
    pass


def _judged_entries(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every judged metric object of a spec, whichever judge spelling declared it."""
    metric = spec.get("metric")
    if isinstance(metric, Mapping):
        return [dict(metric)]
    raw = spec.get("metrics")
    if (not isinstance(raw, Sequence) or isinstance(raw, (str, bytes))
            or not all(isinstance(item, Mapping) for item in raw)):
        raise RegistryError("registered CampaignSpec declares no readable judged metric")
    return [dict(item) for item in raw]


def _metric_name(entry: Mapping[str, Any], index: int) -> str:
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RegistryError(f"judged metric {index} must carry a non-empty name")
    return name.strip()


DDL = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS experiments(
 experiment_id TEXT PRIMARY KEY, spec_digest TEXT NOT NULL, stages TEXT NOT NULL, metric TEXT NOT NULL,
 direction TEXT NOT NULL CHECK(direction IN ('maximize','minimize')), win_condition TEXT NOT NULL,
 rope REAL NOT NULL CHECK(rope>=0), baseline_throughput REAL NOT NULL CHECK(baseline_throughput>=0));
CREATE TABLE IF NOT EXISTS runs(
 run_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id), idea_id TEXT NOT NULL,
 stage TEXT NOT NULL, family TEXT NOT NULL, params TEXT NOT NULL, metrics TEXT NOT NULL, code_ref TEXT NOT NULL,
 device_fingerprint TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN
 ('running','complete','succeeded','failed','voided','superseded')), verdict TEXT CHECK(verdict IS NULL OR verdict IN
 ('adopted','rejected','parked','voided','abandoned','baseline')), started_at REAL NOT NULL, finished_at REAL,
 claim_owner TEXT NOT NULL, heartbeat_at REAL NOT NULL, CHECK(COALESCE(
 (status IN ('running','complete','failed','superseded') AND verdict IS NULL) OR
 (status='succeeded' AND verdict IN ('adopted','rejected','parked','abandoned','baseline')) OR
 (status='voided' AND verdict='voided'),0)));
CREATE TABLE IF NOT EXISTS artifacts(
 artifact_id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(run_id), kind TEXT NOT NULL CHECK(kind IN
 ('checkpoint','oof_predictions','split_manifest','dataset_manifest','report')), uri TEXT NOT NULL,
 bytes INTEGER NOT NULL CHECK(bytes>=0), schema_version TEXT NOT NULL,
 PRIMARY KEY(run_id,artifact_id));
CREATE TABLE IF NOT EXISTS registered_models(
 model_id TEXT PRIMARY KEY, family TEXT NOT NULL, sport_scope TEXT NOT NULL, axis TEXT NOT NULL,
 protocol TEXT NOT NULL, extends_json TEXT);
CREATE TABLE IF NOT EXISTS model_versions(
 model_id TEXT NOT NULL REFERENCES registered_models(model_id), version INTEGER NOT NULL CHECK(version>=1),
 run_id TEXT NOT NULL REFERENCES runs(run_id), artifact_id TEXT NOT NULL,
 checksum TEXT NOT NULL, family_version TEXT NOT NULL, code_sha TEXT NOT NULL,
 preprocessing_hash TEXT NOT NULL, calibration TEXT NOT NULL, thresholds TEXT NOT NULL,
 compat_result TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','incompatible','superseded')),
 PRIMARY KEY(model_id,version),
 FOREIGN KEY(run_id,artifact_id) REFERENCES artifacts(run_id,artifact_id));
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
CREATE TRIGGER IF NOT EXISTS guard_experiments_update BEFORE UPDATE ON experiments
 WHEN registry_authority() NOT IN ('experiment_amended') BEGIN SELECT RAISE(ABORT,'experiments are immutable'); END;
CREATE TRIGGER IF NOT EXISTS guard_experiments_delete BEFORE DELETE ON experiments BEGIN SELECT RAISE(ABORT,'experiments are immutable'); END;
CREATE TRIGGER IF NOT EXISTS guard_runs_insert BEFORE INSERT ON runs
 WHEN registry_authority() NOT IN ('run_created','historical_ledger_imported','historical_archive_imported') BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_runs_update BEFORE UPDATE ON runs
 WHEN registry_authority() NOT IN ('run_completed','run_adjudicated','run_adopted','run_superseded','adoption_invalidated','run_abandoned','run_baselined','adoption_reclassified_as_baseline') BEGIN SELECT RAISE(ABORT,'run write authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_runs_delete BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT,'runs cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS valid_run_pair_insert BEFORE INSERT ON runs WHEN NOT COALESCE(
 (NEW.status IN ('running','complete','failed','superseded') AND NEW.verdict IS NULL) OR
 (NEW.status='succeeded' AND NEW.verdict IN ('adopted','rejected','parked','abandoned','baseline')) OR
 (NEW.status='voided' AND NEW.verdict='voided'),0)
 BEGIN SELECT RAISE(ABORT,'invalid run status/verdict pair'); END;
CREATE TRIGGER IF NOT EXISTS valid_run_pair_update BEFORE UPDATE ON runs WHEN NOT COALESCE(
 (NEW.status IN ('running','complete','failed','superseded') AND NEW.verdict IS NULL) OR
 (NEW.status='succeeded' AND NEW.verdict IN ('adopted','rejected','parked','abandoned','baseline')) OR
 (NEW.status='voided' AND NEW.verdict='voided'),0)
 BEGIN SELECT RAISE(ABORT,'invalid run status/verdict pair'); END;
CREATE TRIGGER IF NOT EXISTS guard_artifacts_insert BEFORE INSERT ON artifacts
 WHEN registry_authority()!='artifact_created' BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_models_insert BEFORE INSERT ON registered_models
 WHEN registry_authority() NOT IN ('registered_model_created','historical_archive_imported') BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_models_update BEFORE UPDATE ON registered_models BEGIN SELECT RAISE(ABORT,'registered_models are immutable'); END;
CREATE TRIGGER IF NOT EXISTS guard_models_delete BEFORE DELETE ON registered_models BEGIN SELECT RAISE(ABORT,'registered_models are immutable'); END;
CREATE TRIGGER IF NOT EXISTS guard_versions_insert BEFORE INSERT ON model_versions
 WHEN registry_authority() NOT IN ('model_version_created','run_adopted','run_baselined') BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_lineage_insert BEFORE INSERT ON lineage
 WHEN registry_authority() NOT IN ('lineage_created','run_adopted','run_baselined') BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_lineage_update BEFORE UPDATE ON lineage BEGIN SELECT RAISE(ABORT,'lineage is immutable'); END;
CREATE TRIGGER IF NOT EXISTS guard_lineage_delete BEFORE DELETE ON lineage BEGIN SELECT RAISE(ABORT,'lineage is immutable'); END;
CREATE TRIGGER IF NOT EXISTS guard_aliases_insert BEFORE INSERT ON aliases
 WHEN registry_authority() NOT IN ('alias_set','run_adopted','run_baselined','registry_finalized','promotion_rolled_back','unpromotion_rolled_back') BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_aliases_update BEFORE UPDATE ON aliases
 WHEN registry_authority() NOT IN ('alias_set','run_adopted','run_baselined','adoption_invalidated','registry_finalized','promotion_rolled_back','unpromotion_rolled_back') BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_aliases_delete BEFORE DELETE ON aliases
 WHEN registry_authority()!='promotion_rolled_back' BEGIN SELECT RAISE(ABORT,'aliases cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS guard_events_insert BEFORE INSERT ON events
 WHEN registry_authority()='' BEGIN SELECT RAISE(ABORT,'registry projection authority required'); END;
CREATE TRIGGER IF NOT EXISTS guard_events_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS guard_events_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events are immutable'); END;
"""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _as_mapping(value: object) -> dict[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, Mapping):
        raise RegistryError("win_condition must be a mapping")
    return dict(parsed)


def _is_numeric_floor(key: str, previous: object, current: object) -> bool:
    """Whether ``key`` is a numeric floor that may rise but must not fall.

    Keys carrying ``at_least`` or starting ``minimum`` are floors by name. Anything else
    has no direction we can defend, so a rewrite is refused rather than guessed at.
    """
    if isinstance(previous, bool) or isinstance(current, bool):
        return False
    if not isinstance(previous, (int, float)) or not isinstance(current, (int, float)):
        return False
    return "at_least" in key or key.startswith("minimum")


def _require_win_condition_tightening(old: Mapping[str, Any], new: Mapping[str, Any]) -> None:
    """Refuse a loosening. Additional constraints and raised floors are the only legal edits."""
    missing = sorted(set(old) - set(new))
    if missing:
        raise RegistryError(
            f"amending win_condition cannot drop constraints {missing}"
        )
    raised = False
    for key, previous in old.items():
        current = new[key]
        if _is_numeric_floor(key, previous, current):
            if float(current) < float(previous):
                raise RegistryError(
                    f"amending win_condition cannot loosen {key}: {current} < {previous}"
                )
            if float(current) > float(previous):
                raised = True
        elif current != previous:
            raise RegistryError(
                f"amending win_condition cannot rewrite {key!r} "
                f"({previous!r} -> {current!r}); only numeric floors may rise"
            )
    added = [key for key in new if key not in old]
    if not added and not raised:
        raise RegistryError(
            "amending win_condition requires a tightening: add a constraint or raise a floor"
        )


_CHAMPION_CAPABILITY = object()
_PRODUCTION_CAPABILITY = object()
_TRAINER_CAPABILITY = object()
_ADJUDICATOR_CAPABILITY = object()


class Registry:
    """Single-writer registry: durable event first, recoverable SQLite projection second."""

    def __init__(self, root: str | Path, *, clock: Callable[[], float] = time.time,
                 after_event: Callable[[RegistryEvent], None] | None = None,
                 auto_recover: bool = True) -> None:
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
        # `auto_recover=False` opens the projection WITHOUT replaying, so a caller can
        # inspect whether it is current before deciding to overwrite it -- see
        # `replay_projection`. Every ordinary caller wants the default self-healing open.
        if auto_recover:
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
        expected = expected_projection(events)
        try:
            return projections_agree(db, expected)
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
                self._replay_one(db, event)
                self._record_event(db, event)

    @classmethod
    def _replay_one(cls, db: sqlite3.Connection, event: RegistryEvent) -> None:
        """Project one logged event, naming its event-log line if it cannot be projected.

        An event the projection does not understand is REFUSED here rather than skipped:
        a replay that drops what it cannot read produces a view that looks complete and
        is not. `sequence` is the event's 1-based line in `events.jsonl`.
        """
        try:
            cls._project(db, event)
        except RegistryError as exc:
            raise RegistryError(f"{exc} at events.jsonl line {event.sequence}") from exc

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

    @staticmethod
    def _record_event(db: sqlite3.Connection, event: RegistryEvent) -> None:
        db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)", (event.sequence, event.schema_version,
                   event.event_type, _json(event.payload), event.at, event.previous_hash, event.event_hash))

    @classmethod
    def _project(cls, db: sqlite3.Connection, event: RegistryEvent) -> bool:
        p = event.payload
        op = event.event_type
        if op == "experiment_created":
            db.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?)", (p["experiment_id"], p["spec_digest"],
                       _json(p["stages"]), p["metric"], p["direction"], _json(p["win_condition"]),
                       p["rope"], p["baseline_throughput"]))
        elif op == "experiment_amended":
            row = db.execute(
                "SELECT * FROM experiments WHERE experiment_id=?", (p["experiment_id"],),
            ).fetchone()
            if row is None:
                raise RegistryError("unknown experiment")
            new = p["new"]
            spec_digest = new["spec_digest"] if "spec_digest" in new else row["spec_digest"]
            win_condition = (
                _json(new["win_condition"]) if "win_condition" in new else row["win_condition"]
            )
            db.execute(
                "UPDATE experiments SET spec_digest=?,win_condition=? WHERE experiment_id=?",
                (spec_digest, win_condition, p["experiment_id"]),
            )
        elif op == "run_created":
            cls._insert_run(db, p)
        elif op == "run_adjudicated":
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (p["run_id"],)).fetchone()
            if row is None:
                raise RegistryError("unknown run")
            if row["status"] == p["status"] and row["verdict"] == p["verdict"]:
                return False
            corrects_invalidated = (
                p.get("corrects_invalidated_adoption") is True
                and row["status"] == "superseded"
                and row["verdict"] is None
            )
            if not corrects_invalidated and (
                row["status"] != "complete" or row["verdict"] is not None
            ):
                raise RegistryError("adjudication requires one complete, unadjudicated run")
            db.execute("UPDATE runs SET status=?,verdict=?,finished_at=?,heartbeat_at=? WHERE run_id=?",
                       (p["status"], p["verdict"], p["at"], p["at"], p["run_id"]))
        elif op == "run_abandoned":
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (p["run_id"],)).fetchone()
            if row is None:
                raise RegistryError("unknown run")
            if row["status"] == "succeeded" and row["verdict"] == "abandoned":
                return False
            abandonable = (
                row["status"] == "succeeded" and row["verdict"] in {"rejected", "parked"}
            ) or (
                row["status"] == "superseded" and row["verdict"] is None
            )
            if not abandonable:
                raise RegistryError(
                    "abandonment reclassifies a rejected, parked, or superseded run whose "
                    "hypothesis was "
                    f"never fairly tested; got status={row['status']!r} verdict={row['verdict']!r}"
                )
            db.execute(
                "UPDATE runs SET status='succeeded',verdict='abandoned',heartbeat_at=? WHERE run_id=?",
                (p["at"], p["run_id"]),
            )
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
        elif op == "run_baselined":
            # Constitution X.3: changing the vector is a re-freeze and a re-baseline.
            # Recording that measurement as an adoption is a ledger lie -- nothing won.
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (p["run_id"],)).fetchone()
            if row is None or row["status"] != "complete" or row["verdict"] is not None:
                raise RegistryError("baseline registration requires one complete, unadjudicated run")
            version = p["model_version"]
            db.execute("UPDATE runs SET status='succeeded',verdict='baseline',finished_at=?,heartbeat_at=? "
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
        elif op == "adoption_reclassified_as_baseline":
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (p["run_id"],)).fetchone()
            if row is None:
                raise RegistryError("unknown run")
            if row["status"] == "succeeded" and row["verdict"] == "baseline":
                return False
            if row["status"] != "succeeded" or row["verdict"] != "adopted":
                raise RegistryError(
                    "reclassification as baseline withdraws an improvement verdict from a "
                    f"re-baseline that was filed as an adoption; got status={row['status']!r} "
                    f"verdict={row['verdict']!r}"
                )
            db.execute(
                "UPDATE runs SET verdict='baseline',heartbeat_at=? WHERE run_id=?",
                (p["at"], p["run_id"]),
            )
        elif op == "ratchet_evidence_recorded":
            # Evidence is canonical in the append-only event stream.  Keeping it out of
            # mutable model metadata preserves the eight-table registry contract.
            return
        elif op == "trial_refused":
            return
        elif op in {
            "campaign_spec_registered",
            "campaign_vector_amended",
            "campaign_registration_refused",
            "campaign_outcome_recorded",
            "campaign_landed",
            "campaign_unpromoted",
        }:
            # CampaignSpec is a versioned control-plane contract, not a ninth
            # model-registry entity. Runner refusals and outcomes live beside it
            # in the event log, leaving the eight-table projection unchanged.
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
            corrects_abandonment = (
                row["status"] == "succeeded" and row["verdict"] == "abandoned"
            )
            if row["status"] not in {"running", "complete"} and not corrects_abandonment:
                raise RegistryError(
                    "only a non-terminal or previously abandoned run can be superseded"
                )
            db.execute("UPDATE runs SET status='superseded',verdict=NULL,finished_at=?,heartbeat_at=? WHERE run_id=?",
                       (p["at"], p["at"], p["run_id"]))
        elif op == "historical_ledger_imported":
            experiment = p["experiment"]
            db.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?)", (
                experiment["experiment_id"], experiment["spec_digest"], _json(experiment["stages"]),
                experiment["metric"], experiment["direction"], _json(experiment["win_condition"]),
                experiment["rope"], experiment["baseline_throughput"],
            ))
            for run in p["runs"]:
                LegacyCodeRef.from_mapping(run["code_ref"])
                cls._insert_run(db, run)
        elif op == "historical_archive_imported":
            experiment = p["experiment"]
            db.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?)", (
                experiment["experiment_id"], experiment["spec_digest"], _json(experiment["stages"]),
                experiment["metric"], experiment["direction"], _json(experiment["win_condition"]),
                experiment["rope"], experiment["baseline_throughput"],
            ))
            for run in p["runs"]:
                LegacyCodeRef.from_mapping(run["code_ref"])
                cls._insert_run(db, run)
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
        elif op == "campaign_restarted":
            # Constitution IV: a champion converging below 0.60 is not in a weakness loop, its
            # FRAMING is wrong, and the ledger "records RESTARTED with the reason, never a
            # rejection". A restart re-poses the search: it moves no alias, rewrites no Run and
            # never lowers the bar, so it deliberately has no SQL projection and the eight-table
            # view is unchanged. Before this existed there was no seam for it at all, and three
            # campaigns were told each sweep to record something they had no way to write --
            # the only alternatives on offer corrupted evidence, since abandoning a fairly
            # judged run to carry a campaign-level fact rewrites a verdict the judge did reach.
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
        elif op == "promotion_rolled_back":
            current = {row["alias"]: row for row in db.execute(
                "SELECT * FROM aliases WHERE model_id=? AND alias IN ('champion','production')",
                (p["model_id"],),
            )}
            if any(row["version"] != p["failed_version"] for row in current.values()):
                raise RegistryError("failed landing rollback found aliases moved by another writer")
            cls._restore_aliases(db, p["model_id"], p["aliases"])
        elif op == "unpromotion_rolled_back":
            cls._restore_aliases(db, p["model_id"], p["aliases"])
        else:
            raise RegistryError(f"unknown event type {op!r}")
        return True

    @staticmethod
    def _restore_aliases(
        db: sqlite3.Connection,
        model_id: str,
        aliases: Mapping[str, Mapping[str, Any]],
    ) -> None:
        """Restore one model's complete champion/production alias pair."""
        unknown = set(aliases) - {"champion", "production"}
        if unknown:
            raise RegistryError(f"cannot restore unknown aliases {sorted(unknown)!r}")
        for alias in ("champion", "production"):
            row = aliases.get(alias)
            if row is None:
                db.execute("DELETE FROM aliases WHERE model_id=? AND alias=?", (model_id, alias))
            else:
                db.execute(
                    "INSERT INTO aliases VALUES(?,?,?,?,?,?) ON CONFLICT(model_id,alias) DO UPDATE SET "
                    "version=excluded.version,set_by=excluded.set_by,reason=excluded.reason,at=excluded.at",
                    tuple(row[key] for key in
                          ("model_id", "alias", "version", "set_by", "reason", "at")),
                )

    @staticmethod
    def _insert_run(db: sqlite3.Connection, p: Mapping[str, Any]) -> None:
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (p["run_id"],
                   p["experiment_id"], p["idea_id"], p["stage"], p["family"], _json(p["params"]),
                   _json(p["metrics"]), _json(p["code_ref"]), p["device_fingerprint"], p["status"],
                   p.get("verdict"), p["started_at"], p.get("finished_at"), p["claim_owner"],
                   p["heartbeat_at"]))

    def create_experiment(self, **values: Any) -> None:
        self._write("experiment_created", values)

    def amend_experiment(self, experiment_id: str, *, reason: str, **fields: Any) -> None:
        """Append-only tightening of a registered experiment's declared win condition.

        The experiments row is otherwise immutable: metric, direction, stages, rope and
        baseline_throughput cannot move, and a win condition can only get *stricter*. The
        amendment is itself evidence -- old value, new value, and the reason -- the same
        way an adjudication is. A silent overwrite of the bar a campaign is judged by
        would be worse than a bar that could not move.
        """
        if not str(reason).strip():
            raise RegistryError("experiment amendment requires a reason")
        allowed = {"win_condition", "spec_digest"}
        unknown = set(fields) - allowed
        if unknown:
            raise RegistryError(
                f"experiment amendment cannot rewrite {sorted(unknown)}; "
                f"only {sorted(allowed)} may change, and win_condition only by tightening"
            )
        if "win_condition" not in fields:
            raise RegistryError("experiment amendment requires a win_condition tightening")
        existing = next(
            (row for row in self.rows("experiments") if row["experiment_id"] == experiment_id),
            None,
        )
        if existing is None:
            raise RegistryError(f"unknown experiment {experiment_id!r}")
        old_win = _as_mapping(existing["win_condition"])
        new_win = dict(fields["win_condition"])
        _require_win_condition_tightening(old_win, new_win)
        new_fields: dict[str, Any] = {"win_condition": new_win}
        old_fields: dict[str, Any] = {"win_condition": old_win}
        if "spec_digest" in fields and fields["spec_digest"] != existing["spec_digest"]:
            old_fields["spec_digest"] = existing["spec_digest"]
            new_fields["spec_digest"] = fields["spec_digest"]
        self._write("experiment_amended", {
            "experiment_id": experiment_id,
            "reason": str(reason).strip(),
            "old": old_fields,
            "new": new_fields,
        })

    def amend_judged_vector(
        self,
        campaign_id: str,
        *,
        metrics: Sequence[Mapping[str, Any]],
        reason: str,
        scoring_corpora: Mapping[str, Sequence[Mapping[str, object]]],
    ) -> bool:
        """Change the DIMENSION of a campaign's judged vector: add a metric, or demote one.

        The right objectives are rarely knowable when a campaign freezes its judge, and a
        campaign stuck optimising the wrong set is worse off than one that re-freezes. The
        sibling of :meth:`amend_experiment` -- append-only, reason-bearing, refusing by name
        -- for the judge rather than the win condition, because the judged vector lives in
        the registered CampaignSpec and not in the immutable ``experiments`` row.

        Four things make it honest, and all four are enforced here or by the readers this
        writes for:

        1. The WHOLE new vector is revalidated exactly as registration validates it --
           per-entry floor, direction and paired method, ``legacy_scalar_rope`` still refused
           under a vector -- and the rope is recomputed for every judged metric, the newly
           added one included, by the same ``compute_campaign_rope``. An amendment cannot
           smuggle in a declaration registration would have refused.
        2. A written ``reason`` is required and recorded. An empty one is refused by name.
        3. A removed metric is DEMOTED to a diagnostic, never deleted: it is carried in
           ``diagnostic_metrics`` so runs keep reporting it and it keeps appearing in
           evidence. It stops adjudicating; it does not stop being measured.
        4. A metric that is CURRENTLY DECIDING cannot be removed -- if the most recent
           adjudication rejected an arm on it, removing it is removing the referee that just
           ruled against you, and it is refused naming the metric and quoting the run.

        Adjudication then refuses to pair a run judged under the amended vector against a
        champion measured under the old one until the campaign re-baselines; see
        :func:`~knowledge.ml_registry.services.paired_adjudication.guard_vector_rebaseline`.
        """
        from knowledge.ml_registry.contracts import CampaignSpec, ContractError
        from knowledge.ml_registry.policy_gate import compute_campaign_rope
        from knowledge.ml_registry.services.paired_adjudication import (
            VECTOR_AMENDED,
            campaign_diagnostic_metrics,
            effective_campaign_spec,
            guard_adoption_floor,
            guard_vector_judge,
        )

        if not str(reason).strip():
            raise RegistryError("judged vector amendment requires a reason")
        if (not isinstance(metrics, Sequence) or isinstance(metrics, (str, bytes))
                or not metrics or not all(isinstance(item, Mapping) for item in metrics)):
            raise RegistryError(
                "judged vector amendment requires the whole new vector as a non-empty list "
                "of judged metric objects"
            )
        spec = effective_campaign_spec(self, campaign_id)
        if spec is None:
            raise RegistryError(f"unknown campaign spec {campaign_id!r}")

        old_entries = tuple(_judged_entries(spec))
        new_entries = tuple(dict(item) for item in metrics)
        old_names = [_metric_name(entry, index) for index, entry in enumerate(old_entries)]
        new_names = [_metric_name(entry, index) for index, entry in enumerate(new_entries)]
        added = [name for name in new_names if name not in old_names]
        removed = [name for name in old_names if name not in new_names]
        if not added and not removed:
            raise RegistryError(
                f"judged vector amendment requires an added or removed judged metric; "
                f"the vector is already {new_names}"
            )
        self._refuse_removing_a_deciding_metric(campaign_id, removed)

        candidate = dict(spec)
        candidate.pop("metric", None)
        candidate.pop("metrics", None)
        candidate.pop("rope", None)
        candidate["metrics"] = [dict(entry) for entry in new_entries]
        try:
            amended = CampaignSpec.from_mapping(candidate)
            # The SAME guards registration runs, in the same order: a single-entry vector
            # normalises back to the scalar judge and is validated as one.
            if amended.metric is not None:
                guard_adoption_floor(amended.metric)
            else:
                guard_vector_judge(amended.metrics)
        except ContractError as exc:
            raise RegistryError(f"judged vector amendment is not a valid campaign spec: {exc}") from exc
        canonical = amended.to_mapping()
        try:
            canonical["rope"] = compute_campaign_rope(amended, scoring_corpora)
        except ContractError as exc:
            raise RegistryError(f"judged vector amendment cannot recompute its rope: {exc}") from exc

        by_name = {name: entry for name, entry in zip(old_names, old_entries)}
        diagnostics = [dict(item) for item in campaign_diagnostic_metrics(self, campaign_id)]
        known = {str(item.get("name")) for item in diagnostics}
        for name in removed:
            if name not in known:
                diagnostics.append(dict(by_name[name]))
        # A metric promoted back INTO the vector is no longer a diagnostic; it adjudicates.
        diagnostics = [item for item in diagnostics if str(item.get("name")) not in set(new_names)]
        diagnostic_names = [str(item.get("name")) for item in diagnostics]

        self._write(VECTOR_AMENDED, {
            "campaign_id": campaign_id,
            "reason": str(reason).strip(),
            "added": added,
            "removed": removed,
            "old": {"judged_metrics": old_names,
                    "diagnostic_metrics": [str(item.get("name")) for item in
                                           campaign_diagnostic_metrics(self, campaign_id)]},
            "new": {"judged_metrics": new_names, "diagnostic_metrics": diagnostic_names},
            "diagnostic_metrics": diagnostics,
            "spec": canonical,
        })
        return True

    def _refuse_removing_a_deciding_metric(self, campaign_id: str, removed: Sequence[str]) -> None:
        """Refuse, by name, removing a metric the last adjudication rejected an arm on.

        This is the abuse the whole rule is built around: an arm regresses on an objective,
        and the objective disappears. The evidence the judge already recorded is what
        refuses it -- the deciding metrics of the most recent REJECTED adjudication -- so the
        refusal quotes the run and the regression rather than asserting a policy.
        """
        if not removed:
            return
        experiment_runs = {row["run_id"] for row in self.rows("runs")
                           if row["experiment_id"] == campaign_id}
        latest = next(
            (event for event in reversed(self.list_events())
             if event.event_type in {"run_adjudicated", "run_adopted"}
             and event.payload.get("run_id") in experiment_runs),
            None,
        )
        if latest is None or latest.payload.get("verdict") != "rejected":
            return
        evidence = latest.payload.get("adjudication_evidence")
        if not isinstance(evidence, Mapping):
            return
        deciding = evidence.get("deciding_metrics")
        deciding = list(deciding) if isinstance(deciding, Sequence) and not isinstance(
            deciding, (str, bytes)) else []
        per_metric = evidence.get("metrics")
        run_id = latest.payload.get("run_id")
        for name in removed:
            if name not in deciding:
                continue
            detail = ""
            if isinstance(per_metric, Mapping) and isinstance(per_metric.get(name), Mapping):
                item = per_metric[name]
                interval = item.get("interval")
                detail = f" (gain {item.get('gain')}"
                if isinstance(interval, Sequence) and not isinstance(interval, (str, bytes)):
                    detail += f", interval {list(interval)}"
                detail += ")"
            raise RegistryError(
                f"judged vector amendment cannot remove {name!r}: it is currently deciding -- "
                f"the most recent adjudication REJECTED run {run_id!r} on it{detail}. A metric "
                "that just ruled against an arm stays in the vector until an arm is judged "
                "without it deciding; removing the referee is not an amendment."
            )

    def create_run(self, **values: Any) -> None:
        code_ref = CodeRef.from_mapping(values["code_ref"])
        try:
            subprocess.run(
                ["git", "-C", code_ref.repo, "cat-file", "-e", f"{code_ref.sha}^{{commit}}"],
                check=True, capture_output=True, text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            # A refusal names its cause. This check destroyed a measured 13-minute run on
            # 2026-08-27 because code_ref.repo was the logical name "sports_analysis" rather
            # than a path, and `git -C sports_analysis` resolved against the caller's cwd.
            # The bare message said neither which repo nor which sha, so the caller could not
            # tell a bad path from a missing commit.
            detail = getattr(exc, "stderr", None) or str(exc)
            resolved = Path(code_ref.repo)
            reason = (
                f"declared repo {code_ref.repo!r} is not an existing directory "
                f"(resolved from cwd {Path.cwd()} to {resolved.absolute()}) -- code_ref.repo is "
                "a filesystem path to the git repo, not a repository name"
                if not resolved.is_dir() else
                f"repo {resolved.absolute()} has no such commit"
            )
            raise RegistryError(
                f"code_ref sha {code_ref.sha} does not exist as a commit in its declared repo: "
                f"{reason}; git said: {str(detail).strip()}"
            ) from exc
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
                        adjudication_evidence: Mapping[str, object] | None = None,
                        capability: object) -> None:
        if capability is not _ADJUDICATOR_CAPABILITY:
            raise RegistryError("run verdict requires adjudication authority")
        if verdict == "adopted":
            raise RegistryError("adoption requires the atomic model-version and champion promotion path")
        expected = {"rejected": "succeeded", "parked": "succeeded", "abandoned": "succeeded",
                    "voided": "voided"}
        if expected.get(verdict) != status or not reason:
            raise RegistryError("run verdict and terminal status are inconsistent")
        payload: dict[str, object] = {
            "run_id": run_id, "verdict": verdict, "status": status,
            "reason": reason, "at": self.clock(),
        }
        if adjudication_evidence is not None:
            payload["adjudication_evidence"] = dict(adjudication_evidence)
        self._write("run_adjudicated", payload)

    def _adjudicate_invalidated_adoption(
        self, *, run_id: str, verdict: str, reason: str,
        adjudication_evidence: Mapping[str, object] | None = None,
        capability: object,
    ) -> None:
        """File the corrected verdict after an invalid adoption is rolled back."""
        if capability is not _ADJUDICATOR_CAPABILITY:
            raise RegistryError("invalidated-adoption correction requires adjudication authority")
        if verdict not in {"rejected", "parked"} or not reason.strip():
            raise RegistryError("invalidated adoption correction requires rejected or parked")
        run = next((row for row in self.rows("runs") if row["run_id"] == run_id), None)
        invalidated = any(
            event.event_type == "adoption_invalidated"
            and event.payload.get("adoption_run_id") == run_id
            for event in self.events.read()
        )
        if (
            run is None
            or run["status"] != "superseded"
            or run["verdict"] is not None
            or not invalidated
        ):
            raise RegistryError(
                "correction requires a superseded adoption with an invalidation event"
            )
        payload: dict[str, object] = {
            "run_id": run_id,
            "verdict": verdict,
            "status": "succeeded",
            "reason": reason,
            "at": self.clock(),
            "corrects_invalidated_adoption": True,
        }
        if adjudication_evidence is not None:
            payload["adjudication_evidence"] = dict(adjudication_evidence)
        self._write("run_adjudicated", payload)

    def _adopt_run_and_promote(self, *, run_id: str, model_id: str, reason: str,
                               model_version: Mapping[str, Any],
                               adjudication_evidence: Mapping[str, object] | None = None,
                               capability: object) -> bool:
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
        if adjudication_evidence is not None:
            payload["adjudication_evidence"] = dict(adjudication_evidence)
        if prior:
            if prior[-1].payload != payload:
                raise RegistryError("atomic adoption retry drifted from its full semantic payload")
            self.recover()
            return False
        self._validate_adoption_payload(run_id=run_id, model_id=model_id, reason=reason,
                                        model_version=version)
        self._write("run_adopted", payload)
        return True

    def _register_baseline_and_promote(self, *, run_id: str, model_id: str, reason: str,
                                       model_version: Mapping[str, Any],
                                       capability: object) -> bool:
        """Promote a re-measured champion as the campaign baseline with no improvement verdict.

        Constitution X.3: a vector (or judge) change is a re-freeze and a re-baseline.
        The number is the new floor; recording it as ``adopted`` claims a win nobody earned.
        """
        if capability is not _ADJUDICATOR_CAPABILITY:
            raise RegistryError("baseline registration requires adjudication authority")
        version = dict(model_version)
        version["model_id"] = model_id
        version["run_id"] = run_id
        prior = [event for event in self.events.read()
                 if event.event_type == "run_baselined" and event.payload.get("run_id") == run_id]
        champion = next((row for row in self.rows("aliases")
                         if row["model_id"] == model_id and row["alias"] == "champion"), None)
        parent_version = (prior[-1].payload.get("parent_version") if prior
                          else champion["version"] if champion is not None else None)
        payload = {"run_id": run_id, "reason": reason, "model_version": version,
                   "parent_version": parent_version}
        if prior:
            if prior[-1].payload != payload:
                raise RegistryError("baseline registration retry drifted from its full semantic payload")
            self.recover()
            return False
        self._validate_adoption_payload(run_id=run_id, model_id=model_id, reason=reason,
                                        model_version=version)
        self._write("run_baselined", payload)
        return True

    def _reclassify_adoption_as_baseline(self, *, run_id: str, reason: str,
                                         capability: object) -> None:
        """Withdraw an improvement verdict from a re-baseline that was filed as an adoption.

        The measurement, artifact and champion alias stand. Only the verdict label changes:
        the run answered a new judge, it did not beat the old one. Does not roll the alias
        back -- that would destroy the baseline the campaign is already scoring against.
        """
        if capability is not _ADJUDICATOR_CAPABILITY:
            raise RegistryError("baseline reclassification requires adjudication authority")
        if not reason.strip():
            raise RegistryError("baseline reclassification requires a reason")
        self._write("adoption_reclassified_as_baseline", {
            "run_id": run_id, "reason": reason, "at": self.clock(),
        })

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
        artifact = next((row for row in self.rows("artifacts")
                         if row["artifact_id"] == artifact_id and row["run_id"] == run_id), None)
        if artifact is None:
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

    def record_campaign_restarted(self, *, campaign_id: str, reason: str) -> bool:
        """Record a constitution-IV hard restart in the event log, never as a rejection.

        The payload always carries the literal marker ``RESTARTED`` beside the campaign id, so an
        auditor reading ``events.payload`` sees both without decoding a private schema -- that is
        exactly what ``overnight/bin/campaign_health.py`` greps for before it stops re-issuing the
        hard-restart finding. Idempotent on the exact payload; a NEW reason is a new restart.

        Deliberately does not require a registered experiment: a campaign can be re-posed while
        it is still seeding, which is the case IV most often applies to.
        """
        if not str(campaign_id).strip() or not str(reason).strip():
            raise RegistryError("campaign restart requires an id and reason")
        payload = {"campaign_id": campaign_id.strip(), "experiment_id": campaign_id.strip(),
                   "marker": "RESTARTED", "reason": reason.strip()}
        for event in self.events.read():
            if event.event_type == "campaign_restarted" and event.payload == payload:
                return False
        self._write("campaign_restarted", payload)
        return True

    def _abandon_run(self, *, run_id: str, reason: str, capability: object) -> None:
        """Reclassify a rejected, parked, or superseded run as abandoned.

        Abandoned is not a verdict the judge reached: it is a decision taken because the
        hypothesis was never fairly tested (fitted on a superseded mute base, killed mid-fit,
        scored against a broken incumbent or slate). A rejection it did not reach must never be
        cited later as proof the approach fails. The prior verdict stays in the event log; the
        projection's verdict becomes ``abandoned`` so readers cannot treat it as a refutation.
        """
        if capability is not _ADJUDICATOR_CAPABILITY:
            raise RegistryError("abandonment requires adjudication authority")
        if not reason.strip():
            raise RegistryError("abandonment requires a reason")
        self._write("run_abandoned", {"run_id": run_id, "reason": reason, "at": self.clock()})

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

    def register_campaign_spec(
        self,
        spec: Mapping[str, Any],
        *,
        scoring_corpora: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
        structural_validator: Callable[[Mapping[str, Any]], object] | None = None,
    ) -> bool:
        """Persist a validated project-owned CampaignSpec in the canonical event log.

        Campaign specifications are control-plane inputs rather than registry entities,
        so they deliberately have no ninth projection table.  The latest event for a
        campaign is the durable portfolio-manifest snapshot used by readiness.  The
        injectable validator is the seam for the project-owned structural gate; until
        that gate exists, fixtures can prove that its refusals propagate unchanged.
        """
        from knowledge.ml_registry.contracts import CampaignSpec, ContractError
        from knowledge.ml_registry.policy_gate import compute_campaign_rope

        if structural_validator is not None:
            structural_validator(spec)
        campaign = CampaignSpec.from_mapping(spec)
        # THE ADOPTION FLOOR IS DECLARED HERE, ONCE, before any run of this campaign -- the
        # same "set it with the judge, never renegotiate it" rule the Praxis-space path
        # enforces at `write_path.register_model`. Refused at registration for the same
        # reason `sigmas` is: a floor that cannot state a gain must not survive to silently
        # become the default when the first arm is adjudicated.
        from knowledge.ml_registry.services.paired_adjudication import (
            guard_adoption_floor,
            guard_vector_judge,
        )

        try:
            if campaign.metric is not None:
                guard_adoption_floor(campaign.metric)
            else:
                # A vector judge validates every judged metric at registration -- floor,
                # direction, and a per-metric paired adjudication policy -- so nothing
                # unusable survives to adjudication time.
                guard_vector_judge(campaign.metrics)
        except RegistryError as exc:
            raise ContractError(str(exc)) from exc
        if campaign.rope is not None:
            raise ContractError(
                "rope is registration-derived and may not be supplied in the campaign spec; "
                "omit rope and pass scoring_corpora so the registry can recompute it"
            )
        if scoring_corpora is None:
            raise ContractError(
                "scoring_corpora is required to register a campaign; pass the spec's named "
                "scoring corpus so its split-unit bootstrap rope can be computed"
            )
        canonical = campaign.to_mapping()
        canonical["rope"] = compute_campaign_rope(campaign, scoring_corpora)
        campaign_id = canonical["campaign_id"]
        prior = [event for event in self.events.read()
                 if event.event_type in {
                     "campaign_spec_registered", "campaign_registration_refused",
                 } and event.payload.get("campaign_id") == campaign_id]
        if (prior and prior[-1].event_type == "campaign_spec_registered"
                and prior[-1].payload == canonical):
            return False
        self._write("campaign_spec_registered", canonical)
        return True

    def record_campaign_registration_refusal(self, campaign_id: str, reason: str) -> bool:
        """Keep a rejected portfolio entry visible without making it a registered spec."""
        if not campaign_id.strip() or not reason.strip():
            raise RegistryError("campaign registration refusal requires an id and reason")
        payload = {"campaign_id": campaign_id.strip(), "reason": reason.strip()}
        prior = [event for event in self.events.read()
                 if event.event_type in {
                     "campaign_spec_registered", "campaign_registration_refused",
                 } and event.payload.get("campaign_id") == payload["campaign_id"]]
        if (prior and prior[-1].event_type == "campaign_registration_refused"
                and prior[-1].payload == payload):
            return False
        self._write("campaign_registration_refused", payload)
        return True

    def record_campaign_outcome(self, outcome: Mapping[str, Any]) -> bool:
        """Commit one terminal campaign outcome idempotently before another dispatch."""
        from knowledge.ml_registry.contracts import CampaignOutcomeRecord

        record = CampaignOutcomeRecord.from_mapping(outcome)
        payload = record.to_mapping()
        prior = [event for event in self.events.read()
                 if event.event_type == "campaign_outcome_recorded"
                 and event.payload.get("campaign_id") == record.campaign_id]
        if prior:
            if prior[-1].payload != payload:
                raise RegistryError("campaign outcome retry drifted from its terminal verdict")
            self.recover()
            return False
        self._write("campaign_outcome_recorded", payload)
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
        if alias == "champion":
            # A champion must point at a run that actually measured something. Four models were
            # given champions today whose runs carried validity=invalid with metric 0.0 and
            # throughput 0.0 -- id-reservation registrations that never ran. Adjudication voids an
            # INVALID run, but a direct registration path reaches this seam without passing
            # through it, so the guard has to live here too.
            #
            # This matters because the law is that a MISSING promotion FAILS rather than falling
            # back: a campaign declaring `model: <x>@champion` must not silently receive a fiction
            # instead of an error. An alias resolving to an unmeasured run is worse than no alias.
            effective = self.effective_model_version(model_id, version)
            run = next(
                (row for row in self.rows("runs") if row["run_id"] == effective["run_id"]), None
            )
            if run is not None:
                try:
                    validity = json.loads(run["metrics"] or "{}").get("validity")
                except (TypeError, ValueError):
                    validity = None
                if validity == "invalid":
                    raise RegistryError(
                        f"champion alias for {model_id!r} would point at run "
                        f"{effective['run_id']!r} whose validity is 'invalid'; a champion must "
                        "name a run that measured something"
                    )
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

    def _rollback_failed_landing(
        self,
        *,
        model_id: str,
        failed_version: int,
        aliases: Mapping[str, Mapping[str, Any]],
        capability: object,
    ) -> None:
        """Atomically restore both aliases after the external landing writer refuses."""
        if capability is not _PRODUCTION_CAPABILITY:
            raise RegistryError("failed landing rollback requires finalize authority")
        self._write("promotion_rolled_back", {
            "model_id": model_id,
            "failed_version": failed_version,
            "aliases": {name: dict(row) for name, row in aliases.items()},
        })

    def _record_landed_promotion(
        self,
        *,
        model_id: str,
        version: int,
        landing_commit: str,
        aliases: Mapping[str, Mapping[str, Any]],
        capability: object,
    ) -> None:
        """Record the external half needed to undo one successful promotion."""
        if capability is not _PRODUCTION_CAPABILITY:
            raise RegistryError("landed promotion requires finalize authority")
        if not landing_commit.strip():
            raise RegistryError("landed promotion requires a commit")
        self._write("campaign_landed", {
            "model_id": model_id,
            "version": version,
            "landing_commit": landing_commit.strip(),
            "aliases": {name: dict(row) for name, row in aliases.items()},
        })

    def _restore_unpromotion(
        self,
        *,
        model_id: str,
        aliases: Mapping[str, Mapping[str, Any]],
        capability: object,
    ) -> None:
        """Compensate an alias rollback when the external landing inverse refuses."""
        if capability is not _PRODUCTION_CAPABILITY:
            raise RegistryError("unpromotion rollback requires finalize authority")
        self._write("unpromotion_rolled_back", {
            "model_id": model_id,
            "aliases": {name: dict(row) for name, row in aliases.items()},
        })

    def _record_campaign_unpromoted(
        self,
        *,
        model_id: str,
        landing_commit: str,
        revert_commit: str,
        capability: object,
    ) -> None:
        if capability is not _PRODUCTION_CAPABILITY:
            raise RegistryError("unpromotion record requires finalize authority")
        self._write("campaign_unpromoted", {
            "model_id": model_id,
            "landing_commit": landing_commit,
            "revert_commit": revert_commit,
        })

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


# --- replaying the event log into the SQLite projection ----------------------------------
# The event log is the durable record; `registry.sqlite3` is only a projection of it. That
# is only TRUE if the projection can actually be rebuilt from the log, which is what this
# section provides. It adds no second projector and no second schema: the projection is
# built by `Registry._project` against `DDL`, exactly as the live writer builds it, so a
# replayed view cannot drift from a written one.


@dataclass(frozen=True)
class ReplayReport:
    """What one :func:`replay_projection` call read, verified and wrote."""

    events: int
    #: The projection already on disk agreed with the log, row for row, on every table.
    current: bool
    #: This call actually rewrote the projection. Always ``False`` under ``check_only``.
    rebuilt: bool
    #: Row counts of the projection the log implies, per table.
    rows: Mapping[str, int]
    #: Where the replaced projection was kept, when one was replaced.
    quarantine: Path | None


def expected_projection(events: Sequence[RegistryEvent]) -> sqlite3.Connection:
    """The in-memory projection ``events`` imply, built by the one and only projector.

    Events are applied in file order, so the result is a pure function of the log.
    """
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    authority = {"value": ""}
    db.create_function("registry_authority", 0, lambda: authority["value"])
    db.executescript(DDL)
    for event in events:
        authority["value"] = event.event_type
        Registry._replay_one(db, event)
        Registry._record_event(db, event)
    db.commit()
    return db


def _projected_tables(db: sqlite3.Connection) -> list[str]:
    return [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()]


def projections_agree(actual: sqlite3.Connection, expected: sqlite3.Connection) -> bool:
    """Whether two projections hold identical rows, in order, on every declared table."""
    for table in _projected_tables(expected):
        columns = ",".join(row[1] for row in expected.execute(f"PRAGMA table_info({table})").fetchall())
        left = [tuple(row) for row in actual.execute(f"SELECT {columns} FROM {table} ORDER BY rowid")]
        right = [tuple(row) for row in expected.execute(f"SELECT {columns} FROM {table} ORDER BY rowid")]
        if left != right:
            return False
    return True


def _refuse_torn_log(path: Path) -> None:
    """Refuse a truncated or unparsable FINAL line rather than letting it be quarantined.

    :meth:`EventLog.read` repairs a torn tail -- it moves the partial bytes aside and
    truncates the file -- because the live writer must survive a crash mid-append. A replay
    must not: it reads the log as evidence, and silently trimming the very end would hide
    exactly the loss the reader came to detect. Every other malformed line, and any break in
    the hash chain, is already refused by ``read`` naming its line number.
    """
    if not path.exists():
        return
    content = path.read_bytes()
    if not content:
        return
    lines = content.splitlines()
    if not content.endswith((b"\n", b"\r")):
        raise EventLogError(f"event line {len(lines)} is truncated: no terminating newline")
    try:
        json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise EventLogError(f"malformed event line {len(lines)}") from exc


def _read_only(db_path: Path) -> sqlite3.Connection:
    """Open a projection strictly for reading: no ``writer.lock``, no PRAGMA writes.

    ``Registry._connect`` sets ``journal_mode=WAL`` and ``synchronous=FULL``, both of which
    WRITE; a check must be safe to run while a campaign is writing, so it takes neither,
    and never touches the lock.

    Honest caveat: SQLite cannot read a WAL-mode database without its shared-memory index,
    so this open MATERIALISES ``registry.sqlite3-shm`` and an empty ``-wal`` beside the
    database if they are absent. Those are transient sidecars every reader creates -- an
    ordinary ``registry-status`` does the same -- and the durable record (the database file
    itself and ``events.jsonl``) is left byte-identical. ``immutable=1`` would avoid even
    that, and is deliberately NOT used: it asserts the file cannot change underneath the
    reader, which is exactly false on a live registry.
    """
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)


def replay_projection(root: str | Path, *, overwrite: bool = False,
                      check_only: bool = False) -> ReplayReport:
    """Reconstruct ``root/registry.sqlite3`` from ``root/events.jsonl``.

    This is what makes the append-only log the durable record: a projection lost, corrupted,
    or clobbered by a ``git checkout`` is rebuilt from the log alone. It drives the SAME
    engine the writer self-heals with (:meth:`Registry.recover`), exposed as a deliberate,
    guarded operation.

    Refuses rather than guesses. Every event carries the SHA-256 of its own body together
    with its predecessor's digest, so the log is a hash CHAIN and any edit, reorder, or
    deletion breaks it at a line :meth:`EventLog.read` names. A torn final line is refused
    rather than repaired (:func:`_refuse_torn_log`), and an ``event_type`` the projection
    does not know is refused naming its line BEFORE the database on disk is touched -- a
    replay that dropped what it could not read would produce a view that looks complete and
    is not.

    Idempotent: replaying an already-current log rewrites nothing and reports
    ``current=True, rebuilt=False``. A rebuild never destroys the projection it replaces --
    that file is moved to ``registry.sqlite3.projection-quarantine``, named in the report.

    ``overwrite=False`` refuses to replace an existing projection that disagrees with the
    log; replacing one is a decision the caller must make explicitly. ``check_only=True``
    writes NOTHING at all -- it reads the log, verifies the chain, and compares against a
    read-only handle on the projection, so it is safe against a live registry.
    """
    root = Path(root)
    log_path, db_path = root / "events.jsonl", root / "registry.sqlite3"
    _refuse_torn_log(log_path)
    events = EventLog(log_path).read()
    expected = expected_projection(events)
    try:
        rows = {table: expected.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in _projected_tables(expected)}
        if check_only:
            if not db_path.exists():
                return ReplayReport(len(events), False, False, rows, None)
            actual = _read_only(db_path)
            try:
                current = projections_agree(actual, expected)
            finally:
                actual.close()
            return ReplayReport(len(events), current, False, rows, None)
        existed = db_path.exists()
        # `auto_recover=False`: opening a registry normally replays the log by itself,
        # which would silently overwrite the projection before the guard below could run.
        registry = Registry(root, auto_recover=False)
        with exclusive_file_lock(registry.lock_path):
            with registry._connect() as db:
                current = projections_agree(db, expected)
            if current:
                return ReplayReport(len(events), True, False, rows, None)
            if existed and not overwrite:
                raise RegistryError(
                    f"{db_path} disagrees with {log_path} and would be replaced; "
                    "pass overwrite to authorise it"
                )
            registry._rebuild_projection(events)
        quarantine = db_path.with_name(f"{db_path.name}.projection-quarantine")
        return ReplayReport(len(events), False, True, rows, quarantine if existed else None)
    finally:
        expected.close()
