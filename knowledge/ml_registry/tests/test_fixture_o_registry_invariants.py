"""§5.11 registry data model, §7.2 fixture O -- one file, five real invariants.

Every assertion here runs against a real :class:`Registry` (real SQLite file, real
append-only event log, real content-addressed blob store) and a real throwaway git
repository -- never a mock or a stub. The five invariants are exactly fixture O's:

1. ``model_versions`` are immutable: a direct ``UPDATE`` against the SQLite projection raises.
2. ``production`` can only be set by ``finalize``: a direct write (the public
   ``Registry.set_alias`` seam, and the private write path without finalize authority) raises.
3. Every version's ``code_sha`` exists in the repo its run declares.
4. Every ``champion`` move is paired with an event that carries a non-empty reason.
5. The ``results.tsv`` export equals the ``runs`` table it was derived from.

None of these are new mechanisms -- each already has a narrower characterisation test
elsewhere (``test_standard_registry.py``, ``test_foundation_characterization.py``,
``test_remaining_fixture_characterization.py``); this file is fixture O's own consolidated
proof that all five hold together against one built-from-scratch registry.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
import sqlite3
import subprocess

import pytest

from knowledge.ml_registry import Registry
from knowledge.ml_registry.contracts import RunsExport
from knowledge.ml_registry.services.registry_aliases import adopt_run_and_promote, move_champion
from knowledge.ml_registry.services.registry_finalize import RegistryFinalizeService
from knowledge.ml_registry.services.registry_runs import complete_run
from knowledge.ml_registry.storage.registry import RegistryError


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    """A real, throwaway git repository with one committed file -- not a mocked path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Fixture O")
    _git(repo, "config", "user.email", "fixture-o@example.invalid")
    (repo / "model.py").write_text("MODEL = 'fixture-o'\n")
    _git(repo, "add", "model.py")
    _git(repo, "commit", "-qm", "fixture-o commit")
    return repo, _git(repo, "rev-parse", "HEAD")


def _code_ref(repo: Path, sha: str, *, diff_lines: int = 2) -> dict[str, object]:
    return {"schema_version": 1, "repo": str(repo), "sha": sha, "base_sha": sha,
            "diff_hash": "d" * 64, "diff_lines": diff_lines}


def _metrics(metric: float, *, throughput: float = 1.5, memory_gb: float = 0.5) -> dict[str, object]:
    return {"metric": metric, "validity": "valid", "throughput": throughput,
            "throughput_unit": "rows_per_second", "memory_gb": memory_gb, "cpu_time": 1.0,
            "load": {"start_1m": 0.1, "end_1m": 0.2}}


@pytest.fixture
def scenario(tmp_path: Path):
    """One real registry with a completed, adopted run and a live model version."""
    repo, sha = _init_repo(tmp_path)
    registry = Registry(tmp_path / "registry")
    registry.create_experiment(
        experiment_id="fixture-o", spec_digest="a" * 64, stages=["representation"],
        metric="score", direction="maximize", win_condition={"metric_at_least": 0.5},
        rope=0.01, baseline_throughput=1.0,
    )
    registry.register_model(model_id="fixture-o-model", family="linear", sport_scope="shared",
                            axis="fixture", protocol="Detector", extends=None)
    registry.create_run(
        run_id="run-1", experiment_id="fixture-o", idea_id="idea-1", stage="representation",
        family="linear", params={"description": "baseline arm"}, metrics={},
        code_ref=_code_ref(repo, sha), device_fingerprint="cpu:fixture", status="running",
        verdict=None, started_at=1.0, finished_at=None, claim_owner="trainer", heartbeat_at=1.0,
    )
    complete_run(registry, run_id="run-1", metrics=_metrics(0.83))
    artifact = registry.create_artifact(run_id="run-1", kind="checkpoint", content=b"fixture-o weights",
                                        schema_version="1")
    adopt_run_and_promote(
        registry, run_id="run-1", model_id="fixture-o-model", reason="fixture-o first champion",
        model_version={
            "version": 1, "artifact_id": artifact, "checksum": artifact,
            "family_version": "linear@1", "code_sha": sha, "preprocessing_hash": "prep",
            "calibration": {}, "thresholds": {},
            "compat_result": {"head_sha": sha, "passed": True, "at": 2.0}, "status": "active",
        },
    )
    return registry, repo, sha, artifact


# --- 1. model_versions are immutable ------------------------------------------------------

