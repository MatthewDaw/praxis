"""Offline tests for the MCP tool functions (httpx + identity mocked).

No real network or Cognito: ``identity.token``/``active_org``/``api_base`` are
monkeypatched and ``httpx.get``/``httpx.post`` are stubbed to capture the
request, so we assert the tools hit the right endpoint with Bearer +
X-Praxis-Org and surface the backend payload.
"""

import json

import httpx

from knowledge.mcp import identity, server


def _extract_json(out: str) -> dict:
    """Pull the structured ```json block out of a tool's dual-format string."""
    block = out.split("```json", 1)[1].split("```", 1)[0]
    return json.loads(block)


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

    # Structured output: a human summary line plus a consumable JSON block.
    assert "added" in out
    data = _extract_json(out)
    assert data["action"] == "add"
    assert data["id"] == "x"
    assert data["summary"] == "added"
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
        return _Resp({"context": "uv is the package manager here", "hits": []})

    monkeypatch.setattr(server.httpx, "get", fake_get)

    out = server.praxis_get_context("how do I install deps?", top_k=3)

    assert "uv is the package manager here" in out
    data = _extract_json(out)
    assert data["context"] == "uv is the package manager here"
    assert data["hits"] == []
    assert captured["url"] == "http://api.test/context"
    assert captured["params"] == {"query": "how do I install deps?", "top_k": 3}
    assert captured["headers"]["Authorization"] == "Bearer id-tok"
    assert captured["headers"]["X-Praxis-Org"] == "acme"


def test_ingest_posts_documents_with_auth(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_post(url, json, headers):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _Resp({"results": [{"id": "f1", "action": "ingested"}], "count": 1})

    monkeypatch.setattr(server.httpx, "post", fake_post)

    out = server.praxis_ingest("We deploy on Fridays.", source="handbook", state="active")

    assert captured["url"] == "http://api.test/ingest"
    assert captured["json"] == {
        "documents": [{"text": "We deploy on Fridays.", "source": "handbook"}],
        "state": "active",
    }
    assert captured["headers"]["Authorization"] == "Bearer id-tok"
    assert captured["headers"]["X-Praxis-Org"] == "acme"
    data = _extract_json(out)
    assert data["count"] == 1
    assert data["results"][0]["action"] == "ingested"


def test_get_contradictions_formats_pairs(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_get(url, headers):
        captured["url"] = url
        captured["headers"] = headers
        return _Resp(
            [
                {
                    "id": "a__b",
                    "status": "pending",
                    "slot": {"subject": "log level", "attribute": "verbosity"},
                    "members": [
                        {"id": "a", "content": "logs should be verbose", "state": "active"},
                        {"id": "b", "content": "logs should be terse", "state": "active"},
                    ],
                    "pairs": [
                        {
                            "id": "a__b",
                            "status": "pending",
                            "a": {"id": "a", "content": "logs should be verbose", "state": "active"},
                            "b": {"id": "b", "content": "logs should be terse", "state": "active"},
                        }
                    ],
                }
            ]
        )

    monkeypatch.setattr(server.httpx, "get", fake_get)

    out = server.praxis_get_contradictions()

    assert captured["url"] == "http://api.test/contradictions"
    assert captured["headers"]["Authorization"] == "Bearer id-tok"
    assert captured["headers"]["X-Praxis-Org"] == "acme"
    assert "a__b" in out
    assert "logs should be verbose" in out and "logs should be terse" in out
    assert "id=a" in out and "id=b" in out


def test_get_contradictions_empty(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(server.httpx, "get", lambda url, headers: _Resp([]))
    assert "No contradictions" in server.praxis_get_contradictions()


def test_resolve_contradiction_keep_id(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_post(url, json, headers):
        captured["url"] = url
        captured["json"] = json
        return _Resp({"kept": "a", "removed": "b"})

    monkeypatch.setattr(server.httpx, "post", fake_post)

    out = server.praxis_resolve_contradiction("a__b", keep_id="a")

    assert captured["url"] == "http://api.test/contradictions/a__b/resolve"
    assert captured["json"] == {"keepId": "a"}
    assert "a__b" in out


def test_resolve_contradiction_custom_text(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "post",
        lambda url, json, headers: captured.update(json=json) or _Resp({"ok": True}),
    )

    server.praxis_resolve_contradiction("a__b", custom_text="logs verbose in dev, terse in prod")

    assert captured["json"] == {"customText": "logs verbose in dev, terse in prod"}


def test_resolve_contradiction_requires_a_choice(monkeypatch):
    _patch_identity(monkeypatch)
    out = server.praxis_resolve_contradiction("a__b")
    assert "keep_id" in out and "custom_text" in out


def test_list_graph_returns_all_facts_with_state_filter(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_get(url, params, headers):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _Resp(
            [
                {"id": "f1", "state": "active", "content": "use uv, not pip"},
                {"id": "f2", "state": "active", "title": "ci runs on push"},
            ]
        )

    monkeypatch.setattr(server.httpx, "get", fake_get)

    out = server.praxis_list_graph(state="active")

    assert captured["url"] == "http://api.test/candidates"
    assert captured["params"] == {"state": "active"}
    assert captured["headers"]["X-Praxis-Org"] == "acme"
    assert "id=f1" in out and "use uv, not pip" in out
    assert "id=f2" in out and "ci runs on push" in out


def test_list_graph_no_filter_sends_no_params(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "get",
        lambda url, params, headers: captured.update(params=params) or _Resp([]),
    )
    out = server.praxis_list_graph()
    assert captured["params"] == {}
    assert "empty" in out.lower()


def test_insert_fact_posts_to_candidates(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_post(url, json, headers):
        captured["url"] = url
        captured["json"] = json
        return _Resp({"id": "new1", "state": "proposed"})

    monkeypatch.setattr(server.httpx, "post", fake_post)

    out = server.praxis_insert_fact("a title", "raw content", provenance="manual")

    assert captured["url"] == "http://api.test/candidates"
    assert captured["json"] == {
        "title": "a title",
        "content": "raw content",
        "provenance": "manual",
    }
    assert "new1" in out and "proposed" in out


def test_edit_fact_patches_only_given_fields(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_patch(url, json, headers):
        captured["url"] = url
        captured["json"] = json
        return _Resp({"id": "f1", "state": "active"})

    monkeypatch.setattr(server.httpx, "patch", fake_patch)

    out = server.praxis_edit_fact("f1", content="updated text")

    assert captured["url"] == "http://api.test/candidates/f1"
    assert captured["json"] == {"content": "updated text"}  # title/provenance omitted
    assert "f1" in out


def test_edit_fact_requires_a_field(monkeypatch):
    _patch_identity(monkeypatch)
    out = server.praxis_edit_fact("f1")
    assert "Nothing to edit" in out


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
