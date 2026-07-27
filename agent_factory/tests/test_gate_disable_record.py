"""Acceptance test for S7: gate stand-down records disable variables on project marker.

Verifies that when a Stop gate stands down because a factory disable variable
(FACTORY_GATE_DISABLED, FACTORY_PLAN_GATE_DISABLED) or the Praxis auth disable
variable (PRAXIS_AUTH_DISABLED) is set, the gate writes the variable name and
observed value onto the project's Praxis build marker. Also verifies clearing.
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
import _ticket_state as ts  # noqa: E402


class FakePraxis:
    """In-memory Praxis that tracks build-marker writes."""

    def __init__(self):
        self._facts = {}          # cid -> meta dict
        self._scopes = {}         # cid -> scope
        self._seq = 0

    def get_fact(self, cid, *, space=None, snapshot=None, not_found_ok=False):
        if cid not in self._facts:
            if not_found_ok:
                return {}
            raise _praxis.PraxisUnreachable(f"404 {cid}")
        return {"id": cid, "meta": dict(self._facts[cid])}

    def patch_meta(self, cid, meta_dict, *, space=None, snapshot=None):
        meta = self._facts.setdefault(cid, {})
        for k, v in meta_dict.items():
            if v is None:
                meta.pop(k, None)
            else:
                meta[k] = v
        return {"id": cid, "meta": dict(meta)}

    def facts_by(self, category=None, meta=None, *, space=None, snapshot=None):
        if category == ts.BUILD_MARKER_CATEGORY:
            return [{"id": cid, "scope": scope, "meta": dict(self._facts.get(cid) or {})}
                    for cid, scope in self._scopes.items()]
        if category == ts.PLANNING_MARKER_CATEGORY:
            return [{"id": cid, "scope": scope, "meta": dict(self._facts.get(cid) or {})}
                    for cid, scope in self._scopes.items()
                    if cid.startswith("generated-planning")]
        return []

    def ensure_build_marker(self, project, *, space=None, snapshot=None):
        for cid, scope in self._scopes.items():
            if scope == project and cid.startswith("generated-build"):
                return cid
        self._seq += 1
        cid = f"generated-build-marker-{self._seq}"
        self._scopes[cid] = project
        self._facts.setdefault(cid, {})
        return cid

    def ensure_planning_marker(self, project, *, space=None, snapshot=None):
        for cid, scope in self._scopes.items():
            if scope == project and cid.startswith("generated-planning"):
                return cid
        self._seq += 1
        cid = f"generated-planning-marker-{self._seq}"
        self._scopes[cid] = project
        self._facts.setdefault(cid, {})
        return cid

    # functions that gates call via the _praxis module
    def incomplete_requirements(self, project, **k):
        return []

    def _load_dotenv(self):
        pass

    def preflight(self, **k):
        return _praxis.PreflightResult(
            True, "ok", "team-app", "org", "http://localhost:8000", (), ())


def _install(monkeypatch):
    """Install FakePraxis into _ticket_state and the _praxis module."""
    fake = FakePraxis()
    monkeypatch.setattr(ts, "_praxis", fake)
    # Also patch the real _praxis module functions the gates call
    monkeypatch.setattr(_praxis, "_load_dotenv", fake._load_dotenv)
    monkeypatch.setattr(_praxis, "incomplete_requirements", fake.incomplete_requirements)
    monkeypatch.setattr(_praxis, "preflight", fake.preflight)
    return fake


OWNER = "sess-A"


def _run_build_gate(monkeypatch, env_vars=None, session=OWNER):
    """Drive build_completeness_gate.main() with controlled env. The gate MUST import inside
    this function (after monkeypatching is set up)."""
    import build_completeness_gate as gate  # noqa: E402
    if env_vars is None:
        env_vars = {}
    monkeypatch.delenv("FACTORY_GATE_DISABLED", raising=False)
    monkeypatch.delenv("PRAXIS_AUTH_DISABLED", raising=False)
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)
    # Skip session_touched (no transcript -> fall through to Praxis read)
    monkeypatch.setattr(gate, "session_touched", lambda path, signals: None)
    monkeypatch.setattr(gate, "_active_project", lambda cwd: "prd-team-app")
    monkeypatch.setattr(gate, "_session_owner", lambda d: session)
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO(json.dumps({"session_id": session, "cwd": "/x/team-app"})))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    try:
        with pytest.raises(SystemExit):
            gate.main()
    finally:
        os.environ.pop("FACTORY_PROJECT", None)
    out = buf.getvalue().strip()
    parsed = json.loads(out) if out else {}
    decision = "block" if parsed.get("decision") == "block" else "allow"
    advice = (parsed.get("hookSpecificOutput") or {}).get("additionalContext", "")
    return {"decision": decision, "reason": advice}


def _run_plan_gate(monkeypatch, env_vars=None, session=OWNER):
    """Drive plan_completeness_gate.main() with controlled env."""
    import plan_completeness_gate as gate  # noqa: E402
    if env_vars is None:
        env_vars = {}
    monkeypatch.delenv("FACTORY_PLAN_GATE_DISABLED", raising=False)
    monkeypatch.delenv("PRAXIS_AUTH_DISABLED", raising=False)
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(gate, "session_touched", lambda path, signals: None)
    monkeypatch.setattr(gate, "_active_project", lambda cwd: "prd-team-app")
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO(json.dumps({"session_id": session, "cwd": "/x/team-app"})))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    try:
        with pytest.raises(SystemExit):
            gate.main()
    finally:
        os.environ.pop("FACTORY_PROJECT", None)
    out = buf.getvalue().strip()
    parsed = json.loads(out) if out else {}
    decision = "block" if parsed.get("decision") == "block" else "allow"
    advice = (parsed.get("hookSpecificOutput") or {}).get("additionalContext", "")
    return {"decision": decision, "reason": advice}


# --------------------------------------------------------------------------- build marker id + lifecycle


def test_build_marker_project_strips_prefix():
    assert ts.build_marker_project("team-app") == "team-app"
    assert ts.build_marker_project("prd-team-app") == "team-app"


def test_build_marker_id_empty_until_created(monkeypatch):
    _install(monkeypatch)
    assert ts.build_marker_id("team-app") == ""


def test_build_marker_bootstrapped_and_idempotent(monkeypatch):
    _install(monkeypatch)
    mid = ts.build_marker_id("team-app", create=True)
    assert mid
    assert ts.build_marker_id("prd-team-app", create=True) == mid
    assert ts.build_marker_id("team-app") == mid


def test_build_marker_stamp_binds_to_plan_snapshot(monkeypatch):
    """stamp_gate_disable writes to the build marker in the plan snapshot."""
    fake = _install(monkeypatch)
    mid = ts.build_marker_id("team-app", create=True)
    r = ts.stamp_gate_disable("team-app", "FACTORY_GATE_DISABLED", "1")
    assert r["id"] == mid
    meta = fake._facts[mid]
    assert meta.get(ts.M_GATE_DISABLED_AT) is not None
    assert meta.get(ts.M_GATE_DISABLE_VARS) == {"FACTORY_GATE_DISABLED": "1"}


def test_gate_disable_vars_accumulate(monkeypatch):
    """Multiple disable vars are accumulated, not overwritten."""
    fake = _install(monkeypatch)
    mid = ts.build_marker_id("team-app", create=True)
    ts.stamp_gate_disable("team-app", "FACTORY_GATE_DISABLED", "1")
    ts.stamp_gate_disable("team-app", "PRAXIS_AUTH_DISABLED", "1")
    meta = fake._facts[mid]
    assert meta.get(ts.M_GATE_DISABLE_VARS) == {
        "FACTORY_GATE_DISABLED": "1",
        "PRAXIS_AUTH_DISABLED": "1",
    }


def test_clear_gate_disable(monkeypatch):
    """clear_gate_disable removes the disable marker."""
    fake = _install(monkeypatch)
    mid = ts.build_marker_id("team-app", create=True)
    ts.stamp_gate_disable("team-app", "FACTORY_GATE_DISABLED", "1")
    ts.clear_gate_disable("team-app")
    meta = fake._facts[mid]
    assert meta.get(ts.M_GATE_DISABLED_AT) is None
    assert meta.get(ts.M_GATE_DISABLE_VARS) is None


def test_clear_is_noop_when_never_stamped(monkeypatch):
    _install(monkeypatch)
    assert ts.clear_gate_disable("never-stamped") is True


def test_gate_disable_vars_empty_when_never_stamped(monkeypatch):
    """Reading disable vars when never stamped returns empty dict."""
    _install(monkeypatch)
    assert ts.gate_disable_vars("team-app") == {}


# --------------------------------------------------------------------------- build gate stands down + records


def test_build_gate_stands_down_and_records_factory_disable(monkeypatch):
    """FACTORY_GATE_DISABLED=1 => stand down AND stamp the marker."""
    fake = _install(monkeypatch)
    r = _run_build_gate(monkeypatch, env_vars={"FACTORY_GATE_DISABLED": "1"})
    assert r["decision"] == "allow"
    assert "STOOD DOWN" in r["reason"]
    assert "FACTORY_GATE_DISABLED" in r["reason"]

    # Marker must be stamped
    mid = ts.build_marker_id("team-app")
    assert mid
    meta = fake._facts.get(mid, {})
    assert meta.get(ts.M_GATE_DISABLE_VARS) == {"FACTORY_GATE_DISABLED": "1"}


def test_build_gate_stands_down_and_records_praxis_auth_disable(monkeypatch):
    """PRAXIS_AUTH_DISABLED=1 => stand down AND stamp the marker."""
    fake = _install(monkeypatch)
    r = _run_build_gate(monkeypatch, env_vars={"PRAXIS_AUTH_DISABLED": "1"})
    assert r["decision"] == "allow"
    assert "STOOD DOWN" in r["reason"]
    assert "PRAXIS_AUTH_DISABLED" in r["reason"]

    # Marker must be stamped
    mid = ts.build_marker_id("team-app")
    assert mid
    meta = fake._facts.get(mid, {})
    assert meta.get(ts.M_GATE_DISABLE_VARS) == {"PRAXIS_AUTH_DISABLED": "1"}


def test_build_gate_records_both_disable_vars(monkeypatch):
    """Both FACTORY_GATE_DISABLED=1 and PRAXIS_AUTH_DISABLED=1 set => both recorded."""
    fake = _install(monkeypatch)
    r = _run_build_gate(monkeypatch,
                        env_vars={"FACTORY_GATE_DISABLED": "1", "PRAXIS_AUTH_DISABLED": "1"})
    assert r["decision"] == "allow"

    mid = ts.build_marker_id("team-app")
    assert mid
    meta = fake._facts.get(mid, {})
    assert meta.get(ts.M_GATE_DISABLE_VARS) == {
        "FACTORY_GATE_DISABLED": "1",
        "PRAXIS_AUTH_DISABLED": "1",
    }


def test_build_gate_no_disable_does_not_stamp(monkeypatch):
    """Without any disable var, the gate does NOT stamp the marker."""
    fake = _install(monkeypatch)
    # No disable var set -> gate proceeds to arming/enforce, but with empty incomplete
    # set and no marker/claim, it will ALLOW as inert.
    r = _run_build_gate(monkeypatch)
    assert r["decision"] == "allow"

    # No build marker stamped
    mid = ts.build_marker_id("team-app")
    assert mid == ""
    # Ensure marker was never created
    build_markers = [c for c, s in fake._scopes.items() if s == "team-app"]
    assert len(build_markers) == 0


def test_build_gate_clear_on_terminate(monkeypatch):
    """After a stand-down, clear_gate_disable removes the records."""
    fake = _install(monkeypatch)
    # First, stand down with disable
    _run_build_gate(monkeypatch, env_vars={"FACTORY_GATE_DISABLED": "1"})
    mid = ts.build_marker_id("team-app")
    assert mid
    assert fake._facts[mid].get(ts.M_GATE_DISABLE_VARS) == {"FACTORY_GATE_DISABLED": "1"}

    # Now clear
    ts.clear_gate_disable("team-app")
    meta = fake._facts[mid]
    assert meta.get(ts.M_GATE_DISABLED_AT) is None
    assert meta.get(ts.M_GATE_DISABLE_VARS) is None


# --------------------------------------------------------------------------- plan gate stands down + records


def test_plan_gate_stands_down_and_records_plan_disable(monkeypatch):
    """FACTORY_PLAN_GATE_DISABLED=1 => stand down AND stamp the marker."""
    fake = _install(monkeypatch)
    r = _run_plan_gate(monkeypatch, env_vars={"FACTORY_PLAN_GATE_DISABLED": "1"})
    assert r["decision"] == "allow"
    assert "STOOD DOWN" in r["reason"]
    assert "FACTORY_PLAN_GATE_DISABLED" in r["reason"]

    # Marker must be stamped
    mid = ts.build_marker_id("team-app")
    assert mid
    meta = fake._facts.get(mid, {})
    assert meta.get(ts.M_GATE_DISABLE_VARS) == {"FACTORY_PLAN_GATE_DISABLED": "1"}


def test_plan_gate_stands_down_and_records_auth_disable(monkeypatch):
    """PRAXIS_AUTH_DISABLED=1 => plan gate stands down AND stamps the marker."""
    fake = _install(monkeypatch)
    r = _run_plan_gate(monkeypatch, env_vars={"PRAXIS_AUTH_DISABLED": "1"})
    assert r["decision"] == "allow"
    assert "STOOD DOWN" in r["reason"]
    assert "PRAXIS_AUTH_DISABLED" in r["reason"]

    # Marker must be stamped
    mid = ts.build_marker_id("team-app")
    assert mid
    meta = fake._facts.get(mid, {})
    assert meta.get(ts.M_GATE_DISABLE_VARS) == {"PRAXIS_AUTH_DISABLED": "1"}


# --------------------------------------------------------------------------- report readability


def test_gate_disable_vars_readable_for_report(monkeypatch):
    """gate_disable_vars returns a dict readable by a report."""
    _install(monkeypatch)
    ts.build_marker_id("team-app", create=True)
    ts.stamp_gate_disable("team-app", "FACTORY_GATE_DISABLED", "1")
    ts.stamp_gate_disable("team-app", "PRAXIS_AUTH_DISABLED", "1")

    vars_dict = ts.gate_disable_vars("team-app")
    assert isinstance(vars_dict, dict)
    assert vars_dict["FACTORY_GATE_DISABLED"] == "1"
    assert vars_dict["PRAXIS_AUTH_DISABLED"] == "1"
    # Report can render this:
    report_line = ", ".join(f"{k}={v}" for k, v in sorted(vars_dict.items()))
    assert "FACTORY_GATE_DISABLED=1" in report_line
    assert "PRAXIS_AUTH_DISABLED=1" in report_line