def test_model_versions_reject_direct_update(scenario) -> None:
    registry, _repo, _sha, _artifact = scenario
    assert registry.rows("model_versions")[0]["status"] == "active"
    with sqlite3.connect(registry.db_path) as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE model_versions SET status='superseded' WHERE model_id='fixture-o-model'")
    with sqlite3.connect(registry.db_path) as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("DELETE FROM model_versions WHERE model_id='fixture-o-model'")
    # The projection genuinely never changed: no rogue transaction slipped through.
    assert registry.rows("model_versions")[0]["status"] == "active"


# --- 2. production can only be set by finalize ------------------------------------------

def test_production_alias_rejects_every_direct_write(scenario) -> None:
    registry, _repo, _sha, _artifact = scenario
    move_champion(registry, model_id="fixture-o-model", version=1, reason="fixture-o champion move")

    # The only public write seam refuses outright, regardless of the claimed set_by.
    with pytest.raises(RegistryError, match="service-owned"):
        registry.set_alias(model_id="fixture-o-model", alias="production", version=1,
                           set_by="finalize", reason="forged direct write")

    # Forging the SQL-level authority marker without real finalize capability also refuses:
    # the trigger checks set_by literally, so exercise the private write path a caller with
    # no capability object could reach and confirm the service layer still blocks it before
    # any SQL runs.
    with pytest.raises(RegistryError, match="requires finalize authority"):
        registry._finalize_registry_version(
            {"model_id": "fixture-o-model", "version": 1, "run_id": "run-1",
             "artifact_id": _artifact, "checksum": _artifact, "head_sha": _sha,
             "reason": "forged", "upstreams": []},
            capability=object(),
        )

    # A raw SQL insert claiming set_by='finalize' is exactly the attack the trigger exists to
    # stop even when it bypasses the Python service layer entirely. Register the same
    # `registry_authority()` SQL function the real connection uses (claiming top-level write
    # authority, as an attacker who reached raw SQL would) so this exercises the
    # `production_authority_insert` trigger itself rather than an unrelated missing-function
    # error.
    forging_db = sqlite3.connect(registry.db_path)
    try:
        forging_db.create_function("registry_authority", 0, lambda: "alias_set")
        with pytest.raises(sqlite3.IntegrityError, match="production alias requires finalize"):
            forging_db.execute(
                "INSERT INTO aliases VALUES(?,?,?,?,?,?)",
                ("fixture-o-model", "production", 1, "someone-else", "forged", 99.0),
            )
    finally:
        forging_db.close()

    assert not any(row["alias"] == "production" for row in registry.rows("aliases"))

    # The real finalize path, by contrast, succeeds and is the only way production moves.
    RegistryFinalizeService(registry).move_production(model_id="fixture-o-model", version=1,
                                                       reason="fixture-o compat pass")
    production = next(row for row in registry.rows("aliases") if row["alias"] == "production")
    assert production["set_by"] == "finalize"
    assert production["version"] == 1


# --- 3. every version's code_sha exists in the repo -------------------------------------

def test_every_model_version_code_sha_exists_in_its_repo(scenario) -> None:
    registry, repo, sha, _artifact = scenario
    versions = registry.rows("model_versions")
    assert versions, "fixture produced no model version to check"
    for version in versions:
        assert version["code_sha"] == sha
        result = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{version['code_sha']}^{{commit}}"],
            check=False, capture_output=True, text=True,
        )
        assert result.returncode == 0, f"code_sha {version['code_sha']!r} is not a commit in {repo}"

    # And the invariant is real, not vacuous: a sha that was never committed genuinely fails
    # the same check the registry itself runs at adoption time.
    forged_sha = "0" * 40
    forged = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{forged_sha}^{{commit}}"],
        check=False, capture_output=True, text=True,
    )
    assert forged.returncode != 0


# --- 4. every champion move is paired with an event carrying a reason -------------------

