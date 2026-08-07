"""The universal lane must LAND, and must never fail silently.

Regression for the sotos build of 2026-07-31: every finished ticket carried ``universal_contract: []``
and no ``minimalism-dry`` entry in ``required_validations``. Cause: the build host's ``python3`` was
3.9, which has no stdlib ``tomllib``, so ``_universal_checks``' lazy
``from agent_factory.seeded_checks import ...`` raised ``ModuleNotFoundError`` — and a bare
``except Exception: return []`` turned "the quality gates could not load" into "there are no quality
gates", silently, across a whole run.

Two properties are pinned here:
  * END-TO-END, against the REAL ``seeded_checks.toml`` (no ``_universal_checks`` monkeypatch —
    the stubbed tests in ``test_promote_universal_gating.py`` could not have caught this): a normal
    non-exempt ticket's contract CONTAINS the promoted check as ``kind="graded"``; an exempt one
    does not.
  * A LOADING FAILURE IS NEVER AN EMPTY LANE: it is recovered out-of-process, or it raises.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402
from agent_factory.seeded_checks import universal_seeded_checks  # noqa: E402

# Whatever the shipped library promotes — the test asserts the WIRING, not a hardcoded check name,
# but it does insist the library actually promotes something (a `promote_universal` flag that reaches
# no ticket is precisely the bug).
_PROMOTED = [c.check_id for c in universal_seeded_checks() if c.rubric is not None]


def _promoted_for(tags):
    """The promoted ids a ticket with ``tags`` should carry: ["*"] universals always, a tag-scoped
    universal (applies_to narrower than ["*"]) only on intersecting tags."""
    norm = {ts.normalize_tag(t) for t in tags}
    out = []
    for c in universal_seeded_checks():
        if c.rubric is None:
            continue
        offer = {ts.normalize_tag(t) for t in c.applies_to}
        if "*" in offer or (offer & norm):
            out.append(c.check_id)
    return out


def _entries(reqs):
    return {r["id"]: r for r in reqs if isinstance(r, dict) and (r.get("meta") or {}).get("universal")}


def _break_in_process_import(monkeypatch):
    """Make the lazy ``from agent_factory.seeded_checks import ...`` fail the way Python 3.9 does —
    a ``None`` entry in ``sys.modules`` makes the import statement raise ImportError."""
    monkeypatch.setitem(sys.modules, "agent_factory.seeded_checks", None)


# ------------------------------------------------------------------ end-to-end, real seeded library

def test_library_promotes_at_least_one_graded_universal():
    assert _PROMOTED, "seeded_checks.toml declares no graded promote_universal check"


def test_non_exempt_ticket_contract_contains_the_universal_graded_entry():
    # The exact shape of a real ticket: plain feature tags, no declared paths, no exemption.
    tags = ["chatbot", "backend", "auth"]
    reqs = ts.contract_with_floor("CHAT1", "given X the system does Y", resolved=[],
                                  ticket_meta={"tags": tags}, paths=[])
    universal = _entries(reqs)
    expected = _promoted_for(tags)
    assert set(universal) == set(expected)
    for cid, entry in universal.items():
        assert entry["meta"]["kind"] == "graded"
        assert entry["meta"]["rubric"]["axes"]  # serialized, not a live Rubric object
        assert entry["meta"]["source_check_id"] == cid
    # …and it is part of the pinned coverage contract, not a decoration.
    assert set(expected) <= {ts._check_id(r) for r in reqs}


def test_ui_ticket_carries_the_tag_scoped_surface_universal_and_backend_does_not():
    """The farming_analysis regression, end-to-end against the REAL library: a ui-tagged ticket's
    MANDATORY contract contains rendered-surface-has-substance as a gating graded entry, with no
    authoring agent opting in; a backend ticket's does not."""
    assert "rendered-surface-has-substance" in _PROMOTED  # the library actually promotes it

    ui = _entries(ts.contract_with_floor("R25", "map view renders", resolved=[],
                                         ticket_meta={"tags": ["ui", "map"]}, paths=[]))
    assert "rendered-surface-has-substance" in ui
    assert ui["rendered-surface-has-substance"]["meta"]["report_only"] is False  # it GATES

    backend = _entries(ts.contract_with_floor("B1", "api returns rows", resolved=[],
                                              ticket_meta={"tags": ["backend"]}, paths=[]))
    assert "rendered-surface-has-substance" not in backend


