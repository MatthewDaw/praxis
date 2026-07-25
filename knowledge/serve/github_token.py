"""In-process cache for the GitHub personal access token (R1).

The token lives ONLY in AWS Secrets Manager (see ``infra/lib/backend-service-stack.ts``,
which creates the secret and grants the App Runner instance role read access — it is
never a plaintext ``runtimeEnvironmentVariables`` entry). This module fetches it at
most once per process and caches the value in memory; callers that hit an upstream
authentication failure (e.g. a GitHub 401) call :func:`invalidate_github_token_cache`
so the *next* call re-fetches, picking up a rotated token without a redeploy.

The token value is NEVER logged, NEVER returned from an HTTP response, and NEVER
written to the knowledge graph — only the secret *name* and fetch outcome are logged.
"""

from __future__ import annotations

import logging
import os
import threading

import boto3

log = logging.getLogger(__name__)

# Secrets Manager entry holding the raw GitHub PAT (a plain string, not JSON).
DEFAULT_SECRET = "praxis/github/token"
DEFAULT_REGION = "us-east-1"

_lock = threading.Lock()
_cached_token: str | None = None
_fetched = False


def _fetch_from_secrets_manager() -> str | None:
    secret_name = os.environ.get("GITHUB_TOKEN_SECRET_NAME", DEFAULT_SECRET)
    region = os.environ.get("AWS_REGION", DEFAULT_REGION)
    try:
        client = boto3.client("secretsmanager", region_name=region)
        raw = client.get_secret_value(SecretId=secret_name)["SecretString"]
    except Exception:
        # No creds, no network, missing secret — caller handles None.
        log.warning("github token fetch failed for secret %s", secret_name)
        return None
    log.info("github token fetched from secret %s", secret_name)
    return raw.strip() if raw else None


def get_github_token(force_refresh: bool = False) -> str | None:
    """Return the cached GitHub token, fetching it once per process.

    ``force_refresh=True`` bypasses the cache and re-fetches unconditionally (used
    by :func:`invalidate_github_token_cache` callers after an upstream auth failure).
    """
    global _cached_token, _fetched
    with _lock:
        if force_refresh or not _fetched:
            _cached_token = _fetch_from_secrets_manager()
            _fetched = True
        return _cached_token


def invalidate_github_token_cache() -> None:
    """Mark the cache stale so the next :func:`get_github_token` call re-fetches.

    Call this after an upstream authentication failure (e.g. a GitHub 401), so a
    rotated token is picked up without a redeploy.
    """
    global _fetched
    with _lock:
        _fetched = False
