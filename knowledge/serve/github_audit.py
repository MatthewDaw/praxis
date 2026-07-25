"""Audit logging for GitHub token use (the productivity feature's data source).

The GitHub personal-access token backing the productivity feature (repo
discovery, commit-history fetch, ...) is a shared secret with a wide blast
radius if it leaks. This module is the SOLE write path for recording that the
token was used: every call records ``timestamp``/``endpoint``/``repo_count``
and NEVER the token value itself — :func:`record_github_token_use` has no
parameter through which a caller could even pass one, and :func:`_redact`
belt-and-suspenders-scrubs the one free-text field (``endpoint``) in case a
caller accidentally interpolates a secret into it.

Storage is an append-only JSON-lines file under ``knowledge/serve/data/``
(already gitignored as server runtime data — see repo ``.gitignore``), so the
log is trivially greppable exactly as the acceptance check does: a literal
``grep`` for a token prefix over the log file must find nothing.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Token-shaped substrings that must never reach the audit log. Mirrors the
# repo-wide `no-github-token-leak` build-validation check's pattern.
_TOKEN_PATTERN = re.compile(r"(github_pat_|ghp_)[A-Za-z0-9_]{10,}")

_REDACTED = "[REDACTED]"

_DEFAULT_LOG_PATH = Path(__file__).resolve().parent / "data" / "github_audit.log"


def _redact(text: str) -> str:
    """Replace any token-shaped substring in ``text`` with a redaction marker."""
    return _TOKEN_PATTERN.sub(_REDACTED, text)


def audit_log_path(log_path: str | Path | None = None) -> Path:
    """Resolve the audit log file path (env override, then the default)."""
    if log_path is not None:
        return Path(log_path)
    env_path = os.environ.get("GITHUB_AUDIT_LOG_PATH")
    if env_path:
        return Path(env_path)
    return _DEFAULT_LOG_PATH


def record_github_token_use(
    *, endpoint: str, repo_count: int, log_path: str | Path | None = None
) -> dict[str, Any]:
    """Append one audit entry for a single use of the GitHub token.

    Records ``timestamp`` (UTC ISO-8601), ``endpoint`` (redacted defensively),
    and ``repo_count`` — nothing else. Returns the entry that was written.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "endpoint": _redact(str(endpoint)),
        "repo_count": int(repo_count),
    }
    path = audit_log_path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read_audit_log(log_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return every recorded entry, oldest first (``[]`` if the log doesn't exist yet)."""
    path = audit_log_path(log_path)
    if not path.exists():
        return []
    entries = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def contains_token_leak(log_path: str | Path | None = None) -> bool:
    """True if the raw audit log file contains a github_pat_/ghp_-shaped token.

    Mirrors a literal ``grep`` over the log file, per the acceptance condition.
    """
    path = audit_log_path(log_path)
    if not path.exists():
        return False
    return bool(_TOKEN_PATTERN.search(path.read_text(encoding="utf-8")))
