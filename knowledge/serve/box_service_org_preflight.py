"""R15: the box service preflights, at claim time and before launching a
session, that the hook-client org and the MCP-tool org agree for the box
principal.

The box principal talks to Praxis through two independent clients that each
resolve their own active org: the Stop-hook client
(``agent_factory/hooks/_praxis.py``, env-driven) and the MCP-tool client
(``knowledge/mcp/identity.py``, cached-login-driven). Nothing enforces they
stay in sync, and af-build treats ANY divergence between them as a fail-loud
stop (see ``knowledge/mcp/identity.py:factory_org`` docstring: "the MCP-tool
org and the hook-client org always agree — the one hard rule"). Catching that
divergence here, before a job is claimed or a session is launched, costs
seconds instead of the hours it costs to discover mid-run.

This module is pure decision logic with no I/O: the box service resolves both
org strings via its existing clients and passes them in.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class BoxPrincipalOrgMismatch(RuntimeError):
    """Raised when the hook-client org and the MCP-tool org disagree for the
    box principal (R15). Fail-loud: names both orgs so the operator can find
    the divergent credential without hunting."""

    def __init__(self, hook_client_org: str, mcp_tool_org: str) -> None:
        self.hook_client_org = hook_client_org
        self.mcp_tool_org = mcp_tool_org
        super().__init__(
            f"box principal org mismatch: hook-client org={hook_client_org!r} vs "
            f"mcp-tool org={mcp_tool_org!r} — refusing to claim/launch (R15)"
        )


def preflight_org_agreement(hook_client_org: str, mcp_tool_org: str) -> None:
    """Raise :class:`BoxPrincipalOrgMismatch` unless the two orgs agree
    (stripped, exact match). Called at both points R15 requires: before a job
    is claimed, and again before its session is launched."""
    if (hook_client_org or "").strip() != (mcp_tool_org or "").strip():
        raise BoxPrincipalOrgMismatch(hook_client_org, mcp_tool_org)


def claim_job(hook_client_org: str, mcp_tool_org: str, do_claim: Callable[[], T]) -> T:
    """Preflight org agreement, then perform the claim via ``do_claim``.

    On mismatch, raises before ``do_claim`` runs — so a divergent box
    principal never claims a job."""
    preflight_org_agreement(hook_client_org, mcp_tool_org)
    return do_claim()


def launch_session(hook_client_org: str, mcp_tool_org: str, do_launch: Callable[[], T]) -> T:
    """Preflight org agreement, then launch the session via ``do_launch``.

    On mismatch, raises before ``do_launch`` runs — so no session is
    launched."""
    preflight_org_agreement(hook_client_org, mcp_tool_org)
    return do_launch()
