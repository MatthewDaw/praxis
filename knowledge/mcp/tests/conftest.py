"""Shared fixtures for the MCP client tests.

``knowledge.mcp.server`` runs ``load_dotenv()`` at import, which walks up to the
repo ``.env`` — and that file legitimately carries ``PRAXIS_API_KEY``. Since the
API key now takes precedence over the Cognito bearer in ``_headers`` (the durable
org-scoped agent credential), a leaked key would silently switch every bearer test
onto the key path. Clear it (and the dev seam) by default so each test picks its
own auth mode explicitly; the API-key tests set ``PRAXIS_API_KEY`` themselves.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_auth_env(monkeypatch):
    monkeypatch.delenv("PRAXIS_API_KEY", raising=False)
    monkeypatch.delenv("PRAXIS_MCP_AUTH_DISABLED", raising=False)
