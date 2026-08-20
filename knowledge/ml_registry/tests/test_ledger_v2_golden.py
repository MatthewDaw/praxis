from __future__ import annotations

from pathlib import Path

import pytest

from knowledge.ml_registry.bootstrap import read_ledger
from knowledge.ml_registry.cli import load_ledger_rows
from knowledge.ml_registry.contracts import LedgerV2
from knowledge.ml_registry.contracts.ledger_v2 import read_ledger_compatibility
from knowledge.ml_registry.floor import load_ledger_values
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.write_path import load_ledger_commits


GOLDEN = (
    "commit\tmetric_value\tmemory_gb\tstatus\tdescription\tthroughput\tdiff_lines\n"
    "rev-a:control\t0.75\t1.25\tok\tcontrol measurement\t10.0\t0\n"
    "rev-b:candidate\t0.80\t1.50\tok\tcandidate measurement\t12.0\t4\n"
)


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_all_ledger_readers_replay_the_synthesized_golden_identically(
    tmp_path: Path, newline: str
) -> None:
    payload = GOLDEN.replace("\n", newline)
    path = tmp_path / "ledger.tsv"
    path.write_bytes(payload.encode())

    canonical = LedgerV2.parse(payload)
    header, bootstrap_rows = read_ledger(path)
    commits = load_ledger_commits(path)
    values = load_ledger_values(path)
    rows = load_ledger_rows(path)

    expected_keys = ["rev-a:control", "rev-b:candidate"]
    assert header == list(GOLDEN.splitlines()[0].split("\t"))
    assert [row["commit"] for row in bootstrap_rows] == expected_keys
    assert [row.commit for row in canonical.rows] == expected_keys
    assert commits == frozenset(expected_keys)
    assert values == {"rev-a:control": 0.75, "rev-b:candidate": 0.80}
    assert set(rows) == set(expected_keys)
    for row in canonical.rows:
        projection = rows[row.commit]
        assert projection.value == row.metric_value
        assert projection.throughput == row.throughput
        assert projection.diff_lines == row.diff_lines
        assert projection.status == row.status.value


def test_legacy_val_bpb_and_unfair_retry_share_the_canonical_compatibility_projection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-results.tsv"
    path.write_text(
        "commit\tval_bpb\tmemory_gb\tstatus\tdescription\tthroughput\tdiff_lines\n"
        "rev-a:arm\t0.90\t1.0\tbudget_exhausted\tpartial\t8.0\t2\n"
        "rev-a:arm\t0.72\t1.0\tok\tretry\t10.0\t3\n"
        "rev-b:arm\tbad\t1.0\terrored\tcrash\t0\t0\n"
    )

    canonical = read_ledger_compatibility(path)
    header, bootstrap_rows = read_ledger(path)
    commits = load_ledger_commits(path)
    values = load_ledger_values(path)
    rows = load_ledger_rows(path)

    assert header == list(canonical.header)
    assert bootstrap_rows == [dict(row) for row in canonical.raw_rows]
    assert commits == canonical.commits == frozenset({"rev-a:arm", "rev-b:arm"})
    assert values == canonical.metric_values
    assert values == {"rev-a:arm": 0.72}
    assert set(rows) == set(canonical.measurements) == {"rev-a:arm"}
    assert rows["rev-a:arm"].value == 0.72
    assert rows["rev-a:arm"].throughput == 10.0
    assert rows["rev-a:arm"].diff_lines == 3.0
    assert rows["rev-a:arm"].status == "ok"


def test_metric_only_projection_keeps_a_value_that_the_verdict_projection_cannot_use(
    tmp_path: Path,
) -> None:
    path = tmp_path / "partial.tsv"
    path.write_text(
        "commit\tmetric_value\tthroughput\tdiff_lines\tstatus\n"
        "rev-a:arm\t0.72\tnot-recorded\t3\tok\n"
    )

    assert load_ledger_values(path) == {"rev-a:arm": 0.72}
    assert load_ledger_rows(path) == {}


def test_malformed_verdict_row_does_not_reserve_fair_key_before_valid_retry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "retry.tsv"
    path.write_text(
        "commit\tmetric_value\tthroughput\tdiff_lines\tstatus\n"
        "rev-a:arm\t0.90\tnot-recorded\t2\tok\n"
        "rev-a:arm\t0.72\t10\t3\tok\n"
    )

    rows = load_ledger_rows(path)
    assert set(rows) == {"rev-a:arm"}
    assert rows["rev-a:arm"].value == 0.72


