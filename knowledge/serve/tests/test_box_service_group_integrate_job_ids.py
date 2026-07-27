"""Acceptance test for ticket R74 (026ae9c5abe5429ea98d14fc23a95dc2):

Given a merged group of three members, the commit message and pull-request body each name all
three job ids and branches, and those branches still exist (nothing in the sequence deletes a
member branch) until the pull request merges.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from knowledge.serve.box_service_group_integrate import (
    GroupIntegrationTarget,
    run_group_integration_sequence,
)
from knowledge.serve.box_service_integrate import RepoIntegrationLock


@dataclass
class Proc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class ScriptedRunner:
    calls: list = field(default_factory=list)

    def __call__(self, args, cwd, capture_output=True, text=True, check=False):
        self.calls.append((tuple(args), cwd))
        sub = args[1] if len(args) > 1 else None
        if sub in ("status", "fetch", "log", "reset", "commit", "push"):
            return Proc()
        if sub == "merge" and "--squash" in args:
            return Proc()
        if sub == "rev-parse":
            return Proc(stdout="cafef00d\n")
        raise AssertionError(f"unexpected git call: {args}")


def make_target(**overrides) -> GroupIntegrationTarget:
    defaults = dict(
        main_worktree_path="/repos/widgets/main",
        origin_repo="git@github.com:acme/widgets.git",
        member_branches=["job/job-1", "job/job-2", "job/job-3"],
        member_job_ids=["job-1", "job-2", "job-3"],
        pr_base="main",
        integration_ref="refs/heads/integrate/group-1",
    )
    defaults.update(overrides)
    return GroupIntegrationTarget(**defaults)


def test_commit_message_and_pr_body_name_every_member_job_id_and_branch():
    lock = RepoIntegrationLock()
    target = make_target()
    runner = ScriptedRunner()
    pr_bodies = []

    def pr_creator(t, sha):
        pr_bodies.append(t)
        return "https://github.com/acme/widgets/pull/9"

    run_group_integration_sequence(
        target, holder_id="holder-1", lock=lock, runner=runner, pr_creator=pr_creator,
    )

    commit_calls = [c for c in runner.calls if c[0][1] == "commit"]
    assert len(commit_calls) == 1
    commit_message = commit_calls[0][0][-1]
    for job_id, branch in zip(target.member_job_ids, target.member_branches):
        assert job_id in commit_message, f"commit message missing job id {job_id!r}: {commit_message!r}"
        assert branch in commit_message, f"commit message missing branch {branch!r}: {commit_message!r}"

    # The PR body (constructed by the default pr_creator, driven off the same target) must also
    # name every member job id and branch.
    from knowledge.serve.box_service_group_integrate import default_group_pr_creator

    captured_body = {}

    def capturing_pr_creator(t, sha):
        # Reuse the real default's argv-building path by calling it against a runner-like stub.
        return default_group_pr_creator(t, sha)

    import subprocess as _subprocess

    real_run = _subprocess.run

    def fake_run(args, **kwargs):
        captured_body["args"] = args
        return Proc(stdout="https://github.com/acme/widgets/pull/9")

    _subprocess.run = fake_run
    try:
        default_group_pr_creator(target, "cafef00d")
    finally:
        _subprocess.run = real_run

    body_index = captured_body["args"].index("--body") + 1
    pr_body = captured_body["args"][body_index]
    for job_id, branch in zip(target.member_job_ids, target.member_branches):
        assert job_id in pr_body, f"PR body missing job id {job_id!r}: {pr_body!r}"
        assert branch in pr_body, f"PR body missing branch {branch!r}: {pr_body!r}"


def test_member_branches_are_never_deleted_by_the_group_sequence():
    """The ScriptedRunner raises on any unexpected git call, so a stray ``branch -d``/``push
    --delete`` for a member branch would fail this test outright — asserting branches are
    preserved through the whole sequence (they only stop existing when the PR is later merged,
    which is outside this module's responsibility)."""
    lock = RepoIntegrationLock()
    target = make_target()
    runner = ScriptedRunner()

    run_group_integration_sequence(
        target, holder_id="holder-1", lock=lock, runner=runner,
        pr_creator=lambda t, sha: "https://github.com/acme/widgets/pull/9",
    )

    assert not any("-d" in c[0] or "--delete" in c[0] for c in runner.calls)
