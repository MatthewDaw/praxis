"""R1 acceptance: mutation guards on a registered model's judging contract."""

from __future__ import annotations

import pytest

from knowledge.ml_registry.guards import (
    PROTECTED_MODEL_FIELDS,
    guard_baseline_move,
    guard_model_mutation,
)
from knowledge.ml_registry.schema import RegistryValidationError


@pytest.mark.parametrize("field", sorted(PROTECTED_MODEL_FIELDS))
def test_worker_sourced_mutation_of_a_protected_field_is_refused_naming_it(field):
    """A worker-sourced write mutating metric/direction/win_condition/baseline_runs/
    baseline_throughput/diff_size_limit is refused naming that field."""
    with pytest.raises(RegistryValidationError) as excinfo:
        guard_model_mutation({field: "new-value"}, source="worker")
    assert excinfo.value.field == field
    assert field in str(excinfo.value)


def test_worker_sourced_mutation_of_an_unprotected_field_is_allowed():
    guard_model_mutation({"description": "renamed"}, source="worker")  # must not raise


@pytest.mark.parametrize("source", ["worker", "adjudication", "operator", "totally-made-up", ""])
def test_a_protected_field_may_not_be_patched_from_ANY_source(source):
    """The guard is a deny-all, not a denylist of one source.

    It previously refused only ``source == "worker"``, which made it a no-op for every
    other string -- and ``--source`` is free text at the CLI. Setting
    a repointed bar/``baseline_throughput=99`` through any other source drove a trial
    whose LEDGER value was a clear loss to ``succeeded`` and advanced the baseline onto
    the losing commit. Both operands of the comparison must come from the ledger, so the
    threshold is no more patchable than the value.
    """
    with pytest.raises(RegistryValidationError) as excinfo:
        guard_model_mutation({"baseline_runs": ["cherry-picked"]}, source=source)
    assert excinfo.value.field == "baseline_runs"


def test_baseline_move_from_adjudication_is_allowed():
    guard_baseline_move({"baseline": "commit-def456"}, source="adjudication")  # must not raise


@pytest.mark.parametrize("source", ["worker", "supervisor", ""])
def test_baseline_move_from_anywhere_other_than_adjudication_is_refused(source):
    with pytest.raises(RegistryValidationError) as excinfo:
        guard_baseline_move({"baseline": "commit-def456"}, source=source)
    assert excinfo.value.field == "baseline"


def test_patch_without_baseline_is_untouched_by_the_baseline_guard():
    guard_baseline_move({"description": "renamed"}, source="worker")  # must not raise
