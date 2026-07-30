"""R41 — af-clean's findings ledger: a closure-aware cleared-file skip cache plus a
sticky, symbol-scoped rejection record.

Acceptance: a declined finding does not re-surface after an unrelated edit elsewhere in
its file nor after a later excision round rewrote that file; a file whose caller was
deleted is still re-evaluated; a reachability veto with no named reason is refused; a
quarantined symbol persists across runs with a recorded review state and is
re-adjudicated when its own text changes; and a rubric-version bump does not
mass-expire prior rejections.
"""

from __future__ import annotations

import pytest

from agent_factory.af_clean_findings_ledger import (
    FindingsLedger,
    ReachabilityVetoRefused,
)


def test_rejection_survives_unrelated_edit_elsewhere_in_its_file():
    ledger = FindingsLedger()
    symbol_source = "def helper():\n    return 1\n"

    ledger.record_rejection(symbol_id="mod.helper", symbol_source=symbol_source, rubric_version="v1")

    # The rest of the file changes (other lines edited) but this symbol's own text is
    # unchanged -- the prior rejection must not re-surface.
    assert ledger.is_rejected(symbol_id="mod.helper", symbol_source=symbol_source)


def test_rejection_survives_a_later_excision_round_rewriting_the_file():
    ledger = FindingsLedger()
    symbol_source = "def helper():\n    return 1\n"
    ledger.record_rejection(symbol_id="mod.helper", symbol_source=symbol_source, rubric_version="v1")

    # A later excision round removed unrelated dead code above/below in the same file,
    # shifting nothing about this symbol's own normalized text.
    reformatted_same_symbol = "def helper():\n\n    return 1\n"  # only incidental blank-line churn
    assert ledger.is_rejected(symbol_id="mod.helper", symbol_source=reformatted_same_symbol)


def test_rejection_is_re_adjudicated_when_its_own_text_changes():
    ledger = FindingsLedger()
    ledger.record_rejection(symbol_id="mod.helper", symbol_source="def helper():\n    return 1\n", rubric_version="v1")

    changed_source = "def helper():\n    return 2\n"
    assert not ledger.is_rejected(symbol_id="mod.helper", symbol_source=changed_source)


def test_quarantined_symbol_persists_with_recorded_review_state():
    ledger = FindingsLedger()
    source = "def quarantined():\n    return 1\n"
    ledger.record_rejection(symbol_id="mod.quarantined", symbol_source=source, rubric_version="v1", review_state="quarantined")

    entry = ledger.rejection_state("mod.quarantined")
    assert entry is not None
    assert entry.review_state == "quarantined"
    assert ledger.is_rejected(symbol_id="mod.quarantined", symbol_source=source)


def test_rubric_version_bump_does_not_mass_expire_prior_rejections():
    ledger = FindingsLedger()
    for i in range(5):
        ledger.record_rejection(symbol_id=f"mod.sym{i}", symbol_source=f"def sym{i}():\n    return {i}\n", rubric_version="v1")

    # A rubric version bump alone (no text change) must not expire any of these.
    for i in range(5):
        source = f"def sym{i}():\n    return {i}\n"
        assert ledger.is_rejected(symbol_id=f"mod.sym{i}", symbol_source=source, current_rubric_version="v2")


def test_cleared_file_skip_requires_unchanged_closure_and_job_inventory():
    ledger = FindingsLedger()
    ledger.record_cleared_file(
        file_path="pkg/mod.py",
        content_hash="filehash-1",
        rubric_version="v1",
        closure_hash="closure-1",
        job_inventory_hash="inventory-1",
    )

    # Unchanged everything -> skip judgment.
    assert ledger.should_skip_judgment(
        file_path="pkg/mod.py",
        content_hash="filehash-1",
        rubric_version="v1",
        closure_hash="closure-1",
        job_inventory_hash="inventory-1",
    )


def test_caller_deleted_changes_closure_hash_so_file_is_re_evaluated():
    ledger = FindingsLedger()
    ledger.record_cleared_file(
        file_path="pkg/mod.py",
        content_hash="filehash-1",
        rubric_version="v1",
        closure_hash="closure-with-caller",
        job_inventory_hash="inventory-1",
    )

    # mod.py's own text is unchanged, but its transitive dependency closure changed
    # because a caller elsewhere in the repo was deleted -- must be re-evaluated, not
    # skipped.
    assert not ledger.should_skip_judgment(
        file_path="pkg/mod.py",
        content_hash="filehash-1",
        rubric_version="v1",
        closure_hash="closure-without-caller",
        job_inventory_hash="inventory-1",
    )


def test_reachability_veto_with_no_named_reason_is_refused():
    ledger = FindingsLedger()
    with pytest.raises(ReachabilityVetoRefused):
        ledger.record_reachability_veto(symbol_id="mod.helper", reason="")

    with pytest.raises(ReachabilityVetoRefused):
        ledger.record_reachability_veto(symbol_id="mod.helper", reason=None)


def test_reachability_veto_with_a_named_reason_is_recorded():
    ledger = FindingsLedger()
    ledger.record_reachability_veto(symbol_id="mod.helper", reason="invoked via getattr dispatch table")

    assert ledger.reachability_veto_reason("mod.helper") == "invoked via getattr dispatch table"