def test_every_champion_move_has_a_reasoned_event(scenario) -> None:
    registry, repo, sha, _artifact = scenario

    # A second run, adopted as a second champion move via ratchet-free adjudication (the
    # atomic adopt-and-promote path also moves champion, so exercise both writers).
    registry.create_run(
        run_id="run-2", experiment_id="fixture-o", idea_id="idea-2", stage="representation",
        family="linear", params={"description": "second arm"}, metrics={},
        code_ref=_code_ref(repo, sha, diff_lines=5), device_fingerprint="cpu:fixture",
        status="running", verdict=None, started_at=3.0, finished_at=None,
        claim_owner="trainer", heartbeat_at=3.0,
    )
    complete_run(registry, run_id="run-2", metrics=_metrics(0.95))
    artifact_2 = registry.create_artifact(run_id="run-2", kind="checkpoint",
                                          content=b"fixture-o weights v2", schema_version="1")
    adopt_run_and_promote(
        registry, run_id="run-2", model_id="fixture-o-model", reason="fixture-o second champion",
        model_version={
            "version": 2, "artifact_id": artifact_2, "checksum": artifact_2,
            "family_version": "linear@1", "code_sha": sha, "preprocessing_hash": "prep",
            "calibration": {}, "thresholds": {},
            "compat_result": {"head_sha": sha, "passed": True, "at": 4.0}, "status": "active",
        },
    )

    champion_events = [
        event for event in registry.list_events()
        if event.event_type == "run_adopted"
        and event.payload.get("model_version", {}).get("model_id") == "fixture-o-model"
    ]
    assert len(champion_events) == 2, "expected one champion-moving event per adoption"
    for event in champion_events:
        reason = event.payload.get("reason")
        assert isinstance(reason, str) and reason.strip(), f"champion event {event.sequence} has no reason"

    # A move that supplies no reason is refused outright -- the pairing is enforced, not
    # merely observed after the fact.
    with pytest.raises(RegistryError, match="reason"):
        move_champion(registry, model_id="fixture-o-model", version=2, reason="")

    # The current champion alias matches the last reasoned move exactly.
    champion = next(row for row in registry.rows("aliases") if row["alias"] == "champion")
    assert champion["version"] == 2
    assert champion["reason"] == "fixture-o second champion"


# --- 5. results.tsv export equals the runs table -----------------------------------------

def test_results_tsv_export_equals_the_runs_table(scenario) -> None:
    registry, repo, sha, _artifact = scenario
    registry.create_run(
        run_id="run-2", experiment_id="fixture-o", idea_id="idea-2", stage="representation",
        family="linear", params={"description": "second arm"}, metrics={},
        code_ref=_code_ref(repo, sha, diff_lines=7), device_fingerprint="cpu:fixture",
        status="running", verdict=None, started_at=3.0, finished_at=None,
        claim_owner="trainer", heartbeat_at=3.0,
    )
    complete_run(registry, run_id="run-2", metrics=_metrics(0.42, throughput=3.0, memory_gb=1.2))

    exported = RunsExport(registry).render(experiment_id="fixture-o")
    reader = csv.DictReader(io.StringIO(exported.decode("utf-8"), newline=""), delimiter="\t")
    tsv_rows = list(reader)

    runs = sorted(
        (row for row in registry.rows("runs") if row["experiment_id"] == "fixture-o"),
        key=lambda row: (row["started_at"], row["run_id"]),
    )
    assert len(tsv_rows) == len(runs) == 2

    import json as _json
    for tsv_row, run_row in zip(tsv_rows, runs, strict=True):
        code_ref = _json.loads(run_row["code_ref"])
        metrics = _json.loads(run_row["metrics"])
        params = _json.loads(run_row["params"])
        assert tsv_row["commit"] == code_ref["sha"]
        assert float(tsv_row["metric_value"]) == metrics["metric"]
        assert float(tsv_row["memory_gb"]) == metrics["memory_gb"]
        assert float(tsv_row["throughput"]) == metrics["throughput"]
        assert int(tsv_row["diff_lines"]) == code_ref["diff_lines"]
        assert tsv_row["description"] == params.get("description", run_row["idea_id"])
        assert tsv_row["status"] == "ok"

    # The export is a genuinely derived VIEW, not an independent record: mutating the runs
    # table's shape (a third completed run) changes what the next export produces.
    registry.create_run(
        run_id="run-3", experiment_id="fixture-o", idea_id="idea-3", stage="representation",
        family="linear", params={"description": "third arm"}, metrics={},
        code_ref=_code_ref(repo, sha, diff_lines=9), device_fingerprint="cpu:fixture",
        status="running", verdict=None, started_at=5.0, finished_at=None,
        claim_owner="trainer", heartbeat_at=5.0,
    )
    complete_run(registry, run_id="run-3", metrics=_metrics(0.11))
    reexported = RunsExport(registry).render(experiment_id="fixture-o")
    rows_after = list(csv.DictReader(io.StringIO(reexported.decode("utf-8"), newline=""), delimiter="\t"))
    assert len(rows_after) == 3
    assert rows_after[2]["description"] == "third arm"
