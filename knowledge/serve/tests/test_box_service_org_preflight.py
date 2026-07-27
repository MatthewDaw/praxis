"""Acceptance test for ticket R15 (3757642b98da4fd8bcff65a717685995):

given a box whose hook-client org differs from its MCP-tool org, claiming a
job fails with both orgs named and no session is launched; given agreement,
the session launches.
"""

from __future__ import annotations

import pytest

from knowledge.serve.box_service_org_preflight import (
    BoxPrincipalOrgMismatch,
    claim_job,
    launch_session,
    preflight_org_agreement,
)


def test_claim_fails_loud_naming_both_orgs_on_mismatch() -> None:
    calls: list[str] = []
    with pytest.raises(BoxPrincipalOrgMismatch) as exc_info:
        claim_job("org-alpha", "org-beta", lambda: calls.append("claimed"))

    assert "org-alpha" in str(exc_info.value)
    assert "org-beta" in str(exc_info.value)
    assert calls == []  # the underlying claim never ran


def test_no_session_launched_on_org_mismatch() -> None:
    calls: list[str] = []
    with pytest.raises(BoxPrincipalOrgMismatch):
        launch_session("org-alpha", "org-beta", lambda: calls.append("launched"))

    assert calls == []  # the underlying launch never ran


def test_claim_and_launch_succeed_when_orgs_agree() -> None:
    assert claim_job("org-alpha", "org-alpha", lambda: "claimed") == "claimed"
    assert launch_session("org-alpha", "org-alpha", lambda: "session-123") == "session-123"


def test_preflight_org_agreement_tolerates_surrounding_whitespace() -> None:
    preflight_org_agreement(" org-alpha ", "org-alpha")  # does not raise


def test_preflight_org_agreement_raises_on_mismatch() -> None:
    with pytest.raises(BoxPrincipalOrgMismatch):
        preflight_org_agreement("org-alpha", "org-beta")
