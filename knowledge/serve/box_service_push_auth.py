"""Job-worktree vs main-worktree push authentication (R12).

A job worktree cannot reach a network remote under its own power. Concretely:

* a git worktree does not get its own remotes — remotes are repository-level, not
  worktree-level (the R12 panel revision: "git remotes are per-repository not per-worktree", so
  the original "no network remote at all" property was unsatisfiable under R10/R11's shared-clone
  design). What IS worktree-scoped, once a repo opts in via ``extensions.worktreeConfig``, is
  per-worktree config (``git config --worktree``) — the seam this module uses.
* the JOB worktree's ``remote.origin.pushurl`` is overridden, worktree-locally, to the repo's own
  local bare clone (the box's local mirror, ``box_service_clone.RepoClone.clone_path``) — so a
  plain push from inside a job session never leaves the box — and its
  ``credential.helper`` is cleared, so an attempt to push straight at the network fetch URL
  (bypassing the remote alias) has no credential helper to authenticate with.
* the MAIN worktree gets NO such override: its push targets the real ``origin_url``, authenticated
  with a token resolved fresh from ``github_token`` and embedded directly in the one-off push URL
  — never written to a persisted credential helper, so the secret never lands in on-disk git
  config.

Every git call routes through an injectable ``runner`` (the seam already used by
``box_service_clone`` and ``box_service_job_worktree``), so this is unit-testable against real
throwaway repos with no live network remote or credential store.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

from knowledge.serve.box_service_clone import RepoClone
from knowledge.serve.box_service_job_worktree import JobWorktree
from knowledge.serve.github_token import resolve_github_token

#: Same shape as ``subprocess.run`` — the seam a fake runner replaces in tests.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]
TokenResolver = Callable[[], "str | None"]


class PushAuthError(RuntimeError):
    """Raised when a required git-config call or push fails. Never silently swallowed (R17:
    refuse rather than degrade)."""


def _run(runner: Runner, cwd: str, *args: str) -> str:
    proc = runner(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise PushAuthError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip()}")
    return proc.stdout


def enable_worktree_config(repo_clone: RepoClone, *, runner: Runner = subprocess.run) -> None:
    """Opt the repo's bare clone into per-worktree config (idempotent — safe to call on every
    ``ensure``), so a job worktree's push destination/credentials can diverge from the main
    worktree's without a second git remote."""
    _run(runner, repo_clone.clone_path, "config", "extensions.worktreeConfig", "true")


def lock_job_worktree_to_local_mirror(
    job_worktree: JobWorktree, repo_clone: RepoClone, *, runner: Runner = subprocess.run
) -> None:
    """Give ``job_worktree`` a push destination and credential posture distinct from the repo's
    main worktree (R12): its ``pushurl`` is pinned, worktree-locally, at the repo's own local bare
    clone (never the network ``origin_url``), and any inherited credential helper is cleared.

    Idempotent — safe to call on a resumed job worktree; its worktree config is simply rewritten
    to the same values.
    """
    enable_worktree_config(repo_clone, runner=runner)
    # Enabling extensions.worktreeConfig turns OFF git's usual special-casing that keeps a linked
    # worktree from inheriting the bare clone's shared `core.bare = true` — without this, every
    # linked worktree would itself start reporting bare and refuse ordinary work-tree operations.
    _run(runner, job_worktree.path, "config", "--worktree", "core.bare", "false")
    _run(runner, job_worktree.path, "config", "--worktree", "remote.origin.pushurl", repo_clone.clone_path)
    _run(runner, job_worktree.path, "config", "--worktree", "credential.helper", "")


def job_worktree_pushurl(job_worktree_path: str, *, runner: Runner = subprocess.run) -> str:
    """The push URL a plain push resolves to from inside ``job_worktree_path`` — read back
    via git itself (not re-derived locally), so a caller asserts the ACTUAL configured behaviour."""
    proc = runner(
        ["git", "config", "--get", "remote.origin.pushurl"],
        cwd=job_worktree_path, capture_output=True, text=True, check=False,
    )
    return proc.stdout.strip()


def job_worktree_credential_helper(
    job_worktree_path: str, *, runner: Runner = subprocess.run
) -> "str | None":
    """The resolved ``credential.helper`` value inside ``job_worktree_path``. An empty string
    means it was explicitly cleared (R12: nothing resolves to authenticate a push aimed at the
    network URL); ``None`` means the key is unset/absent entirely."""
    proc = runner(
        ["git", "config", "--get", "credential.helper"],
        cwd=job_worktree_path, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def authenticated_push_url(origin_url: str, token: str) -> str:
    """``origin_url`` with an ``x-access-token:<token>@`` credential embedded in its netloc,
    scoped to one push call and never persisted to disk.

    A URL with no network authority (e.g. a bare local filesystem path — never what a real
    ``origin_url`` is, but what a same-host integration test stands in with) has nothing to embed
    a credential into, so it is returned unchanged.
    """
    parts = urlsplit(origin_url)
    if not parts.hostname:
        return origin_url
    netloc = f"x-access-token:{token}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def push_main_worktree(
    repo_clone: RepoClone,
    ref: str,
    *,
    token_resolver: TokenResolver = resolve_github_token,
    runner: Runner = subprocess.run,
) -> None:
    """Push ``ref`` from the repo's main worktree to its real ``origin_url`` — the box service's
    own push, distinct from a job worktree's — authenticated with a token resolved fresh from
    ``github_token`` and embedded directly in the push URL for this one call only.

    Raises :class:`PushAuthError` if no token resolves (the push is refused, never attempted
    unauthenticated) or if the push itself fails.
    """
    token = token_resolver()
    if not token:
        raise PushAuthError("no GitHub token resolved — main worktree push refused")
    push_url = authenticated_push_url(repo_clone.origin_url, token)
    proc = runner(
        ["git", "push", push_url, ref],
        cwd=repo_clone.main_worktree_path, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise PushAuthError(f"main worktree push of {ref!r} failed: {proc.stderr.strip()}")
