"""R19 acceptance: the research-target check reads metric/direction from the model's
frozen registration record instead of assuming ``val_bpb``, treats the existing
unversioned ledger header as version 0, and fails closed on a model record whose audit
trail shows a direct fact edit bypassing the registry's write path.

Runs ``agent_factory/scripts/checks/af_ml_research_target.py`` as a real subprocess --
not an import -- matching the pattern the check itself documents (an external signal, not
the agent's own judgment).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK = REPO_ROOT / "agent_factory" / "scripts" / "checks" / "af_ml_research_target.py"

LEGACY_HEADER = "commit\tval_bpb\tmemory_gb\tstatus\tdescription\n"
V1_HEADER = "commit\tmetric_value\tmemory_gb\tstatus\tdescription\n"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _write_ledger(tmp_path: Path, header: str, rows: list[str]) -> Path:
    path = tmp_path / "results.tsv"
    path.write_text(header + "".join(row + "\n" for row in rows))
    return path


def _write_model_record(
    tmp_path: Path, metric: str, direction: str, audit_trail: list[dict[str, str]] | None = None
) -> Path:
    record = {
        "meta": {"metric": metric, "direction": direction},
        "auditTrail": audit_trail if audit_trail is not None else [
            {"action": "created", "actor": "registry"},
        ],
    }
    path = tmp_path / "model.json"
    path.write_text(json.dumps(record))
    return path


def test_existing_unversioned_ledger_evaluates_unchanged_as_version_0(tmp_path: Path) -> None:
    ledger = _write_ledger(
        tmp_path,
        LEGACY_HEADER,
        [f"c{i}\t{2.0 - i * 0.01:.4f}\t4.0\tok\trun {i}" for i in range(12)],
    )
    result = _run("--results", str(ledger), "--min-experiments", "10", "--min-improvement", "0.05")
    assert result.returncode == 0, result.stderr
    assert "version 0" in result.stdout
    assert "PASS" in result.stdout


def test_ledger_for_a_maximize_direction_model_evaluates_correctly(tmp_path: Path) -> None:
    ledger = _write_ledger(
        tmp_path,
        V1_HEADER,
        [f"c{i}\t{100.0 + i}\t4.0\tok\trun {i}" for i in range(12)],
    )
    model = _write_model_record(tmp_path, metric="throughput", direction="maximize")
    result = _run(
        "--results", str(ledger),
        "--min-experiments", "10",
        "--min-improvement", "5",
        "--model-record", str(model),
    )
    assert result.returncode == 0, result.stderr
    # best row (c11 = 111) beats baseline (c0 = 100) by 11, in the maximize direction
    assert "improvement: +11.000000" in result.stdout


def test_an_unrecognised_header_exits_2(tmp_path: Path) -> None:
    ledger = _write_ledger(
        tmp_path,
        "commit\tscore\tmem\tresult\tnote\n",
        ["c0\t1.0\t4.0\tok\trun 0"],
    )
    result = _run("--results", str(ledger), "--min-experiments", "1", "--target-bpb", "1.0")
    assert result.returncode == 2
    assert "not a recognised ledger version" in result.stderr


def test_a_model_record_mutated_by_a_direct_fact_edit_fails_the_check_closed(tmp_path: Path) -> None:
    ledger = _write_ledger(
        tmp_path,
        V1_HEADER,
        [f"c{i}\t{100.0 - i}\t4.0\tok\trun {i}" for i in range(12)],
    )
    model = _write_model_record(
        tmp_path,
        metric="val_bpb",
        direction="minimize",
        audit_trail=[
            {"action": "created", "actor": "registry"},
            {"action": "edited", "actor": "some-human"},
        ],
    )
    result = _run(
        "--results", str(ledger),
        "--min-experiments", "10",
        "--min-improvement", "1",
        "--model-record", str(model),
    )
    assert result.returncode == 2
    assert "bypassed the write path" in result.stderr


def test_model_record_without_tampering_is_trusted(tmp_path: Path) -> None:
    ledger = _write_ledger(
        tmp_path,
        V1_HEADER,
        [f"c{i}\t{100.0 - i}\t4.0\tok\trun {i}" for i in range(12)],
    )
    model = _write_model_record(tmp_path, metric="val_bpb", direction="minimize")
    result = _run(
        "--results", str(ledger),
        "--min-experiments", "10",
        "--min-improvement", "1",
        "--model-record", str(model),
    )
    assert result.returncode == 0, result.stderr


def test_version_0_ledger_with_a_model_record_naming_a_different_metric_is_refused(tmp_path: Path) -> None:
    ledger = _write_ledger(
        tmp_path,
        LEGACY_HEADER,
        [f"c{i}\t{2.0 - i * 0.01:.4f}\t4.0\tok\trun {i}" for i in range(12)],
    )
    model = _write_model_record(tmp_path, metric="throughput", direction="maximize")
    result = _run(
        "--results", str(ledger),
        "--min-experiments", "10",
        "--min-improvement", "0.05",
        "--model-record", str(model),
    )
    assert result.returncode == 2
