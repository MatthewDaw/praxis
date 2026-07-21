"""Locks the `python -m agent_factory.tools.doctor` report logic, offline.

The doctor's live probes (DB connect, MCP identity, the preflight) are driven by a canned preflight
result; the DB / MCP-identity checks degrade to advisory WARN when their packages aren't importable
in this venv, which is exactly the graceful behavior we assert. Covers: an all-green run exits 0, a
misconfig preflight makes the run exit 1 and names the fix, and the org-agreement rule is reported.
"""

import importlib.util
import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _praxis  # noqa: E402

# The tool lives at agent_factory/tools/doctor.py, but under pytest there is no `agent_factory`
# package (no __init__.py), so `import agent_factory.tools.doctor` can't reach it. Load it directly
# from its file (it self-inserts hooks/ at import, exactly as the `-m` invocation does) — the same
# pattern test_resolve_preview_coverage uses.
_TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "doctor.py"
_spec = importlib.util.spec_from_file_location("doctor_under_test", _TOOL_PATH)
doctor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(doctor)


def _pf(ok, kind, failures=(), org="acme", src="PRAXIS_ORG"):
    return _praxis.PreflightResult(ok, kind, org, src, "http://localhost:8000",
                                   tuple(failures), ())


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_live_probes(monkeypatch):
    """Neutralize the checks that touch the environment (DB connect, MCP identity import), so these
    tests exercise the report/exit logic deterministically regardless of which venv / DSN is present.
    Each test drives outcomes through the mocked preflight + the identity-cache path only."""
    monkeypatch.setattr(doctor, "check_db",
                        lambda: doctor.Check("DB reachable", True, "stubbed ok"))
    monkeypatch.setattr(doctor, "check_org_agreement",
                        lambda pf: doctor.Check("MCP org == hook org", True, "both resolve 'acme'"))


def test_all_green_run_exits_zero(monkeypatch, tmp_path, capsys):
    cache = tmp_path / "mcp.json"
    cache.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PRAXIS_MCP_CACHE", str(cache))  # identity-cache-present PASS
    monkeypatch.setattr(doctor._praxis, "preflight", lambda **k: _pf(True, "ok"))

    code = doctor.run(None)
    out = capsys.readouterr().out
    assert code == 0
    assert "PASS" in out and "HTTP API + hook auth" in out
    assert "all required checks PASS" in out


def test_misconfig_run_exits_one_and_names_fix(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PRAXIS_MCP_CACHE", str(tmp_path / "missing.json"))  # cache absent => FAIL
    monkeypatch.setattr(
        doctor._praxis, "preflight",
        lambda **k: _pf(False, "misconfig", ("COGNITO_CLIENT_ID is unset — set it in .env.",)))

    code = doctor.run(None)
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out and "COGNITO_CLIENT_ID" in out
    assert "identity cache present" in out  # the absent-cache check is surfaced
    assert "RESULT: FAIL" in out


def test_org_agreement_is_reported(monkeypatch, tmp_path, capsys):
    cache = tmp_path / "mcp.json"
    cache.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PRAXIS_MCP_CACHE", str(cache))
    monkeypatch.setattr(doctor._praxis, "preflight", lambda **k: _pf(True, "ok"))
    doctor.run(None)
    out = capsys.readouterr().out
    assert "MCP org == hook org" in out  # the one hard tenancy rule always appears in the report
