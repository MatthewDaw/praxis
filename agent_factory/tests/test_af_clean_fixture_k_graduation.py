from __future__ import annotations

from types import SimpleNamespace

from agent_factory.af_clean.executable_diff import ExecutableDiffResult
from agent_factory.af_clean.fixture_k_graduation import (
    DIFF_ALLOWLIST,
    LOCATIONS,
    PATH,
    RULE,
    WITNESSES,
    apply,
    findings,
    reverse_correction_diff,
)
from agent_factory.af_clean.findings import CLASS_TEST_GRADUATION


def test_boundary_is_exact_and_class_correct() -> None:
    assert {(item.location.file, item.location.line) for item in findings()} == set(LOCATIONS)
    assert {item.change_class for item in findings()} == {CLASS_TEST_GRADUATION}
    assert DIFF_ALLOWLIST == {PATH}
    assert len(WITNESSES) == 3


def test_reverse_diff_is_marker_file_only(monkeypatch, tmp_path) -> None:
    observed = {}

    def run(argv, **kwargs):
        observed["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=f"diff --git a/{PATH} b/{PATH}\n", stderr="")

    monkeypatch.setattr("agent_factory.af_clean.fixture_k_graduation.subprocess.run", run)
    assert reverse_correction_diff(tmp_path, "correction")
    assert observed["argv"][-1] == PATH
    assert observed["argv"][-4:-2] == ["correction", "correction^"]


def test_apply_forwards_immutable_boundary(monkeypatch, tmp_path) -> None:
    observed = {}
    monkeypatch.setattr(
        "agent_factory.af_clean.fixture_k_graduation.reverse_correction_diff",
        lambda root, ref: f"diff --git a/{PATH} b/{PATH}\n",
    )

    def bounded(**kwargs):
        observed.update(kwargs)
        return ExecutableDiffResult((PATH,), 3, CLASS_TEST_GRADUATION)

    monkeypatch.setattr(
        "agent_factory.af_clean.fixture_k_graduation.apply_bounded_executable_diff", bounded,
    )
    result = apply(tmp_path, "correction")
    assert result.change_class == CLASS_TEST_GRADUATION
    assert observed["expected_rule"] == RULE
    assert observed["expected_locations"] == frozenset(LOCATIONS)
    assert observed["witnesses"] == WITNESSES
