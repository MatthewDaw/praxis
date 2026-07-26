"""Acceptance test for ticket R10 (clone-on-first-sight, cabccbf9):

given a job for a repo the box has never seen, the clone and its main
worktree are created automatically; given a second job for that repo, no
second clone is created.
"""

from __future__ import annotations

import subprocess

from knowledge.serve.box_service_clone import RepoCloneError, RepoCloneManager, repo_slug


class FakeDisk:
    """Tracks which paths a fake ``git`` runner has "created" on disk, so
    ``path_exists`` and the runner agree without any real filesystem or git
    remote (mirrors ``session_launcher``'s injectable-runner seam)."""

    def __init__(self) -> None:
        self.existing_dirs: set[str] = set()
        self.clone_calls: list[str] = []
        self.worktree_calls: list[str] = []

    def path_exists(self, path: str) -> bool:
        return path in self.existing_dirs

    def runner(self, args, **kwargs) -> "subprocess.CompletedProcess[str]":
        if args[:2] == ["git", "clone"]:
            origin_url, clone_path = args[3], args[4]
            self.clone_calls.append(origin_url)
            self.existing_dirs.add(clone_path)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["git", "worktree", "add"]:
            main_worktree_path = args[3]
            self.worktree_calls.append(main_worktree_path)
            self.existing_dirs.add(main_worktree_path)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected git call: {args}")


def make_manager(disk: FakeDisk) -> RepoCloneManager:
    return RepoCloneManager(
        "/box/clones", runner=disk.runner, path_exists=disk.path_exists
    )


def test_repo_the_box_has_never_seen_gets_a_clone_and_main_worktree_created():
    disk = FakeDisk()
    manager = make_manager(disk)

    result, created = manager.ensure("git@github.com:acme/widgets.git")

    assert created is True
    assert disk.clone_calls == ["git@github.com:acme/widgets.git"]
    assert disk.worktree_calls == [result.main_worktree_path]
    assert result.main_worktree_path in disk.existing_dirs
    assert result.clone_path in disk.existing_dirs


def test_second_job_for_same_repo_creates_no_second_clone():
    disk = FakeDisk()
    manager = make_manager(disk)

    first, first_created = manager.ensure("git@github.com:acme/widgets.git")
    second, second_created = manager.ensure("git@github.com:acme/widgets.git")

    assert first_created is True
    assert second_created is False
    assert disk.clone_calls == ["git@github.com:acme/widgets.git"]  # only once
    assert len(disk.worktree_calls) == 1
    assert second == first


def test_different_repos_each_get_their_own_clone():
    disk = FakeDisk()
    manager = make_manager(disk)

    a, a_created = manager.ensure("git@github.com:acme/widgets.git")
    b, b_created = manager.ensure("git@github.com:acme/gadgets.git")

    assert a_created is True
    assert b_created is True
    assert a.clone_path != b.clone_path
    assert a.main_worktree_path != b.main_worktree_path
    assert sorted(disk.clone_calls) == [
        "git@github.com:acme/gadgets.git",
        "git@github.com:acme/widgets.git",
    ]


def test_repo_slug_is_stable_across_calls():
    assert repo_slug("git@github.com:acme/widgets.git") == repo_slug(
        "git@github.com:acme/widgets.git"
    )


def test_clone_failure_raises_and_does_not_attempt_worktree_add():
    disk = FakeDisk()

    def failing_runner(args, **kwargs):
        if args[:2] == ["git", "clone"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="repo not found")
        raise AssertionError("worktree add must not be attempted after a failed clone")

    manager = RepoCloneManager("/box/clones", runner=failing_runner, path_exists=disk.path_exists)

    try:
        manager.ensure("git@github.com:acme/missing.git")
        raise AssertionError("expected RepoCloneError")
    except RepoCloneError as exc:
        assert "missing.git" in str(exc)
