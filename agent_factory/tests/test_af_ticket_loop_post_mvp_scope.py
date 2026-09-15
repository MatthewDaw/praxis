"""The loop must never dispatch, or wait on, a post-MVP ticket.

build_target already excludes scope=post-mvp from the build, but ready_batch and claimable did not, so
the loop dispatched PM6 (a budget-gated Cognito migration) the moment its prerequisite finished
(mvpvue, 2026-09-15). These tests run the SHIPPED shell functions against stub Praxis modules.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"

STUB_PRAXIS = """
def facts_by(**kw):
    return [
        {"id": "f1", "meta": {"requirement_id": "T05", "scope": "mvp"}},
        {"id": "f2", "meta": {"requirement_id": "PM6", "scope": "post-mvp"}},
        {"id": "f3", "meta": {"requirement_id": "T06"}},
    ]
"""
STUB_TS = """
def ready_tickets(facts):
    return list(facts)
def parked_on_manual(t, ref):
    return False
def owes_work(t):
    return True
"""


def _function(name: str) -> str:
    text = SCRIPT.read_text()
    start = text.index(f"\n{name}(){{")
    end = text.index("\n}\n", start)
    return text[start + 1 : end + 3]


def _run(tmp: Path, call: str) -> str:
    (tmp / "_praxis.py").write_text(STUB_PRAXIS)
    (tmp / "_ticket_state.py").write_text(STUB_TS)
    prog = f"PY={sys.executable}\nPROJECT=proj\n" + _function(call.split()[0]) + "\n" + call
    res = subprocess.run(["bash", "-c", prog], capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=str(tmp)))
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def test_a_ready_post_mvp_ticket_is_never_dispatched(tmp_path: Path) -> None:
    assert _run(tmp_path, "ready_batch 15").split() == ["T05", "T06"]


def test_post_mvp_tickets_do_not_hold_the_drain_gate_open(tmp_path: Path) -> None:
    assert _run(tmp_path, "claimable") == "2"
