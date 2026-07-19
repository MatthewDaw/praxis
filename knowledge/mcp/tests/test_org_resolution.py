"""Offline tests for the ONE org-precedence rule and its consumers (no network/Cognito).

The active org is resolved in a single place — :func:`identity.resolve_org`
(explicit ``PRAXIS_ORG`` pin > cached selection > default) — and everything that
reports or sends the org must agree with it: the MCP header builder
(``server._headers`` -> ``X-Praxis-Org``), ``praxis_whoami``, ``praxis_select_org``,
and the stdlib Stop-hook mirror (``agent_factory/hooks/_praxis._resolve_org``).
These tests pin the cache at a tmp file and stub any would-be network call.
"""

import json
import sys

from knowledge.mcp import identity, server


def _write_cache(path, **overrides):
    data = {
        "refresh_token": "rt",
        "sub": "user-1",
        "email": "a@b.com",
        "org_id": "acme",
        "api_base": "http://api.test",
    }
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------- 1. PRECEDENCE

def test_resolve_org_precedence_and_whitespace():
    # pin wins over cache and default.
    assert identity.resolve_org("pin", "cache", "def") == "pin"
    # no pin -> cached selection wins over default.
    assert identity.resolve_org("", "cache", "def") == "cache"
    # neither pin nor cache -> the default.
    assert identity.resolve_org("", "", "def") == "def"
    # whitespace-only is treated as absent, and a real value is stripped.
    assert identity.resolve_org("   ", "cache", "def") == "cache"
    assert identity.resolve_org("  pin  ", "cache", "def") == "pin"
    assert identity.resolve_org("", "  cache  ", "def") == "cache"


# ---------------------------------------------------- 2. WHOAMI == HEADER ORG

def test_whoami_reports_the_pinned_header_org_not_cached(monkeypatch, tmp_path):
    """The core bug: with a pin set, active_org / the header / whoami must all report
    the PINNED org, never the (stale) cached ``org_id`` that writes would NOT hit."""
    cache = tmp_path / "mcp.json"
    _write_cache(cache, org_id="agent-factory")  # cache says one org...
    monkeypatch.setenv("PRAXIS_MCP_CACHE", str(cache))
    monkeypatch.setenv("PRAXIS_ORG", "sotos")  # ...but the env PINS another
    monkeypatch.delenv("PRAXIS_MCP_AUTH_DISABLED", raising=False)

    # The resolver returns the header (pinned) value, not the cached one.
    assert identity.active_org() == "sotos"

    # Make a login look present and stub the token mint so _headers() runs offline.
    monkeypatch.setattr(identity, "is_logged_in", lambda: True)
    monkeypatch.setattr(identity, "token", lambda: "id-tok")

    # The MCP header builder sends exactly what active_org() resolves.
    headers = server._headers()
    assert headers["X-Praxis-Org"] == identity.active_org() == "sotos"

    # whoami reports the same pinned/header org (what add_insight/facts_by actually hit),
    # not the cached "agent-factory". Stub list_my_orgs to avoid the /me network call.
    monkeypatch.setattr(identity, "list_my_orgs", lambda: [{"orgId": "sotos"}])
    out = server.praxis_whoami()
    assert "active org: sotos" in out
    assert "agent-factory" in out  # the overridden cache value is named in the note...
    # ...but only as the overridden selection, never as the active org.
    assert "active org: agent-factory" not in out


# ------------------------------------------------- 3. SELECT_ORG FAILS LOUD

def test_select_org_refuses_when_pin_contradicts(monkeypatch, tmp_path):
    cache = tmp_path / "mcp.json"
    _write_cache(cache, org_id="agent-factory")
    monkeypatch.setenv("PRAXIS_MCP_CACHE", str(cache))
    monkeypatch.setenv("PRAXIS_ORG", "sotos")
    monkeypatch.delenv("PRAXIS_MCP_AUTH_DISABLED", raising=False)

    out = server.praxis_select_org("other-org")
    # Loud refusal naming BOTH the pin and the requested org.
    assert "sotos" in out and "other-org" in out
    # The cache must NOT have been changed.
    assert identity.load_identity().org_id == "agent-factory"


def test_select_org_sets_when_no_pin(monkeypatch, tmp_path):
    cache = tmp_path / "mcp.json"
    _write_cache(cache, org_id="agent-factory")
    monkeypatch.setenv("PRAXIS_MCP_CACHE", str(cache))
    monkeypatch.delenv("PRAXIS_ORG", raising=False)  # no pin -> select writes the cache
    monkeypatch.delenv("PRAXIS_MCP_AUTH_DISABLED", raising=False)

    out = server.praxis_select_org("new-org")
    assert "new-org" in out
    assert identity.load_identity().org_id == "new-org"


# ---------------------------------------------------- 4. HOOK MIRROR AGREES

def test_hook_resolver_mirrors_identity_resolve_org():
    sys.path.insert(
        0, "/Users/matthewdaw/Documents/official_repos/praxis/agent_factory/hooks"
    )
    import _praxis  # stdlib-only Stop-hook mirror

    triples = [
        ("pin", "cache", "def"),
        ("", "cache", "def"),
        ("", "", "def"),
        ("  ", "cache", "def"),
        ("  pin  ", "cache", "def"),
        ("", "  cache  ", "def"),
        ("", "", ""),
        ("sotos", "agent-factory", "agent-factory"),
        ("", "agent-factory", "agent-factory"),
    ]
    for p, c, d in triples:
        assert _praxis._resolve_org(p, c, d) == identity.resolve_org(p, c, d), (p, c, d)
