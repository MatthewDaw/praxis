"""
===============================================================================
FILE: services/data_provider.py
AUTHOR: Monica Peters
CREATED: 2026-06-18

PURPOSE:
Protocol and factory for candidate data access. UI components depend on this
interface — not on pandas, mock_data, or HTTP — so React or another client
can share Matthew's API without importing dashboard UI code.

USAGE:
    provider = get_data_provider()
    candidates = provider.list_candidates(state="proposed")

OPERATIONAL:
- PRAXIS_API_BASE_URL unset → MockDataProvider (local dev, no backend)
- PRAXIS_API_BASE_URL set     → ApiDataProvider (Days 6–7 integration)
- PRAXIS_API_TOKEN            → Cognito Bearer JWT for the live API
- PRAXIS_ORG_ID               → active org sent as X-Praxis-Org (default "default")
===============================================================================
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from models.candidate import Candidate, CandidateState


@runtime_checkable
class DataProvider(Protocol):
    """Contract for human-gate candidate reads and approval mutations."""

    def list_candidates(self, state: CandidateState | None = None) -> list[Candidate]:
        """Return candidates, optionally filtered by lifecycle state."""
        ...

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        """Return a single candidate by id."""
        ...

    def promote(self, candidate_id: str) -> Candidate:
        """Advance proposed → suggested → active."""
        ...

    def reject(self, candidate_id: str, reason: str | None = None) -> None:
        """Reject and remove a candidate from the active review queue."""
        ...

    def resolve_contradiction(
        self,
        contradiction_id: str,
        *,
        resolution: str,
        keep_id: str,
    ) -> Candidate:
        """Resolve a contradiction pair; keep_id is the winning candidate."""
        ...


def get_data_provider() -> DataProvider:
    """
    Select provider from environment.

    Teammates running pipeline/eval only need Matthew's API — not this factory.
    """
    base_url = os.environ.get("PRAXIS_API_BASE_URL", "").strip()
    if base_url:
        from services.api_client import ApiDataProvider

        token = os.environ.get("PRAXIS_API_TOKEN")
        org_id = os.environ.get("PRAXIS_ORG_ID", "default").strip() or "default"
        return ApiDataProvider(base_url=base_url, token=token, org_id=org_id)

    from services.mock_provider import MockDataProvider

    return MockDataProvider()
