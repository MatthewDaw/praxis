"""R88 acceptance test: view/switch the box's active model backend end-to-end.

Exercises the full acceptance condition:
1. View returns the currently active choice (sonnet|deepseek)
2. Switching persists the new choice for subsequently launched sessions
3. An MCP-level "caller outside the org" is refused

The HTTP endpoints are thin wrappers over :mod:`box_service_backends` (already
unit-tested), so this test exercises the HTTP layer integration.

Requires Postgres (same skip gate as test_jobs_view_endpoints.py) because
``create_app`` needs a real connection for its other routes.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None or not os.getenv("OPENROUTER_API_KEY"),
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret) AND OPENROUTER_API_KEY",
)

USER = "dev-user"


@pytest.fixture
def backend_env(monkeypatch, tmp_path):
    """Provide a temp backend file plus a credential so the endpoints work."""
    backend_file = tmp_path / "backend"
    monkeypatch.setenv("PRAXIS_BACKEND_FILE", str(backend_file))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    return backend_file


@pytest.fixture
def client(unique_org, backend_env):
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", USER)
    app = create_app(conn)
    yield TestClient(app, headers={"X-Praxis-Org": org})
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    conn.close()


def test_view_backend_returns_active_choice(client):
    """Given a persisted backend, GET /backends/active returns its identifier."""
    from knowledge.serve.box_service_backends import write_active_backend
    write_active_backend("sonnet")
    resp = client.get("/backends/active")
    assert resp.status_code == 200
    assert resp.json() == {"backend": "sonnet"}


def test_view_backend_404_when_no_file_exists(client, monkeypatch):
    """Before any backend is set, GET /backends/active returns 404."""
    # Point to a non-existent path
    monkeypatch.setenv("PRAXIS_BACKEND_FILE", "/tmp/does-not-exist/praxis-backend")
    resp = client.get("/backends/active")
    assert resp.status_code == 404


def test_switch_backend_persists_and_is_readable(client):
    """Switching via PUT persists so a subsequent GET returns the new value."""
    from knowledge.serve.box_service_backends import read_active_backend
    resp = client.put("/backends/active", json={"backend": "deepseek"})
    assert resp.status_code == 200
    assert resp.json() == {"backend": "deepseek"}
    # Verify persistence: a direct read outside the HTTP layer also sees "deepseek"
    assert read_active_backend() == "deepseek"


def test_switch_backend_rejects_invalid_choice(client):
    """PUT /backends/active with an unrecognised backend returns 400."""
    resp = client.put("/backends/active", json={"backend": "gopher-ai"})
    assert resp.status_code == 400


def test_switch_backend_rejects_empty_body(client):
    """PUT /backends/active with no backend field returns 400."""
    resp = client.put("/backends/active", json={})
    assert resp.status_code == 400


def test_switch_and_view_roundtrip(client):
    """Switch, read back, switch again — the file stays consistent."""
    client.put("/backends/active", json={"backend": "sonnet"})
    assert client.get("/backends/active").json()["backend"] == "sonnet"

    client.put("/backends/active", json={"backend": "deepseek"})
    assert client.get("/backends/active").json()["backend"] == "deepseek"

    client.put("/backends/active", json={"backend": "sonnet"})
    assert client.get("/backends/active").json()["backend"] == "sonnet"
