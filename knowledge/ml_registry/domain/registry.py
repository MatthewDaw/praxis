from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from knowledge.ml_registry.contracts import CodeRef, LegacyCodeRef
from .run import RunMetrics, RunStatus
from .status import Verdict


@dataclass(frozen=True)
class Experiment:
    experiment_id: str
    spec_digest: str
    stages: tuple[str, ...]
    metric: str
    direction: str
    win_condition: Mapping[str, Any]
    rope: float
    baseline_throughput: float


@dataclass(frozen=True)
class Run:
    run_id: str
    experiment_id: str
    idea_id: str
    stage: str
    family: str
    params: Mapping[str, Any]
    metrics: RunMetrics
    code_ref: CodeRef | LegacyCodeRef
    device_fingerprint: str
    status: RunStatus
    verdict: Verdict | None
    started_at: float
    finished_at: float | None
    claim_owner: str
    heartbeat_at: float


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    run_id: str
    kind: str
    uri: str
    bytes: int
    schema_version: str


@dataclass(frozen=True)
class RegisteredModel:
    model_id: str
    family: str
    sport_scope: str
    axis: str
    protocol: str
    extends: Mapping[str, Any] | None


@dataclass(frozen=True)
class ModelVersion:
    model_id: str
    version: int
    run_id: str
    artifact_id: str
    checksum: str
    family_version: str
    code_sha: str
    preprocessing_hash: str
    calibration: Mapping[str, Any]
    thresholds: Mapping[str, Any]
    compat_result: Mapping[str, Any]
    status: str


@dataclass(frozen=True)
class Lineage:
    child_model_id: str
    child_version: int
    parent_model_id: str
    parent_version: int
    kind: str


@dataclass(frozen=True)
class Alias:
    model_id: str
    alias: str
    version: int
    set_by: str
    reason: str
    at: float
