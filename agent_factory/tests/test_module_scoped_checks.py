"""Locks module-scoped check execution in ``hooks/_ticket_state.py``.

A monorepo's universal ``npm --prefix backend test`` gate makes a frontend-only ticket run the whole
backend suite to prove nothing about its own change. ``scope_checks_to_changes`` splits a resolved
set into (run, skip) against the ticket's diff, deriving each check's module from the command itself
(``--prefix``/``cd``/``-C``) unless the author declared ``meta.when_changed``.

The three fail-SAFE rules are the point of the test file — a silently skipped gate is worse than a
slow one, so skipping happens only on positive evidence:

  1. an unscoped check (no predicate, no inferable module) always runs
  2. an unknown diff runs everything
  3. a change outside every known module root runs everything (root config, shared/, CI)
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402

BE = "npm --prefix backend run build && npm --prefix backend test"
FE = "cd frontend && npm run test:run"


def _check(cid, run, when_changed=None):
    meta = {"applies_to": ["*"], "scope": "validation", "run": run}
    if when_changed is not None:
        meta["when_changed"] = when_changed
    return {"id": cid, "category": "check", "scope": "validation", "meta": meta}


def _ids(checks):
    return sorted(c["id"] for c in checks)


# --------------------------------------------------------------------------- module inference

def test_npm_prefix_names_the_module():
    assert ts.infer_module_roots(_check("be", BE)) == ["backend"]


def test_cd_names_the_module():
    assert ts.infer_module_roots(_check("fe", FE)) == ["frontend"]


def test_make_dash_c_names_the_module():
    assert ts.infer_module_roots(_check("svc", "make -C service-a test")) == ["service-a"]


def test_a_repo_wide_command_names_no_module():
    assert ts.infer_module_roots(_check("knip", "npx --yes knip")) == []


def test_a_declared_predicate_beats_inference():
    chk = _check("be", BE, when_changed=["shared/**", "backend/**"])
    assert ts.check_scope_globs(chk) == ["shared/**", "backend/**"]


# --------------------------------------------------------------------------- the split

def test_a_frontend_only_change_skips_the_backend_suite():
    run, skip = ts.scope_checks_to_changes([_check("be", BE), _check("fe", FE)],
                                           ["frontend/src/pages/Chatbot.tsx"])
    assert _ids(run) == ["fe"] and _ids(skip) == ["be"]


def test_a_backend_only_change_skips_the_frontend_suite():
    run, skip = ts.scope_checks_to_changes([_check("be", BE), _check("fe", FE)],
                                           ["backend/src/services/chatbot.ts"])
    assert _ids(run) == ["be"] and _ids(skip) == ["fe"]


def test_a_change_touching_both_runs_both():
    run, skip = ts.scope_checks_to_changes(
        [_check("be", BE), _check("fe", FE)],
        ["backend/src/x.ts", "frontend/src/y.tsx"])
    assert _ids(run) == ["be", "fe"] and skip == []


def test_a_skipped_check_records_why():
    _, skip = ts.scope_checks_to_changes([_check("be", BE), _check("fe", FE)], ["frontend/a.tsx"])
    assert "no changed path matches" in skip[0]["meta"]["skipped_reason"]


def test_scoping_does_not_mutate_the_source_fact():
    be = _check("be", BE)
    ts.scope_checks_to_changes([be, _check("fe", FE)], ["frontend/a.tsx"])
    assert "skipped_reason" not in be["meta"]


# --------------------------------------------------------------------------- the fail-safe rules

def test_rule_1_an_unscoped_check_always_runs():
    # `npx knip` names no module — absence of a predicate is not "applies to nothing".
    run, skip = ts.scope_checks_to_changes([_check("knip", "npx --yes knip"), _check("fe", FE)],
                                           ["frontend/a.tsx"])
    assert _ids(run) == ["fe", "knip"] and skip == []


def test_rule_2_an_unknown_diff_runs_everything():
    for diff in ([], None, ["  "]):
        run, skip = ts.scope_checks_to_changes([_check("be", BE), _check("fe", FE)], diff)
        assert _ids(run) == ["be", "fe"] and skip == []


def test_rule_3_a_root_level_change_runs_everything():
    # A root package.json / CI config edit has unbounded blast radius — never scope it away.
    run, skip = ts.scope_checks_to_changes([_check("be", BE), _check("fe", FE)],
                                           ["package.json", "frontend/a.tsx"])
    assert _ids(run) == ["be", "fe"] and skip == []


def test_rule_3_a_shared_directory_change_runs_everything():
    run, skip = ts.scope_checks_to_changes([_check("be", BE), _check("fe", FE)],
                                           ["shared/types/patient.ts"])
    assert _ids(run) == ["be", "fe"] and skip == []


def test_a_declared_predicate_can_widen_a_module_scoped_check():
    # Declaring shared/** on the backend gate keeps it running for cross-cutting type changes while
    # still skipping it for frontend-only edits.
    be = _check("be", BE, when_changed=["backend/**", "shared/**"])
    fe = _check("fe", FE, when_changed=["frontend/**", "shared/**"])
    run, skip = ts.scope_checks_to_changes([be, fe], ["shared/types/patient.ts"])
    assert _ids(run) == ["be", "fe"] and skip == []
    run, skip = ts.scope_checks_to_changes([be, fe], ["frontend/a.tsx"])
    assert _ids(run) == ["fe"] and _ids(skip) == ["be"]


def test_inference_can_be_turned_off():
    run, skip = ts.scope_checks_to_changes([_check("be", BE), _check("fe", FE)],
                                           ["frontend/a.tsx"], infer=False)
    assert _ids(run) == ["be", "fe"] and skip == []
