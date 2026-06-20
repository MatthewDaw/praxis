"""Tests for the read-only Phoenix proxy: normalization, routing, secret-safety."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from frontend.phoenix_proxy.app import (
    PhoenixSettings,
    _aggregate_tokens,
    create_app,
    normalize_span,
    normalize_trace,
)

SECRET_KEY = "phx-super-secret-key"

SAMPLE_TRACE = {
    "trace_id": "abc123",
    "start_time": "2026-06-18T10:00:00Z",
    "latency_ms": 1234.567,
    "status_code": "OK",
    "spans": [
        {
            "name": "llm_call",
            "span_kind": "LLM",
            "latency_ms": 900.0,
            "status_code": "OK",
            "attributes": {
                "llm.model_name": "claude-sonnet",
                "llm.token_count.prompt": 120,
                "llm.token_count.completion": 45,
            },
        },
        {
            "name": "retriever",
            "span_kind": "RETRIEVER",
            "start_time": 1.0,
            "end_time": 1.2,
            "status_code": "OK",
            "attributes": {},
        },
    ],
}


def _settings() -> PhoenixSettings:
    return PhoenixSettings(
        base_url="https://phoenix.example.com",
        api_key=SECRET_KEY,
        project="praxis-eval",
    )


def test_normalize_span_derives_latency_from_timestamps() -> None:
    span = normalize_span(
        {"name": "x", "span_kind": "CHAIN", "start_time": 2.0, "end_time": 2.5}
    )
    assert span["latencyMs"] == 500.0
    assert span["kind"] == "CHAIN"


def test_aggregate_tokens_sums_and_backfills_total() -> None:
    tokens = _aggregate_tokens(SAMPLE_TRACE["spans"])
    assert tokens == {"prompt": 120, "completion": 45, "total": 165}


def test_aggregate_tokens_none_when_absent() -> None:
    assert _aggregate_tokens([{"attributes": {}}]) == {
        "prompt": None,
        "completion": None,
        "total": None,
    }


def test_normalize_trace_shape_and_deep_link() -> None:
    out = normalize_trace(
        SAMPLE_TRACE, base_url="https://phoenix.example.com", project="praxis-eval"
    )
    assert out["traceId"] == "abc123"
    assert out["latencyMs"] == 1234.57
    assert out["model"] == "claude-sonnet"
    assert out["spanCount"] == 2
    assert out["tokens"]["total"] == 165
    assert (
        out["phoenixUrl"]
        == "https://phoenix.example.com/projects/praxis-eval/traces/abc123"
    )


def test_traces_endpoint_returns_normalized_payload() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [SAMPLE_TRACE], "next_cursor": None})

    app = create_app(_settings(), transport=httpx.MockTransport(handler))
    client = TestClient(app)

    resp = client.get("/phoenix/traces", params={"trace_id": "abc123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["project"] == "praxis-eval"
    assert len(body["traces"]) == 1
    assert body["traces"][0]["traceId"] == "abc123"
    # Proxy must send the bearer key upstream...
    assert captured["auth"] == f"Bearer {SECRET_KEY}"
    assert "include_spans=true" in captured["url"]


def test_traces_endpoint_never_leaks_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Phoenix error bodies could contain the key; proxy must not forward them.
        return httpx.Response(401, text=f"invalid key {SECRET_KEY}")

    app = create_app(_settings(), transport=httpx.MockTransport(handler))
    client = TestClient(app)
    resp = client.get("/phoenix/traces", params={"trace_id": "abc123"})
    assert resp.status_code == 502
    assert SECRET_KEY not in json.dumps(resp.json())


def test_session_filter_forwarded() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"data": [SAMPLE_TRACE]})

    app = create_app(_settings(), transport=httpx.MockTransport(handler))
    client = TestClient(app)
    resp = client.get("/phoenix/traces", params={"session_id": "sess-9"})
    assert resp.status_code == 200
    assert "session_identifier=sess-9" in captured["url"]


def test_unconfigured_proxy_returns_503() -> None:
    app = create_app(
        PhoenixSettings(base_url="https://phoenix.example.com", api_key=None, project=None)
    )
    client = TestClient(app)
    resp = client.get("/phoenix/traces", params={"trace_id": "abc"})
    assert resp.status_code == 503


def test_project_404_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    app = create_app(_settings(), transport=httpx.MockTransport(handler))
    client = TestClient(app)
    resp = client.get("/phoenix/traces", params={"project": "missing"})
    assert resp.status_code == 404


def test_health_reports_configuration() -> None:
    app = create_app(_settings())
    client = TestClient(app)
    body = client.get("/health").json()
    assert body == {"status": "ok", "phoenixConfigured": True, "project": "praxis-eval"}
    assert SECRET_KEY not in json.dumps(body)
