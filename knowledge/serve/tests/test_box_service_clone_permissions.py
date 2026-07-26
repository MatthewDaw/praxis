"""Acceptance test for ticket R55 (95ca0b9d8480416d831b3eb39828fede):

given a build-session user attempting to write the shared clone's config,
hooks directory, or main worktree, the write is denied by filesystem
permissions; the box service itself retains write access.

A build-session user is modeled as running under a distinct OS uid from the
box service. A second real OS user is not available in the test sandbox, so
these tests assert the property that actually stands between a non-owner uid
and a write: mode bits that grant nothing to group or other on the clone
root, ``.git/config``, and ``.git/hooks`` (recursively) -- plus that
``core.hooksPath`` is pinned to the clone's own hooks dir so a session cannot
retarget it elsewhere.
"""

from __future__ import annotations

import os
import stat
import subprocess

import pytest

from knowledge.serve.box_service_clone_lockdown import (
    ClonePermissionError,
    OWNER_ONLY_DIR_MODE,
    OWNER_ONLY_FILE_MODE,
    lock_clone_permissions,
    verify_locked_down,
)


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _init_clone(tmp_path):
    clone_path = tmp_path / "repo"
    clone_path.mkdir(mode=0o755)
    subprocess.run(["git", "init", "-q", str(clone_path)], check=True)
    # Loosen the freshly-created tree so the test proves lockdown actually
    # tightens permissions, rather than merely observing git's own defaults.
    os.chmod(clone_path, 0o755)
    os.chmod(clone_path / ".git" / "config", 0o644)
    os.chmod(clone_path / ".git" / "hooks", 0o755)
    return clone_path


def test_lockdown_denies_group_and_other_on_config_hooks_and_worktree(tmp_path):
    clone_path = _init_clone(tmp_path)

    lock_clone_permissions(clone_path)

    # group and other carry zero bits everywhere a build-session write could
    # land: the checked-out main worktree, .git/config, and .git/hooks.
    assert _mode(clone_path) & 0o077 == 0
    assert _mode(clone_path / ".git" / "config") & 0o077 == 0
    assert _mode(clone_path / ".git" / "hooks") & 0o077 == 0
    # the owning (box-service) user retains full read/write/execute
    assert _mode(clone_path) == OWNER_ONLY_DIR_MODE
    assert _mode(clone_path / ".git" / "config") == OWNER_ONLY_FILE_MODE
    assert _mode(clone_path / ".git" / "hooks") == OWNER_ONLY_DIR_MODE


def test_lockdown_locks_existing_hook_files_recursively(tmp_path):
    clone_path = _init_clone(tmp_path)
    sample_hook = clone_path / ".git" / "hooks" / "pre-commit.sample"
    if sample_hook.exists():
        os.chmod(sample_hook, 0o755)

    lock_clone_permissions(clone_path)

    if sample_hook.exists():
        assert _mode(sample_hook) & 0o077 == 0


def test_lockdown_pins_core_hooks_path_to_the_clones_own_hooks_dir(tmp_path):
    clone_path = _init_clone(tmp_path)

    lock_clone_permissions(clone_path)

    proc = subprocess.run(
        ["git", "-C", str(clone_path), "config", "core.hooksPath"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == str(clone_path / ".git" / "hooks")


def test_verify_locked_down_reports_false_before_and_true_after(tmp_path):
    clone_path = _init_clone(tmp_path)

    assert verify_locked_down(clone_path) is False

    lock_clone_permissions(clone_path)

    assert verify_locked_down(clone_path) is True


def test_lockdown_refuses_a_path_with_no_git_dir(tmp_path):
    not_a_clone = tmp_path / "plain-dir"
    not_a_clone.mkdir()

    with pytest.raises(ClonePermissionError):
        lock_clone_permissions(not_a_clone)


def test_lockdown_raises_when_pinning_hooks_path_fails_and_leaves_tree_unchanged(tmp_path):
    clone_path = _init_clone(tmp_path)

    def failing_runner(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")

    with pytest.raises(ClonePermissionError):
        lock_clone_permissions(clone_path, runner=failing_runner)

    # the failure happened before any chmod, so the tree is unchanged
    assert _mode(clone_path) == 0o755
