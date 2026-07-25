"""Audit log for backend GitHub-token use (R12).

Every request that spends the backend GitHub token (the productivity feature's
GraphQL client, still incomplete as R2/R3) must be recorded as a timestamp,
endpoint and repository count — and the token value itself must NEVER be
recorded. This module is the single place that writes that record so every
GitHub-backed caller reuses one audited path instead of re-implementing
logging per caller.

Entries go to the standard ``github.audit`` logger as one JSON line per use:
captured by App Runner's CloudWatch log group in production (durable, with no
new infrastructure) and by pytest's ``caplog`` in tests. See
``docs/solutions/conventions/github-token-storage.md`` for the rotation
cadence, named owner and revocation runbook this log exists to support.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

logger = logging.getLogger("github.audit")

# Matches a raw GitHub token prefix so one can never reach the audit log even
# if a caller accidentally interpolates it into ``endpoint``.
_TOKEN_RE = re.compile(r"(?:github_pat_|ghp_)[A-Za-z0-9_]{10,}")

_REDACTED = "[redacted-github-token]"


def _redact(value: str) -> str:
    return _TOKEN_RE.sub(_REDACTED, value)


def record_github_use(endpoint: str, repository_count: int) -> dict[str, Any]:
    """Log one audited use of the backend GitHub token.

    Emits exactly ``{"timestamp", "endpoint", "repository_count"}`` — never
    the token value — as a single JSON line on the ``github.audit`` logger,
    and returns the same dict for callers that also want it directly.
    """
    entry: dict[str, Any] = {
        "timestamp": time.time(),
        "endpoint": _redact(str(endpoint)),
        "repository_count": int(repository_count),
    }
    logger.info(json.dumps(entry))
    return entry
