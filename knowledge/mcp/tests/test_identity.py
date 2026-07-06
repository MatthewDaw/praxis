"""Offline tests for the cached-identity helpers (no real Cognito/network).

We point the cache at a tmp file and assert ``load_identity`` raises a clear
login hint when missing and returns the cached tenant when present.
"""

import json

import pytest

from knowledge.mcp import identity


def test_load_identity_raises_when_cache_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PRAXIS_MCP_CACHE", str(tmp_path / "mcp.json"))
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
    monkeypatch.setenv("PRAXIS_MCP_CACHE", str(cache))

    tenant = identity.load_identity()
    assert tenant.sub == "user-1"
    assert tenant.org_id == "acme"
    assert identity.active_org() == "acme"
    assert identity.api_base() == "http://api.test"


def _write_cache(path, **overrides):
    data = {
        "refresh_token": "rt",
        "sub": "user-1",
        "email": "a@b.com",
        "org_id": "acme",
        "api_base": "http://api.test",
    }
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_active_space_returns_cached_default_ignoring_env(monkeypatch, tmp_path):
    """active_space() is a purely LOCAL default (praxis_select_space); no env pathway.

    Under the tenancy redesign the ``PRAXIS_SPACE`` env → ``X-Praxis-Space`` header
    pathway is gone (working memory always resolves to the authenticated sub). The
    cached ``space_id`` is now just a client-side default for the ``space`` PARAM of
    snapshot/mount ops, and the removed env override no longer influences it.
    """
    cache = tmp_path / "mcp.json"
    _write_cache(cache, space_id="cached-space")
    monkeypatch.setenv("PRAXIS_MCP_CACHE", str(cache))

    # A now-defunct PRAXIS_SPACE env var must NOT override the cached default.
    monkeypatch.setenv("PRAXIS_SPACE", "env-space")
    assert identity.active_space() == "cached-space"

    monkeypatch.delenv("PRAXIS_SPACE", raising=False)
    assert identity.active_space() == "cached-space"


def test_active_space_defaults_empty_for_old_cache(monkeypatch, tmp_path):
    """A cache file written before spaces existed (no space_id key) reads as ""."""
    cache = tmp_path / "mcp.json"
    _write_cache(cache)  # no space_id
    monkeypatch.setenv("PRAXIS_MCP_CACHE", str(cache))
    monkeypatch.delenv("PRAXIS_SPACE", raising=False)
    assert identity.active_space() == ""
