"""The hook's ``whoami`` one-liner + the no-longer-silent .env resolution (blocker #4).

Offline: ``_request`` (the only network op) is monkeypatched to return canned
``/whoami`` payloads, so we assert the ONE line the operator sees — including the
crisp "key scoped to org X but PRAXIS_ORG=Y" mismatch — without a server.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _praxis  # noqa: E402


def _clear_env(monkeypatch):
    for k in ("PRAXIS_API_KEY", "PRAXIS_ORG", "PRAXIS_MCP_CACHE", "PRAXIS_AUTH_DISABLED",
              "PRAXIS_API_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def test_whoami_line_reports_identity(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("PRAXIS_ORG", "bestie")
    monkeypatch.setenv("PRAXIS_API_BASE_URL", "http://localhost:8000")
    monkeypatch.setattr(_praxis, "_request", lambda *a, **k: {
        "sub": "u-bestie", "authMode": "key", "keyOrg": "bestie",
        "requestedOrg": "bestie", "orgMatch": True, "detail": "",
    })
    who = _praxis.whoami()
    assert who.ok is True
    line = who.line()
    assert "backend=http://localhost:8000" in line
    assert "resolved_org=bestie (via PRAXIS_ORG)" in line
    assert "principal=u-bestie" in line
    assert "auth_mode=key" in line
    assert "key_org=bestie" in line
    assert "MISMATCH" not in line


def test_whoami_line_flags_key_org_mismatch(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("PRAXIS_ORG", "bestie")
    # The key the backend resolved is scoped to 'sotos' — the classic footgun.
    monkeypatch.setattr(_praxis, "_request", lambda *a, **k: {
        "sub": "u", "authMode": "key", "keyOrg": "sotos",
        "requestedOrg": "bestie", "orgMatch": False, "detail": "key scoped to org 'sotos' ...",
    })
    who = _praxis.whoami()
    assert who.ok is False
    line = who.line()
    assert "MISMATCH" in line
    assert "key scoped to org 'sotos'" in line
    assert "PRAXIS_ORG='bestie'" in line


def test_whoami_unreachable_is_not_ok(monkeypatch):
    _clear_env(monkeypatch)

    def _boom(*a, **k):
        raise _praxis.PraxisUnreachable("connection refused")

    monkeypatch.setattr(_praxis, "_request", _boom)
    who = _praxis.whoami()
    assert who.ok is False
    assert "cannot reach" in who.line() and "MISMATCH" in who.line()


def test_load_dotenv_records_the_authoritative_path(monkeypatch, tmp_path):
    # The loader must not be silent about WHICH file won — it records the path it
    # actually read, so a stale-copy-at-a-different-inode bug is visible.
    import os
    env = tmp_path / ".env"
    env.write_text("PRAXIS_SENTINEL_XYZ=from-file\n", encoding="utf-8")
    monkeypatch.delenv("PRAXIS_SENTINEL_XYZ", raising=False)
    monkeypatch.chdir(tmp_path)  # cwd/.env is one of the searched candidates

    loaded = _praxis._load_dotenv()
    # A file was found and the module recorded exactly which one (never silent).
    assert loaded is not None
    assert _praxis.LOADED_ENV_PATH == loaded
    # If OURS was the authoritative file (no repo-root .env shadowed it), its value loaded.
    if loaded == env.resolve():
        assert os.environ.get("PRAXIS_SENTINEL_XYZ") == "from-file"
