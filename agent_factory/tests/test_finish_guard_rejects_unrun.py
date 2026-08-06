"""A pinned validation that never ran must not count as a pass.

`passed=None` means "this check was never executed" -- the state a pinned check
sits in between being resolved and being run. It is NOT a verdict, and a ticket
carrying one has not been verified.

Observed 2026-08-06 on appeal_engine COV-1B: a worker re-claimed the ticket,
re-pinned its checks (which resets each to None), and was then interrupted. Four
checks that had previously passed sat at None. The gate itself behaved
correctly; the ticket was nonetheless marked finished, because a human path
wrote build_state directly and never consulted this function. These tests pin
the gate's half of that contract so a refactor cannot quietly turn `None` into a
pass -- `bool(None)` being falsy is load-bearing, not incidental.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402


def _ticket(*passed_values):
    """One ticket with one required requirement covered by N pinned checks."""
    return {
        "meta": {
            "required_validations": ["r1"],
            "pinned_checks": [
                {
                    "covers": ["r1"],
                    "passed": p,
                    "run": f"cmd-{i}",
                    "validation_id": f"v{i}",
                }
                for i, p in enumerate(passed_values)
            ],
        }
    }


def test_all_passed_is_the_only_way_through():
    assert ts.all_validations_passed(_ticket(True)) is True


def test_a_single_unrun_check_blocks_completion():
    """The COV-1B case: some checks green, one never executed."""
    assert ts.all_validations_passed(_ticket(True, None)) is False


def test_every_check_unrun_blocks_completion():
    assert ts.all_validations_passed(_ticket(None)) is False


def test_a_failing_check_blocks_completion():
    assert ts.all_validations_passed(_ticket(True, False)) is False


def test_unrun_is_not_rescued_by_a_later_passing_check():
    """Order must not matter: a None anywhere in the set is disqualifying."""
    assert ts.all_validations_passed(_ticket(None, True, True)) is False


def test_no_pinned_checks_is_not_a_pass():
    """A ticket cannot self-certify 'nothing to check, therefore done'."""
    assert ts.all_validations_passed({"meta": {"required_validations": ["r1"], "pinned_checks": []}}) is False


def test_no_required_validations_is_not_a_pass():
    assert ts.all_validations_passed({"meta": {"required_validations": [], "pinned_checks": []}}) is False
