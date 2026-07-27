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

The account-wide PAT backing that main-worktree push is FETCHED PER INTEGRATION
(``github_token.fetch_github_token_uncached``), never cached for the process's lifetime the way
the productivity route's reads are — a token revoked or rotated mid-run (the 90-day operator
calendar obligation documented in ``docs/solutions/conventions/github-token-storage.md``) must
surface at the very NEXT integration, not only once the whole box-service process restarts. When
the remote rejects that credential, the push raises :class:`PushCredentialRejectedError` — a
distinct, credential-naming reason, never an opaque git failure — and (when a ``job`` is given)
records ``FailureClass.PUSH_CREDENTIAL_REJECTED`` as needs-attention.

Every git call routes through an injectable ``runner`` (the seam already used by
``box_service_clone`` and ``box_service_job_worktree``), so this is unit-testable against real
throwaway repos with no live network remote or credential store.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

from knowledge.serve.box_service_clone import RepoClone
from knowledge.serve.box_service_failures import FailureClass, record_failure
from knowledge.serve.box_service_job_worktree import JobWorktree
from knowledge.serve.box_service_models import Job
from knowledge.serve.github_token import DEFAULT_SECRET_NAME, fetch_github_token_uncached

#: Same shape as ``subprocess.run`` — the seam a fake runner replaces in tests.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]
TokenResolver = Callable[[], "str | None"]

#: Substrings of a failed push's stderr that name an authentication/authorization rejection
#: (a bad, revoked, or rotated-out-from-under-it credential) rather than an unrelated git
#: failure (e.g. a non-fast-forward, a network timeout) — case-insensitive.
_AUTH_FAILURE_MARKERS = (
    "authentication failed",
    "invalid username or token",
    "could not read username",
    "could not read password",
    "bad credentials",
    "support for password authentication was removed",
    "403",
)


def _looks_like_authentication_failure(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _AUTH_FAILURE_MARKERS)


def _push_credential_secret_name() -> str:
    return os.environ.get("GITHUB_TOKEN_SECRET_NAME", DEFAULT_SECRET_NAME)


class PushAuthError(RuntimeError):
    """Raised when a required git-config call or push fails. Never silently swallowed (R17:
    refuse rather than degrade)."""


class PushCredentialRejectedError(PushAuthError):
    """The account-wide GitHub PAT was rejected by the remote pushing from the main worktree —
    revoked or rotated mid-run — distinct from a token that never resolved at all
    (:class:`PushAuthError` alone) or an unrelated git failure, so the operator sees exactly
    which credential needs rotating/replacing rather than an opaque push error."""


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
    token_resolver: TokenResolver = fetch_github_token_uncached,
    runner: Runner = subprocess.run,
    job: "Job | None" = None,
) -> None:
    """Push ``ref`` from the repo's main worktree to its real ``origin_url`` — the box service's
    own push, distinct from a job worktree's — authenticated with a token resolved fresh from
    ``github_token`` and embedded directly in the push URL for this one call only.

    ``token_resolver`` defaults to :func:`github_token.fetch_github_token_uncached`, which hits
    Secrets Manager on EVERY call rather than reusing a process-lifetime cache: the account-wide
    PAT is rotated on a 90-day operator calendar obligation (or revoked early on suspected
    exposure), and a rotated/revoked value must be picked up at the very next integration, not
    only after a redeploy.

    Raises :class:`PushAuthError` if no token resolves (the push is refused, never attempted
    unauthenticated), or :class:`PushCredentialRejectedError` — a distinct, credential-naming
    subclass — if the remote rejects the resolved token (revoked or rotated mid-run) rather than
    an opaque generic push failure. Either way, when ``job`` is given, the rejection is also
    recorded as ``FailureClass.PUSH_CREDENTIAL_REJECTED`` (needs-attention, naming the secret).
    """
    secret_name = _push_credential_secret_name()
    token = token_resolver()
    if not token:
        if job is not None:
            record_failure(
                job, FailureClass.PUSH_CREDENTIAL_REJECTED,
                detail=f"no GitHub token resolved from secret {secret_name!r}",
            )
        raise PushAuthError(
            f"no GitHub token resolved from secret {secret_name!r} — main worktree push refused"
        )
    push_url = authenticated_push_url(repo_clone.origin_url, token)
    proc = runner(
        ["git", "push", push_url, ref],
        cwd=repo_clone.main_worktree_path, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if _looks_like_authentication_failure(stderr):
            if job is not None:
                record_failure(
                    job, FailureClass.PUSH_CREDENTIAL_REJECTED,
                    detail=f"GitHub PAT from secret {secret_name!r} rejected by remote",
                )
            raise PushCredentialRejectedError(
                f"GitHub PAT from secret {secret_name!r} was rejected pushing {ref!r} "
                f"(revoked or rotated?): {stderr}"
            )
        raise PushAuthError(f"main worktree push of {ref!r} failed: {stderr}")
