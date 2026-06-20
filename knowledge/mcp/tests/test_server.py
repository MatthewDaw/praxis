"""Offline tests for the MCP tool functions (httpx + identity mocked).

No real network or Cognito: ``identity.token``/``active_org``/``api_base`` are
monkeypatched and ``httpx.get``/``httpx.post`` are stubbed to capture the
request, so we assert the tools hit the right endpoint with Bearer +
X-Praxis-Org and surface the backend payload.
"""

import httpx

from knowledge.mcp import identity, server


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=self)


def _patch_identity(monkeypatch):
    # Simulate a logged-in identity with an active org so the data tools' lazy
    # readiness guard (_not_ready) passes through to the HTTP call.
    monkeypatch.setattr(identity, "is_logged_in", lambda: True)
    monkeypatch.setattr(identity, "token", lambda: "id-tok")
    monkeypatch.setattr(identity, "active_org", lambda: "acme")
    monkeypatch.setattr(identity, "api_base", lambda: "http://api.test")


def test_add_insight_posts_with_auth_and_returns_summary(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_post(url, json, headers):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _Resp({"summary": "added", "action": "add", "id": "x"})

    monkeypatch.setattr(server.httpx, "post", fake_post)

    out = server.praxis_add_insight("use uv, not pip", scope="global", category="constraint")

    assert out == "added"
    assert captured["url"] == "http://api.test/insights"
    assert captured["json"] == {
        "insight": "use uv, not pip",
        "scope": "global",
        "category": "constraint",
    }
    assert captured["headers"]["Authorization"] == "Bearer id-tok"
    assert captured["headers"]["X-Praxis-Org"] == "acme"


def test_get_context_gets_with_auth_and_returns_context(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_get(url, params, headers):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _Resp({"context": "uv is the package manager here"})

    monkeypatch.setattr(server.httpx, "get", fake_get)

    out = server.praxis_get_context("how do I install deps?", top_k=3)

    assert out == "uv is the package manager here"
    assert captured["url"] == "http://api.test/context"
    assert captured["params"] == {"query": "how do I install deps?", "top_k": 3}
    assert captured["headers"]["Authorization"] == "Bearer id-tok"
    assert captured["headers"]["X-Praxis-Org"] == "acme"


def test_data_tool_when_not_logged_in_guides_to_login(monkeypatch):
    monkeypatch.setattr(identity, "is_logged_in", lambda: False)
    out = server.praxis_get_context("anything")
    assert "praxis_login" in out and "not logged in" in out.lower()


def test_praxis_login_auto_selects_single_org(monkeypatch):
    from knowledge.mcp.identity import Tenant

    tenant = Tenant("rt", "sub-1", "me@x.com", "acme", "http://api.test")
    monkeypatch.setattr(identity, "authenticate", lambda e, p: (tenant, [{"orgId": "acme"}]))
    out = server.praxis_login("me@x.com", "pw")
    assert "acme" in out and "me@x.com" in out


def test_auth_failure_maps_to_friendly_message(monkeypatch):
    _patch_identity(monkeypatch)

    def fake_get(url, params, headers):
        return _Resp({}, status_code=403)

    monkeypatch.setattr(server.httpx, "get", fake_get)

    out = server.praxis_get_context("anything")
    assert "login" in out.lower()
