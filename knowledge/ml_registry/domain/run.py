from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from types import MappingProxyType
from typing import Any, Mapping

from knowledge.ml_registry.contracts.ledger_v2 import ThroughputUnit


class RunMetricError(ValueError):
    pass


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETE = "complete"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    VOIDED = "voided"
    SUPERSEDED = "superseded"


class RunValidity(str, Enum):
    VALID = "valid"
    INVALID = "invalid"


VALID_RUN_STATUS_VERDICT_PAIRS = frozenset({
    (RunStatus.RUNNING.value, None),
    (RunStatus.COMPLETE.value, None),
    (RunStatus.SUCCEEDED.value, "adopted"),
    (RunStatus.SUCCEEDED.value, "rejected"),
    (RunStatus.SUCCEEDED.value, "parked"),
    (RunStatus.SUCCEEDED.value, "abandoned"),
    (RunStatus.FAILED.value, None),
    (RunStatus.VOIDED.value, "voided"),
    (RunStatus.SUPERSEDED.value, None),
})


@dataclass(frozen=True)
class RunLoad:
    start_1m: float
    end_1m: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_1m", _nonnegative(self.start_1m, "load.start_1m"))
        object.__setattr__(self, "end_1m", _nonnegative(self.end_1m, "load.end_1m"))

    @classmethod
    def from_mapping(cls, value: object) -> "RunLoad":
        if not isinstance(value, Mapping) or set(value) != {"start_1m", "end_1m"}:
            raise RunMetricError("run metric load requires exactly start_1m and end_1m")
        return cls(start_1m=value["start_1m"], end_1m=value["end_1m"])

    def to_mapping(self) -> dict[str, float]:
        return {"start_1m": self.start_1m, "end_1m": self.end_1m}


@dataclass(frozen=True)
class RunMetrics:
    #: One finite number under a scalar judge; under a vector judge, an object of finite
    #: numbers keyed by metric name -- every judged metric plus any diagnostics the run
    #: chose to report (adjudication reads the judged names and ignores the rest).
    metric: float | Mapping[str, float]
    validity: RunValidity
    throughput: float
    throughput_unit: ThroughputUnit
    memory_gb: float
    cpu_time: float
    load: RunLoad

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric", _metric_value(self.metric))
        object.__setattr__(self, "validity", _enum(RunValidity, self.validity, "validity"))
        object.__setattr__(self, "throughput", _nonnegative(self.throughput, "throughput"))
        object.__setattr__(self, "throughput_unit",
                           _enum(ThroughputUnit, self.throughput_unit, "throughput_unit"))
        object.__setattr__(self, "memory_gb", _nonnegative(self.memory_gb, "memory_gb"))
        object.__setattr__(self, "cpu_time", _nonnegative(self.cpu_time, "cpu_time"))
        if not isinstance(self.load, RunLoad):
            object.__setattr__(self, "load", RunLoad.from_mapping(self.load))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RunMetrics":
        required = {"metric", "validity", "throughput", "throughput_unit", "memory_gb", "cpu_time", "load"}
        if set(value) != required:
            missing = sorted(required - set(value))
            extra = sorted(set(value) - required)
            raise RunMetricError(f"run metrics require exactly {sorted(required)}; missing={missing}, extra={extra}")
        return cls(
            metric=value["metric"], validity=value["validity"], throughput=value["throughput"],
            throughput_unit=value["throughput_unit"], memory_gb=value["memory_gb"],
            cpu_time=value["cpu_time"], load=RunLoad.from_mapping(value["load"]),
        )

    def to_mapping(self) -> Mapping[str, Any]:
        metric = dict(self.metric) if isinstance(self.metric, Mapping) else self.metric
        return MappingProxyType({
            "metric": metric, "validity": self.validity.value,
            "throughput": self.throughput, "throughput_unit": self.throughput_unit.value,
            "memory_gb": self.memory_gb, "cpu_time": self.cpu_time, "load": self.load.to_mapping(),
        })


def _metric_value(value: object) -> float | Mapping[str, float]:
    """A scalar run metric, or a vector run's per-metric object, each value finite.

    The object form exists for campaigns whose judge is a metric VECTOR: the run reports
    every judged metric keyed by name (diagnostics may ride along and are ignored by
    adjudication). Keys must be non-empty strings and every value a finite number.
    """
    if not isinstance(value, Mapping):
        return _finite(value, "metric")
    if not value:
        raise RunMetricError("run metric object must name at least one metric")
    validated: dict[str, float] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not name.strip():
            raise RunMetricError("run metric object keys must be non-empty metric names")
        validated[name] = _finite(item, f"metric[{name}]")
    return validated


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RunMetricError(f"run metric {field} must be a finite number")
    return float(value)


def _nonnegative(value: object, field: str) -> float:
    result = _finite(value, field)
    if result < 0:
        raise RunMetricError(f"run metric {field} must be non-negative")
    return result


def _enum(enum_type: type[Enum], value: object, field: str):
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        choices = ", ".join(item.value for item in enum_type)
        raise RunMetricError(f"run metric {field} must be one of: {choices}") from exc
