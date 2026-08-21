from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_factory.af_clean import p6_artifact_projections as driver
from agent_factory.af_clean.executable_diff import ExecutableDiffRefused
from agent_factory.af_clean.findings import CLASS_CONSOLIDATION, admit_finding


def test_p6_encodes_exactly_three_admitted_located_findings() -> None:
    findings = driver.p6_findings()
    assert len(findings) == 3
    assert {(item.location.file, item.location.line) for item in findings} == set(driver.P6_LOCATIONS)
    assert {item.rule for item in findings} == {driver.P6_RULE}
    assert {item.change_class for item in findings} == {CLASS_CONSOLIDATION}
    assert all(admit_finding(item).admitted for item in findings)
    repo = Path(__file__).resolve().parents[2]
    expected_classes = {
        "knowledge/ml_registry/manifests.py": "class ManifestRegistry:",
        "knowledge/ml_registry/artifact_cache.py": "class ArtifactCacheIndex:",
        "knowledge/ml_registry/portfolio.py": "class Portfolio:",
    }
    for file, line in driver.P6_LOCATIONS:
        assert (repo / file).read_text(encoding="utf-8").splitlines()[line - 1] == expected_classes[file]


def test_p6_patch_boundary_is_exactly_three_owners_plus_the_golden() -> None:
    assert driver.P6_DIFF_ALLOWLIST == frozenset({
        "knowledge/ml_registry/manifests.py",
        "knowledge/ml_registry/artifact_cache.py",
        "knowledge/ml_registry/portfolio.py",
        "knowledge/ml_registry/tests/test_artifact_projection_golden.py",
    })
    assert len(driver.P6_DIFF_ALLOWLIST) == 4
    assert "knowledge/ml_registry/storage/artifact_store.py" not in driver.P6_DIFF_ALLOWLIST
    assert "knowledge/ml_registry/storage/projections.py" not in driver.P6_DIFF_ALLOWLIST
    assert not any("fixtures/artifact_projections" in path for path in driver.P6_DIFF_ALLOWLIST)
    assert not any("services/finalize" in path for path in driver.P6_DIFF_ALLOWLIST)


def test_p6_witnesses_pin_golden_first_then_the_full_registry() -> None:
    assert tuple(command.argv for command in driver.P6_WITNESSES) == (
        (
            "env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
            "knowledge/ml_registry/tests/test_artifact_projection_golden.py",
            "-q", "-p", "no:cacheprovider",
        ),
        (
            "env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
            "knowledge/ml_registry/tests", "-q", "-p", "no:cacheprovider",
        ),
    )


def test_generates_only_the_allowlisted_revision_diff(tmp_path: Path, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout="diff --git a/a b/a\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert driver.generate_prebuilt_diff(tmp_path, "candidate", base_ref="base").startswith("diff")
    assert seen["argv"] == [
        "git", "diff", "--no-ext-diff", "--binary", "base", "candidate", "--",
        *sorted(driver.P6_DIFF_ALLOWLIST),
    ]


def test_apply_pins_every_safety_input_and_carries_no_reasoning(tmp_path: Path, monkeypatch) -> None:
    patch = tmp_path / "p6.diff"
    patch.write_text("diff --git a/a b/a\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def apply(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(applied_paths=(), witnesses_run=2, change_class=CLASS_CONSOLIDATION)

    monkeypatch.setattr(driver, "apply_bounded_executable_diff", apply)
    result = driver.apply_p6_diff(tmp_path, patch, verifier_runner="blind-runner")
    assert result.witnesses_run == 2
    assert captured["expected_rule"] == driver.P6_RULE
    assert captured["expected_locations"] == frozenset(driver.P6_LOCATIONS)
    assert captured["diff_allowlist"] == driver.P6_DIFF_ALLOWLIST
    assert captured["witnesses"] == driver.P6_WITNESSES
    assert captured["change_class"] == CLASS_CONSOLIDATION
    assert not {"reasoning", "rationale", "witness_output", "stdout", "stderr"}.intersection(captured)


def test_apply_refuses_overrides_of_every_safety_constant(tmp_path: Path) -> None:
    patch = tmp_path / "p6.diff"
    patch.write_text("diff --git a/a b/a\n", encoding="utf-8")
    for key in (
        "diff", "findings", "expected_rule", "expected_locations", "diff_allowlist",
        "witnesses", "change_class",
    ):
        with pytest.raises(TypeError, match="cannot be overridden"):
            driver.apply_p6_diff(tmp_path, patch, **{key: object()})


def test_apply_refuses_a_candidate_with_any_extra_path(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=tmp_path, check=True)
    (tmp_path / "base").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    patch = tmp_path / "p6.diff"
    patch.write_text(
        "diff --git a/knowledge/ml_registry/storage/artifact_store.py "
        "b/knowledge/ml_registry/storage/artifact_store.py\n"
        "--- a/knowledge/ml_registry/storage/artifact_store.py\n"
        "+++ b/knowledge/ml_registry/storage/artifact_store.py\n"
        "@@ -0,0 +1 @@\n+extra\n",
        encoding="utf-8",
    )
    with pytest.raises(ExecutableDiffRefused, match="diff paths differ from exact allowlist"):
        driver.apply_p6_diff(tmp_path, patch, verifier_runner=object())
