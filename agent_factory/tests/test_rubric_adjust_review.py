"""U7: the CLI's pure ticket->observation folding + in-flight detection."""

from __future__ import annotations

from agent_factory.rubric_adjust import observations_from_tickets


def _ticket(build_state, pinned):
    return {"id": "R1", "meta": {"build_state": build_state, "pinned_checks": pinned}}


def _graded(src, passed, axis_scores, defects=(), vid="v1"):
    return {"validation_id": vid, "kind": "graded", "source_check_id": src, "passed": passed,
            "verdict": {"passed": passed, "axis_scores": axis_scores, "defects": list(defects)}}


def test_folds_graded_verdicts_into_observations():
    tickets = [_ticket("finished", [_graded("security-review", True, {"authz": 0.9})])]
    obs, in_flight = observations_from_tickets(tickets)
    assert len(obs) == 1 and obs[0].check_id == "security-review"
    assert obs[0].axis_scores == {"authz": 0.9} and obs[0].converged and not in_flight


def test_blocked_ticket_marks_non_converged():
    tickets = [_ticket("blocked", [_graded("chk", False, {"a": 0.3})])]
    obs, _ = observations_from_tickets(tickets)
    assert obs[0].converged is False


def test_in_progress_ticket_is_in_flight():
    tickets = [_ticket("in_progress", [_graded("chk", None, {"a": 0.5})])]
    _, in_flight = observations_from_tickets(tickets)
    assert in_flight == {"chk"}


def test_binary_and_unlinked_graded_entries_ignored():
    tickets = [_ticket("finished", [
        {"validation_id": "b1", "run": "pytest", "passed": True},        # binary
        {"validation_id": "g0", "kind": "graded", "passed": True,        # graded, no source link
         "verdict": {"axis_scores": {"a": 0.9}, "defects": []}},
    ])]
    obs, in_flight = observations_from_tickets(tickets)
    assert obs == [] and in_flight == set()


def test_reconstructs_defects_from_verdict():
    d = {"file": "x.py", "line": 4, "problem": "leak", "remedy": "close", "confidence": 7}
    tickets = [_ticket("finished", [_graded("chk", False, {"a": 0.9}, defects=[d])])]
    obs, _ = observations_from_tickets(tickets)
    assert obs[0].defects[0].problem == "leak" and obs[0].defects[0].confidence == 7
