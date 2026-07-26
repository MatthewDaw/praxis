"""R5: dispatching a remote job is a separate action from building it.

The dispatching session must not claim a ticket and must not stamp a whole-set run
marker: ``hooks/build_completeness_gate.py`` arms (and blocks the session's turn) only
when the session owns a live claim or a non-stale run marker. A dispatcher that touched
either would block its own turn against the gate it just armed.

These tests run fully offline: the ticket-state mutators are monkeypatched to detect
any call, and the gate itself is driven with a monkeypatched ``incomplete_requirements``
(the same harness ``test_build_gate_scenarios.py`` uses), so no Praxis network is
needed.
"""

from __future__ import annotations

import ast
import io
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HOOKS = str(_REPO_ROOT / "agent_factory" / "hooks")
_AF_SRC = str(_REPO_ROOT / "agent_factory" / "src")
# ``agent_factory`` is a namespace package: this repo root ALSO contributes an
# ``agent_factory`` portion (docs/skills/etc, no ``resumability`` submodule), so
# ``_AF_SRC`` (which has the real ``agent_factory.resumability``) must be on sys.path
# BEFORE ``agent_factory`` is imported for the first time — once a namespace package is
# bound in sys.modules from a partial portion, adding sibling portions afterwards does
# not reliably extend its already-resolved ``__path__``.
for _p in (_AF_SRC, _HOOKS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _praxis  # noqa: E402
import _ticket_state as ts  # noqa: E402
import build_completeness_gate as gate  # noqa: E402

from knowledge.serve.dispatch import dispatch_job

DISPATCH_SRC = (Path(__file__).resolve().parent.parent / "dispatch.py").read_text(encoding="utf-8")

OWNER = "dispatching-session"


def test_dispatch_returns_a_queued_job():
    job = dispatch_job("acme-app", "prd-acme-app")
    assert job.project == "acme-app"
    assert job.snapshot == "prd-acme-app"
    assert job.state == "queued"
    assert job.id


def test_dispatch_requires_project_and_snapshot():
    with pytest.raises(ValueError):
        dispatch_job("", "prd-acme-app")
    with pytest.raises(ValueError):
        dispatch_job("acme-app", "")


def test_dispatch_module_never_references_ticket_state():
    """Structural guarantee (R5): dispatch.py does not import ``_ticket_state`` at all,
    so it cannot reach ``claim``/``stamp_run`` even indirectly."""
    tree = ast.parse(DISPATCH_SRC)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    assert not any("_ticket_state" in n for n in names)


def test_dispatch_does_not_claim_or_stamp_run_marker(monkeypatch):
    calls = []
    monkeypatch.setattr(ts, "claim", lambda *a, **k: calls.append("claim") or True)
    monkeypatch.setattr(ts, "stamp_run", lambda *a, **k: calls.append("stamp_run"))

    dispatch_job("acme-app", "prd-acme-app")

    assert calls == []


def _run_gate(monkeypatch, items, session=OWNER):
    """Drive the real Stop-hook entrypoint offline (mirrors test_build_gate_scenarios.py)."""
    monkeypatch.setattr(_praxis, "incomplete_requirements", lambda project, **k: items)
    monkeypatch.setenv("FACTORY_PROJECT", "prd-acme-app")
    monkeypatch.delenv("FACTORY_GATE_DISABLED", raising=False)
    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(json.dumps({"session_id": session, "cwd": "/x/acme-app"})),
    )
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    with pytest.raises(SystemExit):
        gate.main()
    out = buf.getvalue().strip()
    parsed = json.loads(out) if out else {}
    return "block" if parsed.get("decision") == "block" else "allow"


def test_dispatching_session_ends_its_turn_without_the_gate_blocking(monkeypatch):
    """AE1 / the ticket's acceptance condition: given a session that dispatches a
    remote job, after dispatch it holds zero ticket claims and zero run markers, and
    its turn ends without the completeness gate blocking."""
    job = dispatch_job("acme-app", "prd-acme-app")
    assert job.state == "queued"

    # The dispatching session's own claim/run-marker footprint on the incomplete set is
    # empty — no requirement item carries this session as claim_owner or run_owner —
    # exactly what a dispatch (as opposed to a build) leaves behind.
    other_owner_item = {
        "id": "R1", "text": "unrelated in-flight ticket",
        "meta": {"requirement_id": "R1", "build_state": "incomplete"},
    }
    assert _run_gate(monkeypatch, [other_owner_item], session=OWNER) == "allow"
