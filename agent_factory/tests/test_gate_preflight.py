"""Locks the gate's two DX fixes, offline:

  * NO-OP FAST-PATH (C2) — a session whose transcript shows ZERO factory signal stands down WITHOUT
    a Praxis read, so an unrelated session is never blocked by a Praxis outage; a transcript WITH a
    factory signal (or none supplied) still falls through to the hard, fail-closed read.
  * LOUD PREFLIGHT (A) — when the Praxis read fails closed, the block reason carries the PRECISE
    preflight diagnostic (which piece is missing) instead of the old generic "check PRAXIS_*".
"""

import io
import json
import os
import sys
from pathlib import Path

import pytest

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _praxis  # noqa: E402
import build_completeness_gate as gate  # noqa: E402

OWNER = "sess-A"


def _run(monkeypatch, *, incomplete, transcript_path=None, tmp_path=None):
    monkeypatch.setenv("FACTORY_PROJECT", "team-app")
    monkeypatch.delenv("FACTORY_GATE_DISABLED", raising=False)
    if tmp_path is not None:
        monkeypatch.setenv("PRAXIS_MCP_CACHE", str(tmp_path / "mcp.json"))  # isolate markers/caches
    monkeypatch.setattr(_praxis, "incomplete_requirements", incomplete)
    payload = {"session_id": OWNER, "cwd": "/x/team-app"}
    if transcript_path is not None:
        payload["transcript_path"] = str(transcript_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setattr(sys, "stderr", err)
    try:
        with pytest.raises(SystemExit):
            gate.main()
    finally:
        os.environ.pop("FACTORY_PROJECT", None)
    out = buf.getvalue().strip()
    parsed = json.loads(out) if out else {}
    decision = parsed.get("decision", "allow")
    reason = parsed.get("reason", "") if decision == "block" else \
        (parsed.get("hookSpecificOutput") or {}).get("additionalContext", "")
    return {"decision": decision, "reason": reason, "stderr": err.getvalue()}


def _boom(project, **k):
    raise _praxis.PraxisUnreachable("connection refused")


# --------------------------------------------------------------------------- C2 no-op fast-path

def test_noop_session_allows_without_praxis(monkeypatch, tmp_path):
    """A transcript with zero factory signal => allow, and Praxis is NEVER consulted (even though it
    would raise). This is the fix: an unrelated session is not blocked when Praxis is down."""
    t = tmp_path / "transcript.jsonl"
    t.write_text('{"role":"user","content":"help me center a div"}\n', encoding="utf-8")

    called = {"n": 0}

    def _spy(project, **k):
        called["n"] += 1
        raise _praxis.PraxisUnreachable("should not be reached")

    r = _run(monkeypatch, incomplete=_spy, transcript_path=t, tmp_path=tmp_path)
    assert r["decision"] == "allow"
    assert called["n"] == 0  # the fail-closed read was skipped entirely


def test_factory_signal_transcript_falls_through_to_failclosed(monkeypatch, tmp_path):
    """A transcript that mentions the factory falls through to the hard read — still fail-closed."""
    t = tmp_path / "transcript.jsonl"
    t.write_text('{"content":"claimed ticket via mcp__praxis and stamp_run"}\n', encoding="utf-8")
    monkeypatch.setattr(_praxis, "preflight", lambda **k: _praxis.PreflightResult(
        False, "unreachable", "team-app", "PRAXIS_ORG", "http://localhost:8000", ("server down",), ()))
    r = _run(monkeypatch, incomplete=_boom, transcript_path=t, tmp_path=tmp_path)
    assert r["decision"] == "block" and "PRAXIS UNREACHABLE" in r["reason"]


def test_missing_transcript_still_failcloses(monkeypatch, tmp_path):
    """No transcript path (unknown engagement) must NOT fast-path — it falls through to fail-closed."""
    monkeypatch.setattr(_praxis, "preflight", lambda **k: _praxis.PreflightResult(
        False, "unreachable", "team-app", "PRAXIS_ORG", "http://localhost:8000", ("x",), ()))
    r = _run(monkeypatch, incomplete=_boom, transcript_path=None, tmp_path=tmp_path)
    assert r["decision"] == "block"


# --------------------------------------------------------------------------- A loud preflight

def test_failclosed_block_carries_precise_preflight(monkeypatch, tmp_path):
    """The block reason names the EXACT missing piece (via preflight), not a generic hint, and the
    diagnostic is also emitted once to stderr so it surfaces in headless logs."""
    t = tmp_path / "transcript.jsonl"
    t.write_text('{"content":"running af-build for prd-team-app"}\n', encoding="utf-8")
    pf = _praxis.PreflightResult(
        False, "misconfig", "agent-factory", "default", "http://localhost:8000",
        ("COGNITO_CLIENT_ID is unset — set it in agent_factory/.env.",), ())
    monkeypatch.setattr(_praxis, "preflight", lambda **k: pf)

    r = _run(monkeypatch, incomplete=_boom, transcript_path=t, tmp_path=tmp_path)
    assert r["decision"] == "block"
    assert "PREFLIGHT" in r["reason"] and "COGNITO_CLIENT_ID" in r["reason"]
    assert "MISCONFIGURED" in r["reason"]
    assert "COGNITO_CLIENT_ID" in r["stderr"]  # shouted to stderr for headless diagnosability
