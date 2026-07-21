"""The routing predicate that sends a requirement TICKET to the identity-keyed upsert (no DB needed).

``_is_requirement_ticket`` decides whether an ``add_insight`` write is a build unit (route to
``_requirement_upsert``, which never text-merges) or a plain requirement assertion (leave on the
normal reconciled dedup path). A ticket is ``category="requirement"`` carrying ``meta.build_state``
— the shape af-intake-plan's AMEND (C0), the WORK-review remediation emit, and the plan panel mint.
This locks the scoping so the fix changes ONLY the add-a-ticket flows, never full-intake extraction.
"""

from __future__ import annotations

from knowledge.serve.app import _is_requirement_ticket


def test_requirement_with_build_state_is_a_ticket():
    assert _is_requirement_ticket("requirement", {"build_state": "incomplete"}) is True
    assert _is_requirement_ticket("requirement", {"build_state": "finished", "requirement_id": "R1"}) is True


def test_requirement_without_build_state_is_not_a_ticket():
    # A plain requirement ASSERTION (full-intake extraction) carries no build_state -> normal path.
    assert _is_requirement_ticket("requirement", {"tags": ["auth"]}) is False
    assert _is_requirement_ticket("requirement", {}) is False
    assert _is_requirement_ticket("requirement", None) is False
    assert _is_requirement_ticket("requirement", {"build_state": ""}) is False  # blank is absent


def test_non_requirement_categories_are_never_tickets():
    assert _is_requirement_ticket("check", {"build_state": "incomplete"}) is False
    assert _is_requirement_ticket("learning", {"build_state": "incomplete"}) is False
    assert _is_requirement_ticket(None, {"build_state": "incomplete"}) is False
    assert _is_requirement_ticket("", {"build_state": "incomplete"}) is False
