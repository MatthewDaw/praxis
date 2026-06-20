"""Shared test fixtures for the serve package.

Offline tests run with PRAXIS_AUTH_DISABLED=1 so the ``current_user`` dependency
returns a fixed dev principal instead of verifying a real Cognito JWT (see
auth.py). Set at import time so it covers module-level app construction too.
"""

from __future__ import annotations

import os

os.environ.setdefault("PRAXIS_AUTH_DISABLED", "1")

import pytest


@pytest.fixture
def unique_org(request):
    # Unique per test node so reruns and parallel tenants never collide.
    return "test_" + request.node.name
