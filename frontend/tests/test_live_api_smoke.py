"""Optional live smoke tests against Matthew's candidate API.

Skipped unless PRAXIS_API_BASE_URL is set. Run with Matthew's server up:

    $env:PRAXIS_API_BASE_URL = "http://localhost:8000"
    $env:PYTHONPATH = "frontend"
    uv run pytest frontend/tests/test_live_api_smoke.py -v
"""

from __future__ import annotations

import os

import pytest

from models.candidate import CandidateState
from services.api_client import ApiDataProvider

pytestmark = pytest.mark.skipif(
    not os.environ.get("PRAXIS_API_BASE_URL", "").strip(),
    reason="set PRAXIS_API_BASE_URL to run live API smoke tests",
)


@pytest.fixture
def provider() -> ApiDataProvider:
    base = os.environ["PRAXIS_API_BASE_URL"].strip().rstrip("/")
    token = os.environ.get("PRAXIS_API_TOKEN", "").strip() or None
    org_id = os.environ.get("PRAXIS_ORG_ID", "default").strip() or "default"
    return ApiDataProvider(base_url=base, token=token, org_id=org_id)


def test_live_list_candidates(provider: ApiDataProvider) -> None:
    rows = provider.list_candidates()
    assert len(rows) > 0
    first = rows[0]
    assert first.id
    assert first.title
    assert first.provenance


def test_live_promote_proposed_to_suggested(provider: ApiDataProvider) -> None:
    proposed = next(
        (c for c in provider.list_candidates() if c.state is CandidateState.PROPOSED),
        None,
    )
    if proposed is None:
        pytest.skip("no proposed candidates available for promote smoke")
    updated = provider.promote(proposed.id)
    assert updated.state is CandidateState.SUGGESTED


def test_live_reject_decays(provider: ApiDataProvider) -> None:
    target = next(
        (
            c
            for c in provider.list_candidates()
            if c.state is CandidateState.PROPOSED and c.id != "cand_1"
        ),
        None,
    )
    if target is None:
        pytest.skip("no spare proposed candidate for reject smoke")
    provider.reject(target.id, reason="live smoke test")
    decayed = provider.get_candidate(target.id)
    assert decayed is not None
    assert decayed.state is CandidateState.DECAYED


def test_live_resolve_contradiction_if_pair_present(provider: ApiDataProvider) -> None:
    primary = provider.get_candidate("cand_9")
    rival = provider.get_candidate("cand_16")
    if primary is None or rival is None:
        pytest.skip("cand_9/cand_16 not in live store")
    if "cand_16" not in primary.contradiction_ids:
        pytest.skip("contradiction link already resolved in live store")
    updated = provider.resolve_contradiction(
        "cand_9__cand_16",
        resolution="keep_primary",
        keep_id="cand_9",
    )
    assert updated.id == "cand_9"
    assert "cand_16" not in updated.contradiction_ids
    assert rival.id == "cand_16"
    decayed = provider.get_candidate("cand_16")
    assert decayed is not None
    assert decayed.state is CandidateState.DECAYED
