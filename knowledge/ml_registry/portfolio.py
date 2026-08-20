"""Durable portfolio planning and cross-model artifact lineage.

This module deliberately sits beside, rather than inside, ``RegistrySpace``.  The
model/idea/trial registry describes one executable campaign; ``Portfolio`` describes
when campaigns are allowed to become executable and which exact upstream artifacts
their evidence depends on.

Lineage is load bearing, not decorative.  Every artifact records the exact artifact
ids it was produced from, readiness walks that ancestry transitively, and a campaign
whose declared dependencies are not fully covered by an artifact's recorded lineage
cannot register that artifact at all.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable


class PortfolioValidationError(ValueError):
    """A portfolio graph, transition, or persisted document is invalid."""


class CampaignStatus(str, Enum):
    PLANNED = "PLANNED"
    BLOCKED = "BLOCKED"
    ACTIVATABLE = "ACTIVATABLE"
    SEEDING = "SEEDING"
    READY = "READY"


class Verdict(str, Enum):
    """The closed verdict vocabulary, mirroring :mod:`knowledge.ml_registry.verdict`.

    It is duplicated rather than imported so that the portfolio stays free of the
    single-campaign registry's write path; the values are asserted equal in tests.
    """

    ADOPTED = "adopted"
    PARKED = "parked"
    REJECTED = "rejected"
    VOIDED = "voided"


VERDICTS = frozenset(item.value for item in Verdict)
#: Only an adopted artifact is evidence a downstream campaign may be pinned to.
PASSING_VERDICTS = frozenset({Verdict.ADOPTED.value})
#: An ancestor with one of these verdicts poisons everything derived from it.
FAILED_VERDICTS = frozenset({Verdict.REJECTED.value, Verdict.VOIDED.value})

#: Sentinel distinguishing "this artifact provably had no inputs" (``()``) from
#: "this document predates lineage and we do not know" (``None``).
UNKNOWN_LINEAGE = None


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PortfolioValidationError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _unit_interval(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PortfolioValidationError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise PortfolioValidationError(f"{label} must be finite")
    if not 0.0 <= number <= 1.0:
        raise PortfolioValidationError(f"{label} must be between 0 and 1")
    return number


def _verdict(value: Any, label: str) -> str:
    _string(value, label)
    if value not in VERDICTS:
        raise PortfolioValidationError(
            f"{label} must be one of {sorted(VERDICTS)}, got {value!r}"
        )
    return value


def _string_sequence(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
        raise PortfolioValidationError(f"{label} must be an array of strings")
    items = tuple(value)
    for item in items:
        _string(item, f"{label} entry")
    return items


@dataclass(frozen=True)
class ArtifactDependency:
    """The complete, immutable input contract required by a campaign."""

    upstream_model_id: str
    artifact_id: str
    required_verdict: str
    dataset_manifest_hash: str
    split_manifest_hash: str
    prediction_manifest_hash: str
    minimum_coverage: float = 1.0

    def __post_init__(self) -> None:
        for name in (
            "upstream_model_id",
            "artifact_id",
            "dataset_manifest_hash",
            "split_manifest_hash",
            "prediction_manifest_hash",
        ):
            _string(getattr(self, name), f"dependency {name}")
        _verdict(self.required_verdict, "dependency required_verdict")
        object.__setattr__(
            self,
            "minimum_coverage",
            _unit_interval(self.minimum_coverage, "minimum_coverage"),
        )


@dataclass
class Artifact:
    id: str
    model_id: str
    verdict: str
    dataset_manifest_hash: str
    split_manifest_hash: str
    prediction_manifest_hash: str
    coverage: float
    created_at: str
    superseded_by: str | None = None
    superseded_at: str | None = None
    input_artifact_ids: tuple[str, ...] | None = ()

    def __post_init__(self) -> None:
        for name in (
            "id",
            "model_id",
            "dataset_manifest_hash",
            "split_manifest_hash",
            "prediction_manifest_hash",
            "created_at",
        ):
            _string(getattr(self, name), f"artifact {name}")
        self.verdict = _verdict(self.verdict, "artifact verdict")
        self.coverage = _unit_interval(self.coverage, "artifact coverage")
        self.superseded_by = _optional_string(self.superseded_by, "artifact superseded_by")
        self.superseded_at = _optional_string(self.superseded_at, "artifact superseded_at")
        if self.input_artifact_ids is not UNKNOWN_LINEAGE:
            self.input_artifact_ids = tuple(
                sorted(set(_string_sequence(self.input_artifact_ids, "artifact input_artifact_ids")))
            )

    @property
    def current(self) -> bool:
        return self.superseded_by is None

    @property
    def lineage_known(self) -> bool:
        return self.input_artifact_ids is not UNKNOWN_LINEAGE


@dataclass
class Campaign:
    id: str
    model_id: str
    dependencies: list[ArtifactDependency] = field(default_factory=list)
    status: CampaignStatus = CampaignStatus.PLANNED
    stale: bool = False
    blocked_reasons: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        _string(self.id, "campaign id")
        _string(self.model_id, "campaign model_id")
        if not isinstance(self.dependencies, list) or not all(
            isinstance(item, ArtifactDependency) for item in self.dependencies
        ):
            raise PortfolioValidationError("campaign dependencies must be a list of dependencies")
        if not isinstance(self.status, CampaignStatus):
            raise PortfolioValidationError("campaign status must be a CampaignStatus")
        if not isinstance(self.stale, bool):
            raise PortfolioValidationError("campaign stale must be a boolean")
        self.blocked_reasons = list(_string_sequence(self.blocked_reasons, "campaign blocked_reasons"))
        if not isinstance(self.history, list) or not all(
            isinstance(item, dict) for item in self.history
        ):
            raise PortfolioValidationError("campaign history must be a list of objects")


@dataclass(frozen=True)
class Readiness:
    activatable: bool
    reasons: tuple[str, ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Portfolio:
    """A campaign DAG with optional atomic JSON persistence."""

    SCHEMA_VERSION = 2

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.campaigns: dict[str, Campaign] = {}
        self.artifacts: dict[str, Artifact] = {}

    # ------------------------------------------------------------------ load/save

    @classmethod
    def load(cls, path: str | Path) -> "Portfolio":
        portfolio = cls(path)
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PortfolioValidationError(f"invalid portfolio document: {exc}") from exc
        if not isinstance(document, dict):
            raise PortfolioValidationError("portfolio document must be a JSON object")
        version = document.get("schema_version")
        if version != cls.SCHEMA_VERSION:
            if isinstance(version, int) and version < cls.SCHEMA_VERSION:
                raise PortfolioValidationError(
                    f"portfolio schema_version {version} predates version "
                    f"{cls.SCHEMA_VERSION}; migrate the document by re-registering its "
                    "artifacts with explicit input_artifact_ids lineage"
                )
            raise PortfolioValidationError("unsupported portfolio schema_version")
        try:
            if not isinstance(document.get("artifacts", []), list) or not isinstance(document.get("campaigns", []), list):
                raise TypeError("campaigns and artifacts must be arrays")
            for item in document.get("artifacts", []):
                if not isinstance(item, dict):
                    raise TypeError("every artifact must be a JSON object")
                raw = dict(item)
                raw["input_artifact_ids"] = raw.get("input_artifact_ids", UNKNOWN_LINEAGE)
                artifact = Artifact(**raw)
                if artifact.id in portfolio.artifacts:
                    raise ValueError(f"duplicate artifact id {artifact.id!r}")
                portfolio.artifacts[artifact.id] = artifact
            for item in document.get("campaigns", []):
                if not isinstance(item, dict):
                    raise TypeError("every campaign must be a JSON object")
                raw = dict(item)
                raw_dependencies = raw.pop("dependencies", [])
                if not isinstance(raw_dependencies, list):
                    raise TypeError("campaign dependencies must be an array")
                dependencies = [ArtifactDependency(**dep) for dep in raw_dependencies]
                raw["status"] = CampaignStatus(raw["status"])
                campaign = Campaign(dependencies=dependencies, **raw)
                if campaign.id in portfolio.campaigns:
                    raise ValueError(f"duplicate campaign id {campaign.id!r}")
                portfolio.campaigns[campaign.id] = campaign
        except (TypeError, KeyError, ValueError, AttributeError) as exc:
            raise PortfolioValidationError(f"malformed portfolio document: {exc}") from exc
        portfolio._assert_pointers_resolve()
        portfolio.validate()
        return portfolio

    def _assert_pointers_resolve(self) -> None:
        for artifact in self.artifacts.values():
            if artifact.superseded_by is not None and artifact.superseded_by not in self.artifacts:
                raise PortfolioValidationError(
                    f"malformed portfolio document: artifact {artifact.id!r} is superseded by "
                    f"unknown artifact {artifact.superseded_by!r}"
                )
            for parent in artifact.input_artifact_ids or ():
                if parent not in self.artifacts:
                    raise PortfolioValidationError(
                        f"malformed portfolio document: artifact {artifact.id!r} has lineage to "
                        f"unknown artifact {parent!r}"
                    )

    def save(self) -> None:
        if self.path is None:
            raise PortfolioValidationError("cannot save a portfolio without a path")
        self.validate()
        document = {
            "schema_version": self.SCHEMA_VERSION,
            "campaigns": [asdict(value) for _, value in sorted(self.campaigns.items())],
            "artifacts": [asdict(value) for _, value in sorted(self.artifacts.items())],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, sort_keys=True, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    # ------------------------------------------------------------------ mutation

    def register_artifact(
        self,
        artifact_id: str,
        model_id: str,
        *,
        verdict: str,
        dataset_manifest_hash: str,
        split_manifest_hash: str,
        prediction_manifest_hash: str,
        coverage: float,
        input_artifact_ids: Iterable[str] = (),
    ) -> Artifact:
        if artifact_id in self.artifacts:
            raise PortfolioValidationError(f"artifact {artifact_id!r} already exists")
        lineage = tuple(sorted(set(_string_sequence(input_artifact_ids, "input_artifact_ids"))))
        for parent_id in lineage:
            parent = self.artifacts.get(parent_id)
            if parent is None:
                raise PortfolioValidationError(
                    "artifact lineage must reference existing artifact ids"
                )
            if not parent.current:
                raise PortfolioValidationError(
                    f"artifact lineage may not reference superseded artifact {parent_id!r} "
                    f"(superseded by {parent.superseded_by!r})"
                )
        producer = self._producer_campaign(model_id)
        if producer is not None and producer.dependencies:
            required = {dependency.artifact_id for dependency in producer.dependencies}
            missing = sorted(required - set(lineage))
            if missing:
                raise PortfolioValidationError(
                    f"artifact {artifact_id!r} is produced by campaign {producer.id!r} which "
                    f"declares dependencies; its lineage must include {missing!r}"
                )
        artifact = Artifact(
            artifact_id, model_id, verdict, dataset_manifest_hash, split_manifest_hash,
            prediction_manifest_hash, coverage, _now(), input_artifact_ids=lineage,
        )
        self.artifacts[artifact_id] = artifact
        return artifact

    def add_campaign(
        self,
        campaign_id: str,
        model_id: str,
        dependencies: Iterable[ArtifactDependency] = (),
    ) -> Campaign:
        if campaign_id in self.campaigns:
            raise PortfolioValidationError(f"campaign {campaign_id!r} already exists")
        campaign = Campaign(campaign_id, model_id, list(dependencies))
        campaign.history.append({"at": _now(), "event": "created", "status": campaign.status.value})
        self.campaigns[campaign_id] = campaign
        try:
            self.validate()
        except Exception:
            del self.campaigns[campaign_id]
            raise
        return campaign

    def repin(
        self,
        campaign_id: str,
        dependencies: Iterable[ArtifactDependency],
    ) -> Campaign:
        """Re-pin a stale campaign onto fresh dependencies, clearing ``stale``.

        The staleness flag is cleared only when EVERY new dependency resolves to a
        current, verdict-passed, hash-matched artifact whose entire ancestry is
        likewise clean.  Anything less leaves the campaign stale and refuses.
        """
        campaign = self._campaign(campaign_id)
        candidates = list(dependencies)
        if not all(isinstance(item, ArtifactDependency) for item in candidates):
            raise PortfolioValidationError("repin requires ArtifactDependency values")
        problems: list[str] = []
        for dependency in candidates:
            prefix = f"dependency {dependency.artifact_id!r}"
            if dependency.required_verdict not in PASSING_VERDICTS:
                problems.append(
                    f"{prefix} required_verdict {dependency.required_verdict!r} is not a "
                    f"passing verdict"
                )
            artifact = self.artifacts.get(dependency.artifact_id)
            if artifact is None:
                problems.append(f"{prefix} is missing")
                continue
            problems.extend(self._dependency_problems(dependency, artifact, prefix))
            if artifact.verdict not in PASSING_VERDICTS:
                problems.append(f"{prefix} verdict {artifact.verdict!r} is not a passing verdict")
            problems.extend(self._ancestry_problems(dependency.artifact_id, prefix))
        if problems:
            raise PortfolioValidationError(
                "cannot repin campaign " + repr(campaign_id) + ": " + "; ".join(problems)
            )
        previous = list(campaign.dependencies)
        campaign.dependencies = candidates
        try:
            self.validate()
        except Exception:
            campaign.dependencies = previous
            raise
        campaign.stale = False
        campaign.blocked_reasons = []
        campaign.history.append({
            "at": _now(),
            "event": "repinned",
            "dependencies": [dependency.artifact_id for dependency in candidates],
        })
        self.refresh(campaign_id)
        return campaign

    # ------------------------------------------------------------------ readiness

    def _dependency_problems(
        self, dependency: ArtifactDependency, artifact: Artifact, prefix: str
    ) -> list[str]:
        reasons: list[str] = []
        if not artifact.current:
            reasons.append(f"{prefix} was superseded by {artifact.superseded_by!r}")
        checks = (
            (artifact.model_id, dependency.upstream_model_id, "upstream model"),
            (artifact.verdict, dependency.required_verdict, "verdict"),
            (artifact.dataset_manifest_hash, dependency.dataset_manifest_hash, "dataset manifest"),
            (artifact.split_manifest_hash, dependency.split_manifest_hash, "split manifest"),
            (artifact.prediction_manifest_hash, dependency.prediction_manifest_hash,
             "prediction manifest"),
        )
        for actual, expected, label in checks:
            if actual != expected:
                reasons.append(f"{prefix} {label} is {actual!r}, expected {expected!r}")
        if artifact.coverage < dependency.minimum_coverage:
            reasons.append(
                f"{prefix} coverage {artifact.coverage:g} is below "
                f"{dependency.minimum_coverage:g}"
            )
        return reasons

    def _ancestry_problems(self, artifact_id: str, prefix: str) -> list[str]:
        """Walk lineage transitively, cycle guarded, reporting every poisoned ancestor."""
        problems: list[str] = []
        seen: set[str] = set()
        queue: deque[str] = deque([artifact_id])
        while queue:
            current_id = queue.popleft()
            if current_id in seen:
                continue
            seen.add(current_id)
            artifact = self.artifacts.get(current_id)
            if artifact is None:
                continue
            if not artifact.lineage_known:
                problems.append(
                    f"{prefix} ancestor {current_id!r} has unknown lineage and cannot be trusted"
                )
                continue
            for parent_id in artifact.input_artifact_ids or ():
                parent = self.artifacts.get(parent_id)
                if parent is None:
                    problems.append(f"{prefix} ancestor {parent_id!r} is missing")
                    continue
                if not parent.current:
                    problems.append(
                        f"{prefix} ancestor {parent_id!r} was superseded by "
                        f"{parent.superseded_by!r}"
                    )
                elif parent.verdict in FAILED_VERDICTS:
                    problems.append(
                        f"{prefix} ancestor {parent_id!r} has failed verdict {parent.verdict!r}"
                    )
                queue.append(parent_id)
        return problems

    def readiness(self, campaign_id: str) -> Readiness:
        campaign = self._campaign(campaign_id)
        reasons: list[str] = []
        if campaign.stale:
            reasons.append("campaign is stale and must be reseeded with current artifact lineage")
        for dependency in campaign.dependencies:
            artifact = self.artifacts.get(dependency.artifact_id)
            prefix = f"dependency {dependency.artifact_id!r}"
            if artifact is None:
                reasons.append(f"{prefix} is missing")
                continue
            reasons.extend(self._dependency_problems(dependency, artifact, prefix))
            reasons.extend(self._ancestry_problems(dependency.artifact_id, prefix))
        return Readiness(not reasons, tuple(reasons))

    def refresh(self, campaign_id: str) -> Readiness:
        campaign = self._campaign(campaign_id)
        result = self.readiness(campaign_id)
        campaign.blocked_reasons = list(result.reasons)
        if campaign.status in {CampaignStatus.PLANNED, CampaignStatus.BLOCKED, CampaignStatus.ACTIVATABLE}:
            target = CampaignStatus.ACTIVATABLE if result.activatable else CampaignStatus.BLOCKED
            self._transition(campaign, target, "readiness evaluated")
        return result

    def start_seeding(self, campaign_id: str) -> None:
        campaign = self._campaign(campaign_id)
        if campaign.status != CampaignStatus.ACTIVATABLE:
            raise PortfolioValidationError("only an ACTIVATABLE campaign can start seeding")
        if campaign.stale or not self.readiness(campaign_id).activatable:
            raise PortfolioValidationError("campaign dependencies are not ready")
        self._transition(campaign, CampaignStatus.SEEDING, "seeding started")

    def mark_ready(self, campaign_id: str) -> None:
        campaign = self._campaign(campaign_id)
        if campaign.status != CampaignStatus.SEEDING:
            raise PortfolioValidationError("only a SEEDING campaign can become READY")
        result = self.readiness(campaign_id)
        if not result.activatable:
            raise PortfolioValidationError("campaign dependencies are not ready")
        campaign.blocked_reasons.clear()
        self._transition(campaign, CampaignStatus.READY, "seeding completed")

    def supersede_artifact(self, artifact_id: str, replacement_id: str) -> set[str]:
        old = self._artifact(artifact_id)
        replacement = self._artifact(replacement_id)
        if not old.current:
            raise PortfolioValidationError(f"artifact {artifact_id!r} is already superseded")
        if old.model_id != replacement.model_id:
            raise PortfolioValidationError("replacement artifact must belong to the same model")
        if artifact_id == replacement_id or not replacement.current:
            raise PortfolioValidationError("replacement artifact must be a different current artifact")
        old.superseded_by = replacement_id
        old.superseded_at = _now()

        poisoned = self._descendant_artifacts(artifact_id)
        affected: set[str] = set()
        tainted_models: set[str] = set()
        changed = True
        while changed:
            changed = False
            for campaign in self.campaigns.values():
                if campaign.id in affected:
                    continue
                direct = any(dep.artifact_id in poisoned for dep in campaign.dependencies)
                transitive = any(dep.upstream_model_id in tainted_models for dep in campaign.dependencies)
                if direct or transitive:
                    affected.add(campaign.id)
                    tainted_models.add(campaign.model_id)
                    reason = (
                        f"upstream artifact {artifact_id!r} was superseded by {replacement_id!r}"
                    )
                    campaign.stale = True
                    campaign.blocked_reasons = [reason]
                    self._transition(campaign, CampaignStatus.BLOCKED, reason, force=True)
                    changed = True
        return affected

    def _descendant_artifacts(self, artifact_id: str) -> set[str]:
        """Every artifact whose recorded lineage reaches ``artifact_id``, plus itself."""
        poisoned = {artifact_id}
        changed = True
        while changed:
            changed = False
            for artifact in self.artifacts.values():
                if artifact.id in poisoned or not artifact.lineage_known:
                    continue
                if poisoned & set(artifact.input_artifact_ids or ()):
                    poisoned.add(artifact.id)
                    changed = True
        return poisoned

    # ------------------------------------------------------------------ invariants

    def validate(self) -> None:
        producers: dict[str, set[str]] = {}
        for campaign in self.campaigns.values():
            producers.setdefault(campaign.model_id, set()).add(campaign.id)
        duplicated = sorted(
            model_id for model_id, owners in producers.items() if len(owners) > 1
        )
        if duplicated:
            raise PortfolioValidationError(
                "each model_id may be produced by at most one campaign; duplicated: "
                + ", ".join(duplicated)
            )
        for campaign in self.campaigns.values():
            for dependency in campaign.dependencies:
                artifact = self.artifacts.get(dependency.artifact_id)
                if artifact is None and dependency.upstream_model_id not in producers:
                    raise PortfolioValidationError(
                        f"campaign {campaign.id!r} has dangling dependency: artifact "
                        f"{dependency.artifact_id!r} does not exist and no campaign produces "
                        f"model {dependency.upstream_model_id!r}"
                    )
                if artifact is not None and artifact.model_id != dependency.upstream_model_id:
                    raise PortfolioValidationError(
                        f"campaign {campaign.id!r} dependency model does not own artifact "
                        f"{dependency.artifact_id!r}"
                    )

        edges: dict[str, set[str]] = {campaign_id: set() for campaign_id in self.campaigns}
        for campaign in self.campaigns.values():
            for dependency in campaign.dependencies:
                edges[campaign.id].update(producers.get(dependency.upstream_model_id, set()))
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise PortfolioValidationError(f"campaign dependency cycle includes {node!r}")
            if node in visited:
                return
            visiting.add(node)
            for upstream in edges[node]:
                visit(upstream)
            visiting.remove(node)
            visited.add(node)

        for campaign_id in edges:
            visit(campaign_id)

    def _producer_campaign(self, model_id: str) -> Campaign | None:
        for campaign in self.campaigns.values():
            if campaign.model_id == model_id:
                return campaign
        return None

    def _campaign(self, campaign_id: str) -> Campaign:
        try:
            return self.campaigns[campaign_id]
        except KeyError as exc:
            raise PortfolioValidationError(f"unknown campaign {campaign_id!r}") from exc

    def _artifact(self, artifact_id: str) -> Artifact:
        try:
            return self.artifacts[artifact_id]
        except KeyError as exc:
            raise PortfolioValidationError(f"unknown artifact {artifact_id!r}") from exc

    @staticmethod
    def _transition(
        campaign: Campaign,
        target: CampaignStatus,
        reason: str,
        *,
        force: bool = False,
    ) -> None:
        if campaign.status == target and not force:
            return
        previous = campaign.status
        campaign.status = target
        campaign.history.append(
            {"at": _now(), "event": "status_changed", "from": previous.value,
             "to": target.value, "reason": reason}
        )
