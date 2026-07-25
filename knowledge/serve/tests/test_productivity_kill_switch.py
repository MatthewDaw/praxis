"""``GET /productivity`` must obey a server-side kill switch (no redeploy needed).

Ticket: the productivity feature is controlled by a server-side kill switch that
disables both the route and the tab without a redeploy, so a leaked or revoked
GitHub token can be contained immediately and the page degrades to a disabled
state rather than an error.

These tests exercise the pure switch/status helper directly (unit-level, no
Postgres needed) plus the real HTTP route end-to-end. Auth is bypassed via
conftest (``PRAXIS_AUTH_DISABLED=1``); the route needs no org, so no tenant
scaffolding is required.
"""

from __future__ import annotations

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.productivity import productivity_enabled, productivity_status  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret)",
)


def test_kill_switch_unset_reports_enabled(monkeypatch):
    monkeypatch.delenv("PRODUCTIVITY_KILL_SWITCH", raising=False)
    assert productivity_enabled() is True
    assert productivity_status() == {"status": "enabled"}


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_kill_switch_set_reports_disabled(monkeypatch, value):
    monkeypatch.setenv("PRODUCTIVITY_KILL_SWITCH", value)
    assert productivity_enabled() is False
    assert productivity_status() == {"status": "disabled"}


@pytest.fixture
def client():
    db.bootstrap()
    conn = db.connect()
    yield TestClient(create_app(conn))
    conn.close()


def test_get_productivity_disabled_by_kill_switch(client, monkeypatch):
    monkeypatch.setenv("PRODUCTIVITY_KILL_SWITCH", "1")
    resp = client.get("/productivity")
    assert resp.status_code == 200
    assert resp.json() == {"status": "disabled"}


def test_get_productivity_enabled_when_switch_unset(client, monkeypatch):
    monkeypatch.delenv("PRODUCTIVITY_KILL_SWITCH", raising=False)
    resp = client.get("/productivity")
    assert resp.status_code == 200
    assert resp.json() == {"status": "enabled"}
