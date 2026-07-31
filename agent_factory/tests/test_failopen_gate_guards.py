"""Fail-open sweep — the Stop gates must never turn "I could not determine" into "nothing required".

The bug class: an exception handler or conservative fallback that DISABLES a gate rather than
tripping it. These pin the two gate-side instances found:

* ``build_completeness_gate._plan_escalation_check`` swallowed every non-PlanEscalationError into
  ``return ""`` (== the plan is clear), directly contradicting its own documented contract that an
  unreadable escalation counter refuses the build phase.
* Both Stop gates caught a crash in their own logic and ``sys.exit(0)`` — an ALLOW — in total
  silence, so a gate that stopped gating reported nothing anywhere.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[1] / "hooks"


def _load_gate(monkeypatch, module_name: str, file_name: str):
    """Import a Stop-hook gate module under a private name with its own dir importable."""
    monkeypatch.syspath_prepend(str(_HOOKS))
    spec = importlib.util.spec_from_file_location(module_name, _HOOKS / file_name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- escalation guard


class PlanEscalationError(Exception):
    """Mirrors ``_ticket_state.PlanEscalationError`` — the gate catches it by identity, not name."""


def _install_ticket_state(monkeypatch, is_plan_blocked):
    stub = types.ModuleType("_ticket_state")
    stub.PlanEscalationError = PlanEscalationError
    stub.is_plan_blocked = is_plan_blocked
    monkeypatch.setitem(sys.modules, "_ticket_state", stub)
    return stub


def test_escalation_guard_blocks_when_state_is_unreadable(monkeypatch):
    """An unexpected error reading the escalation state must BLOCK, not read as 'plan is clear'."""
    gate = _load_gate(monkeypatch, "_failopen_build_gate", "build_completeness_gate.py")

    def boom(_project):
        raise TypeError("planning marker meta is not a dict")

    _install_ticket_state(monkeypatch, boom)
    reason = gate._plan_escalation_check("prd-demo")
    assert reason, "an unreadable escalation state must produce a block reason, not silence"
    assert "UNREADABLE" in reason
    assert "TypeError" in reason


def test_escalation_guard_still_blocks_on_corrupt_counter(monkeypatch):
    gate = _load_gate(monkeypatch, "_failopen_build_gate2", "build_completeness_gate.py")

    def corrupt(_project):
        raise PlanEscalationError("counter is garbage")

    _install_ticket_state(monkeypatch, corrupt)
    reason = gate._plan_escalation_check("prd-demo")
    assert "CORRUPT ESCALATION STATE" in reason


def test_escalation_guard_defers_praxis_unreachable_to_the_fail_closed_read(monkeypatch):
    """A transport failure returns "" ONLY so main()'s PRAXIS UNREACHABLE block (which also blocks,
    with the preflight diagnostic) owns the message. It is a deferral to a better block, not a pass."""
    gate = _load_gate(monkeypatch, "_failopen_build_gate3", "build_completeness_gate.py")
    import _praxis  # noqa: PLC0415 - resolved via the hooks dir prepended above

    def unreachable(_project):
        raise _praxis.PraxisUnreachable("connection refused")

    _install_ticket_state(monkeypatch, unreachable)
    assert gate._plan_escalation_check("prd-demo") == ""


def test_escalation_guard_is_clear_when_plan_is_not_blocked(monkeypatch):
    gate = _load_gate(monkeypatch, "_failopen_build_gate4", "build_completeness_gate.py")
    _install_ticket_state(monkeypatch, lambda _project: False)
    assert gate._plan_escalation_check("prd-demo") == ""


# --------------------------------------------------------------------------- crash is never silent


@pytest.mark.parametrize("file_name,banner", [
    ("build_completeness_gate.py", "[build-completeness gate] GATE CRASHED"),
    ("plan_completeness_gate.py", "[plan-completeness gate] GATE CRASHED"),
])
def test_gate_crash_is_loud_on_stderr(tmp_path, file_name, banner):
    """A crash in the gate's own logic still exits 0 (never wedge the agent) but must SHOUT — a
    gate that stops enforcing in silence is the exact failure this sweep exists to remove."""
    gate_src = _HOOKS / file_name
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import io, os, sys\n"
        f"sys.path.insert(0, {str(_HOOKS)!r})\n"
        # Empty stdin -> main() falls through to ``cwd = data.get('cwd') or os.getcwd()``, which is
        # OUTSIDE every try in the gate. Detonating there is a faithful stand-in for a bug in the
        # gate's own logic, which is what the top-level handler exists to catch.
        "sys.stdin = io.StringIO('')\n"
        "def _boom():\n"
        "    raise RuntimeError('synthetic gate bug')\n"
        "os.getcwd = _boom\n"
        f"src = open({str(gate_src)!r}).read()\n"
        "ns = {'__name__': '__main__', '__file__': " + repr(str(gate_src)) + "}\n"
        "exec(compile(src, ns['__file__'], 'exec'), ns)\n",
        encoding="utf-8",
    )
    proc = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, "a gate bug must not wedge the agent"
    assert banner in proc.stderr
    assert "synthetic gate bug" in proc.stderr
