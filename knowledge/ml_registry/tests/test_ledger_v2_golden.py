from __future__ import annotations

from pathlib import Path

import pytest

from knowledge.ml_registry.bootstrap import read_ledger
from knowledge.ml_registry.cli import load_ledger_rows
from knowledge.ml_registry.contracts import LedgerV2
from knowledge.ml_registry.floor import load_ledger_values
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
