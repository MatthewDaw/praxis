"""Locks the duplicate-``run`` collapse in ``hooks/_ticket_state.py``.

A plan accumulates checks that execute the SAME command — typically a universal
``applies_to:["*"]`` gate plus older lane-scoped checks left over from an earlier phase. Resolving
all of them makes every ticket run that suite two or three times for one exit code's worth of proof.
The collapse folds them to one survivor and records what it stands in for, so the coverage contract
shrinks in RUNTIME but never in ACCOUNTABILITY.

The invariants that matter:
  * identical commands collapse; the ``["*"]`` universal survives a lane-scoped duplicate
  * a check with no ``run`` is never collapsed (a graded entry's identity is its text)
  * gating and candidate partitions collapse SEPARATELY — a gate is never folded into a non-gate
  * the survivor names its losers, and the source fact dict is not mutated
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402

SUITE = "npm --prefix backend run build && npm --prefix backend test"


class _DBSpy:
    """In-memory checks DB mirroring the server's array-membership match on ``meta.applies_to``."""

    def __init__(self, checks=None):
        self._checks = checks or []

    def facts_by(self, category=None, meta=None, state="active", space=None, snapshot=None):
        want = (meta or {}).get("applies_to")
        return [c for c in self._checks
                if want is None or want in ((c.get("meta") or {}).get("applies_to") or [])]

    def surface_checks(self, project, screen_id, scope=None, space=None, snapshot=None):
        return []

    def context(self, query, top_k=10, as_of=None, space=None, snapshot=None):
        return []

    def get_fact(self, cid):
        return {"id": cid, "text": cid, "meta": {}}


def _check(cid, applies_to, run=SUITE, scope="validation", candidate=False):
    meta = {"applies_to": applies_to, "scope": scope, "run": run}
    if candidate:
        meta["candidate"] = True
    return {"id": cid, "category": "check", "scope": scope, "meta": meta}


def _install(monkeypatch, checks):
    monkeypatch.setattr(ts, "_praxis", _DBSpy(checks=checks))


def _ticket(tags):
    return {"id": "T1", "meta": {"tags": tags}}


def _ids(checks):
    return sorted(c["id"] for c in checks)


# --------------------------------------------------------------------------- the unit

def test_identical_commands_collapse_to_one():
    got = ts.collapse_duplicate_runs([_check("a", ["backend"]), _check("b", ["phase-1"])])
    assert len(got) == 1


def test_whitespace_differences_are_the_same_command():
    spaced = _check("b", ["phase-1"], run="npm --prefix backend run build   &&  npm --prefix backend test")
    assert len(ts.collapse_duplicate_runs([_check("a", ["backend"]), spaced])) == 1


def test_different_commands_both_survive():
    got = ts.collapse_duplicate_runs([_check("a", ["backend"]),
                                      _check("b", ["frontend"], run="npm --prefix frontend test")])
    assert _ids(got) == ["a", "b"]


def test_wildcard_universal_wins_over_lane_scoped_duplicate():
    # The broadest applicability survives: a ["*"] gate subsumes a tag-scoped copy of the same command.
    got = ts.collapse_duplicate_runs([_check("lane", ["phase-1"]), _check("universal", ["*"])])
    assert [c["id"] for c in got] == ["universal"]


def test_survivor_names_what_it_stands_in_for():
    got = ts.collapse_duplicate_runs([_check("universal", ["*"]),
                                      _check("zzz", ["phase-1"]), _check("aaa", ["backend"])])
    assert got[0]["meta"]["collapsed_duplicates"] == ["aaa", "zzz"]  # deterministic, id-ordered


def test_collapse_does_not_mutate_the_source_fact():
    winner = _check("universal", ["*"])
    ts.collapse_duplicate_runs([winner, _check("lane", ["phase-1"])])
    assert "collapsed_duplicates" not in winner["meta"]


def test_commandless_checks_are_never_collapsed():
    # Graded/rubric entries carry run="" — their identity is their text, so two are still two.
    got = ts.collapse_duplicate_runs([_check("g1", ["*"], run=""), _check("g2", ["*"], run="")])
    assert _ids(got) == ["g1", "g2"]


def test_a_lone_check_is_untouched():
    got = ts.collapse_duplicate_runs([_check("only", ["backend"])])
    assert got == [_check("only", ["backend"])]


# --------------------------------------------------------------------------- through the resolver

def test_resolution_runs_the_suite_once_for_a_multi_tag_ticket(monkeypatch):
    # The sotos shape: a universal gate plus two legacy lane checks running the identical suite.
    _install(monkeypatch, [_check("sotos-universal-backend-green", ["*"]),
                           _check("sotos-backend-vitest", ["backend"]),
                           _check("sotos-phase1-compile-test", ["phase-1"])])
    got = ts.resolve_validation_requirements(_ticket(["backend", "phase-1"]), project="p")
    assert [c["id"] for c in got] == ["sotos-universal-backend-green"]


def test_distinct_lane_checks_still_all_resolve(monkeypatch):
    _install(monkeypatch, [_check("universal", ["*"]),
                           _check("frontend", ["frontend"], run="npm --prefix frontend test"),
                           _check("e2e", ["e2e"], run="npm run test:e2e")])
    got = ts.resolve_validation_requirements(_ticket(["frontend", "e2e"]), project="p")
    assert _ids(got) == ["e2e", "frontend", "universal"]


def test_a_gate_is_never_collapsed_into_a_candidate(monkeypatch):
    # Partitions collapse separately. Were they collapsed together, the candidate could win and the
    # gating check would vanish from the coverage contract — a silent loss of a gate.
    _install(monkeypatch, [_check("gate", ["backend"]),
                           _check("pooled", ["backend"], candidate=True)])
    tkt = _ticket(["backend"])
    assert [c["id"] for c in ts.resolve_validation_requirements(tkt, project="p")] == ["gate"]
    assert [c["id"] for c in ts.pool_candidates(tkt, project="p")] == ["pooled"]
