"""Dispatch (R5, R6, R9, R53): queuing a remote build job is a SEPARATE action from
building it, and carries a self-contained payload.

R9: dispatch fails loud when the working tree or index is dirty, naming every
uncommitted path (see ``check_clean_working_tree``), so the operator never receives a
PR built without changes they could see on screen when they dispatched.

``hooks/build_completeness_gate.py`` arms — and blocks the session's turn — only when
the session holds a live owned ticket claim or a non-stale whole-set run marker (see
``_ticket_state.claim`` / ``_ticket_state.stamp_run``). Those are what a BUILD does. A
dispatching session that also claimed a ticket or stamped a run marker would therefore
block its own turn against the gate it just armed, up to the configured block cap
(citations: requirements R5; ``hooks/build_completeness_gate.py:301``).

``dispatch_job`` is deliberately the ENTIRE dispatch action: it records the job as
queued and returns. It does not — and structurally cannot, since it never imports
``_ticket_state`` — claim a ticket or stamp a run marker.

The payload (R6) carries project slug, snapshot, origin URL, build-base commit SHA,
intended PR base, and Praxis org identity (see
``docs/brainstorms/2026-07-24-af-build-remote-jobs-requirements.md``). Every field is
either supplied directly by the dispatching caller (the MCP tool, which resolves
``org`` server-side from the authenticated principal — never from an untrusted
client-supplied value, R53) or derived from git itself (``build_base_sha``, via
``git rev-parse HEAD``). **No file inside the target repo is ever opened** — there is
no factory-config lookup to source any field from, so a repo with no such file
dispatches identically to one with any config an operator might have added by hand.

Every git call routes through an injectable ``runner`` — same call signature as
``subprocess.run`` — mirroring ``session_launcher.SessionLauncher`` and
``box_service_clone.RepoCloneManager``'s seam, so this is assertable without a live
git remote.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass

#: Same shape as ``subprocess.run`` — the seam a fake runner replaces in tests.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]

#: Fields every dispatch payload must carry (R6).
_REQUIRED_FIELDS = ("project", "snapshot", "origin_url", "pr_base", "org")


class DispatchError(RuntimeError):
    """Raised when the payload cannot be assembled. Never silently swallowed
    (R17: refuse rather than degrade)."""


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


def check_clean_working_tree(repo_root: str, *, runner: Runner = subprocess.run) -> None:
    """Refuse a dirty working tree or index (R9): dispatch fails loud, naming every
    uncommitted path, so the operator never receives a PR built without changes that
    were on screen at dispatch time. A clean tree raises nothing.

    ``git status --porcelain`` reports staged, unstaged, and untracked paths alike —
    all three count as "uncommitted" here, since none of them are what the recorded
    ``build_base_sha`` (HEAD) will actually build.
    """
    proc = runner(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise DispatchError(f"failed to check working tree status: {proc.stderr.strip()}")
    dirty_paths = [line[3:].strip() for line in proc.stdout.splitlines() if line.strip()]
    if dirty_paths:
        raise DispatchError(
            "dispatch refused: uncommitted changes present: " + ", ".join(dirty_paths)
        )


def resolve_dispatch_org(payload: dict, *, credential_org: str) -> str:
    """Derive the org identity for a dispatch server-side from the authenticated
    caller credential (R53), never from the untrusted client-supplied payload.

    ``credential_org`` is the org resolved from the authenticated principal — the
    only trustworthy source. ``payload`` is the raw, untrusted caller-supplied
    dispatch request: if it carries an ``org`` field that disagrees with
    ``credential_org``, the dispatch is rejected outright (a self-asserted org is
    an authorization claim, not data, and is never silently overridden or
    honored); if ``org`` is absent (or falsy), dispatch proceeds using the
    credential-derived org.
    """
    if not credential_org:
        raise DispatchError("resolve_dispatch_org requires a non-empty credential_org")
    payload_org = payload.get("org")
    if payload_org and payload_org != credential_org:
        raise DispatchError(
            f"dispatch payload org {payload_org!r} does not match authenticated "
            f"credential org {credential_org!r}; dispatch refused"
        )
    return credential_org


def build_dispatch_payload(
    *,
    project: str,
    snapshot: str,
    origin_url: str,
    repo_root: str,
    pr_base: str,
    org: str,
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

    A repo containing no factory config file dispatches identically to one
    with any such file the operator might have added: no field here is ever
    sourced by opening a path under ``repo_root``.
    """
    for name in _REQUIRED_FIELDS:
        value = locals()[name]
        if not value:
            raise DispatchError(f"dispatch payload missing required field: {name}")

    check_clean_working_tree(repo_root, runner=runner)
    build_base_sha = resolve_build_base_sha(repo_root, runner=runner)

    return DispatchPayload(
        project=project,
        snapshot=snapshot,
        origin_url=origin_url,
        build_base_sha=build_base_sha,
        pr_base=pr_base,
        org=org,
    )


@dataclass
class DispatchedJob:
    """A queued remote build job, as handed back to the dispatching session."""

    id: str
    project: str
    snapshot: str
    state: str = "queued"


def dispatch_job(project: str, snapshot: str) -> DispatchedJob:
    """Queue a remote build job for ``(project, snapshot)`` and return it.

    This is the whole of dispatch: create a queued job record and return it to the
    caller. It never claims a ticket and never stamps a whole-set run marker (R5) — the
    dispatching session's completeness gate must stay inert after this call, so its turn
    can end without the gate blocking.
    """
    if not project or not snapshot:
        raise ValueError("dispatch_job requires a non-empty project and snapshot")
    return DispatchedJob(id=str(uuid.uuid4()), project=project, snapshot=snapshot)
