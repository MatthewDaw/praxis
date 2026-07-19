"""Offline tests for ``identity.factory_org`` — the ONE "resolve the factory org for
this project" entry point (no network/Cognito).

``factory_org()`` is the org the af-* skills mean by "the factory org": it is exactly
:func:`identity.active_org` (``PRAXIS_ORG`` pin > cached selection), derived from the
PROJECT config — NEVER the hardcoded ``agent-factory`` default. The critical invariant
is agreement across every seam: the org the MCP data tools actually send as
``X-Praxis-Org`` (``server._headers``), the org ``whoami`` reports, and the org the
stdlib Stop-hook client resolves (``agent_factory/hooks/_praxis._resolve_org``) must all
equal ``factory_org()``. These tests pin the cache at a tmp file and stub any network.

Scenario throughout: a project whose ``PRAXIS_ORG`` pin ("sotos") differs from the
cached / global default ("agent-factory").
"""

import json
import sys

from knowledge.mcp import identity, server


def _write_cache(path, **overrides):
    data = {
        "refresh_token": "rt",
        "sub": "user-1",
        "email": "factory@b.com",
        "org_id": "agent-factory",  # the cached/global default...
        "api_base": "http://api.test",
    }
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")


def _pin_project(monkeypatch, tmp_path):
    """A project whose PRAXIS_ORG pins 'sotos', over a cache that says 'agent-factory'."""
    cache = tmp_path / "mcp.json"
    _write_cache(cache, org_id="agent-factory")
    monkeypatch.setenv("PRAXIS_MCP_CACHE", str(cache))
    monkeypatch.setenv("PRAXIS_ORG", "sotos")  # ...pinned to another org by the project
    monkeypatch.delenv("PRAXIS_MCP_AUTH_DISABLED", raising=False)
    return cache


# ---------------------------------------------- 1. factory_org IS the project org

def test_factory_org_is_active_org_and_project_derived(monkeypatch, tmp_path):
    _pin_project(monkeypatch, tmp_path)
    # factory_org() is exactly active_org() — the same single resolver.
    assert identity.factory_org() == identity.active_org()
    # Project-derived: the PRAXIS_ORG pin wins over the cached "agent-factory" default,
    # so it is NOT the hardcoded/cached default.
    assert identity.factory_org() == "sotos"
    assert identity.factory_org() != "agent-factory"


# --------------------------------------- 2. THE HEADER (and whoami) HIT factory_org

def test_header_and_whoami_hit_factory_org(monkeypatch, tmp_path):
    """The org add_insight / facts_by / incomplete_requirements actually hit — the
    ``X-Praxis-Org`` header — equals ``factory_org()``, and whoami reports the same."""
    _pin_project(monkeypatch, tmp_path)

    # Make a login look present and stub the token mint so _headers() runs offline.
    monkeypatch.setattr(identity, "is_logged_in", lambda: True)
    monkeypatch.setattr(identity, "token", lambda: "tok")

    headers = server._headers()
    assert headers["X-Praxis-Org"] == identity.factory_org() == "sotos"

    # whoami reports the pinned/header org (what the data tools hit), not cached agent-factory.
    monkeypatch.setattr(identity, "list_my_orgs", lambda: [{"orgId": "sotos"}])
    out = server.praxis_whoami()
    assert "active org: sotos" in out
    assert "active org: agent-factory" not in out


# ------------------------------------------- 3. THE STOP-HOOK CLIENT RESOLVES SAME

def test_stop_hook_client_resolves_same_factory_org(monkeypatch, tmp_path):
    """The stdlib Stop-hook mirror resolves the SAME org from the same PRAXIS_ORG pin —
    MCP-tool org and hook-client org AGREE."""
    _pin_project(monkeypatch, tmp_path)

    sys.path.insert(
        0, "/Users/matthewdaw/Documents/official_repos/praxis/agent_factory/hooks"
    )
    import os

    import _praxis  # stdlib-only Stop-hook mirror

    # Under PRAXIS_ORG="sotos", the hook resolver returns "sotos" for ANY cache value,
    # and that equals factory_org() — the one hard rule.
    for any_cache in ("agent-factory", "", "whatever", "sotos"):
        assert (
            _praxis._resolve_org(
                os.environ.get("PRAXIS_ORG", ""), any_cache, _praxis.DEFAULT_ORG
            )
            == "sotos"
            == identity.factory_org()
        )


# ------------------------------------------- 4. select_org REFUSES a contradicting org

def test_select_org_refuses_and_leaves_cache(monkeypatch, tmp_path):
    cache = _pin_project(monkeypatch, tmp_path)

    out = server.praxis_select_org("agent-factory")
    # Actionable refusal naming BOTH the pinned org and the requested org.
    assert "sotos" in out and "agent-factory" in out
    # The cache is untouched — still the cached "agent-factory" selection.
    assert identity.load_identity().org_id == "agent-factory"
    assert json.loads(cache.read_text())["org_id"] == "agent-factory"
