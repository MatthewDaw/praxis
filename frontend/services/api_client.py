"""
===============================================================================
FILE: services/api_client.py
AUTHOR: Monica Peters
CREATED: 2026-06-18

PURPOSE:
HTTP DataProvider for Matthew's pipeline API (Days 6–7 integration).

USAGE:
    provider = ApiDataProvider(base_url=os.environ["PRAXIS_API_BASE_URL"])

SECURITY:
- Token via PRAXIS_API_TOKEN environment variable only.

OPERATIONAL:
- Targets docs/integration/candidate-api-v1.md (contract v1).
- Uses stdlib urllib — no extra HTTP dependencies.
- No imports from pipeline/ or eval/ — Matthew owns server implementation.
===============================================================================
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from models.candidate import Candidate, CandidateState
from services.contract_v1 import (
    build_promote_body,
    build_promote_body_implicit,
    build_reject_body,
    build_resolve_body,
    contract_headers,
    parse_candidate_list,
)

logger = logging.getLogger(__name__)


class ApiConflictError(Exception):
    """Raised when the API returns HTTP 409 on a mutation."""

    def __init__(self, message: str, *, candidate_id: str | None = None) -> None:
        super().__init__(message)
        self.candidate_id = candidate_id


class ApiClientError(Exception):
    """Raised when the API returns a non-success HTTP status."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class ApiDataProvider:
    """
    Thin HTTP client over Matthew's REST API.

    A future React app in frontend-react/ should call the same endpoints —
    this class is Streamlit-specific only in that it returns Candidate models
    for the dashboard; the API contract itself is UI-agnostic.
    """

    def __init__(self, base_url: str, token: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token

    def list_candidates(self, state: CandidateState | None = None) -> list[Candidate]:
        query = ""
        if state is not None:
            query = "?" + urllib.parse.urlencode({"state": state.value})
        payload = self._request("GET", f"/candidates{query}")
        rows = parse_candidate_list(payload)
        return [Candidate.from_mapping(row) for row in rows]

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        try:
            payload = self._request("GET", f"/candidates/{urllib.parse.quote(candidate_id, safe='')}")
        except ApiConflictError:
            raise
        except ApiClientError as exc:
            if exc.status_code == 404:
                return None
            raise
        if isinstance(payload, dict):
            return Candidate.from_mapping(payload)
        return None

    def promote(self, candidate_id: str) -> Candidate:
        encoded = urllib.parse.quote(candidate_id, safe="")
        path = f"/candidates/{encoded}/promote"
        current = self.get_candidate(candidate_id)
        if current is None:
            raise KeyError(f"Unknown candidate id: {candidate_id!r}")

        explicit_body = build_promote_body(current_state=current.state)
        try:
            payload = self._request("POST", path, body=explicit_body)
        except ApiClientError as exc:
            if exc.status_code not in (400, 422):
                raise
            logger.info(
                "Promote with targetState rejected (%s); retrying with implicit body",
                exc.status_code,
            )
            payload = self._request("POST", path, body=build_promote_body_implicit())

        if not isinstance(payload, dict):
            raise ValueError("Promote response must be a candidate object")
        return Candidate.from_mapping(payload)

    def reject(self, candidate_id: str, reason: str | None = None) -> None:
        encoded = urllib.parse.quote(candidate_id, safe="")
        self._request(
            "POST",
            f"/candidates/{encoded}/reject",
            body=build_reject_body(reason=reason),
        )

    def resolve_contradiction(
        self,
        contradiction_id: str,
        *,
        resolution: str,
        keep_id: str,
    ) -> Candidate:
        encoded = urllib.parse.quote(contradiction_id, safe="")
        body = build_resolve_body(resolution=resolution, keep_id=keep_id)
        payload = self._request("POST", f"/contradictions/{encoded}/resolve", body=body)
        if not isinstance(payload, dict):
            raise ValueError("Resolve response must include the kept candidate")
        return Candidate.from_mapping(payload)

    def _headers(self) -> dict[str, str]:
        return contract_headers(token=self._token)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                if not raw.strip():
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 409:
                raise ApiConflictError(
                    f"Conflict (409): {detail or exc.reason}",
                    candidate_id=_extract_candidate_id(path),
                ) from exc
            raise ApiClientError(
                f"API {method} {path} failed ({exc.code}): {detail or exc.reason}",
                status_code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"API unreachable: {exc.reason}") from exc


def _extract_candidate_id(path: str) -> str | None:
    prefix = "/candidates/"
    if prefix not in path:
        return None
    remainder = path.split(prefix, 1)[1]
    segment = remainder.split("/", 1)[0]
    return urllib.parse.unquote(segment) if segment else None
