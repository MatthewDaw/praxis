"""Offline tests for the cached-identity helpers (no real Cognito/network).

We point the cache at a tmp file and assert ``load_identity`` raises a clear
login hint when missing and returns the cached tenant when present.
"""

import json

import pytest

from knowledge.mcp import identity


def test_load_identity_raises_when_cache_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(identity, "CACHE_PATH", tmp_path / "mcp.json")
    with pytest.raises(RuntimeError, match="login"):
        identity.load_identity()


def test_load_identity_returns_tenant_when_present(monkeypatch, tmp_path):
    cache = tmp_path / "mcp.json"
    cache.write_text(
        json.dumps(
            {
                "refresh_token": "rt",
                "sub": "user-1",
                "email": "a@b.com",
                "org_id": "acme",
                "api_base": "http://api.test",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(identity, "CACHE_PATH", cache)

    tenant = identity.load_identity()
    assert tenant.sub == "user-1"
    assert tenant.org_id == "acme"
    assert identity.active_org() == "acme"
    assert identity.api_base() == "http://api.test"
