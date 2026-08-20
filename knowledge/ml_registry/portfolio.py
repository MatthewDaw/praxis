"""Durable portfolio planning and cross-model artifact lineage.

This module deliberately sits beside, rather than inside, ``RegistrySpace``.  The
model/idea/trial registry describes one executable campaign; ``Portfolio`` describes
when campaigns are allowed to become executable and which exact upstream artifacts
their evidence depends on.
"""

from __future__ import annotations

import json
import os
import tempfile
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
            "required_verdict",
            "dataset_manifest_hash",
            "split_manifest_hash",
            "prediction_manifest_hash",
        ):
            if not getattr(self, name):
                raise PortfolioValidationError(f"dependency {name} must be non-empty")
        if not 0.0 <= self.minimum_coverage <= 1.0:
            raise PortfolioValidationError("minimum_coverage must be between 0 and 1")


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
    input_artifact_ids: tuple[str, ...] = ()

    @property
    def current(self) -> bool:
        return self.superseded_by is None


@dataclass
class Campaign:
    id: str
    model_id: str
    dependencies: list[ArtifactDependency] = field(default_factory=list)
    status: CampaignStatus = CampaignStatus.PLANNED
    stale: bool = False
    blocked_reasons: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Readiness:
    activatable: bool
    reasons: tuple[str, ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Portfolio:
    """A campaign DAG with optional atomic JSON persistence."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.campaigns: dict[str, Campaign] = {}
        self.artifacts: dict[str, Artifact] = {}

    @classmethod
    def load(cls, path: str | Path) -> "Portfolio":
        portfolio = cls(path)
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PortfolioValidationError(f"invalid portfolio document: {exc}") from exc
        if not isinstance(document, dict):
            raise PortfolioValidationError("portfolio document must be a JSON object")
        if document.get("schema_version") != cls.SCHEMA_VERSION:
            raise PortfolioValidationError("unsupported portfolio schema_version")
        try:
            if not isinstance(document.get("artifacts", []), list) or not isinstance(document.get("campaigns", []), list):
                raise TypeError("campaigns and artifacts must be arrays")
            for item in document.get("artifacts", []):
                raw = dict(item)
                raw["input_artifact_ids"] = tuple(raw.get("input_artifact_ids", ()))
                artifact = Artifact(**raw)
                portfolio.artifacts[artifact.id] = artifact
            for item in document.get("campaigns", []):
                raw = dict(item)
                dependencies = [ArtifactDependency(**dep) for dep in raw.pop("dependencies", [])]
                raw["status"] = CampaignStatus(raw["status"])
                campaign = Campaign(dependencies=dependencies, **raw)
                portfolio.campaigns[campaign.id] = campaign
        except (TypeError, KeyError, ValueError, AttributeError) as exc:
            raise PortfolioValidationError(f"malformed portfolio document: {exc}") from exc
        portfolio.validate()
        return portfolio

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
                json.dump(document, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

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
        if not all((artifact_id, model_id, verdict, dataset_manifest_hash, split_manifest_hash,
                    prediction_manifest_hash)):
            raise PortfolioValidationError("artifact identity and manifest fields must be non-empty")
        if not 0.0 <= coverage <= 1.0:
            raise PortfolioValidationError("artifact coverage must be between 0 and 1")
        lineage = tuple(sorted(set(input_artifact_ids)))
        if any(not item or item not in self.artifacts for item in lineage):
            raise PortfolioValidationError("artifact lineage must reference existing artifact ids")
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
        if not campaign_id or not model_id:
            raise PortfolioValidationError("campaign_id and model_id must be non-empty")
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
            if not artifact.current:
                reasons.append(f"{prefix} was superseded by {artifact.superseded_by!r}")
            checks = (
                (artifact.model_id, dependency.upstream_model_id, "upstream model"),
                (artifact.verdict, dependency.required_verdict, "verdict"),
                (artifact.dataset_manifest_hash, dependency.dataset_manifest_hash, "dataset manifest"),
                (artifact.split_manifest_hash, dependency.split_manifest_hash, "split manifest"),
                (artifact.prediction_manifest_hash, dependency.prediction_manifest_hash, "prediction manifest"),
            )
            for actual, expected, label in checks:
                if actual != expected:
                    reasons.append(f"{prefix} {label} is {actual!r}, expected {expected!r}")
            if artifact.coverage < dependency.minimum_coverage:
                reasons.append(
                    f"{prefix} coverage {artifact.coverage:g} is below "
                    f"{dependency.minimum_coverage:g}"
                )
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

        affected: set[str] = set()
        tainted_models: set[str] = set()
        changed = True
        while changed:
            changed = False
            for campaign in self.campaigns.values():
                if campaign.id in affected:
                    continue
                direct = any(dep.artifact_id == artifact_id for dep in campaign.dependencies)
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

    def validate(self) -> None:
        producers: dict[str, set[str]] = {}
        for campaign in self.campaigns.values():
            producers.setdefault(campaign.model_id, set()).add(campaign.id)
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
