from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_factory.af_clean.findings import CLASS_CONSOLIDATION, admit_finding
from agent_factory.af_clean import p1_ledger_readers as driver


def test_p1_encodes_exactly_four_admitted_located_findings() -> None:
    findings = driver.p1_findings()

    assert len(findings) == 4
    assert {(item.location.file, item.location.line) for item in findings} == set(driver.P1_LOCATIONS)
    assert {item.rule for item in findings} == {driver.P1_RULE}
    assert {item.change_class for item in findings} == {CLASS_CONSOLIDATION}
    assert all(admit_finding(item).admitted for item in findings)


def test_p1_future_patch_and_witness_boundaries_are_exact() -> None:
    assert driver.P1_DIFF_ALLOWLIST == frozenset({
        "knowledge/ml_registry/bootstrap.py",
        "knowledge/ml_registry/write_path.py",
        "knowledge/ml_registry/floor.py",
        "knowledge/ml_registry/cli.py",
        "knowledge/ml_registry/contracts/ledger_v2.py",
        "knowledge/ml_registry/tests/test_ledger_v2_golden.py",
    })
    assert tuple(command.argv for command in driver.P1_WITNESSES) == (
        (
            "env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
            "knowledge/ml_registry/tests/test_ledger_v2_golden.py", "-q", "-p", "no:cacheprovider",
        ),
        (
            "env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
            "knowledge/ml_registry/tests", "-q", "-p", "no:cacheprovider",
        ),
    )


def test_reads_utf8_prebuilt_diff_and_rejects_missing_empty_or_binary(tmp_path: Path) -> None:
    patch = tmp_path / "p1.diff"
    patch.write_text("diff --git a/a b/a\n", encoding="utf-8")
    assert driver.read_prebuilt_diff(patch) == "diff --git a/a b/a\n"

    with pytest.raises(ValueError, match="not a file"):
        driver.read_prebuilt_diff(tmp_path / "missing.diff")
    patch.write_text(" \n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        driver.read_prebuilt_diff(patch)
    patch.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="not UTF-8"):
        driver.read_prebuilt_diff(patch)


def test_generates_reproducible_diff_only_for_the_allowlist(tmp_path: Path, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="diff --git a/a b/a\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert driver.generate_prebuilt_diff(tmp_path, "candidate", base_ref="base").startswith("diff")
    assert seen["argv"] == [
        "git", "diff", "--no-ext-diff", "--binary", "base", "candidate", "--",
        *sorted(driver.P1_DIFF_ALLOWLIST),
    ]
    assert seen["kwargs"] == {"cwd": tmp_path.resolve(), "capture_output": True, "text": True}


def test_apply_driver_pins_boundary_and_exposes_no_reasoning_or_witness_output(
    tmp_path: Path, monkeypatch
) -> None:
    patch = tmp_path / "p1.diff"
    patch.write_text("diff --git a/a b/a\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def apply(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(applied_paths=(), witnesses_run=2, change_class=CLASS_CONSOLIDATION)

    monkeypatch.setattr(driver, "apply_bounded_executable_diff", apply)
    result = driver.apply_p1_diff(tmp_path, patch, verifier_runner="blind-runner")

    assert result.witnesses_run == 2
    assert captured["expected_rule"] == driver.P1_RULE
    assert captured["expected_locations"] == frozenset(driver.P1_LOCATIONS)
    assert captured["diff_allowlist"] == driver.P1_DIFF_ALLOWLIST
    assert captured["witnesses"] == driver.P1_WITNESSES
    assert captured["change_class"] == CLASS_CONSOLIDATION
    # There is no driver parameter or adapter payload field for rationale, findings reasoning,
    # witness stdout, or witness stderr.  The blind verifier receives only the adapter's diff/repo.
    assert not {"reasoning", "rationale", "witness_output", "stdout", "stderr"}.intersection(captured)


def test_apply_driver_refuses_overrides_of_any_p1_safety_constant(tmp_path: Path) -> None:
    patch = tmp_path / "p1.diff"
    patch.write_text("diff --git a/a b/a\n", encoding="utf-8")
    for key in (
        "diff", "findings", "expected_rule", "expected_locations", "diff_allowlist",
        "witnesses", "change_class",
    ):
        with pytest.raises(TypeError, match="cannot be overridden"):
            driver.apply_p1_diff(tmp_path, patch, **{key: object()})