def _outcome(reader, path: Path):
    try:
        value = reader(path)
    except (StopIteration, RegistryValidationError) as exc:
        return (type(exc).__name__, getattr(exc, "field", None))
    if reader is read_ledger:
        header, rows = value
        return (header, rows)
    if reader is load_ledger_rows:
        return {key: (row.value, row.throughput, row.diff_lines, row.status)
                for key, row in value.items()}
    return value


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            "",
            (("StopIteration", None), frozenset(), {}, ("RegistryValidationError", "ledger")),
        ),
        (
            "\nrev-a\t0.7\t3\t4\tok\n",
            (([], [{}]), frozenset({"rev-a"}), {"rev-a": 0.7},
             ("RegistryValidationError", "commit")),
        ),
        (
            "commit\tmetric_value\tthroughput\tdiff_lines\tstatus\n"
            "rev-a\tbad\t3\t4\terrored\nrev-a\t0.7\t3\t4\tok\n",
            ((["commit", "metric_value", "throughput", "diff_lines", "status"], [
                {"commit": "rev-a", "metric_value": "bad", "throughput": "3",
                 "diff_lines": "4", "status": "errored"},
                {"commit": "rev-a", "metric_value": "0.7", "throughput": "3",
                 "diff_lines": "4", "status": "ok"},
            ]), frozenset({"rev-a"}), {"rev-a": 0.7},
             {"rev-a": (0.7, 3.0, 4.0, "ok")}),
        ),
        (
            "commit\tmetric_value\tthroughput\tdiff_lines\tstatus\n"
            "rev-a\t0.9\tbad\t4\tok\nrev-a\t0.7\t3\t4\tok\n",
            ((["commit", "metric_value", "throughput", "diff_lines", "status"], [
                {"commit": "rev-a", "metric_value": "0.9", "throughput": "bad",
                 "diff_lines": "4", "status": "ok"},
                {"commit": "rev-a", "metric_value": "0.7", "throughput": "3",
                 "diff_lines": "4", "status": "ok"},
            ]), frozenset({"rev-a"}), ("RegistryValidationError", "commit"),
             {"rev-a": (0.7, 3.0, 4.0, "ok")}),
        ),
        (
            "commit\tmetric_value\tthroughput\tdiff_lines\tstatus\n"
            "rev-a\t0.9\t3\t4\tbudget_exhausted\nrev-a\t0.7\t3\t5\tok\n",
            ((["commit", "metric_value", "throughput", "diff_lines", "status"], [
                {"commit": "rev-a", "metric_value": "0.9", "throughput": "3",
                 "diff_lines": "4", "status": "budget_exhausted"},
                {"commit": "rev-a", "metric_value": "0.7", "throughput": "3",
                 "diff_lines": "5", "status": "ok"},
            ]), frozenset({"rev-a"}), {"rev-a": 0.7},
             {"rev-a": (0.7, 3.0, 5.0, "ok")}),
        ),
        (
            "commit\tmetric_value\tthroughput\tdiff_lines\tstatus\n"
            "rev-a\t0.9\t3\t4\tok\nrev-a\t0.7\t3\t5\tok\n",
            ((["commit", "metric_value", "throughput", "diff_lines", "status"], [
                {"commit": "rev-a", "metric_value": "0.9", "throughput": "3",
                 "diff_lines": "4", "status": "ok"},
                {"commit": "rev-a", "metric_value": "0.7", "throughput": "3",
                 "diff_lines": "5", "status": "ok"},
            ]), frozenset({"rev-a"}), ("RegistryValidationError", "commit"),
             ("RegistryValidationError", "commit")),
        ),
    ],
    ids=("empty", "blank-header", "unscored-retry", "bad-throughput-retry",
         "unfair-retry", "duplicate-fair"),
)
def test_all_four_wrappers_preserve_malformed_and_duplicate_matrix(
    tmp_path: Path, payload: str, expected: tuple[object, object, object, object],
) -> None:
    path = tmp_path / "matrix.tsv"
    path.write_text(payload)
    readers = (read_ledger, load_ledger_commits, load_ledger_values, load_ledger_rows)
    assert tuple(_outcome(reader, path) for reader in readers) == expected


def test_cli_refuses_missing_header_column_before_consuming_malformed_body(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-throughput.tsv"
    path.write_text(
        "commit\tmetric_value\tdiff_lines\n"
        f"rev-a\t0.7\t{'x' * 200_000}\n"
    )

    with pytest.raises(RegistryValidationError) as excinfo:
        load_ledger_rows(path)
    assert excinfo.value.field == "throughput"
