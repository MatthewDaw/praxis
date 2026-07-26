"""Dispatch: the box-service action that turns a dispatching session's request
into a self-contained job payload (R5, R6), distinct from building itself.

Three guards run before a payload is ever produced, each failing loud rather
than degrading silently:

- **R8** the origin URL must be in the operator's pre-registered allowlist, or
  dispatch is refused (an arbitrary origin must never be cloned and built on a
  host holding administrative credentials).
- **R9** the working tree/index must be clean, or dispatch is refused naming
  every dirty path (the operator must never get a PR built from changes they
  couldn't see on screen when they dispatched).
- **R7** the build base is resolved to a commit SHA *at dispatch time* and
  that SHA — not the branch name — is what the payload carries. A branch
  recorded by name can move between dispatch and whenever the box claims the
  job; resolving eagerly is what keeps the executed build pinned to what the
  operator actually saw.

Org identity is derived from the authenticated caller (``caller_org_id``), a
trusted server-side parameter, never from the untrusted dispatch ``payload``
dict — a ``payload["org"]`` value, if present, is always discarded.

All git calls route through an injectable ``runner`` (same signature as
:func:`subprocess.run`), matching the seam pattern used by
``session_launcher.SessionLauncher`` — so this is unit-testable against a real
throwaway repo without any hidden global state.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass

#: Same shape as ``subprocess.run`` — the seam a fake runner replaces in tests.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class DispatchError(RuntimeError):
    """Base class for a refused dispatch. Never silently swallowed."""


class OriginNotAllowedError(DispatchError):
    """The dispatch payload's origin URL is not in the operator's allowlist (R8)."""


class DirtyWorkingTreeError(DispatchError):
    """The working tree or index has uncommitted paths at dispatch time (R9)."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        super().__init__(
            "dispatch refused: uncommitted changes at "
            + ", ".join(paths)
        )


@dataclass(frozen=True)
class DispatchPayload:
    """The self-contained job payload (R6): nothing is read from a file
    committed in the target repo at claim/execution time — everything the box
    needs to build travels with the payload itself."""

    project: str
    snapshot: str
    origin_url: str
    branch: str
    build_base_sha: str
    pr_base: str
    org_id: str


def _run_git(runner: Runner, cwd: str, *args: str) -> str:
    proc = runner(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise DispatchError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def dispatch_job(
    payload: dict,
    *,
    caller_org_id: str,
    allowlist: Iterable[str],
    cwd: str,
    runner: Runner = subprocess.run,
) -> DispatchPayload:
    """Validate and resolve a raw dispatch request into a :class:`DispatchPayload`.

    ``payload`` is the untrusted dispatch request: ``project``, ``snapshot``,
    ``origin_url``, ``branch``, ``pr_base``, and an optional (ignored) ``org``.
    ``caller_org_id`` is the org identity derived server-side for the
    authenticated caller — it always wins over any ``payload["org"]``.

    Raises :class:`OriginNotAllowedError` if ``origin_url`` is not in
    ``allowlist``, or :class:`DirtyWorkingTreeError` naming every dirty path
    if ``cwd``'s working tree or index is not clean. Otherwise resolves
    ``branch`` to its current commit SHA and returns the payload with that SHA
    pinned as ``build_base_sha`` — the branch name is provenance only from
    this point on.
    """
    origin_url = payload["origin_url"]
    if origin_url not in set(allowlist):
        raise OriginNotAllowedError(f"origin {origin_url!r} is not in the allowlist")

    status = _run_git(runner, cwd, "status", "--porcelain")
    if status.strip():
        paths = [line[3:] for line in status.splitlines() if line.strip()]
        raise DirtyWorkingTreeError(paths)

    branch = payload["branch"]
    build_base_sha = _run_git(runner, cwd, "rev-parse", branch).strip()

    return DispatchPayload(
        project=payload["project"],
        snapshot=payload["snapshot"],
        origin_url=origin_url,
        branch=branch,
        build_base_sha=build_base_sha,
        pr_base=payload["pr_base"],
        org_id=caller_org_id,
    )
