"""U1 — the structural resumability probe (``agent_factory.resumability``).

A pure, offline predicate over a ticket's Praxis meta plus its already-resolved required-validation
set: is a cold worker able to reconstruct what "done" means from state alone? Resumable iff the ticket
is coverable-from-state — ``(non-empty acceptance) OR (non-empty resolved required_validations)`` — AND
a ``verify`` mode is set, AND every ``depends_on`` names a real plan requirement id (checked only when
the caller supplies the universe of known ids).

The coverability half deliberately MIRRORS ``contract_with_floor``'s own rule (acceptance floor OR
resolved checks) so the probe never starves a check-covered / acceptance-less ticket — the
false-positive the adversarial review caught. No Praxis calls: the caller passes the resolved set.
"""

from agent_factory.resumability import resumability_report


def test_fully_specified_ticket_is_resumable():
    meta = {"acceptance": "given X, system does Y observable via Z", "verify": "automated",
            "depends_on": ["R1"]}
    rep = resumability_report(meta, resolved_required=[], known_requirement_ids={"R1"})
    assert rep == {"resumable": True, "missing": []}


def test_acceptance_less_but_check_covered_is_resumable():
    # The regression the review flagged: a ticket with NO acceptance text but a non-empty resolved
    # required set is coverable-from-state and MUST NOT route back (mirrors the acceptance floor).
    meta = {"acceptance": "", "verify": "automated"}
    rep = resumability_report(meta, resolved_required=[{"id": "CHK-7"}])
    assert rep["resumable"] is True
    assert rep["missing"] == []


def test_no_acceptance_and_no_checks_misses_contract():
    meta = {"verify": "automated"}
    rep = resumability_report(meta, resolved_required=[])
    assert rep["resumable"] is False
    assert rep["missing"] == ["contract"]


def test_verify_unset_is_not_resumable():
    meta = {"acceptance": "it works"}  # no verify mode declared
    rep = resumability_report(meta, resolved_required=[])
    assert rep["resumable"] is False
    assert "verify" in rep["missing"]


def test_dangling_dependency_is_not_resumable():
    # depends_on naming a requirement that does not exist in the plan -> dangling -> surfaced.
    meta = {"acceptance": "it works", "verify": "automated", "depends_on": ["R99"]}
    rep = resumability_report(meta, resolved_required=[], known_requirement_ids={"R1", "R2"})
    assert rep["resumable"] is False
    assert "depends_on" in rep["missing"]


def test_real_dependency_is_resumable():
    meta = {"acceptance": "it works", "verify": "automated", "depends_on": ["R2"]}
    rep = resumability_report(meta, resolved_required=[], known_requirement_ids={"R1", "R2"})
    assert rep == {"resumable": True, "missing": []}


def test_dependency_check_skipped_without_a_known_universe():
    # With no universe supplied the caller cannot judge danglingness, so the dep dimension is a no-op
    # (never a false dangling). This is how the claim-time guard avoids flagging a FINISHED prereq
    # that has already dropped out of the live incomplete set.
    meta = {"acceptance": "it works", "verify": "automated", "depends_on": ["gone"]}
    rep = resumability_report(meta, resolved_required=[])
    assert rep == {"resumable": True, "missing": []}


def test_manual_ticket_with_human_signoff_acceptance_is_resumable():
    meta = {"acceptance": "the flow feels right to a human reviewer", "verify": "manual"}
    rep = resumability_report(meta, resolved_required=[])
    assert rep == {"resumable": True, "missing": []}


def test_missing_dimensions_are_reported_in_a_stable_order():
    # Everything absent: no contract, no verify -> both surfaced, contract first.
    rep = resumability_report({}, resolved_required=[])
    assert rep["resumable"] is False
    assert rep["missing"] == ["contract", "verify"]


def test_none_meta_is_handled():
    rep = resumability_report(None, resolved_required=None)
    assert rep["resumable"] is False
    assert "contract" in rep["missing"]
