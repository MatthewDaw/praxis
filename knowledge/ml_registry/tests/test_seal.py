from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from knowledge.ml_registry.runtime.seal import (
    ProposerProfile,
    SealError,
    SeatbeltProfile,
    launch_proposer,
    launch_sealed_arm,
)


# Commands the fake sandbox reports as killed by the profile, standing in for the kernel refusing
# the syscall: the arm's two legs, and the proposer's three.
_REFUSED_COMMANDS = frozenset({
    "network-arm", "outside-write-arm",
    "sealed-read-proposer", "outside-write-proposer", "harness-write-proposer",
})


class FakeSeatbelt:
    def __init__(self, *, failed_control: str | None = None) -> None:
        self.failed_control = failed_control
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        self.calls.append((argv, environment))
        command = argv[4:]
        if command[:2] == [sys.executable, "-c"]:
            leg = command[-1]
            return subprocess.CompletedProcess(argv, 1 if leg == self.failed_control else 0)
        if _REFUSED_COMMANDS.intersection(command):
            return subprocess.CompletedProcess(argv, 1)
        predictions_file = environment.get("PRAXIS_PREDICTIONS_FILE")
        if predictions_file is None:
            return subprocess.CompletedProcess(argv, 0)  # a proposer emits no predictions artifact
        Path(predictions_file).write_text("row_id,prediction\n1,0.75\n")
        return subprocess.CompletedProcess(argv, 0)


def _sealed(tmp_path: Path) -> Path:
    sealed = tmp_path / "sealed.csv"
    sealed.write_text("label\n1\n")
    return sealed


def _profile(tmp_path: Path) -> SeatbeltProfile:
    predictions = tmp_path / "predictions"
    predictions.mkdir()
    return SeatbeltProfile.create(sealed_file=_sealed(tmp_path), predictions_dir=predictions)


def _proposer_profile(tmp_path: Path) -> ProposerProfile:
    """The proposer's real shape: the scorer it must not touch lives inside its own worktree."""
    harness = tmp_path / "arm" / "harness"
    harness.mkdir(parents=True)
    return ProposerProfile.create(
        arm_worktree=tmp_path / "arm", sealed_file=_sealed(tmp_path), scoring_harness=harness,
    )


def test_profile_uses_canonical_paths_and_denies_network_and_other_writes(tmp_path: Path) -> None:
    profile = _profile(tmp_path)

    assert profile.sealed_file == Path(str(profile.sealed_file.resolve()))
    assert profile.predictions_dir == Path(str(profile.predictions_dir.resolve()))
    assert "(deny network*)" in profile.text
    assert f'(deny file-read* (subpath "{profile.sealed_file}"))' in profile.text
    assert "(deny file-write*)" in profile.text
    assert f'(allow file-write* (subpath "{profile.predictions_dir}"))' in profile.text


