"""
Canonical PRAXIS candidate API contract v1 — shared by api_client and contract tests.

Matthew implements the server; the dashboard client targets these shapes. See
docs/integration/candidate-api-v1.md for the full specification.
"""

from __future__ import annotations

import os
from typing import Any

from models.candidate import CandidateState, next_promotion_state

CONTRACT_VERSION = "1"
CONTRACT_HEADER = "X-Praxis-Contract"
ORG_HEADER = "X-Praxis-Org"

# H11: a contradiction cluster is settled by a single ``keep`` primitive —
# "all" (every member holds: a dismissed false positive), "none" (reject all), or
# a list of fact ids to keep (reject the rest). This subsumes keep-both,
# reject-all, and pick-a-winner; ``customText`` (replace the cluster with one
# reconciled fact) stays the only other shape.
KEEP_ALL = "all"
KEEP_NONE = "none"


def contract_version() -> str:
    """Contract version from PRAXIS_CONTRACT_VERSION (default 1)."""
    return os.environ.get("PRAXIS_CONTRACT_VERSION", CONTRACT_VERSION).strip() or CONTRACT_VERSION


def contract_headers(*, token: str | None = None, org_id: str | None = None) -> dict[str, str]:
    """Build request headers for the candidate API.

    The server hard-requires a Cognito Bearer JWT on every data route and
    resolves the active org from ``X-Praxis-Org`` (mirrors the React client's
    ``contractHeaders(token, orgId)``).
    """
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        f"{CONTRACT_HEADER}": contract_version(),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if org_id:
        headers[ORG_HEADER] = org_id
    return headers


def build_promote_body(*, current_state: CandidateState) -> dict[str, str]:
    """Explicit targetState per canonical v1 contract."""
    next_state = next_promotion_state(current_state)
    if next_state is None:
        raise ValueError(f"Cannot promote from state {current_state.value!r}")
    return {"targetState": next_state.value}


def build_promote_body_implicit() -> dict[str, Any]:
    """Fallback when server auto-advances one lifecycle step."""
    return {}


def build_resolve_body(
    *, keep: str | list[str] | None = None, custom_text: str | None = None
) -> dict[str, Any]:
    """Body for ``POST /contradictions/{id}/resolve``.

    Pass ``keep`` ("all" | "none" | list of ids to keep) or ``custom_text`` (a
    reconciled fact that replaces the whole cluster).
    """
    if custom_text and custom_text.strip():
        return {"customText": custom_text}
    if isinstance(keep, str):
        if keep not in (KEEP_ALL, KEEP_NONE):
            raise ValueError(f"keep string must be {KEEP_ALL!r} or {KEEP_NONE!r}")
        return {"keep": keep}
    if isinstance(keep, (list, tuple)):
        ids = [str(k) for k in keep]
        if not ids:
            raise ValueError("keep list must name at least one fact id (or use 'none')")
        return {"keep": ids}
    raise ValueError("keep ('all'/'none'/[ids]) or custom_text is required")


def build_reject_body(*, reason: str | None = None) -> dict[str, str]:
    body: dict[str, str] = {}
    if reason:
        body["reason"] = reason
    return body


def build_create_api_key_body(*, label: str | None = None) -> dict[str, Any]:
    """Body for ``POST /apikeys``. Always sends ``label`` (null when omitted)."""
    normalized = label.strip() if isinstance(label, str) else label
    return {"label": normalized or None}


def parse_api_key_list(payload: Any) -> list[dict[str, Any]]:
    """Pull the API-key rows out of a ``GET /apikeys`` response."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("apiKeys") or payload.get("keys") or []
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def contradiction_pair_id(primary_id: str, rival_id: str) -> str:
    """Canonical contradiction id: {primaryId}__{rivalId}."""
    return f"{primary_id}__{rival_id}"


def parse_candidate_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("candidates", [])
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []
