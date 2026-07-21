"""Locks the hook's LOUD, PRECISE auth preflight (the antidote to the silent headless hang).

Offline: every network op (_mint_cognito_token / _request) is monkeypatched, and each test points the
identity cache + preflight cache at a tmp dir so nothing touches ~/.praxis. Covers:
  * MISCONFIG classification — names the EXACT missing piece (cache / COGNITO_CLIENT_ID) + a fix,
  * UNREACHABLE classification — auth material present but the live probe fails,
  * OK — API key path and a healthy Cognito path,
  * org resolution + source, and the default-org WARNING,
  * disk caching — a second call within TTL does not re-probe.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _praxis  # noqa: E402


def _isolate(monkeypatch, tmp_path):
    """Point the identity + preflight caches at a tmp dir and clear all auth env."""
    monkeypatch.setenv("PRAXIS_MCP_CACHE", str(tmp_path / "mcp.json"))
    for k in ("PRAXIS_API_KEY", "PRAXIS_ORG", "COGNITO_CLIENT_ID", "COGNITO_REGION",
              "PRAXIS_AUTH_DISABLED", "PRAXIS_API_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def _write_cache(tmp_path, **fields):
    import json
    data = {"refresh_token": "rt", "sub": "s", "org_id": "", "api_base": "http://x"}
    data.update(fields)
    (tmp_path / "mcp.json").write_text(json.dumps(data), encoding="utf-8")


def test_misconfig_missing_cache_and_client_id_names_both(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)  # no cache file, no COGNITO_CLIENT_ID
    pf = _praxis.preflight(live=True, use_cache=False)
    assert pf.ok is False and pf.kind == "misconfig"
    blob = pf.message()
    assert "identity cache" in blob and "MISSING" in blob      # names the cache
    assert "COGNITO_CLIENT_ID" in blob                          # names the client id
    assert "MISCONFIGURED" in blob                              # loud, will-not-self-heal framing


def test_misconfig_when_client_id_missing_but_cache_present(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _write_cache(tmp_path)  # has refresh_token, but COGNITO_CLIENT_ID still unset
    pf = _praxis.preflight(live=True, use_cache=False)
    assert pf.kind == "misconfig"
    assert "COGNITO_CLIENT_ID" in pf.message()
    assert "identity cache" not in "".join(pf.failures)  # cache is fine — not named as a failure


def test_unreachable_when_material_present_but_probe_fails(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _write_cache(tmp_path)
    monkeypatch.setenv("COGNITO_CLIENT_ID", "cid")
    monkeypatch.setattr(_praxis, "_mint_cognito_token", lambda: "tok")  # mint ok

    def _boom(method, path, **k):
        raise _praxis.PraxisUnreachable("connection refused")
    monkeypatch.setattr(_praxis, "_request", _boom)  # API probe fails

    pf = _praxis.preflight(live=True, use_cache=False)
    assert pf.ok is False and pf.kind == "unreachable"
    assert "UNREACHABLE" in pf.message() and "did not answer" in pf.message()


def test_ok_with_api_key(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("PRAXIS_API_KEY", "k")
    monkeypatch.setattr(_praxis, "_request", lambda *a, **k: {})  # probe ok
    pf = _praxis.preflight(live=True, use_cache=False)
    assert pf.ok is True and pf.kind == "ok"


def test_default_org_warns_but_does_not_fail(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("PRAXIS_API_KEY", "k")
    monkeypatch.setattr(_praxis, "_request", lambda *a, **k: {})
    pf = _praxis.preflight(live=True, use_cache=False)
    assert pf.org == _praxis.DEFAULT_ORG and pf.org_source == "default"
    assert any("PRAXIS_ORG is unset" in w for w in pf.warnings)
    assert pf.ok is True  # a warning never fails the verdict


def test_pinned_org_is_reported_as_source(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("PRAXIS_API_KEY", "k")
    monkeypatch.setenv("PRAXIS_ORG", "acme")
    monkeypatch.setattr(_praxis, "_request", lambda *a, **k: {})
    pf = _praxis.preflight(live=True, use_cache=False)
    assert pf.org == "acme" and pf.org_source == "PRAXIS_ORG" and not pf.warnings


def test_disk_cache_avoids_reprobe_within_ttl(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("PRAXIS_API_KEY", "k")
    calls = {"n": 0}

    def _probe(*a, **k):
        calls["n"] += 1
        return {}
    monkeypatch.setattr(_praxis, "_request", _probe)

    first = _praxis.preflight(live=True, use_cache=True)
    assert first.ok and calls["n"] == 1
    second = _praxis.preflight(live=True, use_cache=True)  # served from disk cache
    assert second.ok and calls["n"] == 1  # NOT re-probed
