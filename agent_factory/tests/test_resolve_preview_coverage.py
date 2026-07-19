"""Locks the ``--require-coverage`` (alias ``--assert-covered``) coverage gate on
``tools/resolve_preview.py``.

The tool is READ-ONLY and derives its floor-only-automated signal from the SAME resolution path the
live build uses (``resolve_validation_requirements`` -> ``contract_with_floor``). Here we avoid a live
Praxis by monkeypatching the two module objects the TOOL holds: ``rp._praxis.incomplete_requirements``
(the ticket list) and ``rp.ts.resolve_validation_requirements`` (the canned DECLARED checks per ticket).
``contract_with_floor`` / ``project_ref`` / ``_lane_of`` run for real — they are pure and offline.

A ticket's declared checks are ZERO (floor-only) iff ``resolve_validation_requirements`` returns [] —
the floor is added ONLY by ``contract_with_floor``. Automated + floor-only == a coverage gap the flag
must catch; MANUAL floor-only is exempt (its floor is a human sign-off).
"""

import importlib.util
import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

# The tool lives at agent_factory/tools/resolve_preview.py, but under pytest the real
# ``src/agent_factory`` package shadows the ``agent_factory.tools`` namespace path, so a plain
# ``import agent_factory.tools.resolve_preview`` can't reach it. Load it directly from its file
# (it self-inserts hooks/ onto sys.path at import, exactly as the ``-m`` invocation does), then
# monkeypatch the ``_praxis`` / ``ts`` module objects it holds.
_TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "resolve_preview.py"
_spec = importlib.util.spec_from_file_location("resolve_preview_under_test", _TOOL_PATH)
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)


def _ticket(req_id, verify, tags, acceptance="it works"):
    return {"id": req_id, "meta": {"requirement_id": req_id, "verify": verify,
                                   "tags": list(tags), "acceptance": acceptance}}


# Canned tickets keyed by requirement_id; the DECLARED checks each resolves to are the value.
# [] declared checks == floor-only (only the acceptance floor would cover it).
_AUTO_FLOOR = _ticket("R-auto-floor", "automated", [])
_MANUAL_FLOOR = _ticket("R-manual-floor", "manual", [])
_AUTO_COVERED = _ticket("R-auto-covered", "automated", ["auth"])

_DECLARED = {
    "R-auto-floor": [],
    "R-manual-floor": [],
    "R-auto-covered": [{"id": "auth-e2e", "meta": {"applies_to": ["auth"]}}],
}


def _install(monkeypatch, tickets):
    """Patch the two module objects the tool holds so no live Praxis is touched."""
    def fake_incomplete(project, *, exclude_leased=False, space=None, snapshot=None):
        return list(tickets)

    def fake_resolve(ticket, project="", scope="validation", override=None):
        req_id = (ticket.get("meta") or {}).get("requirement_id") or ticket.get("id")
        return list(_DECLARED[req_id])

    monkeypatch.setattr(rp._praxis, "incomplete_requirements", fake_incomplete)
    monkeypatch.setattr(rp.ts, "resolve_validation_requirements", fake_resolve)


def test_automated_floor_only_fails_and_names_requirement(monkeypatch, capsys):
    _install(monkeypatch, [_AUTO_FLOOR])
    rc = rp.main(["team-app", "--require-coverage"])
    err = capsys.readouterr().err
    assert rc != 0                                   # coverage gap -> non-zero exit
    assert "R-auto-floor" in err                     # names the offending requirement
    assert "--require-coverage" in err               # clear attribution


def test_flag_is_opt_in_no_flag_returns_zero(monkeypatch, capsys):
    _install(monkeypatch, [_AUTO_FLOOR])             # SAME floor-only plan...
    rc = rp.main(["team-app"])                        # ...but no flag
    assert rc == 0                                    # opt-in: silent success


def test_manual_floor_only_is_exempt(monkeypatch, capsys):
    _install(monkeypatch, [_MANUAL_FLOOR])
    rc = rp.main(["team-app", "--require-coverage"])
    assert rc == 0                                    # manual floor is a human sign-off, not a gap


def test_automated_with_declared_check_passes(monkeypatch, capsys):
    _install(monkeypatch, [_AUTO_COVERED])
    rc = rp.main(["team-app", "--require-coverage"])
    assert rc == 0                                    # a real declared check -> covered


def test_alias_assert_covered_also_gates(monkeypatch, capsys):
    _install(monkeypatch, [_AUTO_FLOOR])
    rc = rp.main(["team-app", "--assert-covered"])    # alias behaves identically
    err = capsys.readouterr().err
    assert rc != 0
    assert "R-auto-floor" in err


def test_mixed_plan_gates_only_the_automated_floor_only(monkeypatch, capsys):
    _install(monkeypatch, [_AUTO_FLOOR, _MANUAL_FLOOR, _AUTO_COVERED])
    rc = rp.main(["team-app", "--require-coverage"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "R-auto-floor" in err                      # the only true gap is named
    assert "R-manual-floor" not in err                # manual exempt
    assert "R-auto-covered" not in err                # covered
