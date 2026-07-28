"""GitHub personal-access-token resolution for the productivity backend (R1).

Resolves the token from the AWS Secrets Manager secret named by
``GITHUB_TOKEN_SECRET_NAME`` (see ``infra/lib/config.ts``). Fetched once per
process and kept in an in-process cache; call :func:`invalidate_github_token`
after an upstream authentication failure (e.g. a GitHub 401) so the *next* call
re-fetches — picking up a rotated secret value without a redeploy.

LOCAL DEV ONLY, :data:`LOCAL_TOKEN_ENV_VAR` takes precedence over Secrets
Manager. The deployed backend must NEVER set it: App Runner logs and describes
its own environment variables in plaintext, so a token living there would leak
into deploy tooling and console views — which is the whole reason the deployed
path reads Secrets Manager at request time instead (see
``infra/lib/backend-service-stack.ts``, which deliberately keeps the token OUT
of ``runtimeEnvironmentVariables``). The escape hatch exists because a developer
running the server on their laptop already has a working GitHub credential in
their ``gh`` CLI keyring, and that credential can reach private repos owned by
orgs the account merely belongs to — repos the deployed fine-grained PAT cannot
see at all. Point the local server at it with::

    PRAXIS_GITHUB_TOKEN="$(gh auth token)" python -m knowledge.serve

The token value itself is never logged, never written to the knowledge graph,
and never returned in any HTTP response: this module only ever hands the raw
string to a caller that attaches it to an outbound GitHub request.
"""

from __future__ import annotations

import os
import threading

import boto3

DEFAULT_SECRET_NAME = "praxis/github/token"
DEFAULT_REGION = "us-east-1"

# Local-development override, checked BEFORE Secrets Manager. Deliberately NOT named
# ``GITHUB_TOKEN``: that name is already read by CDK at DEPLOY time to seed the secret
# (``infra/lib/backend-service-stack.ts``), and a developer with it exported for some
# unrelated tool should not silently redirect the running server's credential.
LOCAL_TOKEN_ENV_VAR = "PRAXIS_GITHUB_TOKEN"


def _local_override() -> str | None:
    """The local-dev token from :data:`LOCAL_TOKEN_ENV_VAR`, or ``None`` when unset/blank.

    Blank is treated as unset so an exported-but-empty variable falls through to Secrets
    Manager rather than authenticating every GitHub call as an empty bearer token.
    """
    return os.environ.get(LOCAL_TOKEN_ENV_VAR, "").strip() or None


_lock = threading.Lock()
_cached_token: str | None = None
_fetched = False


def _fetch_from_secrets_manager() -> str | None:
    """The local override if set, else one Secrets Manager round-trip for the current secret
    name/region, degrading to ``None`` on any failure (no creds, no network, missing/malformed
    secret). Shared by both the cached (:func:`resolve_github_token`) and uncached
    (:func:`fetch_github_token_uncached`) readers so the resolution order lives in exactly
    one place."""
    local = _local_override()
    if local is not None:
        return local
    secret_name = os.environ.get("GITHUB_TOKEN_SECRET_NAME", DEFAULT_SECRET_NAME)
    region = os.environ.get("AWS_REGION", DEFAULT_REGION)
    try:
        client = boto3.client("secretsmanager", region_name=region)
        return client.get_secret_value(SecretId=secret_name)["SecretString"]
    except Exception:
        return None


def resolve_github_token() -> str | None:
    """Fetch the GitHub token from Secrets Manager, caching it for the process.

    Returns ``None`` when no secret name is configured or the secret can't be
    fetched (no creds, no network, missing secret) so callers degrade
    gracefully instead of crashing. The token value is never printed or
    logged by this function.
    """
    global _cached_token, _fetched
    with _lock:
        if _fetched:
            return _cached_token
        _cached_token = _fetch_from_secrets_manager()
        _fetched = True
        return _cached_token


def fetch_github_token_uncached() -> str | None:
    """Fetch the GitHub token straight from Secrets Manager, bypassing the process cache.

    The box service's push path (``box_service_push_auth.push_main_worktree``) calls this on
    EVERY integration rather than :func:`resolve_github_token`'s cached value: the credential is
    account-wide and rotates on a 90-day operator calendar obligation (see
    ``docs/solutions/conventions/github-token-storage.md``), and a token revoked or rotated
    mid-run must surface at the very next integration rather than only after the whole process
    restarts. ``resolve_github_token`` keeps its own process-lifetime cache for its one existing
    caller (the productivity route), which already has its own cache-invalidate-on-401 contract
    (R1/R21) and is unaffected by this function.

    Returns ``None`` on the same failure modes as :func:`resolve_github_token` (no secret name
    configured, no creds, no network, missing secret) so callers degrade to their own
    needs-attention handling instead of crashing. Never logs or returns the value anywhere but the
    raw string handed back to the caller.
    """
    return _fetch_from_secrets_manager()


def invalidate_github_token() -> None:
    """Drop the in-process cache so the next :func:`resolve_github_token` call
    re-fetches from Secrets Manager.

    Call this after the token is rejected by GitHub (e.g. a 401/403 on an
    authenticated request) so a token rotated in Secrets Manager is picked up
    without restarting the process.
    """
    global _cached_token, _fetched
    with _lock:
        _cached_token = None
        _fetched = False
