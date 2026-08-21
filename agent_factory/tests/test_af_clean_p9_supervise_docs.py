from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_factory.af_clean.executable_diff import ExecutableDiffResult
from agent_factory.af_clean.findings import CLASS_DOCS_REWRITE
from agent_factory.af_clean.p9_supervise_docs import (
    P9_DIFF_ALLOWLIST,
    P9_LOCATIONS,
    P9_RULE,
    P9_WITNESSES,
    apply_p9_candidate,
    apply_p9_diff,
    generate_prebuilt_diff,
    p9_findings,
    read_prebuilt_diff,
)


def test_p9_boundary_is_exact_and_docs_specific() -> None:
    findings = p9_findings()
    assert len(findings) == 1
    assert findings[0].rule == P9_RULE
    assert findings[0].change_class == CLASS_DOCS_REWRITE
    assert {(item.location.file, item.location.line) for item in findings} == set(P9_LOCATIONS)
    assert len(P9_DIFF_ALLOWLIST) == 2
    assert len(P9_WITNESSES) == 3


def test_generate_diff_is_bounded_to_p9_paths(monkeypatch, tmp_path: Path) -> None:
    observed = {}

    def run(argv, **kwargs):
        observed["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="diff --git a/a b/a\n", stderr="")

    monkeypatch.setattr("agent_factory.af_clean.p9_supervise_docs.subprocess.run", run)
    assert generate_prebuilt_diff(tmp_path, "candidate")
    assert set(observed["argv"][-len(P9_DIFF_ALLOWLIST):]) == set(P9_DIFF_ALLOWLIST)


def test_read_prebuilt_diff_refuses_missing_and_empty(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a file"):
        read_prebuilt_diff(tmp_path / "missing.diff")
    empty = tmp_path / "empty.diff"
    empty.write_text("\n")
    with pytest.raises(ValueError, match="empty"):
        read_prebuilt_diff(empty)


def test_apply_forwards_only_fixed_p9_boundary(monkeypatch, tmp_path: Path) -> None:
    patch = tmp_path / "candidate.diff"
    patch.write_text("diff --git a/agent_factory/skills/af-ml-supervise/SKILL.md "
                     "b/agent_factory/skills/af-ml-supervise/SKILL.md\n")
    observed = {}

    def apply(**kwargs):
        observed.update(kwargs)
        return ExecutableDiffResult(tuple(sorted(P9_DIFF_ALLOWLIST)), 3, CLASS_DOCS_REWRITE)

    monkeypatch.setattr(
        "agent_factory.af_clean.p9_supervise_docs.apply_bounded_executable_diff", apply,
    )
    result = apply_p9_diff(tmp_path, patch)
    assert result.change_class == CLASS_DOCS_REWRITE
    assert observed["expected_rule"] == P9_RULE
    assert observed["expected_locations"] == frozenset(P9_LOCATIONS)
    assert observed["diff_allowlist"] == P9_DIFF_ALLOWLIST
    assert observed["witnesses"] == P9_WITNESSES


def test_apply_candidate_generates_then_forwards_fixed_boundary(monkeypatch, tmp_path: Path) -> None:
    observed = {}
    monkeypatch.setattr(
        "agent_factory.af_clean.p9_supervise_docs.generate_prebuilt_diff",
        lambda root, ref: "diff --git a/a b/a\n",
    )

    def apply(**kwargs):
        observed.update(kwargs)
        return ExecutableDiffResult(tuple(sorted(P9_DIFF_ALLOWLIST)), 3, CLASS_DOCS_REWRITE)

    monkeypatch.setattr(
        "agent_factory.af_clean.p9_supervise_docs.apply_bounded_executable_diff", apply,
    )
    result = apply_p9_candidate(tmp_path, "candidate")
    assert result.change_class == CLASS_DOCS_REWRITE
    assert observed["diff"] == "diff --git a/a b/a\n"
    assert observed["findings"] == p9_findings()
    assert observed["change_class"] == CLASS_DOCS_REWRITE


@pytest.mark.parametrize("name", [
    "diff", "findings", "expected_rule", "expected_locations", "diff_allowlist",
    "witnesses", "change_class",
])
def test_apply_refuses_safety_boundary_overrides(tmp_path: Path, name: str) -> None:
    patch = tmp_path / "candidate.diff"
    patch.write_text("diff --git a/a b/a\n")
    with pytest.raises(TypeError, match="cannot be overridden"):
        apply_p9_diff(tmp_path, patch, **{name: object()})
