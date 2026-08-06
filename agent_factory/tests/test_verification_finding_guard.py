"""A verification finding is not answered by changing nothing.

The post-merge verification round is the only thing that can see a defect living BETWEEN tickets --
two modules each individually green whose interfaces do not meet. It writes its judgement to
meta.regression_detail, but the completion gate reads only pinned checks, so the finding is prose
competing against "all your checks are green". Prose loses: a ticket was regressed with a report
naming the defect, its evidence and the required fix, and closed again TWICE with its file untouched,
because its tests hand-built the very shape the finding said was wrong.

The guard deliberately does NOT block completion on an open finding: verification runs only AFTER a
ticket finishes and merges, so that would deadlock -- the ticket could never reach the verification
that clears it. It fires only on the observed failure: finished, finding open, zero commits.
"""

import sys
from pathlib import Path

for _p in (str(Path(__file__).resolve().parents[1] / "src"),
           str(Path(__file__).resolve().parents[1] / "hooks")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _ticket_state as ts  # noqa: E402

FINDING = {"reason": "geometry and identity each define their own AcquisitionUnit",
           "evidence": "derive_flight_ids raises AttributeError on a geometry unit",
           "required_fix": "unify the type or carry a shared join key"}


def test_zero_commits_against_an_open_finding_is_refused():
    why = ts.finding_unanswered_without_change({"regression_detail": FINDING}, 0)
    assert why and "changed nothing" in why
    assert "AcquisitionUnit" in why, "the operator must see WHICH finding went unanswered"


def test_any_real_change_satisfies_it():
    """Non-negotiable: the guard must never be able to deadlock a ticket."""
    assert ts.finding_unanswered_without_change({"regression_detail": FINDING}, 1) is None


def test_a_resolved_finding_no_longer_gates():
    meta = {"regression_detail": dict(FINDING, resolved=True)}
    assert ts.finding_unanswered_without_change(meta, 0) is None
    assert ts.open_finding(meta) is None


def test_a_ticket_with_no_finding_is_untouched():
    for meta in ({}, {"regression_detail": None}, {"regression_detail": {}},
                 {"regression_detail": {"reason": "   "}}):
        assert ts.finding_unanswered_without_change(meta, 0) is None
        assert ts.open_finding(meta) is None


def test_resolve_finding_marks_it_answered():
    out = ts.resolve_finding({"regression_detail": FINDING})
    assert out["resolved"] is True
    assert out["reason"] == FINDING["reason"], "the report survives for the audit trail"
