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

from models.api_key import ApiKey, CreatedApiKey
from models.candidate import Candidate, CandidateState
from services.contract_v1 import (
    build_create_api_key_body,
    build_promote_body,
    build_promote_body_implicit,
    build_reject_body,
    build_resolve_body,
    contract_headers,
    parse_api_key_list,
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

    Python reference client for candidate-api-v1 — returns typed Candidate models.
    The React dashboard in frontend-react/ calls the same endpoints; the contract
    itself is UI-agnostic.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        org_id: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._org_id = org_id

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
            if _is_promote_conflict(exc):
                raise ApiConflictError(
                    f"Conflict: {exc}",
                    candidate_id=candidate_id,
                ) from exc
            if exc.status_code not in (400, 422):
                raise
            logger.info(
                "Promote with targetState rejected (%s); retrying with implicit body",
                exc.status_code,
            )
            try:
                payload = self._request("POST", path, body=build_promote_body_implicit())
            except ApiClientError as retry_exc:
                if _is_promote_conflict(retry_exc):
                    raise ApiConflictError(
                        f"Conflict: {retry_exc}",
                        candidate_id=candidate_id,
                    ) from retry_exc
                raise

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
        keep: str | list[str] | None = None,
        custom_text: str | None = None,
    ) -> Candidate:
        """Resolve a contradiction cluster (H11).

        ``keep`` is "all" (every member holds), "none" (reject all), or a list of
        fact ids to keep; or pass ``custom_text`` to replace the cluster with one
        reconciled fact. Returns the primary surviving candidate.
        """
        encoded = urllib.parse.quote(contradiction_id, safe="")
        body = build_resolve_body(keep=keep, custom_text=custom_text)
        payload = self._request("POST", f"/contradictions/{encoded}/resolve", body=body)
        if not isinstance(payload, dict):
            raise ValueError("Resolve response must be an object")
        # keep-resolutions return {"kept", "rejected", "facts"}; customText returns a
        # single candidate. Surface the most relevant surviving candidate either way.
        if "facts" in payload:
            facts = payload.get("kept") or payload.get("facts") or []
            if not facts:
                raise ValueError("Resolve response included no facts")
            return Candidate.from_mapping(facts[0])
        return Candidate.from_mapping(payload)

    def list_api_keys(self) -> list[ApiKey]:
        payload = self._request("GET", "/apikeys")
        rows = parse_api_key_list(payload)
        return [ApiKey.from_mapping(row) for row in rows]

    def create_api_key(self, label: str | None = None) -> CreatedApiKey:
        payload = self._request(
            "POST",
            "/apikeys",
            body=build_create_api_key_body(label=label),
        )
        if not isinstance(payload, dict):
            raise ValueError("Create API key response must be an object")
        return CreatedApiKey.from_mapping(payload)

    def revoke_api_key(self, key_id: str) -> ApiKey:
        encoded = urllib.parse.quote(key_id, safe="")
        payload = self._request("POST", f"/apikeys/{encoded}/revoke")
        if not isinstance(payload, dict):
            raise ValueError("Revoke API key response must be an object")
        # The revoke endpoint returns a minimal {"id","revoked":true}; merge it
        # over the current row so callers still get a complete ApiKey.
        merged = {"id": key_id, "revoked": True}
        merged.update(payload)
        return ApiKey.from_mapping(merged)

    def _headers(self) -> dict[str, str]:
        return contract_headers(token=self._token, org_id=self._org_id)

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
            if exc.code in (401, 403):
                # The server requires a Cognito Bearer JWT (PRAXIS_API_TOKEN) and an
                # org the caller belongs to (PRAXIS_ORG_ID -> X-Praxis-Org). Surface
                # that explicitly instead of a generic API error.
                hint = (
                    "missing or invalid bearer token"
                    if exc.code == 401
                    else "token valid but not a member of the requested org"
                )
                raise ApiClientError(
                    f"API {method} {path} unauthorized ({exc.code}): {hint}. "
                    f"Set PRAXIS_API_TOKEN and PRAXIS_ORG_ID (or PRAXIS_AUTH_DISABLED=1 "
                    f"on a dev server). Detail: {detail or exc.reason}",
                    status_code=exc.code,
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


def _is_promote_conflict(exc: ApiClientError) -> bool:
    """Matthew's server returns 400 PromotionError for stale/invalid promote."""
    if exc.status_code != 400:
        return False
    return "cannot promote" in str(exc).lower()
