from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_factory.af_clean.executable_diff import ExecutableDiffResult
from agent_factory.af_clean.findings import CLASS_SPLIT
from agent_factory.af_clean.p8_cli_split import (
    P8_DIFF_ALLOWLIST,
    P8_LOCATIONS,
    P8_RULE,
    P8_WITNESSES,
    apply_p8_diff,
    generate_prebuilt_diff,
    p8_findings,
    read_prebuilt_diff,
)


def test_p8_boundary_is_exact_and_split_specific() -> None:
    findings = p8_findings()
    assert len(findings) == 1
    assert findings[0].rule == P8_RULE
    assert findings[0].change_class == CLASS_SPLIT
    assert {(item.location.file, item.location.line) for item in findings} == set(P8_LOCATIONS)
    assert len(P8_DIFF_ALLOWLIST) == 9
    assert len(P8_WITNESSES) == 2


def test_generate_diff_preserves_rename_detection(monkeypatch, tmp_path: Path) -> None:
    observed = {}

    def run(argv, **kwargs):
        observed["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="diff --git a/a b/a\n", stderr="")

    monkeypatch.setattr("agent_factory.af_clean.p8_cli_split.subprocess.run", run)
    assert generate_prebuilt_diff(tmp_path, "candidate")
    assert "--find-renames" in observed["argv"]
    assert set(observed["argv"][-len(P8_DIFF_ALLOWLIST):]) == set(P8_DIFF_ALLOWLIST)


def test_read_prebuilt_diff_refuses_missing_and_empty(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a file"):
        read_prebuilt_diff(tmp_path / "missing.diff")
    empty = tmp_path / "empty.diff"
    empty.write_text("\n")
    with pytest.raises(ValueError, match="empty"):
        read_prebuilt_diff(empty)


def test_apply_forwards_only_the_fixed_boundary(monkeypatch, tmp_path: Path) -> None:
    patch = tmp_path / "candidate.diff"
    patch.write_text("diff --git a/knowledge/ml_registry/cli.py b/knowledge/ml_registry/cli.py\n")
    observed = {}

    def apply(**kwargs):
        observed.update(kwargs)
        return ExecutableDiffResult(("knowledge/ml_registry/cli.py",), 2, CLASS_SPLIT)

    monkeypatch.setattr("agent_factory.af_clean.p8_cli_split.apply_bounded_executable_diff", apply)
    result = apply_p8_diff(tmp_path, patch)
    assert result.change_class == CLASS_SPLIT
    assert observed["expected_rule"] == P8_RULE
    assert observed["expected_locations"] == frozenset(P8_LOCATIONS)
    assert observed["diff_allowlist"] == P8_DIFF_ALLOWLIST
    assert observed["witnesses"] == P8_WITNESSES
    assert observed["allow_renames"] is True


@pytest.mark.parametrize("name", [
    "diff", "findings", "expected_rule", "expected_locations", "diff_allowlist",
    "witnesses", "change_class",
])
def test_apply_refuses_safety_boundary_overrides(tmp_path: Path, name: str) -> None:
    patch = tmp_path / "candidate.diff"
    patch.write_text("diff --git a/knowledge/ml_registry/cli.py b/knowledge/ml_registry/cli.py\n")
    with pytest.raises(TypeError, match="cannot be overridden"):
        apply_p8_diff(tmp_path, patch, **{name: object()})
