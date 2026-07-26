"""Acceptance test for ticket R10 security lens (95ca0b9d):

given a build-session user attempting to write the shared clone's config,
hooks directory, or main worktree, the write is denied by filesystem
permissions; the box service itself retains write access.

A build-session user is modeled as running under a distinct OS uid from the
box service, so the property that actually stands between it and a write is
mode bits that grant nothing to anyone but the owner — this is what these
tests assert directly, since a second real OS user is not available in the
test sandbox.
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
    # tightens it, rather than merely observing git's own defaults.
    os.chmod(clone_path, 0o755)
    os.chmod(clone_path / ".git" / "config", 0o644)
    os.chmod(clone_path / ".git" / "hooks", 0o755)
    return clone_path


def test_lock_clone_permissions_makes_config_hooks_and_worktree_owner_only(tmp_path):
    clone_path = _init_clone(tmp_path)

    lock_clone_permissions(clone_path)

    assert _mode(clone_path) == OWNER_ONLY_DIR_MODE
    assert _mode(clone_path / ".git" / "config") == OWNER_ONLY_FILE_MODE
    assert _mode(clone_path / ".git" / "hooks") == OWNER_ONLY_DIR_MODE
    # group and other carry zero bits everywhere that matters
    assert _mode(clone_path) & 0o077 == 0
    assert _mode(clone_path / ".git" / "config") & 0o077 == 0
    assert _mode(clone_path / ".git" / "hooks") & 0o077 == 0


def test_lock_clone_permissions_locks_existing_hook_files_recursively(tmp_path):
    clone_path = _init_clone(tmp_path)
    sample_hook = clone_path / ".git" / "hooks" / "pre-commit.sample"
    if sample_hook.exists():
        os.chmod(sample_hook, 0o755)

    lock_clone_permissions(clone_path)

    if sample_hook.exists():
        assert _mode(sample_hook) & 0o077 == 0


def test_lock_clone_permissions_pins_core_hooks_path(tmp_path):
    clone_path = _init_clone(tmp_path)

    lock_clone_permissions(clone_path)

    proc = subprocess.run(
        ["git", "-C", str(clone_path), "config", "core.hooksPath"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == str(clone_path / ".git" / "hooks")


def test_verify_locked_down_true_after_lockdown_false_before(tmp_path):
    clone_path = _init_clone(tmp_path)

    assert verify_locked_down(clone_path) is False

    lock_clone_permissions(clone_path)

    assert verify_locked_down(clone_path) is True


def test_lock_clone_permissions_refuses_a_path_with_no_git_dir(tmp_path):
    not_a_clone = tmp_path / "plain-dir"
    not_a_clone.mkdir()

    with pytest.raises(ClonePermissionError):
        lock_clone_permissions(not_a_clone)


def test_lock_clone_permissions_raises_when_git_config_command_fails(tmp_path):
    clone_path = _init_clone(tmp_path)

    def failing_runner(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")

    with pytest.raises(ClonePermissionError):
        lock_clone_permissions(clone_path, runner=failing_runner)

    # the failure happened before any chmod, so the tree is unchanged
    assert _mode(clone_path) == 0o755
