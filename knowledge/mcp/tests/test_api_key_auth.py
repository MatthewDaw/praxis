"""The MCP tools can authenticate to a specific org with a durable ``pxk_`` key.

Parity with the af-build hook: when ``PRAXIS_API_KEY`` is set, ``_headers`` sends
``X-Praxis-Key`` + the key's org (from the ``PRAXIS_ORG`` pin) and does NOT mint a
Cognito bearer — no login/refresh-token cache required, and it survives restarts.
Precedence mirrors the hook: auth-disabled seam > API key > bearer.
"""

from __future__ import annotations

import pytest

from knowledge.mcp import identity, server


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
    for k in ("PRAXIS_API_KEY", "PRAXIS_ORG", "PRAXIS_MCP_AUTH_DISABLED"):
        monkeypatch.delenv(k, raising=False)


def test_api_key_sends_key_header_and_org_no_bearer(monkeypatch):
    monkeypatch.setenv("PRAXIS_API_KEY", "pxk_bestie_key")
    monkeypatch.setenv("PRAXIS_ORG", "bestie")
    # If it tried to mint a bearer, this would blow up — proving the key path won.
    monkeypatch.setattr(identity, "token", lambda: pytest.fail("must not mint a bearer"))

    headers = server._headers()
    assert headers["X-Praxis-Key"] == "pxk_bestie_key"
    assert headers["X-Praxis-Org"] == "bestie"
    assert "Authorization" not in headers


def test_api_key_takes_precedence_over_bearer(monkeypatch):
    monkeypatch.setenv("PRAXIS_API_KEY", "pxk_sotos_key")
    monkeypatch.setenv("PRAXIS_ORG", "sotos")
    monkeypatch.setattr(identity, "is_logged_in", lambda: True)
    monkeypatch.setattr(identity, "token", lambda: "bearer-tok")
    headers = server._headers()
    assert "X-Praxis-Key" in headers and "Authorization" not in headers
    assert headers["X-Praxis-Org"] == "sotos"


def test_auth_disabled_beats_api_key(monkeypatch):
    monkeypatch.setenv("PRAXIS_MCP_AUTH_DISABLED", "1")
    monkeypatch.setenv("PRAXIS_API_KEY", "pxk_ignored")
    monkeypatch.setenv("PRAXIS_MCP_ORG", "devorg")
    headers = server._headers()
    assert "X-Praxis-Key" not in headers and "Authorization" not in headers
    assert headers["X-Praxis-Org"] == "devorg"


def test_api_key_without_org_raises_precise_hint(monkeypatch):
    monkeypatch.setenv("PRAXIS_API_KEY", "pxk_key")
    # No PRAXIS_ORG pin and no cached login -> must fail with an actionable message.
    monkeypatch.setattr(identity, "load_identity", lambda: (_ for _ in ()).throw(RuntimeError("no cache")))
    with pytest.raises(RuntimeError) as exc:
        server._headers()
    assert "PRAXIS_ORG" in str(exc.value)


def test_whoami_reports_api_key_org(monkeypatch):
    monkeypatch.setenv("PRAXIS_API_KEY", "pxk_key")
    monkeypatch.setenv("PRAXIS_ORG", "bestie")
    out = server.praxis_whoami()
    assert "API-key auth" in out and "bestie" in out
