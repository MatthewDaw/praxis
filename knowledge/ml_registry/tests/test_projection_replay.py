"""The SQLite projection is rebuildable from `events.jsonl`, which is what makes the
append-only log -- not the binary -- the durable record.

The incident these tests exist to defuse: a live registry tracked in git was reverted by a
routine `git checkout --` mid-campaign, a rebuilt 21-trial projection became an 11-trial
snapshot, and already-judged arms were re-queued. A log that two hosts can union and that
a projection can be replayed from is the answer; a replayer that quietly drops what it
cannot read is the same bug wearing a different hat, so every refusal here is load-bearing.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from knowledge.ml_registry import Registry
from knowledge.ml_registry.cli.registry import main
from knowledge.ml_registry.storage import EventLog, EventLogError, RegistryError, replay_projection


REPO = Path(__file__).resolve().parents[3]
SHA = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], check=True,
                     capture_output=True, text=True).stdout.strip()
DIFF = "c" * 64
TABLES = ("aliases", "artifacts", "events", "experiments", "lineage", "model_versions",
          "registered_models", "runs")

#: The real campaign registry this replayer was built against. Present only on the machine
#: that runs that campaign, so the fidelity test against it is opt-in by existence -- it is
#: never written to, only copied.
LIVE_ROOT = Path("/Users/matthewdaw/Documents/official_repos/sports_analysis/campaign_state/ml_registry")


def _metrics() -> dict[str, object]:
    return {"metric": 0.91, "validity": "valid", "throughput": 2.0,
            "throughput_unit": "rows_per_second", "memory_gb": 1.0, "cpu_time": 2.0,
            "load": {"start_1m": 0.25, "end_1m": 0.5}}


def _populated(root: Path) -> Registry:
    """A registry carrying one event of every kind the campaign writer emits."""
    from knowledge.ml_registry.services.registry_aliases import adopt_run_and_promote
    from knowledge.ml_registry.services.registry_runs import complete_run

    registry = Registry(root)
    registry.create_experiment(
        experiment_id="campaign", spec_digest="d" * 64, stages=["representation"], metric="score",
        direction="maximize", win_condition={"metric_at_least": 0.9}, rope=0.01,
        baseline_throughput=1.0,
    )
    registry.amend_experiment(
        "campaign",
        reason="tighten the declared bar",
        win_condition={"metric_at_least": 0.9, "constant_control_margin_at_least": 0.005},
        spec_digest="e" * 64,
    )
    registry.create_run(
        run_id="run-1", experiment_id="campaign", idea_id="idea-1", stage="representation",
        family="linear", params={"description": "baseline"}, metrics={},
        code_ref={"schema_version": 1, "repo": str(REPO), "sha": SHA, "base_sha": SHA,
                  "diff_hash": DIFF, "diff_lines": 3},
        device_fingerprint="cpu:test", status="running", verdict=None, started_at=1.0,
        finished_at=None, claim_owner="worker", heartbeat_at=1.0,
    )
    complete_run(registry, run_id="run-1", metrics=_metrics())
    registry.register_model(model_id="model", family="linear", sport_scope="shared", axis="a01",
                            protocol="Detector", extends=None)
    digest = registry.create_artifact(run_id="run-1", kind="checkpoint", content=b"weights",
                                      schema_version="1")
    adopt_run_and_promote(registry, run_id="run-1", model_id="model", reason="won", model_version=dict(
        version=1, artifact_id=digest, checksum=digest, family_version="linear@1", code_sha=SHA,
        preprocessing_hash="prep", calibration={}, thresholds={},
        compat_result={"head_sha": SHA, "passed": True, "at": 3.0}, status="active",
    ))
    return registry


def _reverted_root(destination: Path, *, log_from: Path, projection_from: Path) -> Path:
    """A registry root reproducing the incident, assembled from two others.

    The log is COMPLETE -- every event the campaign wrote -- while the projection beside it
    is an earlier snapshot, which is what a `git checkout --` of a tracked binary leaves.
    It is assembled in a fresh directory so that no connection is open on either source
    when the replayer reads it, exactly as on a box where the writer has moved on.
    """
    destination.mkdir(parents=True)
    shutil.copy2(log_from / "events.jsonl", destination / "events.jsonl")
    shutil.copytree(log_from / "blobs", destination / "blobs")
    db = sqlite3.connect(projection_from / "registry.sqlite3")
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # fold the WAL in: snapshot one file
    finally:
        db.close()
    shutil.copy2(projection_from / "registry.sqlite3", destination / "registry.sqlite3")
    return destination


def _dump(db_path: Path) -> dict[str, list[tuple[object, ...]]]:
    """Every row of every table, in rowid order, for a row-for-row comparison."""
    db = sqlite3.connect(db_path)
    try:
        return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in TABLES}
    finally:
        db.close()


def _fingerprint(root: Path) -> dict[str, str]:
    """The durable record only: the sidecars SQLite materialises to READ a WAL database
    (`-wal`, `-shm`) are not part of it, and every reader creates them."""
    import hashlib
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()
            and not path.name.endswith(("-wal", "-shm"))}


def test_replay_reconstructs_the_projection_the_live_writer_built(tmp_path: Path) -> None:
    written = _dump(_populated(tmp_path).db_path)
    (tmp_path / "registry.sqlite3").unlink()

    report = replay_projection(tmp_path)

    assert report.rebuilt and not report.current
    assert report.events == len(written["events"])
    assert _dump(tmp_path / "registry.sqlite3") == written


def test_replaying_an_up_to_date_projection_is_a_no_op_and_replay_is_idempotent(tmp_path: Path) -> None:
    _populated(tmp_path)
    before = _fingerprint(tmp_path)

    first = replay_projection(tmp_path)
    second = replay_projection(tmp_path)

    assert (first.current, first.rebuilt) == (True, False)
    assert (second.current, second.rebuilt) == (True, False)
    assert _fingerprint(tmp_path) == before


def test_replaying_a_reconstructed_projection_twice_yields_the_same_rows(tmp_path: Path) -> None:
    _populated(tmp_path)
    (tmp_path / "registry.sqlite3").unlink()

    replay_projection(tmp_path)
    once = _dump(tmp_path / "registry.sqlite3")
    (tmp_path / "registry.sqlite3").unlink()
    replay_projection(tmp_path)

    assert _dump(tmp_path / "registry.sqlite3") == once


def test_an_unknown_event_type_is_refused_naming_its_line_before_the_database_is_touched(
        tmp_path: Path) -> None:
    registry = _populated(tmp_path)
    line = len(registry.events.read()) + 1
    EventLog(tmp_path / "events.jsonl").append("teleported_sideways", {"run_id": "run-1"}, at=9.0)
    before = _fingerprint(tmp_path)

    with pytest.raises(RegistryError, match=f"unknown event type 'teleported_sideways' at "
                                            f"events.jsonl line {line}"):
        replay_projection(tmp_path, overwrite=True)

    # A replay that dropped the event it could not read would leave a view that LOOKS
    # complete. Refusing before any write leaves the operator the projection they had.
    assert _fingerprint(tmp_path) == before


def test_a_torn_final_line_is_refused_rather_than_quarantined(tmp_path: Path) -> None:
    _populated(tmp_path)
    log = tmp_path / "events.jsonl"
    lines = log.read_bytes().splitlines(keepends=True)
    log.write_bytes(b"".join(lines[:-1]) + lines[-1][: len(lines[-1]) // 2])
    torn = log.read_bytes()

    with pytest.raises(EventLogError, match=f"event line {len(lines)} is truncated"):
        replay_projection(tmp_path, overwrite=True)

    # `EventLog.read` REPAIRS a torn tail for the live writer. A replay must not: trimming
    # the end of the record would hide exactly the loss the reader came to detect.
    assert log.read_bytes() == torn
    assert not list(tmp_path.glob("events.jsonl.torn-*"))


def test_a_corrupt_but_terminated_final_line_is_refused_naming_it(tmp_path: Path) -> None:
    _populated(tmp_path)
    log = tmp_path / "events.jsonl"
    lines = log.read_bytes().splitlines(keepends=True)
    log.write_bytes(b"".join(lines[:-1]) + b'{"schema_version": 1, "sequ\n')

    with pytest.raises(EventLogError, match=f"malformed event line {len(lines)}"):
        replay_projection(tmp_path, overwrite=True)


def test_a_tampered_event_breaks_the_hash_chain_at_its_line(tmp_path: Path) -> None:
    _populated(tmp_path)
    log = tmp_path / "events.jsonl"
    lines = log.read_bytes().splitlines(keepends=True)
    event = json.loads(lines[1])
    event["payload"]["idea_id"] = "someone-elses-idea"
    lines[1] = json.dumps(event, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    log.write_bytes(b"".join(lines))

    with pytest.raises(EventLogError, match="event hash chain is broken at line 2"):
        replay_projection(tmp_path, overwrite=True)


def test_an_empty_log_projects_an_empty_but_valid_database(tmp_path: Path) -> None:
    (tmp_path / "events.jsonl").write_bytes(b"")

    report = replay_projection(tmp_path)

    assert report.events == 0 and report.rows == dict.fromkeys(TABLES, 0)
    db = sqlite3.connect(tmp_path / "registry.sqlite3")
    try:
        names = tuple(row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"))
    finally:
        db.close()
    assert names == TABLES
    # Valid means WRITABLE: the empty projection accepts the writer's next event.
    Registry(tmp_path).create_experiment(
        experiment_id="campaign", spec_digest="d" * 64, stages=["representation"], metric="score",
        direction="maximize", win_condition={"metric_at_least": 0.9}, rope=0.01,
        baseline_throughput=1.0,
    )


def test_a_disagreeing_projection_is_not_replaced_without_explicit_overwrite(tmp_path: Path) -> None:
    complete = _dump(_populated(tmp_path / "complete").db_path)
    early = Registry(tmp_path / "early")
    early.create_experiment(
        experiment_id="campaign", spec_digest="d" * 64, stages=["representation"], metric="score",
        direction="maximize", win_condition={"metric_at_least": 0.9}, rope=0.01,
        baseline_throughput=1.0,
    )
    root = _reverted_root(tmp_path / "box", log_from=tmp_path / "complete",
                          projection_from=tmp_path / "early")
    assert len(_dump(root / "registry.sqlite3")["events"]) < len(complete["events"])

    with pytest.raises(RegistryError, match="would be replaced; pass overwrite"):
        replay_projection(root)
    assert len(_dump(root / "registry.sqlite3")["events"]) == 1, "the refusal wrote nothing"

    report = replay_projection(root, overwrite=True)

    assert report.rebuilt and report.quarantine is not None and report.quarantine.exists()
    # The judged history the revert destroyed is back, from the log alone.
    assert _dump(root / "registry.sqlite3") == complete


def test_check_only_writes_nothing_and_reports_drift(tmp_path: Path) -> None:
    _populated(tmp_path / "complete")
    assert replay_projection(tmp_path / "complete", check_only=True).current

    early = Registry(tmp_path / "early")
    early.create_experiment(
        experiment_id="campaign", spec_digest="d" * 64, stages=["representation"], metric="score",
        direction="maximize", win_condition={"metric_at_least": 0.9}, rope=0.01,
        baseline_throughput=1.0,
    )
    root = _reverted_root(tmp_path / "box", log_from=tmp_path / "complete",
                          projection_from=tmp_path / "early")
    before = _fingerprint(root)

    report = replay_projection(root, check_only=True)

    assert not report.current and not report.rebuilt
    assert report.rows["runs"] == 1, "the report describes what the LOG implies, not the file"
    assert _fingerprint(root) == before, "a check must be safe against a live registry"


def test_check_only_never_takes_the_writer_lock(tmp_path: Path) -> None:
    from knowledge.ml_registry.file_lock import exclusive_file_lock

    registry = _populated(tmp_path)
    # Holding the writer lock is exactly the campaign-is-writing case. A check that waited
    # on it would be unusable at the moment an operator most wants to run it.
    with exclusive_file_lock(registry.lock_path):
        assert replay_projection(tmp_path, check_only=True).current


def test_cli_rebuild_projection_checks_refuses_and_replays(tmp_path: Path, capsys) -> None:
    _populated(tmp_path)
    root = str(tmp_path)

    assert main(["rebuild-projection", "--registry-root", root, "--check"]) == 0
    assert "projection is current" in capsys.readouterr().out

    (tmp_path / "registry.sqlite3").unlink()
    assert main(["rebuild-projection", "--registry-root", root, "--check"]) == 1
    assert "STALE" in capsys.readouterr().out

    assert main(["rebuild-projection", "--registry-root", root, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rebuilt"] and payload["rows"]["runs"] == 1

    assert main(["rebuild-projection", "--registry-root", root]) == 0
    assert "already current" in capsys.readouterr().out


def test_cli_refuses_to_overwrite_a_disagreeing_projection_without_the_flag(
        tmp_path: Path, capsys) -> None:
    _populated(tmp_path / "complete")
    early = Registry(tmp_path / "early")
    early.create_experiment(
        experiment_id="campaign", spec_digest="d" * 64, stages=["representation"], metric="score",
        direction="maximize", win_condition={"metric_at_least": 0.9}, rope=0.01,
        baseline_throughput=1.0,
    )
    root = str(_reverted_root(tmp_path / "box", log_from=tmp_path / "complete",
                              projection_from=tmp_path / "early"))

    assert main(["rebuild-projection", "--registry-root", root]) == 1
    assert "pass overwrite to authorise it" in capsys.readouterr().err

    assert main(["rebuild-projection", "--registry-root", root, "--overwrite"]) == 0
    assert "projection rebuilt" in capsys.readouterr().out


@pytest.mark.skipif(not LIVE_ROOT.exists(), reason="the real campaign registry is host-local")
def test_the_real_campaign_log_replays_into_the_projection_it_already_has(tmp_path: Path) -> None:
    """Fidelity against a registry a live writer built, on a COPY: the log is either a full
    ledger or it is not, and only real history can settle that."""
    shutil.copytree(LIVE_ROOT, tmp_path / "copy")
    live = _dump(tmp_path / "copy" / "registry.sqlite3")
    shutil.copytree(tmp_path / "copy", tmp_path / "replayed")
    (tmp_path / "replayed" / "registry.sqlite3").unlink()

    report = replay_projection(tmp_path / "replayed")

    assert report.events == len(live["events"])
    assert _dump(tmp_path / "replayed" / "registry.sqlite3") == live