@pytest.mark.parametrize("meta", [
    {"tags": ["config"]},
    {"tags": ["vendored"]},
    {"tags": ["generated"]},
    {"tags": ["backend"], "universal_exempt": True},
])
def test_exempt_ticket_contract_contains_no_universal_entry(meta):
    reqs = ts.contract_with_floor("T1", "acc", resolved=[], ticket_meta=meta)
    assert _entries(reqs) == {}


def test_path_exempt_ticket_contains_no_universal_entry():
    reqs = ts.contract_with_floor("T1", "acc", resolved=[], ticket_meta={"tags": ["backend"]},
                                  paths=["backend/src/migrations/0007_add_col.sql"])
    assert _entries(reqs) == {}


# --------------------------------------------------------------- a loading failure is never silent

def test_import_failure_recovers_out_of_process_rather_than_emptying_the_lane(monkeypatch, capsys):
    monkeypatch.setenv("PRAXIS_HOOK_PYTHON", sys.executable)
    _break_in_process_import(monkeypatch)

    assert [c.check_id for c in ts._universal_checks()] == [c.check_id for c in universal_seeded_checks()]
    assert "cannot import agent_factory.seeded_checks" in capsys.readouterr().err

    # The recovered records carry an already-serialized rubric; the injector must still emit graded
    # entries identical in shape to the in-process path.
    reqs = ts.contract_with_floor("T1", "acc", resolved=[], ticket_meta={"tags": ["backend"]})
    universal = _entries(reqs)
    assert set(universal) == set(_promoted_for(["backend"]))
    assert all(e["meta"]["kind"] == "graded" and e["meta"]["rubric"]["axes"] for e in universal.values())


def test_unrecoverable_load_raises_instead_of_returning_an_empty_lane(monkeypatch):
    _break_in_process_import(monkeypatch)
    monkeypatch.setattr(ts, "_universal_checks_out_of_process", lambda: None)

    with pytest.raises(ts.UniversalLaneUnavailable):
        ts._universal_checks()


def test_unrecoverable_load_fails_the_contract_rather_than_pinning_an_ungated_one(monkeypatch):
    """The whole point: a build must NOT be able to pin a contract whose universal lane silently
    vanished. contract_with_floor propagates, so start_ticket cannot pin an un-gated ticket."""
    _break_in_process_import(monkeypatch)
    monkeypatch.setattr(ts, "_universal_checks_out_of_process", lambda: None)

    with pytest.raises(ts.UniversalLaneUnavailable):
        ts.contract_with_floor("T1", "acc", resolved=[], ticket_meta={"tags": ["backend"]})


def test_exempt_ticket_still_short_circuits_before_the_loader(monkeypatch):
    """Exemption is decided from meta alone, so an exempt ticket is unaffected by a broken loader —
    the genuinely-optional behaviour that must survive the loudness change."""
    _break_in_process_import(monkeypatch)
    monkeypatch.setattr(ts, "_universal_checks_out_of_process", lambda: None)

    assert ts.contract_with_floor("T1", "acc", resolved=[], ticket_meta={"tags": ["config"]}) != []
    assert _entries(ts.contract_with_floor("T1", "acc", resolved=[],
                                           ticket_meta={"tags": ["config"]})) == {}


def test_sidecar_candidates_exclude_the_interpreter_that_already_failed():
    assert sys.executable not in ts._sidecar_pythons()
