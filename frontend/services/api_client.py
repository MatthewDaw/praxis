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
- Stub methods document expected endpoints; wire when API is published.
- No imports from pipeline/ or eval/ — Matthew owns server implementation.
===============================================================================
"""

from __future__ import annotations

from models.candidate import Candidate, CandidateState


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
        raise NotImplementedError(
            "Days 6–7: GET /candidates — wire when Matthew publishes the API schema."
        )

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        raise NotImplementedError(
            f"Days 6–7: GET /candidates/{candidate_id} — pending pipeline API."
        )

    def promote(self, candidate_id: str) -> Candidate:
        raise NotImplementedError(
            f"Days 6–7: POST /candidates/{candidate_id}/promote — pending pipeline API."
        )

    def reject(self, candidate_id: str, reason: str | None = None) -> None:
        raise NotImplementedError(
            f"Days 6–7: POST /candidates/{candidate_id}/reject — pending pipeline API."
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "X-Praxis-Contract": "1"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers
