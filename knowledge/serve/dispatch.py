"""Dispatch payload construction (R6): the self-contained payload the box
service needs to queue and execute a job.

The payload carries project slug, snapshot, origin URL, build-base commit
SHA, intended PR base, and Praxis org identity (see
``docs/brainstorms/2026-07-24-af-build-remote-jobs-requirements.md``). Every
field is either supplied directly by the dispatching caller (the MCP tool,
which resolves ``org`` server-side from the authenticated principal — never
from an untrusted client-supplied value) or derived from git itself
(``build_base_sha``, via ``git rev-parse HEAD``). **No file inside the target
repo is ever opened** — there is no factory-config lookup to source any
field from, so a repo with no such file dispatches identically to one with
any config an operator might have added by hand.

Every git call routes through an injectable ``runner`` — same call signature
as ``subprocess.run`` — mirroring ``session_launcher.SessionLauncher`` and
``box_service_clone.RepoCloneManager``'s seam, so this is assertable without
a live git remote.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass

#: Same shape as ``subprocess.run`` — the seam a fake runner replaces in tests.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]

#: Fields every dispatch payload must carry (R6).
_REQUIRED_FIELDS = ("project", "snapshot", "origin_url", "pr_base", "org")


class DispatchError(RuntimeError):
    """Raised when the payload cannot be assembled. Never silently swallowed
    (R17: refuse rather than degrade)."""


class OriginNotAllowedError(DispatchError):
    """Raised when ``origin_url`` is absent from the caller's pre-registered
    allowlist (R8). An arbitrary origin must never be clonable and
    autonomously built on a host holding administrative credentials, so this
    is refused rather than degraded, and names the offending origin."""


def validate_origin_allowlist(origin_url: str, allowed_origins: Iterable[str]) -> None:
    """Refuse an ``origin_url`` absent from ``allowed_origins`` (R8).

    Fail-closed: an empty/missing allowlist refuses every origin rather than
    permitting one, since "nothing pre-registered yet" must never read as
    "anything goes".
    """
    if origin_url not in set(allowed_origins):
        raise OriginNotAllowedError(
            f"dispatch refused: origin {origin_url!r} is not in the pre-registered allowlist "
            "for this org"
        )


@dataclass(frozen=True)
class DispatchPayload:
    """The self-contained dispatch payload (R6). Carries everything box-service
    execution needs; nothing else is read from the target repo at claim/launch
    time."""

    project: str
    snapshot: str
    origin_url: str
    build_base_sha: str
    pr_base: str
    org: str


def resolve_build_base_sha(repo_root: str, *, runner: Runner = subprocess.run) -> str:
    """Resolve the repo's current HEAD to a commit SHA via git itself (R7:
    the build base is a resolved commit SHA, never a branch name, because a
    branch recorded by name can move between dispatch and execution). This
    reads git's own object store through the ``git`` CLI — never a file the
    target repo committed for the factory's own purposes."""
    proc = runner(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise DispatchError(f"failed to resolve build-base SHA: {proc.stderr.strip()}")
    sha = proc.stdout.strip()
    if not sha:
        raise DispatchError("git rev-parse HEAD produced no SHA")
    return sha


def build_dispatch_payload(
    *,
    project: str,
    snapshot: str,
    origin_url: str,
    repo_root: str,
    pr_base: str,
    org: str,
    allowed_origins: Iterable[str],
    runner: Runner = subprocess.run,
) -> DispatchPayload:
    """Assemble the self-contained dispatch payload (R6).

    ``project``, ``snapshot``, ``origin_url``, ``pr_base`` are the
    dispatching caller's explicit arguments (an MCP tool call, not a file);
    ``org`` is the caller's identity as resolved server-side from the
    authenticated principal — this function has no separate "requested org"
    parameter for a client-supplied value to slip through as. ``build_base_sha``
    is derived from git, never trusted from the caller, so it is always a
    resolved commit rather than a movable branch name (R7).

    ``allowed_origins`` is the caller's pre-registered allowlist of repos
    known to the operator's org (R8): ``origin_url`` must match one of them
    or dispatch is refused (:class:`OriginNotAllowedError`, naming the
    origin) before anything else is resolved — an arbitrary origin must
    never be clonable and autonomously built on a host holding
    administrative credentials.

    A repo containing no factory config file dispatches identically to one
    with any such file the operator might have added: no field here is ever
    sourced by opening a path under ``repo_root``.
    """
    for name in _REQUIRED_FIELDS:
        value = locals()[name]
        if not value:
            raise DispatchError(f"dispatch payload missing required field: {name}")

    validate_origin_allowlist(origin_url, allowed_origins)

    build_base_sha = resolve_build_base_sha(repo_root, runner=runner)

    return DispatchPayload(
        project=project,
        snapshot=snapshot,
        origin_url=origin_url,
        build_base_sha=build_base_sha,
        pr_base=pr_base,
        org=org,
    )
