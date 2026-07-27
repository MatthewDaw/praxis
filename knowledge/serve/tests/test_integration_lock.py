"""The ``integration-serialized-per-repo`` build check: two same-repo jobs finishing
simultaneously never interleave their reset/merge/publish sequences, a stale lock is
reclaimable, and integration refuses — never resets — a dirty or unpushed main worktree."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from knowledge.serve.box_service_integrate import (
    IntegrationLockedError,
    IntegrationTarget,
    MainWorktreeDirtyError,
    RepoIntegrationLock,
    run_integration_sequence,
)


@dataclass
class Proc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def make_target(**overrides) -> IntegrationTarget:
    defaults = dict(
        main_worktree_path="/repos/widgets/main",
        origin_repo="git@github.com:acme/widgets.git",
        allowlisted_origin="git@github.com:acme/widgets.git",
        job_branch="job/job-1",
        pr_base="main",
        integration_ref="refs/heads/integrate/job-1",
    )
    defaults.update(overrides)
    return IntegrationTarget(**defaults)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@dataclass
class ScriptedRunner:
    """A fake ``subprocess.run`` returning a canned result per git subcommand, with an optional
    hook fired right after ``reset`` so a test can attempt a second, concurrent call mid-sequence."""

    statuses: str = ""
    unpushed_log: str = ""
    merge_ok: bool = True
    on_reset: object = None
    calls: list = field(default_factory=list)

    def __call__(self, args, cwd, capture_output=True, text=True, check=False):
        self.calls.append((tuple(args), cwd))
        sub = args[1] if len(args) > 1 else None
        if sub == "status":
            return Proc(stdout=self.statuses)
        if sub == "fetch":
            return Proc()
        if sub == "log":
            return Proc(stdout=self.unpushed_log)
        if sub == "reset":
            if self.on_reset is not None:
                self.on_reset()
            return Proc()
        if sub == "merge" and "--abort" not in args:
            return Proc(returncode=0 if self.merge_ok else 1, stderr="" if self.merge_ok else "conflict")
        if sub == "merge" and "--abort" in args:
            return Proc()
        if sub == "rev-parse":
            return Proc(stdout="deadbeef\n")
        if sub == "push":
            return Proc()
        raise AssertionError(f"unexpected git call: {args}")


def fake_pr_creator(target, merged_sha):
    return "https://github.com/acme/widgets/pull/1"


def test_lock_refuses_a_second_live_holder_for_the_same_repo():
    lock = RepoIntegrationLock()

    assert lock.acquire("repo-a", "holder-1") is True
    assert lock.acquire("repo-a", "holder-2") is False
    # A different repo is unaffected.
    assert lock.acquire("repo-b", "holder-2") is True


def test_stale_lock_is_reclaimable_by_a_new_holder():
    clock = FakeClock()
    lock = RepoIntegrationLock(clock=clock, ttl=10.0)
    lock.acquire("repo-a", "holder-1")

    clock.now += 11.0

    assert lock.acquire("repo-a", "holder-2") is True
    assert lock.held_by("repo-a") == "holder-2"


def test_heartbeat_keeps_a_lock_live_past_its_ttl():
    clock = FakeClock()
    lock = RepoIntegrationLock(clock=clock, ttl=10.0)
    lock.acquire("repo-a", "holder-1")

    clock.now += 9.0
    assert lock.heartbeat("repo-a", "holder-1") is True
    clock.now += 9.0

    assert lock.acquire("repo-a", "holder-2") is False


def test_release_frees_the_lock_for_a_new_holder():
    lock = RepoIntegrationLock()
    lock.acquire("repo-a", "holder-1")

    assert lock.release("repo-a", "holder-1") is True

    assert lock.acquire("repo-a", "holder-2") is True


def test_two_same_repo_integrations_do_not_interleave():
    lock = RepoIntegrationLock()
    target = make_target()
    attempted_during_first = {}

    def try_second_acquire_mid_sequence():
        attempted_during_first["locked_out"] = not lock.acquire(
            target.main_worktree_path, "holder-2"
        )

    runner = ScriptedRunner(on_reset=try_second_acquire_mid_sequence)

    result = run_integration_sequence(
        target, holder_id="holder-1", lock=lock, runner=runner, pr_creator=fake_pr_creator
    )

    assert attempted_during_first["locked_out"] is True
    assert result.merged_sha == "deadbeef"
    # The lock is released once the whole sequence completes.
    assert lock.acquire(target.main_worktree_path, "holder-2") is True


def test_a_different_live_holder_is_refused_outright():
    lock = RepoIntegrationLock()
    lock.acquire("/repos/widgets/main", "someone-else")
    target = make_target()
    runner = ScriptedRunner()

    with pytest.raises(IntegrationLockedError):
        run_integration_sequence(
            target, holder_id="holder-1", lock=lock, runner=runner, pr_creator=fake_pr_creator
        )
    # Nothing was run — the lock refusal happens before any git call.
    assert runner.calls == []


def test_refuses_a_dirty_main_worktree_and_leaves_it_untouched():
    lock = RepoIntegrationLock()
    target = make_target()
    runner = ScriptedRunner(statuses=" M some/file.py\n")

    with pytest.raises(MainWorktreeDirtyError):
        run_integration_sequence(
            target, holder_id="holder-1", lock=lock, runner=runner, pr_creator=fake_pr_creator
        )

    assert not any(call[0][1] == "reset" for call in runner.calls)
    # The lock is released even on refusal, so a later attempt is not stranded.
    assert lock.acquire(target.main_worktree_path, "holder-2") is True


def test_refuses_a_main_worktree_holding_an_unpushed_commit():
    lock = RepoIntegrationLock()
    target = make_target()
    runner = ScriptedRunner(unpushed_log="abc123 a local commit\n")

    with pytest.raises(MainWorktreeDirtyError):
        run_integration_sequence(
            target, holder_id="holder-1", lock=lock, runner=runner, pr_creator=fake_pr_creator
        )

    assert not any(call[0][1] == "reset" for call in runner.calls)
