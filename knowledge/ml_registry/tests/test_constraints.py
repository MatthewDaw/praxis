"""Constrained multi-metric and slice promotion gates."""

from __future__ import annotations

import math

import pytest

from knowledge.ml_registry.constraints import (
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_REFUSED,
    MetricConstraint,
    MetricContract,
    evaluate_metric_contract,
    validate_metric_contract,
)
from knowledge.ml_registry.schema import RegistryValidationError


def test_legacy_primary_metric_only_remains_valid():
    result = evaluate_metric_contract(
        MetricContract("idf1", "maximize"), {"idf1": 0.81}
    )
    assert result.status == STATUS_PASSED
    assert result.passed
    assert result.primary_value == 0.81
    assert result.evidence == ()


def test_overall_quality_coverage_calibration_and_latency_gates_pass():
    contract = MetricContract(
        "idf1",
        "maximize",
        (
            MetricConstraint("coverage", "maximize", 0.95),
            MetricConstraint("ece", "minimize", 0.04),
            MetricConstraint("latency_ms", "minimize", 20),
        ),
    )
    result = evaluate_metric_contract(
        contract,
        {"metrics": {"idf1": 0.82, "coverage": 0.96, "ece": 0.03, "latency_ms": 20}},
    )
    assert result.status == STATUS_PASSED
    assert [item.passed for item in result.evidence] == [True, True, True]


def test_measured_threshold_breach_is_failure_with_detailed_evidence():
    contract = MetricContract(
        "idf1", "maximize", (MetricConstraint("latency_ms", "minimize", 20),)
    )
    result = evaluate_metric_contract(contract, {"idf1": 0.84, "latency_ms": 21})
    assert result.status == STATUS_FAILED
    assert result.evidence[0].observed == (("overall", 21.0),)
    assert result.evidence[0].threshold == 20
    assert result.evidence[0].passed is False


def test_each_slice_requires_every_domain_to_pass():
    contract = MetricContract(
        "f1",
        "maximize",
        (MetricConstraint("f1", "maximize", 0.75, "each_slice"),),
    )
    result = evaluate_metric_contract(
        contract,
        {"f1": 0.83, "slices": {"soccer": {"f1": 0.80}, "basketball": {"f1": 0.72}}},
    )
    assert result.status == STATUS_FAILED
    assert result.evidence[0].observed == (("basketball", 0.72), ("soccer", 0.80))


@pytest.mark.parametrize(
    ("direction", "values", "expected"),
    [
        ("maximize", {"soccer": 0.81, "tennis": 0.76}, ("tennis", 0.76)),
        ("minimize", {"soccer": 0.03, "tennis": 0.06}, ("tennis", 0.06)),
    ],
)
def test_worst_slice_reports_the_limiting_domain(direction, values, expected):
    threshold = 0.75 if direction == "maximize" else 0.05
    contract = MetricContract(
        "score",
        direction,
        (MetricConstraint("score", direction, threshold, "worst_slice"),),
    )
    row = {
        "score": 0.8,
        "slices": {name: {"score": value} for name, value in values.items()},
    }
    result = evaluate_metric_contract(contract, row)
    assert result.evidence[0].observed == (expected,)
    assert result.status == STATUS_PASSED if direction == "maximize" else STATUS_FAILED


def test_closed_slice_set_refuses_missing_domain_instead_of_passing_partial_evidence():
    contract = MetricContract(
        "f1",
        "maximize",
        (MetricConstraint("f1", "maximize", 0.7, "each_slice", ("soccer", "hockey")),),
    )
    result = evaluate_metric_contract(
        contract, {"f1": 0.9, "slices": {"soccer": {"f1": 0.8}}}
    )
    assert result.status == STATUS_REFUSED
    assert result.evidence[0].passed is None
    assert "hockey" in result.evidence[0].reason


@pytest.mark.parametrize("bad", [None, "fast", True, math.nan, math.inf])
def test_missing_or_invalid_measured_evidence_is_refused(bad):
    contract = MetricContract(
        "f1", "maximize", (MetricConstraint("latency_ms", "minimize", 20),)
    )
    result = evaluate_metric_contract(contract, {"f1": 0.8, "latency_ms": bad})
    assert result.status == STATUS_REFUSED
    assert result.reasons


def test_missing_primary_metric_is_refused_even_when_all_constraints_pass():
    contract = MetricContract(
        "f1", "maximize", (MetricConstraint("coverage", "maximize", 0.9),)
    )
    result = evaluate_metric_contract(contract, {"coverage": 0.95})
    assert result.status == STATUS_REFUSED
    assert result.primary_value is None


@pytest.mark.parametrize(
    ("contract", "field"),
    [
        (MetricContract("bad metric", "maximize"), "primary_metric"),
        (MetricContract("f1", "up"), "primary_direction"),
        (
            MetricContract(
                "f1", "maximize", (MetricConstraint("ece", "minimize", math.nan),)
            ),
            "constraints[0].threshold",
        ),
        (
            MetricContract(
                "f1", "maximize", (MetricConstraint("ece", "minimize", 0.1, "region"),)
            ),
            "constraints[0].scope",
        ),
        (
            MetricContract(
                "f1",
                "maximize",
                (MetricConstraint("ece", "minimize", 0.1, "overall", ("x",)),),
            ),
            "constraints[0].slices",
        ),
    ],
)
def test_invalid_contract_is_rejected_naming_exact_field(contract, field):
    with pytest.raises(RegistryValidationError) as excinfo:
        validate_metric_contract(contract)
    assert excinfo.value.field == field


def test_duplicate_constraint_is_rejected():
    constraint = MetricConstraint("ece", "minimize", 0.1)
    with pytest.raises(RegistryValidationError, match="duplicate"):
        validate_metric_contract(
            MetricContract("f1", "maximize", (constraint, constraint))
        )


def test_huge_integer_is_refused_without_overflowing():
    result = evaluate_metric_contract(
        MetricContract("f1", "maximize"), {"f1": 10 ** 10000}
    )
    assert result.status == STATUS_REFUSED


@pytest.mark.parametrize("direction", [["maximize"], {"maximize": 1}, 5, None])
def test_unhashable_or_non_string_direction_refuses_instead_of_raising_typeerror(direction):
    with pytest.raises(RegistryValidationError) as error:
        validate_metric_contract(MetricContract("idf1", direction))
    assert error.value.field == "primary_direction"


def test_unhashable_constraint_direction_also_refuses():
    contract = MetricContract(
        "idf1", "maximize", (MetricConstraint("fps", ["at_least"], 30.0),)
    )
    with pytest.raises(RegistryValidationError):
        validate_metric_contract(contract)
