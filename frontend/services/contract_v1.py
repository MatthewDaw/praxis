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

# UI / mock resolution labels → API enum (contradiction pair: primary = keep_a side).
_RESOLUTION_TO_API: dict[str, str] = {
    "keep_primary": "keep_a",
    "keep_rival": "keep_b",
    "keep_a": "keep_a",
    "keep_b": "keep_b",
}


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


def normalize_resolution(resolution: str) -> str:
    """Map UI/mock labels to API resolution enum."""
    mapped = _RESOLUTION_TO_API.get(resolution)
    if mapped is None:
        raise ValueError(
            f"Unsupported resolution {resolution!r}; expected one of "
            f"{sorted(_RESOLUTION_TO_API)}"
        )
    return mapped


def build_resolve_body(*, resolution: str, keep_id: str) -> dict[str, str]:
    return {
        "resolution": normalize_resolution(resolution),
        "keepId": keep_id,
    }


def build_reject_body(*, reason: str | None = None) -> dict[str, str]:
    body: dict[str, str] = {}
    if reason:
        body["reason"] = reason
    return body


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
