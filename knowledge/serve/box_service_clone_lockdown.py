"""Filesystem lockdown for the box service's per-repo clone (R10; security
audit lens on R10, ticket 95ca0b9d): the shared clone's git config, its hooks
directory, and its checked-out main worktree are owned by the box-service OS
user and are not writable by build-session users, and ``core.hooksPath`` is
fixed by the service — because a build session that could write the shared
clone could install a hook that runs as the only principal holding the push
credential.

At the filesystem layer, "not writable by build-session users" means: mode
bits that grant the owning (box-service) user full read/write/execute and
grant **nothing** to group or other. A build session is modeled as running
under a distinct OS user/uid from the box service (R10's whole premise), so
once a path carries owner-only mode, any non-owner uid — build sessions
included — is denied by the kernel's own permission check; this module's job
is only to prove it sets those bits correctly, which is what
``verify_locked_down`` asserts.

Like ``session_launcher.SessionLauncher``, the one external call (pinning
``core.hooksPath``) goes through an injectable ``runner`` — same call
signature as :func:`subprocess.run` — so the lockdown is assertable with a
fake in tests.
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

#: Owner gets full access; group and other get none. This is the mode that
#: makes a path unwritable (indeed inaccessible) to any uid but the owner.
OWNER_ONLY_DIR_MODE = 0o700
OWNER_ONLY_FILE_MODE = 0o600

#: Same shape as ``subprocess.run`` — the seam a fake runner replaces in tests
#: (mirrors ``session_launcher.Runner``).
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class ClonePermissionError(RuntimeError):
    """Raised when locking down a clone's permissions fails or the clone is
    not in a lockable shape (R17: refuse rather than degrade silently)."""


def _chmod_tree(root: Path) -> None:
    """Set owner-only mode on ``root`` and everything beneath it."""
    if root.is_dir():
        os.chmod(root, OWNER_ONLY_DIR_MODE)
        for child in root.iterdir():
            _chmod_tree(child)
    else:
        os.chmod(root, OWNER_ONLY_FILE_MODE)


def lock_clone_permissions(clone_path: Path, *, runner: Runner = subprocess.run) -> None:
    """Lock down ``clone_path`` (the per-repo clone + checked-out main
    worktree, R10) to owner-only mode, and pin ``core.hooksPath`` to the
    clone's own ``.git/hooks``.

    Order matters: ``core.hooksPath`` is pinned *before* ``.git/config`` is
    chmod'd owner-only, since writing it is itself a write to that file.
    Locks, in order: the hooks directory (recursively — any file dropped
    there executes on the box service's next git operation), ``.git/config``,
    then the clone root (which covers the checked-out main worktree).
    """
    git_dir = clone_path / ".git"
    config_path = git_dir / "config"
    hooks_dir = git_dir / "hooks"

    if not git_dir.is_dir():
        raise ClonePermissionError(f"{clone_path} has no .git directory to lock down")

    proc = runner(
        ["git", "-C", str(clone_path), "config", "core.hooksPath", str(hooks_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ClonePermissionError(f"failed to pin core.hooksPath: {proc.stderr.strip()}")

    if hooks_dir.exists():
        _chmod_tree(hooks_dir)
    if config_path.exists():
        os.chmod(config_path, OWNER_ONLY_FILE_MODE)
    os.chmod(clone_path, OWNER_ONLY_DIR_MODE)


def _is_owner_only(path: Path, *, expected: int) -> bool:
    mode = stat.S_IMODE(path.stat().st_mode)
    return mode == expected


def verify_locked_down(clone_path: Path) -> bool:
    """``True`` iff ``clone_path``, ``.git/config``, and ``.git/hooks`` are
    all owner-only — i.e. a build-session user (any non-owner uid) has no
    read/write/execute access to any of them. Used both by the acceptance
    test and as a guard the box service can call before trusting a clone.
    """
    git_dir = clone_path / ".git"
    config_path = git_dir / "config"
    hooks_dir = git_dir / "hooks"

    if not _is_owner_only(clone_path, expected=OWNER_ONLY_DIR_MODE):
        return False
    if config_path.exists() and not _is_owner_only(config_path, expected=OWNER_ONLY_FILE_MODE):
        return False
    if hooks_dir.exists() and not _is_owner_only(hooks_dir, expected=OWNER_ONLY_DIR_MODE):
        return False
    return True
