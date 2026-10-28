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


# --------------------------------------------------------------------------- retired hook entry points


@pytest.mark.parametrize("file_name", ["build_completeness_gate.py", "plan_completeness_gate.py"])
def test_retired_hook_exits_without_loading_praxis_or_reading_stdin(tmp_path, file_name):
    """An already-running Claude may still invoke the old path during an outage."""
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import builtins, os, runpy, socket, sys\n"
        "def forbidden(*args, **kwargs):\n"
        "    raise AssertionError('retired hook performed work')\n"
        "original_import = builtins.__import__\n"
        "def guarded_import(name, *args, **kwargs):\n"
        "    if name in ('_gate_common', '_praxis', '_ticket_state'):\n"
        "        forbidden()\n"
        "    return original_import(name, *args, **kwargs)\n"
        "builtins.__import__ = guarded_import\n"
        "socket.socket.connect = forbidden\n"
        "sys.stdin = type('UnreadableInput', (), {'read': forbidden, 'readline': forbidden})()\n"
        "os.environ['FACTORY_PROJECT'] = 'sports-analysis'\n"
        "os.environ['PRAXIS_API_BASE_URL'] = 'http://127.0.0.1:1'\n"
        f"runpy.run_path({str(_HOOKS / file_name)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    proc = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True, timeout=5)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_completeness_hooks_are_not_registered():
    import json

    registration = json.loads((_HOOKS / "hooks.json").read_text())
    commands = [
        hook.get("command", "")
        for groups in registration["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert not any("completeness_gate" in command for command in commands)
