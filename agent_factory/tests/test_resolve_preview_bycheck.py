"""Locks the ``--by-check`` inverted view on ``tools/resolve_preview.py``.

Per-ticket, the tool answers "which checks land on THIS ticket". ``--by-check`` inverts that: for each
building-validation CHECK, which incomplete tickets does its ``applies_to`` pin onto — so an over-broad
predicate that bleeds one check across unrelated concerns is visible at a glance.

We avoid a live Praxis by monkeypatching the two module objects the TOOL holds:
``rp._praxis.incomplete_requirements`` (the ticket list) and ``rp._praxis.facts_by`` (the
building-validation check facts). Matching runs for real via the tool's ``_ticket_tagset`` +
``ts.normalize_tag`` primitives — the SAME normalization the live resolver uses.
"""

import importlib.util
import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

# Load the tool directly from its file (the ``src/agent_factory`` package shadows the
# ``agent_factory.tools`` namespace under pytest), exactly as test_resolve_preview_coverage does.
_TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "resolve_preview.py"
_spec = importlib.util.spec_from_file_location("resolve_preview_bycheck_uut", _TOOL_PATH)
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)


def _ticket(req_id, tags):
    return {"id": req_id, "meta": {"requirement_id": req_id, "verify": "automated",
                                   "tags": list(tags), "acceptance": "it works"}}


def _check(cid, applies_to):
    return {"id": cid, "meta": {"applies_to": list(applies_to)}}


# Two UNRELATED tickets both happen to carry the generic "secrets" tag, plus one auth-only ticket.
_T_BILLING = _ticket("R-billing", ["secrets", "billing"])
_T_UI = _ticket("R-ui", ["secrets", "ui"])
_T_AUTH = _ticket("R-auth", ["auth"])
_TICKETS = [_T_BILLING, _T_UI, _T_AUTH]

# A broad secrets check (bleeds across billing + ui), a narrow auth check, and a wildcard.
_CHECK_SECRETS = _check("secrets-scan", ["secrets"])
_CHECK_AUTH = _check("auth-e2e", ["auth"])
_CHECK_WILD = _check("typecheck", ["*"])
_CHECKS = [_CHECK_SECRETS, _CHECK_AUTH, _CHECK_WILD]


def _install(monkeypatch, tickets, checks):
    def fake_incomplete(project, *, exclude_leased=False, space=None, snapshot=None):
        return list(tickets)

    def fake_facts_by(category=None, meta=None, state="active", space=None, snapshot=None):
        assert category == "check"
        return list(checks)

    monkeypatch.setattr(rp._praxis, "incomplete_requirements", fake_incomplete)
    monkeypatch.setattr(rp._praxis, "facts_by", fake_facts_by)


def _block_for(out, check_id):
    """The lines from ``check: <check_id>`` up to the next ``check:`` header (or EOF)."""
    lines = out.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == f"check: {check_id}")
    end = start + 1
    while end < len(lines) and not lines[end].strip().startswith("check:"):
        end += 1
    return "\n".join(lines[start:end])


def test_broad_check_lists_all_unrelated_tickets(monkeypatch, capsys):
    _install(monkeypatch, _TICKETS, _CHECKS)
    rc = rp.main(["proj", "--by-check"])
    assert rc == 0
    block = _block_for(capsys.readouterr().out, "secrets-scan")
    # The over-broad secrets check bleeds onto BOTH unrelated tickets, and only those.
    assert "R-billing" in block
    assert "R-ui" in block
    assert "R-auth" not in block
    assert "fan-out 2" in block


def test_narrow_check_lands_on_only_its_ticket(monkeypatch, capsys):
    _install(monkeypatch, _TICKETS, _CHECKS)
    rp.main(["proj", "--by-check"])
    block = _block_for(capsys.readouterr().out, "auth-e2e")
    assert "R-auth" in block
    assert "R-billing" not in block
    assert "R-ui" not in block
    assert "fan-out 1" in block


def test_wildcard_check_lists_every_ticket(monkeypatch, capsys):
    _install(monkeypatch, _TICKETS, _CHECKS)
    rp.main(["proj", "--by-check"])
    block = _block_for(capsys.readouterr().out, "typecheck")
    for rid in ("R-billing", "R-ui", "R-auth"):
        assert rid in block
    assert "fan-out 3" in block
    assert "wildcard" in block


def test_broad_check_is_flagged_too_broad(monkeypatch, capsys):
    _install(monkeypatch, _TICKETS, _CHECKS)
    rp.main(["proj", "--by-check"])
    out = capsys.readouterr().out
    # The secrets check straddles unrelated concerns -> flagged; the narrow auth check is not.
    assert "TOO BROAD" in _block_for(out, "secrets-scan")
    assert "TOO BROAD" not in _block_for(out, "auth-e2e")


def test_default_view_is_unchanged_without_flag(monkeypatch, capsys):
    # No --by-check: the by-check header must NOT appear; the per-ticket path runs instead.
    def fake_resolve(ticket, project="", scope="validation", override=None):
        return []
    _install(monkeypatch, [_T_AUTH], _CHECKS)
    monkeypatch.setattr(rp.ts, "resolve_validation_requirements", fake_resolve)
    rp.main(["proj"])
    out = capsys.readouterr().out
    assert "by-check view" not in out
    assert "requirement_id: R-auth" in out
