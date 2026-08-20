from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_factory.af_clean.executable_diff import (
    ExecutableDiffRefused,
    WitnessCommand,
    apply_bounded_executable_diff,
)
from agent_factory.af_clean.findings import CLASS_CONSOLIDATION, Finding, Location


PATCH = """diff --git a/x.py b/x.py
index 5626abf..f719efd 100644
--- a/x.py
+++ b/x.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "x.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "x.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def _finding(*, rule: str = "ledger-reader-consolidation", line: int = 1) -> Finding:
    return Finding(
        rule=rule,
        tier="judgment",
        location=Location(file="x.py", line=line),
        pole="fragmentation",
        change_class=CLASS_CONSOLIDATION,
        chunks=("header policy", "duplicate keys"),
        is_dry=True,
        observable="co-change",
        proposal="merge readers without per-caller flags",
    )


def _verifier(ids=("h1",)):
    def run(argv, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"endorsed_hunk_ids": list(ids)}),
            stderr="",
        )
    return run


def _apply(repo: Path, **overrides):
    kwargs = {
        "repo_root": repo,
        "diff": PATCH,
        "findings": [_finding()],
        "expected_rule": "ledger-reader-consolidation",
        "expected_locations": frozenset({("x.py", 1)}),
        "diff_allowlist": frozenset({"x.py"}),
        "witnesses": (WitnessCommand(("python", "-c", "from pathlib import Path; assert Path('x.py').read_text() == 'VALUE = 2\\n'")),),
        "change_class": CLASS_CONSOLIDATION,
        "verifier_runner": _verifier(),
    }
    kwargs.update(overrides)
    return apply_bounded_executable_diff(**kwargs)


def test_applies_only_after_isolated_witness_and_blind_endorsement(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    seen: dict[str, object] = {}

    def verifier(argv, **kwargs):
        payload = json.loads(kwargs["input"])
        seen["payload"] = payload
        seen["tree_during_verification"] = (repo / "x.py").read_text()
        seen["system_prompt"] = argv[-1]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"endorsed_hunk_ids": ["h1"]}),
            stderr="",
        )

    result = _apply(repo, verifier_runner=verifier)

    assert (repo / "x.py").read_text() == "VALUE = 2\n"
    assert result.applied_paths == ("x.py",)
    assert result.witnesses_run == 1
    assert seen["tree_during_verification"] == "VALUE = 1\n"
    assert set(seen["payload"]) == {"diff", "repo_path"}
    assert seen["payload"]["diff"] == PATCH
    assert "former call site" in str(seen["system_prompt"]).lower()
    assert subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip() == "1"


@pytest.mark.parametrize("ids", [(), ("h2",), ("h1", "h2")])
def test_missing_or_ambiguous_verifier_endorsement_applies_nothing(tmp_path: Path, ids) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(ExecutableDiffRefused, match="did not affirmatively endorse"):
        _apply(repo, verifier_runner=_verifier(ids))
    assert (repo / "x.py").read_text() == "VALUE = 1\n"
    assert subprocess.run(["git", "status", "--short"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout == ""


def test_failed_witness_applies_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(ExecutableDiffRefused, match="tier-2 witness"):
        _apply(repo, witnesses=(WitnessCommand(("python", "-c", "raise SystemExit(9)")),))
    assert (repo / "x.py").read_text() == "VALUE = 1\n"


def test_blind_verifier_cannot_receive_witness_output(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    secret = "witness-only-secret"
    verifier_inputs: dict[str, str] = {}

    def command_runner(argv, **kwargs):
        if argv[:2] == ["python", "-c"]:
            return SimpleNamespace(returncode=0, stdout=secret, stderr=f"{secret}-stderr")
        return subprocess.run(argv, **kwargs)

    def verifier(argv, **kwargs):
        verifier_inputs["argv"] = " ".join(argv)
        verifier_inputs["input"] = kwargs["input"]
        verifier_inputs["env"] = json.dumps(kwargs.get("env", {}), sort_keys=True)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"endorsed_hunk_ids": ["h1"]}),
            stderr="",
        )

    _apply(repo, command_runner=command_runner, verifier_runner=verifier)

    assert all(secret not in value for value in verifier_inputs.values())


def test_refuses_unexpected_dirty_path_before_cloning_or_verification(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "unrelated.txt").write_text("mine\n")
    called = False

    def verifier(argv, **kwargs):
        nonlocal called
        called = True
        return _verifier()(argv, **kwargs)

    with pytest.raises(ExecutableDiffRefused, match="unexpected dirty paths"):
        _apply(repo, verifier_runner=verifier)
    assert called is False
    assert (repo / "x.py").read_text() == "VALUE = 1\n"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"findings": []}, "locations/count differ"),
        ({"findings": [_finding(rule="other")]}, "unexpected finding rule"),
        ({"expected_locations": frozenset({("x.py", 2)})}, "locations/count differ"),
        ({"diff_allowlist": frozenset({"x.py", "y.py"})}, "diff paths differ"),
        ({"witnesses": ()}, "witness commands are required"),
    ],
)
def test_exact_finding_count_rule_location_diff_and_witnesses_are_mandatory(
    tmp_path: Path, override, message: str
) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(ExecutableDiffRefused, match=message):
        _apply(repo, **override)
    assert (repo / "x.py").read_text() == "VALUE = 1\n"


def test_blind_verification_cannot_be_disabled(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(ExecutableDiffRefused, match="cannot be skipped"):
        _apply(repo, verifier_runner=None)
    assert (repo / "x.py").read_text() == "VALUE = 1\n"
