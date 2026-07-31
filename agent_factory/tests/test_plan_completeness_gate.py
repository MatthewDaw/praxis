"""Locks U6: the ``plan_completeness`` Stop hook — arm / enforce / bounded-escalation / fail-closed.

Offline: ``planning_active`` and the ENFORCE predicates are monkeypatched, so we drive the whole
ARM -> ENFORCE -> BLOCK/ALLOW -> escalation path with no network. Attempt state is isolated to a
tmp identity-cache dir (``PRAXIS_MCP_CACHE``).
"""

import io
import json
import sys
from pathlib import Path

import pytest

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _praxis  # noqa: E402
import _ticket_state as ts  # noqa: E402
import plan_completeness_gate as gate  # noqa: E402

OWNER = "sess-A"

# S8: per-_run() mutable state for the in-memory attempt-counter mock (replaces the old local file
# the production gate no longer uses). Cleared between test functions via the autouse fixture below.
_MOCK_ATTEMPTS: dict[str, int] = {}  # snapshot_hash -> attempts


@pytest.fixture(autouse=True)
def _clear_mock_attempts():
    """S8: reset the in-memory attempt counter before each test function."""
    _MOCK_ATTEMPTS.clear()
    yield


def _run(monkeypatch, tmp_path, *, active=True, predicates=None, transcript_path=None,
         planning_active_exc=None):
    monkeypatch.setenv("FACTORY_PROJECT", "team-app")
    monkeypatch.delenv("FACTORY_PLAN_GATE_DISABLED", raising=False)
    monkeypatch.setenv("PRAXIS_MCP_CACHE", str(tmp_path / "mcp.json"))  # isolate the attempts file

    if planning_active_exc is not None:
        def _raise(project, owner=None, now=None):
            raise planning_active_exc
        monkeypatch.setattr(ts, "planning_active", _raise)
    else:
        monkeypatch.setattr(ts, "planning_active", lambda project, owner=None, now=None: active)

    monkeypatch.setattr(gate, "_snapshot_hash", lambda project: "HASH1")
    if predicates is not None:
        monkeypatch.setattr(gate, "_PREDICATES", tuple(predicates))
    # S8: the gate's attempt-tracking now stores escalation state durably in Praxis. The tests
    # run against an ephemeral project with no Praxis space, so monkeypatch them back to a
    # simple in-memory counter (byte-behavior of the old local-file approach) — the production
    # gate's Praxis-stored path is tested by the ticket's own eval validations.
    def _mock_read(snapshot_hash):
        return _MOCK_ATTEMPTS.get(snapshot_hash, 0)
    def _mock_bump(snapshot_hash):
        n = _MOCK_ATTEMPTS.get(snapshot_hash, 0) + 1
        _MOCK_ATTEMPTS[snapshot_hash] = n
        return n
    def _mock_reset():
        _MOCK_ATTEMPTS.clear()
    monkeypatch.setattr(gate, "_read_attempts", _mock_read)
    monkeypatch.setattr(gate, "_bump_attempts", _mock_bump)
    monkeypatch.setattr(gate, "_reset_attempts", _mock_reset)
    # keep preflight offline for the fail-closed diagnostic
    monkeypatch.setattr(_praxis, "preflight", lambda **k: _praxis.PreflightResult(
        ok=False, kind="unreachable", org="o", org_source="default", api_base="x",
        failures=("down",), warnings=()))

    payload = {"session_id": OWNER, "cwd": "/x/team-app"}
    if transcript_path is not None:
        payload["transcript_path"] = str(transcript_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setattr(sys, "stderr", err)
    with pytest.raises(SystemExit):
        gate.main()
    out = buf.getvalue().strip()
    parsed = json.loads(out) if out else {}
    decision = parsed.get("decision", "allow")
    reason = parsed.get("reason", "") if decision == "block" else \
        (parsed.get("hookSpecificOutput") or {}).get("additionalContext", "")
    return {"decision": decision, "reason": reason}


def _pass(project):
    return True, ""


def _fail(reason):
    def _p(project):
        return False, reason
    return _p


# --------------------------------------------------------------------------- ARM

def test_inactive_planning_allows_inert(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, active=False, predicates=[_pass])
    assert r["decision"] == "allow"
    assert r["reason"] == ""   # byte-identical to no hook


def test_scoped_escape_allows_and_notes_build_unaffected(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_PLAN_GATE_DISABLED", "1")
    monkeypatch.setattr(ts, "planning_active", lambda *a, **k: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": OWNER, "cwd": "/x/team-app"})))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    with pytest.raises(SystemExit):
        gate.main()
    parsed = json.loads(buf.getvalue().strip())
    assert "decision" not in parsed  # allow
    ctx = parsed["hookSpecificOutput"]["additionalContext"]
    assert "STOOD DOWN" in ctx and "build enforcement is UNAFFECTED" in ctx


# --------------------------------------------------------------------------- ENFORCE

def test_all_predicates_pass_auto_blesses(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, active=True, predicates=[_pass, _pass, _pass])
    assert r["decision"] == "allow"
    assert "BLESSES" in r["reason"]


def test_plan_gate_failure_blocks_with_reason(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, active=True,
             predicates=[_fail("plan_gate REJECTED (1): [R-CONTRACT-SIGNED] no signed contract")])
    assert r["decision"] == "block"
    assert "R-CONTRACT-SIGNED" in r["reason"]
    assert "1/3" in r["reason"]


def test_contradiction_gap_blocks(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, active=True,
             predicates=[_pass, _fail("contradiction detection has NOT run")])
    assert r["decision"] == "block"
    assert "contradiction detection has NOT run" in r["reason"]


def test_lens_gap_blocks(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, active=True,
             predicates=[_pass, _pass, _fail("lens coverage is INCOMPLETE")])
    assert r["decision"] == "block"
    assert "lens coverage is INCOMPLETE" in r["reason"]


# --------------------------------------------------------------------------- _lens_coverage_complete (S6)
#
# S6: resolving planning-validation lenses against an EMPTY snapshot must return an explicit
# zero-resolved signal that the gate treats as INCOMPLETE coverage (BLOCK), never as trivially
# complete. A non-empty snapshot must still be evaluated normally (gap-by-gap).

def test_lens_coverage_zero_resolved_blocks_not_trivially_complete(monkeypatch):
    """Empty planning-validation snapshot -> resolve returns [] -> predicate must BLOCK naming
    the zero-resolved case, not auto-pass as 'trivially complete'."""
    monkeypatch.setattr(ts, "resolve_validation_requirements", lambda *a, **k: [])
    monkeypatch.setattr(ts, "planning_marker_id", lambda project: "marker-1")
    monkeypatch.setattr(ts, "planning_project", lambda project: project)

    ok, reason = gate._lens_coverage_complete("team-app")

    assert ok is False
    assert "zero" in reason.lower()
    assert "resolved" in reason.lower()


def test_lens_coverage_nonempty_fully_covered_passes(monkeypatch):
    """A non-empty snapshot where every lens is covered still evaluates normally -> complete."""
    monkeypatch.setattr(ts, "resolve_validation_requirements",
                        lambda *a, **k: [{"id": "lens-1", "meta": {"covered": True}}])
    monkeypatch.setattr(ts, "planning_marker_id", lambda project: "marker-1")
    monkeypatch.setattr(ts, "planning_project", lambda project: project)

    ok, reason = gate._lens_coverage_complete("team-app")

    assert ok is True
    assert reason == ""


def test_lens_coverage_nonempty_with_gap_blocks_normally(monkeypatch):
    """A non-empty snapshot with an uncovered lens still names that lens (unchanged behavior)."""
    monkeypatch.setattr(ts, "resolve_validation_requirements",
                        lambda *a, **k: [{"id": "lens-1", "meta": {"covered": True}},
                                          {"id": "lens-2", "meta": {}}])
    monkeypatch.setattr(ts, "planning_marker_id", lambda project: "marker-1")
    monkeypatch.setattr(ts, "planning_project", lambda project: project)

    ok, reason = gate._lens_coverage_complete("team-app")

    assert ok is False
    assert "lens-2" in reason
    assert "lens-1" not in reason


# --------------------------------------------------------------------------- bounded escalation (KTD5)

def test_k_failures_on_unchanged_snapshot_terminates(monkeypatch, tmp_path):
    preds = [_fail("unresolvable contradiction")]
    # default cap is 3: attempts 1 and 2 BLOCK, attempt 3 escalates to a terminal ALLOW.
    r1 = _run(monkeypatch, tmp_path, active=True, predicates=preds)
    r2 = _run(monkeypatch, tmp_path, active=True, predicates=preds)
    r3 = _run(monkeypatch, tmp_path, active=True, predicates=preds)
    assert r1["decision"] == "block" and "1/3" in r1["reason"]
    assert r2["decision"] == "block" and "2/3" in r2["reason"]
    assert r3["decision"] == "allow"
    assert "TERMINAL plan_blocked" in r3["reason"]
    assert "HUMAN is required" in r3["reason"]


def test_changed_snapshot_resets_attempts(monkeypatch, tmp_path):
    preds = [_fail("nope")]
    _run(monkeypatch, tmp_path, active=True, predicates=preds)          # attempt 1/3 on HASH1
    # a new snapshot hash resets the counter -> back to attempt 1/3.
    monkeypatch.setattr(gate, "_snapshot_hash", lambda project: "HASH2")
    monkeypatch.setattr(ts, "planning_active", lambda *a, **k: True)
    monkeypatch.setattr(gate, "_PREDICATES", tuple(preds))
    monkeypatch.setenv("PRAXIS_MCP_CACHE", str(tmp_path / "mcp.json"))
    monkeypatch.setenv("FACTORY_PROJECT", "team-app")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": OWNER, "cwd": "/x/team-app"})))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    with pytest.raises(SystemExit):
        gate.main()
    parsed = json.loads(buf.getvalue().strip())
    assert parsed.get("decision") == "block"
    assert "1/3" in parsed["reason"]


# --------------------------------------------------------------------------- fail-closed + no-op

def test_praxis_unreachable_blocks_loudly(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, active=True,
             planning_active_exc=_praxis.PraxisUnreachable("connection refused"))
    assert r["decision"] == "block"
    assert "PRAXIS UNREACHABLE" in r["reason"]
    assert "FACTORY_PLAN_GATE_DISABLED" in r["reason"]


def test_noop_transcript_allows_without_praxis(monkeypatch, tmp_path):
    # a transcript with ZERO planning signal stands the gate down WITHOUT any Praxis read.
    t = tmp_path / "transcript.jsonl"
    t.write_text("just some ordinary chat about the weather\n", encoding="utf-8")

    def _boom(project, owner=None, now=None):
        raise AssertionError("planning_active must not be consulted for a no-op session")
    monkeypatch.setattr(ts, "planning_active", _boom)
    monkeypatch.setenv("PRAXIS_MCP_CACHE", str(tmp_path / "mcp.json"))
    monkeypatch.setenv("FACTORY_PROJECT", "team-app")
    monkeypatch.delenv("FACTORY_PLAN_GATE_DISABLED", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"session_id": OWNER, "cwd": "/x/team-app", "transcript_path": str(t)})))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    with pytest.raises(SystemExit):
        gate.main()
    assert buf.getvalue().strip() == ""   # inert, byte-identical to no hook
