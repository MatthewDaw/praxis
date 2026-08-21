"""R12 acceptance: af-clean's located-finding admission rules.

A finding is discarded, not reported, unless it clears every mechanical admission rule below.
Each rejection records a machine-readable reason (never a silent drop) so a caller can audit why
a candidate finding never became output.

Acceptance (verbatim from the ticket): a finding with no file:line is dropped with a recorded
reason; a judgment-tier finding with no enumerated chunks is dropped with a recorded reason; a
finding with no declared bloat/fragmentation pole is dropped with a recorded reason; a DRY finding
with no co-change or parameter-accretion observable is dropped; an inline proposal against a
helper with 3+ live callers is refused; and a consolidation needing a flag per caller is rejected
as failed centralization.
"""

from __future__ import annotations

from agent_factory.af_clean.findings import (
    CLASS_CODE_DELETION,
    Finding,
    Location,
    admit_finding,
)


def _base_finding(**overrides) -> Finding:
    defaults = dict(
        rule="wrong-abstraction",
        tier="enforce",
        location=Location(file="pkg/mod.py", line=42),
        pole="bloat",
    )
    defaults.update(overrides)
    return Finding(**defaults)


def test_well_formed_finding_is_admitted():
    finding = _base_finding()
    verdict = admit_finding(finding)
    assert verdict.admitted is True
    assert verdict.reason is None


def test_located_executable_dead_code_deletion_is_a_distinct_admitted_class():
    finding = _base_finding(change_class=CLASS_CODE_DELETION)
    assert admit_finding(finding).admitted is True


def test_finding_with_no_location_is_dropped_with_recorded_reason():
    finding = _base_finding(location=None)
    verdict = admit_finding(finding)
    assert verdict.admitted is False
    assert verdict.reason
    assert "location" in verdict.reason or "file:line" in verdict.reason


def test_finding_with_blank_file_is_dropped():
    finding = _base_finding(location=Location(file="", line=10))
    verdict = admit_finding(finding)
    assert verdict.admitted is False
    assert verdict.reason


def test_finding_with_no_line_is_dropped():
    finding = _base_finding(location=Location(file="pkg/mod.py", line=None))
    verdict = admit_finding(finding)
    assert verdict.admitted is False
    assert verdict.reason


def test_judgment_tier_finding_with_no_enumerated_chunks_is_dropped():
    finding = _base_finding(tier="judgment", chunks=())
    verdict = admit_finding(finding)
    assert verdict.admitted is False
    assert "chunk" in verdict.reason


def test_judgment_tier_finding_with_enumerated_chunks_is_admitted():
    finding = _base_finding(tier="judgment", chunks=("parses input", "validates state", "renders output"))
    verdict = admit_finding(finding)
    assert verdict.admitted is True


def test_finding_with_no_declared_pole_is_dropped():
    finding = _base_finding(pole=None)
    verdict = admit_finding(finding)
    assert verdict.admitted is False
    assert "pole" in verdict.reason


def test_finding_with_invalid_pole_is_dropped():
    finding = _base_finding(pole="shrinkage")
    verdict = admit_finding(finding)
    assert verdict.admitted is False
    assert "pole" in verdict.reason


def test_dry_finding_with_no_observable_is_dropped():
    finding = _base_finding(rule="dry", is_dry=True, observable=None)
    verdict = admit_finding(finding)
    assert verdict.admitted is False
    assert "observable" in verdict.reason or "co-change" in verdict.reason


def test_dry_finding_with_co_change_observable_is_admitted():
    finding = _base_finding(rule="dry", is_dry=True, observable="co-change")
    verdict = admit_finding(finding)
    assert verdict.admitted is True


def test_dry_finding_with_parameter_accretion_observable_is_admitted():
    finding = _base_finding(rule="dry", is_dry=True, observable="parameter-accretion")
    verdict = admit_finding(finding)
    assert verdict.admitted is True


def test_inline_proposal_against_helper_with_three_or_more_live_callers_is_refused():
    finding = _base_finding(proposal="inline", live_caller_count=3)
    verdict = admit_finding(finding)
    assert verdict.admitted is False
    assert "caller" in verdict.reason


def test_inline_proposal_against_helper_with_two_live_callers_is_admitted():
    finding = _base_finding(proposal="inline", live_caller_count=2)
    verdict = admit_finding(finding)
    assert verdict.admitted is True


def test_consolidation_needing_flag_per_caller_is_rejected_as_failed_centralization():
    finding = _base_finding(proposal="consolidate", consolidation_requires_flag_per_caller=True)
    verdict = admit_finding(finding)
    assert verdict.admitted is False
    assert "centraliz" in verdict.reason.lower()


def test_consolidation_without_flag_per_caller_is_admitted():
    finding = _base_finding(proposal="consolidate", consolidation_requires_flag_per_caller=False)
    verdict = admit_finding(finding)
    assert verdict.admitted is True
