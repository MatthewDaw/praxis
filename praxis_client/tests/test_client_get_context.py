"""Offline unit tests for PraxisClient.get_context filter plumbing.

No network: we stub the client's ``_request`` to capture the (method, path) it would
issue, and assert the query string carries the optional positive filters
(category/categories/scope/meta) — and omits them by default (parity).
"""

from __future__ import annotations

import urllib.parse

from praxis_client.client import PraxisClient


def _client(monkeypatch):
    client = PraxisClient(base_url="http://api.test", api_key="pxk_x", org_id="acme")
    captured: dict = {}

    def fake_request(method, path, body=None):
        captured["method"] = method
        captured["path"] = path
        return {"context": "", "hits": []}

    monkeypatch.setattr(client, "_request", fake_request)
    return client, captured


def _query(path: str) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(path.split("?", 1)[1])


def test_get_context_no_filters_is_parity(monkeypatch):
    client, captured = _client(monkeypatch)
    client.get_context("hello", top_k=5)
    assert captured["method"] == "GET"
    q = _query(captured["path"])
    assert q == {"query": ["hello"], "top_k": ["5"]}  # nothing extra


def test_get_context_appends_filters(monkeypatch):
    client, captured = _client(monkeypatch)
    client.get_context(
        "a part",
        category="check",
        categories=["check", "requirement"],
        scope="mvp",
        meta={"scope": "planning"},
    )
    q = _query(captured["path"])
    assert q["category"] == ["check"]
    assert q["categories"] == ["check,requirement"]
    assert q["scope"] == ["mvp"]
    assert q["meta"] == ['{"scope": "planning"}']  # dict serialized to JSON


def test_get_context_accepts_preencoded_meta_string(monkeypatch):
    client, captured = _client(monkeypatch)
    client.get_context("q", meta='{"applies_to": "s-home"}')
    q = _query(captured["path"])
    assert q["meta"] == ['{"applies_to": "s-home"}']