def test_profile_rejects_a_noncanonical_path(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    sealed = real / "sealed.csv"
    sealed.write_text("label\n1\n")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    predictions = tmp_path / "predictions"
    predictions.mkdir()

    with pytest.raises(SealError, match="sealed_file must be canonical"):
        SeatbeltProfile(sealed_file=alias / "sealed.csv", predictions_dir=predictions)


@pytest.mark.parametrize("leg", ["sealed-read", "network-egress", "outside-write"])
def test_three_legged_positive_control_refuses_launch_when_any_leg_fails(
    tmp_path: Path, leg: str,
) -> None:
    runner = FakeSeatbelt(failed_control=leg)

    with pytest.raises(SealError, match=leg):
        launch_sealed_arm(
            _profile(tmp_path), ["valid-arm"], predictions_file="predictions.csv",
            cwd=tmp_path, runner=runner,
        )

    assert all(call[0][0:2] == ["/usr/bin/sandbox-exec", "-p"] for call in runner.calls)


@pytest.mark.parametrize("arm", ["network-arm", "outside-write-arm"])
def test_an_arm_that_violates_the_profile_fails(tmp_path: Path, arm: str) -> None:
    with pytest.raises(SealError, match="arm exited 1"):
        launch_sealed_arm(
            _profile(tmp_path), [arm], predictions_file="predictions.csv",
            cwd=tmp_path, runner=FakeSeatbelt(),
        )


def test_arm_can_emit_only_predictions_and_scoring_runs_outside_sandbox(tmp_path: Path) -> None:
    runner = FakeSeatbelt()
    profile = _profile(tmp_path)

    artifact = launch_sealed_arm(
        profile, ["valid-arm"], predictions_file="predictions.csv",
        cwd=tmp_path, runner=runner,
    )
    score = len(artifact.path.read_text().splitlines()) - 1

    assert artifact.path == profile.predictions_dir / "predictions.csv"
    assert score == 1
    assert len(runner.calls) == 4  # three controls plus the arm; the scorer was not sandboxed


def test_arm_is_rejected_if_it_emits_anything_besides_the_predictions_file(
    tmp_path: Path,
) -> None:
    runner = FakeSeatbelt()
    profile = _profile(tmp_path)

    def noisy_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        result = runner(argv, **kwargs)
        if argv[4:] == ["valid-arm"]:
            (profile.predictions_dir / "self-reported-score.json").write_text('{"score": 1}')
        return result

    with pytest.raises(SealError, match="unexpected output"):
        launch_sealed_arm(
            profile, ["valid-arm"], predictions_file="predictions.csv",
            cwd=tmp_path, runner=noisy_runner,
        )


def test_proposer_profile_confines_writes_and_denies_the_sealed_split(tmp_path: Path) -> None:
    profile = _proposer_profile(tmp_path)

    assert f'(deny file-read* (subpath "{profile.sealed_file}"))' in profile.text
    assert "(deny file-write*)" in profile.text
    assert f'(allow file-write* (subpath "{profile.arm_worktree}"))' in profile.text
    # The harness deny is LAST because Seatbelt takes the last matching rule: the scorer sits
    # inside the writable worktree, so an earlier deny would be overridden by the allow above it.
    assert profile.text.endswith(f'(deny file-write* (subpath "{profile.scoring_harness}"))')


@pytest.mark.parametrize(
    ("broken", "message"),
    [("noncanonical", "arm_worktree must be canonical"),
     ("sealed-inside-worktree", "arm_worktree must not contain the sealed file"),
     ("worktree-inside-harness", "scoring_harness must not contain the arm worktree")],
)
def test_proposer_profile_refuses_a_layout_it_cannot_confine(
    tmp_path: Path, broken: str, message: str,
) -> None:
    worktree = tmp_path / "arm"
    harness = worktree / "harness"
    harness.mkdir(parents=True)
    sealed = _sealed(tmp_path)
    if broken == "noncanonical":
        alias = tmp_path / "alias"
        alias.symlink_to(worktree, target_is_directory=True)
        worktree = alias
    elif broken == "sealed-inside-worktree":
        sealed = worktree / "sealed.csv"
        sealed.write_text("label\n1\n")
    else:
        harness = worktree.parent

    with pytest.raises(SealError, match=message):
        ProposerProfile(arm_worktree=worktree, sealed_file=sealed, scoring_harness=harness)


@pytest.mark.parametrize("leg", ["sealed-read", "outside-write", "harness-write"])
def test_three_legged_proposer_positive_control_refuses_launch_when_any_leg_fails(
    tmp_path: Path, leg: str,
) -> None:
    runner = FakeSeatbelt(failed_control=leg)

    with pytest.raises(SealError, match=leg):
        launch_proposer(_proposer_profile(tmp_path), ["valid-proposer"], runner=runner)

    assert all(call[0][0:2] == ["/usr/bin/sandbox-exec", "-p"] for call in runner.calls)


@pytest.mark.parametrize(
    "proposer", ["sealed-read-proposer", "outside-write-proposer", "harness-write-proposer"],
)
def test_a_proposer_that_violates_its_profile_fails(tmp_path: Path, proposer: str) -> None:
    runner = FakeSeatbelt()

    with pytest.raises(SealError, match="proposer exited 1"):
        launch_proposer(_proposer_profile(tmp_path), [proposer], runner=runner)

    assert len(runner.calls) == 4  # three controls asserted before the proposer ever ran
