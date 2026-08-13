"""BUG E (python half) — a finding whose fix already landed in a PRIOR round must be answerable
WITHOUT a fresh commit this round.

Real incident: a ticket carried two open findings (``resolved=False``) whose fix had ALREADY been
committed in an earlier round. When the ticket finished with no NEW commit this round, the guard
demanded "a commit to answer the finding" and regressed it every ~9 min forever. The findings had
``check_id=None``, so auto-suspend (keyed by check_id) could never fire either.

This half is the ingestion/hook RECOGNITION (the loop's round-scoped streak bound is another agent's
half). ``finding_unanswered_without_change`` now recognizes a finding as answered, with no fresh
commit, when either:
  * its NAMED check passes on the ticket's recorded pinned validations (symptom demonstrably gone), or
  * the caller (a verification round) asserts ``symptom_gone=True`` — the closeable path for an
    unattributed ``check_id=None`` finding, which no per-check pass can ever answer.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402


def _finding(check_id=None, resolved=False):
    f = {"reason": "derive_flight_ids raises on a geometry unit",
         "evidence": "AttributeError", "required_fix": "unify the type", "resolved": resolved}
    if check_id is not None:
        f["check_id"] = check_id
    return f


def _pinned(cid, passed):
    return [{"validation_id": "v1", "covers": [cid], "passed": passed, "run": "x"}]


# --------------------------------------------------------------------------- named-check-passes clause

def test_prior_commit_fix_closes_when_named_check_passes_without_a_new_commit():
    # The finding names check-x; the ticket's pinned validation for check-x PASSES on the current
    # tree (the earlier round's fix). Zero commits THIS round must not regress it.
    meta = {"regression_detail": [_finding(check_id="check-x")],
            ts.M_PINNED_CHECKS: _pinned("check-x", True)}
    assert ts.finding_unanswered_without_change(meta, 0) is None


def test_named_check_still_failing_keeps_the_finding_unanswered():
    meta = {"regression_detail": [_finding(check_id="check-x")],
            ts.M_PINNED_CHECKS: _pinned("check-x", False)}
    why = ts.finding_unanswered_without_change(meta, 0)
    assert why and "changed nothing" in why


def test_a_passing_sibling_check_never_answers_a_different_finding():
    # check-y passing must not close a finding attributed to check-x (R17 scoping preserved).
    meta = {"regression_detail": [_finding(check_id="check-x")],
            ts.M_PINNED_CHECKS: _pinned("check-y", True)}
    assert ts.finding_unanswered_without_change(meta, 0) is not None


# --------------------------------------------------------------------------- check_id=None closeability

def test_unattributed_finding_is_closeable_when_the_round_confirms_symptom_gone():
    meta = {"regression_detail": [_finding(check_id=None)]}
    # Default (undetermined) — behavior unchanged: still unanswered on zero commits.
    assert ts.finding_unanswered_without_change(meta, 0) is not None
    # The verification round positively re-verified the tree: closeable without a fresh commit.
    assert ts.finding_unanswered_without_change(meta, 0, symptom_gone=True) is None


def test_unattributed_finding_closes_via_resolve_finding():
    # The other closeable route: the round stamps it resolved; the guard then passes.
    meta = {"regression_detail": [_finding(check_id=None)]}
    resolved = ts.resolve_finding(meta)
    assert all(d["resolved"] for d in resolved)
    assert ts.finding_unanswered_without_change({"regression_detail": resolved}, 0) is None


# --------------------------------------------------------------------------- unchanged legacy behavior

def test_zero_commits_no_passing_check_still_refuses():
    """Non-negotiable: with no fix evidence at all, a zero-commit finish still stands unanswered."""
    meta = {"regression_detail": [_finding(check_id="check-x")]}   # no pinned validations recorded
    why = ts.finding_unanswered_without_change(meta, 0)
    assert why and "changed nothing" in why


def test_any_real_change_still_satisfies_it():
    meta = {"regression_detail": [_finding(check_id="check-x")]}
    assert ts.finding_unanswered_without_change(meta, 1) is None
