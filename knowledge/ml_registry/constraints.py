"""Constrained metric contracts for ML campaign promotion gates.

The registry's historical contract has one primary metric.  This module keeps that
contract intact and adds optional, hard secondary gates (quality floors, calibration,
coverage, latency, and per-domain requirements) without collapsing unlike quantities
into a composite score.

Input rows deliberately look like external-ledger records::

    {"f1": .84, "latency_ms": 18, "slices": {"soccer": {"f1": .82}}}

Missing or non-numeric evidence is a *refusal*, not a failed experiment.  A measured
value on the wrong side of a threshold is a genuine failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import asdict
import math
import re
from typing import Mapping

from knowledge.ml_registry.schema import RegistryValidationError

DIRECTION_MINIMIZE = "minimize"
DIRECTION_MAXIMIZE = "maximize"
VALID_DIRECTIONS = frozenset({DIRECTION_MINIMIZE, DIRECTION_MAXIMIZE})

SCOPE_OVERALL = "overall"
SCOPE_EACH_SLICE = "each_slice"
SCOPE_WORST_SLICE = "worst_slice"
VALID_SCOPES = frozenset({SCOPE_OVERALL, SCOPE_EACH_SLICE, SCOPE_WORST_SLICE})

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_REFUSED = "refused"

_METRIC_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:/-]*$")


@dataclass(frozen=True)
class MetricConstraint:
    """One hard promotion constraint.

    ``maximize`` means the value must be at least ``threshold``; ``minimize`` means
    it must be at most the threshold.  Slice constraints may either discover every
    slice in the evidence row or name an exact closed set with ``slices``.
    """

    metric: str
    direction: str
    threshold: float
    scope: str = SCOPE_OVERALL
    slices: tuple[str, ...] = ()
    label: str = ""


@dataclass(frozen=True)
class MetricContract:
    """A primary optimization metric plus zero or more hard promotion gates."""

    primary_metric: str
    primary_direction: str
    constraints: tuple[MetricConstraint, ...] = ()


@dataclass(frozen=True)
class ConstraintEvidence:
    """Auditable result for one constraint (or one refusal to evaluate it)."""

    metric: str
    scope: str
    direction: str
    threshold: float
    passed: bool | None
    observed: tuple[tuple[str, float], ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class ConstraintEvaluation:
    """Complete promotion-gate evaluation for one external metric row."""

    status: str
    primary_metric: str
    primary_direction: str
    primary_value: float | None
    evidence: tuple[ConstraintEvidence, ...]
    reasons: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status == STATUS_PASSED

    def to_dict(self) -> dict[str, object]:
        """Return a stable JSON-ready representation for CLI and report consumers."""

        return asdict(self)


def _valid_name(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _METRIC_NAME.fullmatch(value):
        raise RegistryValidationError(
            f"{field} must be a non-empty metric identifier without whitespace",
            field=field,
        )
    return value


def _valid_direction(value: object, *, field: str) -> str:
    if value not in VALID_DIRECTIONS:
        raise RegistryValidationError(
            f"{field} must be one of {sorted(VALID_DIRECTIONS)}, got {value!r}",
            field=field,
        )
    return str(value)


def _finite_number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RegistryValidationError(f"{field} must be numeric", field=field)
    try:
        result = float(value)
    except (OverflowError, ValueError, TypeError) as exc:
        raise RegistryValidationError(f"{field} must be finite", field=field) from exc
    if not math.isfinite(result):
        raise RegistryValidationError(f"{field} must be finite", field=field)
    return result


def validate_metric_contract(contract: MetricContract) -> None:
    """Validate a constrained contract, raising with the exact offending field."""

    _valid_name(contract.primary_metric, field="primary_metric")
    _valid_direction(contract.primary_direction, field="primary_direction")
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for index, constraint in enumerate(contract.constraints):
        prefix = f"constraints[{index}]"
        _valid_name(constraint.metric, field=f"{prefix}.metric")
        _valid_direction(constraint.direction, field=f"{prefix}.direction")
        _finite_number(constraint.threshold, field=f"{prefix}.threshold")
        if constraint.scope not in VALID_SCOPES:
            raise RegistryValidationError(
                f"{prefix}.scope must be one of {sorted(VALID_SCOPES)}",
                field=f"{prefix}.scope",
            )
        if constraint.scope == SCOPE_OVERALL and constraint.slices:
            raise RegistryValidationError(
                f"{prefix}.slices is only valid for a slice scope",
                field=f"{prefix}.slices",
            )
        if len(set(constraint.slices)) != len(constraint.slices):
            raise RegistryValidationError(
                f"{prefix}.slices contains duplicates", field=f"{prefix}.slices"
            )
        for slice_name in constraint.slices:
            _valid_name(slice_name, field=f"{prefix}.slices")
        key = (constraint.metric, constraint.scope, constraint.slices)
        if key in seen:
            raise RegistryValidationError(
                f"duplicate hard constraint for {constraint.metric!r} in {constraint.scope}",
                field=f"{prefix}.metric",
            )
        seen.add(key)


def metric_contract_from_dict(raw: Mapping[str, object]) -> MetricContract:
    """Parse a JSON-shaped dictionary into a validated metric contract."""

    for required in ("primary_metric", "primary_direction"):
        if required not in raw:
            raise RegistryValidationError(
                f"metric contract is missing required field {required!r}",
                field=required,
            )
    raw_constraints = raw.get("constraints", [])
    if not isinstance(raw_constraints, list):
        raise RegistryValidationError(
            "constraints must be a JSON array", field="constraints"
        )
    constraints: list[MetricConstraint] = []
    for index, item in enumerate(raw_constraints):
        field = f"constraints[{index}]"
        if not isinstance(item, Mapping):
            raise RegistryValidationError(f"{field} must be a JSON object", field=field)
        missing = next(
            (name for name in ("metric", "direction", "threshold") if name not in item),
            None,
        )
        if missing is not None:
            raise RegistryValidationError(
                f"{field} is missing required field {missing!r}",
                field=f"{field}.{missing}",
            )
        raw_slices = item.get("slices", [])
        if not isinstance(raw_slices, list) or not all(
            isinstance(name, str) for name in raw_slices
        ):
            raise RegistryValidationError(
                f"{field}.slices must be an array of strings", field=f"{field}.slices"
            )
        constraints.append(
            MetricConstraint(
                metric=item["metric"],  # type: ignore[arg-type]
                direction=item["direction"],  # type: ignore[arg-type]
                threshold=item["threshold"],  # type: ignore[arg-type]
                scope=item.get("scope", SCOPE_OVERALL),  # type: ignore[arg-type]
                slices=tuple(raw_slices),
                label=item.get("label", ""),  # type: ignore[arg-type]
            )
        )
    contract = MetricContract(
        primary_metric=raw["primary_metric"],  # type: ignore[arg-type]
        primary_direction=raw["primary_direction"],  # type: ignore[arg-type]
        constraints=tuple(constraints),
    )
    validate_metric_contract(contract)
    return contract


def _metric_container(row: Mapping[str, object]) -> Mapping[str, object]:
    nested = row.get("metrics")
    return nested if isinstance(nested, Mapping) else row


def _read_metric(row: Mapping[str, object], metric: str) -> tuple[float | None, str]:
    container = _metric_container(row)
    if metric not in container:
        return None, f"required metric {metric!r} is missing"
    try:
        return _finite_number(container[metric], field=metric), ""
    except RegistryValidationError as exc:
        return None, str(exc)


def _meets(value: float, direction: str, threshold: float) -> bool:
    return value <= threshold if direction == DIRECTION_MINIMIZE else value >= threshold


def _evaluate_one(
    constraint: MetricConstraint, row: Mapping[str, object]
) -> ConstraintEvidence:
    if constraint.scope == SCOPE_OVERALL:
        value, reason = _read_metric(row, constraint.metric)
        if value is None:
            return ConstraintEvidence(
                constraint.metric,
                constraint.scope,
                constraint.direction,
                float(constraint.threshold),
                None,
                reason=reason,
            )
        return ConstraintEvidence(
            constraint.metric,
            constraint.scope,
            constraint.direction,
            float(constraint.threshold),
            _meets(value, constraint.direction, constraint.threshold),
            (("overall", value),),
        )

    raw_slices = row.get("slices")
    if not isinstance(raw_slices, Mapping):
        return ConstraintEvidence(
            constraint.metric,
            constraint.scope,
            constraint.direction,
            float(constraint.threshold),
            None,
            reason="required slice evidence is missing",
        )
    names = constraint.slices or tuple(sorted(str(name) for name in raw_slices))
    if not names:
        return ConstraintEvidence(
            constraint.metric,
            constraint.scope,
            constraint.direction,
            float(constraint.threshold),
            None,
            reason="no slices are available to evaluate",
        )
    observed: list[tuple[str, float]] = []
    missing: list[str] = []
    for name in names:
        slice_row = raw_slices.get(name)
        if not isinstance(slice_row, Mapping):
            missing.append(name)
            continue
        value, _ = _read_metric(slice_row, constraint.metric)
        if value is None:
            missing.append(name)
        else:
            observed.append((name, value))
    if missing:
        return ConstraintEvidence(
            constraint.metric,
            constraint.scope,
            constraint.direction,
            float(constraint.threshold),
            None,
            tuple(observed),
            f"required metric {constraint.metric!r} is missing or invalid for slices: "
            + ", ".join(missing),
        )

    if constraint.scope == SCOPE_WORST_SLICE:
        worst = (
            max(observed, key=lambda item: item[1])
            if constraint.direction == DIRECTION_MINIMIZE
            else min(observed, key=lambda item: item[1])
        )
        reported = (worst,)
        passed = _meets(worst[1], constraint.direction, constraint.threshold)
    else:
        reported = tuple(observed)
        passed = all(
            _meets(value, constraint.direction, constraint.threshold)
            for _, value in observed
        )
    return ConstraintEvidence(
        constraint.metric,
        constraint.scope,
        constraint.direction,
        float(constraint.threshold),
        passed,
        reported,
    )


def evaluate_metric_contract(
    contract: MetricContract, row: Mapping[str, object]
) -> ConstraintEvaluation:
    """Evaluate hard gates against a metric row.

    Contract errors raise :class:`RegistryValidationError`. Evidence errors return
    ``refused`` so unavailable measurements can never be mistaken for losing results.
    A legacy single-metric contract is represented by an empty constraint tuple and
    passes whenever its primary value is present and finite.
    """

    validate_metric_contract(contract)
    primary, primary_reason = _read_metric(row, contract.primary_metric)
    evidence = tuple(
        _evaluate_one(constraint, row) for constraint in contract.constraints
    )
    refusals = ([primary_reason] if primary is None else []) + [
        item.reason for item in evidence if item.passed is None
    ]
    if refusals:
        status = STATUS_REFUSED
    elif any(item.passed is False for item in evidence):
        status = STATUS_FAILED
    else:
        status = STATUS_PASSED
    return ConstraintEvaluation(
        status,
        contract.primary_metric,
        contract.primary_direction,
        primary,
        evidence,
        tuple(refusals),
    )
