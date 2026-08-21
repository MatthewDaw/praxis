"""Artifact-derived campaign readiness over the standard model registry."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Sequence

from knowledge.ml_registry.contracts import CampaignSpec
from knowledge.ml_registry.storage.blobs import BlobError
from knowledge.ml_registry.storage.registry import Registry, RegistryError


class ReadinessError(RegistryError):
    """The portfolio specification cannot be resolved unambiguously."""


@dataclass(frozen=True)
class ArtifactReadiness:
    campaign_id: str
    ready: bool
    artifact_id: str | None
    reason: str


def _specs_from_events(registry: Registry) -> tuple[CampaignSpec, ...]:
    latest: dict[str, Mapping[str, Any]] = {}
    for event in registry.list_events():
        if event.event_type == "campaign_spec_registered":
            latest[str(event.payload["campaign_id"])] = event.payload
    return tuple(CampaignSpec.from_mapping(latest[key]) for key in sorted(latest))


def _artifact_type(item: Mapping[str, Any], *, owner: str) -> str:
    value = item.get("artifact_type")
    if not isinstance(value, str) or not value:
        raise ReadinessError(f"{owner} has an invalid artifact_type")
    return value


def _producer_contract(
    specs: Mapping[str, CampaignSpec], requirement: Mapping[str, Any], *, consumer_id: str,
) -> tuple[CampaignSpec, Mapping[str, Any]]:
    producer_id = requirement.get("producer_campaign_id")
    if not isinstance(producer_id, str) or not producer_id:
        raise ReadinessError(
            f"campaign {consumer_id!r} requirement has no producer_campaign_id"
        )
    producer = specs.get(producer_id)
    if producer is None:
        raise ReadinessError(
            f"campaign {consumer_id!r} requires {_artifact_type(requirement, owner=consumer_id)!r} "
            f"from unknown producer {producer_id!r}"
        )
    artifact_type = _artifact_type(requirement, owner=consumer_id)
    matches = [item for item in producer.produces
               if _artifact_type(item, owner=producer_id) == artifact_type]
    if len(matches) != 1:
        raise ReadinessError(
            f"campaign {consumer_id!r} requires exactly one {producer_id}:{artifact_type} "
            f"producer contract; found {len(matches)}"
        )
    return producer, matches[0]


def validate_artifact_graph(specs: Sequence[CampaignSpec]) -> None:
    """Reject missing/ambiguous producers and cycles in the derived graph."""
    by_id = {spec.campaign_id: spec for spec in specs}
    if len(by_id) != len(specs):
        raise ReadinessError("portfolio contains duplicate campaign_id values")
    edges: dict[str, tuple[str, ...]] = {}
    for spec in specs:
        producers: list[str] = []
        for requirement in spec.requires:
            producer, _ = _producer_contract(by_id, requirement, consumer_id=spec.campaign_id)
            producers.append(producer.campaign_id)
        edges[spec.campaign_id] = tuple(producers)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(campaign_id: str, path: tuple[str, ...]) -> None:
        if campaign_id in visiting:
            start = path.index(campaign_id)
            raise ReadinessError("artifact dependency cycle: " +
                                 " -> ".join((*path[start:], campaign_id)))
        if campaign_id in visited:
            return
        visiting.add(campaign_id)
        for producer_id in edges[campaign_id]:
            visit(producer_id, (*path, campaign_id))
        visiting.remove(campaign_id)
        visited.add(campaign_id)

    for campaign_id in sorted(edges):
        visit(campaign_id, ())


def _production_version(registry: Registry, producer_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    versions = registry.rows("model_versions")
    runs = {row["run_id"]: row for row in registry.rows("runs")}
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for alias in registry.rows("aliases"):
        if alias["alias"] != "production":
            continue
        version = next((row for row in versions
                        if row["model_id"] == alias["model_id"]
                        and row["version"] == alias["version"]), None)
        if version is None:
            continue
        run = runs.get(version["run_id"])
        if run is not None and run["experiment_id"] == producer_id:
            candidates.append((version, run))
    if len(candidates) != 1:
        raise ReadinessError(
            f"producer {producer_id!r} has {len(candidates)} production aliases; expected exactly one"
        )
    return candidates[0]


def explain_readiness(
    registry: Registry, campaign_id: str, *, specs: Sequence[CampaignSpec] | None = None,
) -> ArtifactReadiness:
    """Explain whether every required type resolves to current verified production bytes."""
    portfolio = tuple(specs) if specs is not None else _specs_from_events(registry)
    validate_artifact_graph(portfolio)
    by_id = {spec.campaign_id: spec for spec in portfolio}
    consumer = by_id.get(campaign_id)
    if consumer is None:
        raise ReadinessError(f"unknown campaign spec {campaign_id!r}")
    if not consumer.requires:
        return ArtifactReadiness(campaign_id, True, None, "no upstream artifacts required")

    artifacts = {row["artifact_id"]: row for row in registry.rows("artifacts")}
    for requirement in consumer.requires:
        producer, produced = _producer_contract(by_id, requirement, consumer_id=campaign_id)
        artifact_type = _artifact_type(requirement, owner=campaign_id)
        try:
            version, run = _production_version(registry, producer.campaign_id)
        except ReadinessError as exc:
            return ArtifactReadiness(campaign_id, False, None, str(exc))
        artifact_id = version["artifact_id"]
        artifact = artifacts.get(artifact_id)
        if artifact is None or artifact["run_id"] != run["run_id"] or artifact["kind"] != artifact_type:
            return ArtifactReadiness(
                campaign_id, False, artifact_id,
                f"artifact {artifact_id} does not satisfy {producer.campaign_id}:{artifact_type}",
            )
        if version["checksum"] != artifact_id:
            return ArtifactReadiness(campaign_id, False, artifact_id,
                                     f"artifact {artifact_id} checksum differs from its model version")
        try:
            registry.blobs.verify(artifact_id, artifact["bytes"])
        except BlobError as exc:
            return ArtifactReadiness(campaign_id, False, artifact_id,
                                     f"artifact {artifact_id} checksum verification failed: {exc}")

        expected_schema = str(produced.get("schema_version", ""))
        if not expected_schema or artifact["schema_version"] != expected_schema:
            return ArtifactReadiness(
                campaign_id, False, artifact_id,
                f"artifact {artifact_id} is stale: schema {artifact['schema_version']!r}, "
                f"producer now declares {expected_schema!r}",
            )
        effective = registry.effective_model_version(version["model_id"], version["version"])
        if effective["effective_status"] == "superseded":
            return ArtifactReadiness(campaign_id, False, artifact_id,
                                     f"artifact {artifact_id} belongs to superseded lineage")
        if effective["effective_status"] != "active":
            return ArtifactReadiness(
                campaign_id, False, artifact_id,
                f"artifact {artifact_id} production version is {effective['effective_status']}",
            )
        code_ref = json.loads(run["code_ref"])
        compat = effective["effective_compat_result"]
        if (compat.get("passed") is not True or not code_ref.get("repo")
                or compat.get("head_sha") != registry._git_head(code_ref["repo"])):
            return ArtifactReadiness(campaign_id, False, artifact_id,
                                     f"artifact {artifact_id} compatibility evidence is stale")

    return ArtifactReadiness(campaign_id, True, None, "all required production artifacts verified")
