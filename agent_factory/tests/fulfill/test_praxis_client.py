"""U5 — runtime Praxis client tests.

No network: a threaded stub HTTP server records requests and returns canned shapes. Asserts the
seed/outcome write bodies, the read parsing, the fail-closed behavior on 5xx, and the
``x-praxis-space`` header rule. A guarded live smoke test (env ``PRAXIS_FULFILL_LIVE=1``) round-trips
a throwaway space.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_factory.fulfill.praxis_client import FulfillPraxis, PraxisUnreachable


class _StubHandler(BaseHTTPRequestHandler):
    # set per-test: maps (method, path-prefix) -> (status, json-body); also records requests.
    routes: dict = {}
    recorded: list = []

    def log_message(self, *a):  # silence
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode() if length else ""
        body = json.loads(raw) if raw.strip() else {}
        path = self.path.split("?")[0]
        type(self).recorded.append({
            "method": method, "path": path, "body": body,
            "space": self.headers.get("x-praxis-space"),
            "org": self.headers.get("x-praxis-org"),
            "query": self.path,
        })
        for (m, prefix), (status, payload) in type(self).routes.items():
            if m == method and path.startswith(prefix):
                self._send(status, payload)
                return
        self._send(404, {"detail": "no stub route"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


@pytest.fixture
def stub(monkeypatch):
    _StubHandler.routes = {}
    _StubHandler.recorded = []
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    monkeypatch.setenv("PRAXIS_API_BASE_URL", f"http://{host}:{port}")
    monkeypatch.setenv("PRAXIS_AUTH_DISABLED", "1")
    monkeypatch.delenv("PRAXIS_SPACE", raising=False)
    try:
        yield _StubHandler
    finally:
        server.shutdown()


def test_seed_requirement_posts_expected_body(stub):
    stub.routes = {("POST", "/insights"): (200, {"id": "fact-1", "action": "added"})}
    px = FulfillPraxis(space="sess-abc")
    fid = px.ingest_requirement(
        text="The taxpayer's filing status is recorded.",
        source="prd-tax-1040-2025",
        scope="mvp",
        meta={"requirement_id": "T1", "field": "filing_status"},
    )
    assert fid == "fact-1"
    rec = stub.recorded[-1]
    assert rec["path"] == "/insights"
    assert rec["body"]["source"] == "prd-tax-1040-2025"
    assert rec["body"]["category"] == "requirement"
    assert rec["body"]["raw"] is True
    assert rec["body"]["meta"]["requirement_id"] == "T1"


def test_record_outcome_posts_success(stub):
    stub.routes = {("POST", "/facts/"): (200, {"ok": True})}
    FulfillPraxis(space="s").record_outcome("cid-9", True)
    rec = stub.recorded[-1]
    assert rec["path"] == "/facts/cid-9/outcome"
    assert rec["body"] == {"success": True}


def test_reads_parse_documented_shapes(stub):
    stub.routes = {
        ("GET", "/requirements/incomplete"): (200, {"incomplete": [{"id": "T1"}, {"id": "T2"}]}),
        ("GET", "/requirements/completeness"): (200, {"project": "p", "total": 6, "complete": 0}),
        ("GET", "/surfaces/coverage"): (200, {"uncoveredSurfaces": [], "uncoveredRequirements": []}),
    }
    px = FulfillPraxis(space="s")
    assert len(px.incomplete_requirements("tax-1040-2025")) == 2
    assert px.completeness_summary("tax-1040-2025")["total"] == 6
    cov = px.surface_coverage("tax-1040-2025", scope="mvp")
    assert cov["uncoveredSurfaces"] == []


def test_incomplete_strips_prd_prefix(stub):
    stub.routes = {("GET", "/requirements/incomplete"): (200, {"incomplete": []})}
    FulfillPraxis(space="s").incomplete_requirements("prd-tax-1040-2025")
    # the BARE name must reach the server (never prd-prd-...).
    assert "project=tax-1040-2025" in stub.recorded[-1]["query"]


def test_5xx_raises_fail_closed(stub):
    stub.routes = {("GET", "/requirements/incomplete"): (503, {"detail": "down"})}
    with pytest.raises(PraxisUnreachable):
        FulfillPraxis(space="s").incomplete_requirements("tax-1040-2025")


def test_space_header_present_iff_active(stub):
    stub.routes = {("GET", "/requirements/completeness"): (200, {"total": 0})}
    FulfillPraxis(space="sess-xyz").completeness_summary("p")
    assert stub.recorded[-1]["space"] == "sess-xyz"

    FulfillPraxis(space=None).completeness_summary("p")
    assert stub.recorded[-1]["space"] is None


def test_bind_surface_posts_edge(stub):
    stub.routes = {("POST", "/surfaces/bind"): (200, {"surfaceId": "surf-1"})}
    FulfillPraxis(space="s").bind_surface("fact-1", "form-1040", "tax-1040-2025", title="1040")
    rec = stub.recorded[-1]
    assert rec["body"]["requirementFactId"] == "fact-1"
    assert rec["body"]["screenId"] == "form-1040"


@pytest.mark.skipif(os.environ.get("PRAXIS_FULFILL_LIVE") != "1",
                    reason="live Praxis smoke test (set PRAXIS_FULFILL_LIVE=1)")
def test_live_smoke_roundtrip():
    import uuid

    space = f"fulfilltest-{uuid.uuid4().hex[:8]}"
    px = FulfillPraxis(space=space)
    px.create_space(space, name="af-fulfill smoke")
    summary = px.completeness_summary("tax-1040-2025")
    # a fresh space holds no requirements yet.
    assert summary.get("total", 0) == 0
