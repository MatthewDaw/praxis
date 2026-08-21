"""Deterministic, read-only legacy views over canonical registry records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from .registry import Registry, RegistryError


SIDECAR_SCHEMA = "legacy-artifact-projection/v1"
_MANIFEST_KINDS = {"dataset_manifest": "datasets", "split_manifest": "splits",
                   "oof_predictions": "predictions"}


@dataclass(frozen=True)
class LegacyArtifactDependency:
    upstream_model_id: str
    artifact_id: str
    required_verdict: str
    dataset_manifest_hash: str
    split_manifest_hash: str
    prediction_manifest_hash: str
    minimum_coverage: float = 1.0


@dataclass(frozen=True)
class LegacyCampaignProjection:
    campaign_id: str
    model_id: str
    dependencies: tuple[LegacyArtifactDependency, ...]


@dataclass(frozen=True)
class PortfolioProjectionSpec:
    """Explicit non-registry input needed to reconstruct campaign readiness."""

    schema_version: int
    campaigns: tuple[LegacyCampaignProjection, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise RegistryError("unsupported portfolio projection schema_version")
        ids = [campaign.campaign_id for campaign in self.campaigns]
        if any(not item for item in ids) or len(ids) != len(set(ids)):
            raise RegistryError("portfolio projection campaign ids must be unique and non-empty")


def _json_object(value: str | bytes, *, field: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RegistryError(f"{field} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise RegistryError(f"{field} must be a JSON object")
    return decoded


def _run_artifacts(registry: Registry, run_id: str) -> list[dict[str, Any]]:
    return sorted((row for row in registry.rows("artifacts") if row["run_id"] == run_id),
                  key=lambda row: (row["kind"], row["artifact_id"]))


def _artifact_document(registry: Registry, artifact: Mapping[str, Any]) -> dict[str, Any]:
    path = registry.blobs.verify(artifact["artifact_id"], artifact["bytes"])
    return _json_object(path.read_bytes(), field=f"artifact {artifact['artifact_id']}")


def _projected_versions(registry: Registry) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return every version with one immutable migration-projection sidecar."""
    result = []
    versions = sorted(registry.rows("model_versions"),
                      key=lambda row: (row["model_id"], row["version"]))
    for version in versions:
        sidecars = [artifact for artifact in _run_artifacts(registry, version["run_id"])
                    if artifact["kind"] == "report" and artifact["schema_version"] == SIDECAR_SCHEMA]
        if not sidecars:
            continue
        if len(sidecars) != 1:
            raise RegistryError("a projected model version must have exactly one legacy sidecar")
        sidecar = _artifact_document(registry, sidecars[0])
        if sidecar.get("schema_version") != 1:
            raise RegistryError("unsupported legacy artifact projection sidecar version")
        result.append((version, sidecar))
    return result


def project_manifest_registry(registry: Registry) -> dict[str, Any]:
    """Reproduce ``ManifestRegistry`` from all projected historical versions."""
    document: dict[str, Any] = {"schema_version": 1, "datasets": [], "splits": [], "predictions": []}
    seen: set[tuple[str, str]] = set()
    for version, _sidecar in _projected_versions(registry):
        for artifact in _run_artifacts(registry, version["run_id"]):
            collection = _MANIFEST_KINDS.get(artifact["kind"])
            if collection is None:
                continue
            manifest = _artifact_document(registry, artifact)
            identity = manifest.get("id")
            if not isinstance(identity, str) or not identity:
                raise RegistryError(f"{artifact['kind']} artifact lacks a manifest id")
            marker = (collection, identity)
            if marker not in seen:
                document[collection].append(manifest)
                seen.add(marker)
    for collection in _MANIFEST_KINDS.values():
        document[collection].sort(key=lambda item: item["id"])
    return document


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _alias_versions(registry: Registry, alias_name: str) -> set[tuple[str, int]]:
    return {(row["model_id"], row["version"]) for row in registry.rows("aliases")
            if row["alias"] == alias_name}


def _successors(registry: Registry, projected: Mapping[tuple[str, int], tuple[dict[str, Any], dict[str, Any]]]) -> dict[tuple[str, int], tuple[dict[str, Any], dict[str, Any]]]:
    result = {}
    for edge in registry.rows("lineage"):
        parent_key = (edge["parent_model_id"], edge["parent_version"])
        child_key = (edge["child_model_id"], edge["child_version"])
        if parent_key in projected and child_key in projected:
            result[parent_key] = projected[child_key]
    return result


def _adoption_times(registry: Registry) -> dict[tuple[str, int], float]:
    times = {}
    for event in registry.list_events():
        if event.event_type == "run_adopted":
            version = event.payload["model_version"]
            times[(version["model_id"], version["version"])] = event.at
    return times


def project_artifact_cache_index(registry: Registry) -> dict[str, Any]:
    """Reproduce active and superseded cache history from versions and aliases."""
    projected_rows = _projected_versions(registry)
    projected = {(version["model_id"], version["version"]): (version, sidecar)
                 for version, sidecar in projected_rows}
    champions = _alias_versions(registry, "champion")
    successors = _successors(registry, projected)
    entries: dict[str, Any] = {}
    ids: dict[tuple[str, int], tuple[str, str]] = {}
    active: dict[str, str] = {}
    for version, sidecar in projected_rows:
        cache = sidecar.get("cache")
        if not isinstance(cache, dict) or not isinstance(cache.get("key"), dict):
            raise RegistryError("legacy sidecar lacks cache metadata and key")
        payload = {name: cache.get(name) for name in
                   ("key", "uri", "checksum", "coverage", "prediction_scope")}
        entry_id, key_id = _digest(payload), _digest(payload["key"])
        key = (version["model_id"], version["version"])
        ids[key] = (entry_id, key_id)
        entries[entry_id] = {"entry_id": entry_id, **payload}
        if key in champions:
            active[key_id] = entry_id
    superseded = {ids[parent][0]: ids[(child[0]["model_id"], child[0]["version"])][0]
                  for parent, child in successors.items()}
    return {"version": 1, "entries": entries, "active": active, "superseded": superseded}


def _legacy_input_ids(registry: Registry, version: Mapping[str, Any], projected: Mapping[tuple[str, int], tuple[dict[str, Any], dict[str, Any]]]) -> list[str]:
    result = set()
    for edge in registry.rows("lineage"):
        if (edge["child_model_id"], edge["child_version"]) != (version["model_id"], version["version"]):
            continue
        parent = projected.get((edge["parent_model_id"], edge["parent_version"]))
        if parent is not None:
            result.add(str(parent[1]["artifact"]["id"]))
    return sorted(result)


def _portfolio_artifacts(registry: Registry) -> list[dict[str, Any]]:
    projected_rows = _projected_versions(registry)
    projected = {(version["model_id"], version["version"]): (version, sidecar)
                 for version, sidecar in projected_rows}
    successors, times = _successors(registry, projected), _adoption_times(registry)
    runs = {row["run_id"]: row for row in registry.rows("runs")}
    artifacts = []
    for version, sidecar in projected_rows:
        key = (version["model_id"], version["version"])
        legacy = sidecar.get("artifact")
        if not isinstance(legacy, dict):
            raise RegistryError("legacy sidecar lacks artifact metadata")
        successor = successors.get(key)
        successor_key = None if successor is None else (successor[0]["model_id"], successor[0]["version"])
        artifacts.append({
            "id": legacy["id"], "model_id": version["model_id"],
            "verdict": runs[version["run_id"]]["verdict"],
            "dataset_manifest_hash": legacy["dataset_manifest_hash"],
            "split_manifest_hash": legacy["split_manifest_hash"],
            "prediction_manifest_hash": legacy["prediction_manifest_hash"],
            "coverage": legacy["coverage"],
            "created_at": datetime.fromtimestamp(times[key], timezone.utc).isoformat(),
            "superseded_by": None if successor is None else successor[1]["artifact"]["id"],
            "superseded_at": None if successor_key is None else datetime.fromtimestamp(times[successor_key], timezone.utc).isoformat(),
            "input_artifact_ids": _legacy_input_ids(registry, version, projected),
        })
    return sorted(artifacts, key=lambda item: item["id"])


def _readiness(campaign: LegacyCampaignProjection, artifacts: Mapping[str, Mapping[str, Any]]) -> tuple[bool, list[str]]:
    reasons = []
    for dependency in campaign.dependencies:
        artifact = artifacts.get(dependency.artifact_id)
        prefix = f"dependency {dependency.artifact_id!r}"
        if artifact is None:
            reasons.append(f"{prefix} is missing")
            continue
        if artifact["superseded_by"] is not None:
            reasons.append(f"{prefix} was superseded by {artifact['superseded_by']!r}")
        checks = ((artifact["model_id"], dependency.upstream_model_id, "upstream model"),
                  (artifact["verdict"], dependency.required_verdict, "verdict"),
                  (artifact["dataset_manifest_hash"], dependency.dataset_manifest_hash, "dataset manifest"),
                  (artifact["split_manifest_hash"], dependency.split_manifest_hash, "split manifest"),
                  (artifact["prediction_manifest_hash"], dependency.prediction_manifest_hash, "prediction manifest"))
        for actual, expected, label in checks:
            if actual != expected:
                reasons.append(f"{prefix} {label} is {actual!r}, expected {expected!r}")
        if artifact["coverage"] < dependency.minimum_coverage:
            reasons.append(f"{prefix} coverage {artifact['coverage']:g} is below {dependency.minimum_coverage:g}")
    return not reasons, reasons


def project_portfolio_artifacts(registry: Registry, *, portfolio_spec: PortfolioProjectionSpec) -> dict[str, Any]:
    """Reproduce legacy artifacts and readiness with an explicit campaign-spec input."""
    artifacts = _portfolio_artifacts(registry)
    by_id = {artifact["id"]: artifact for artifact in artifacts}
    campaigns = []
    for campaign in sorted(portfolio_spec.campaigns, key=lambda item: item.campaign_id):
        ready, reasons = _readiness(campaign, by_id)
        campaigns.append({"id": campaign.campaign_id, "model_id": campaign.model_id,
                          "dependencies": [asdict(item) for item in campaign.dependencies],
                          "status": "ACTIVATABLE" if ready else "BLOCKED", "stale": False,
                          "blocked_reasons": reasons, "history": []})
    return {"schema_version": 1, "campaigns": campaigns, "artifacts": artifacts}


def canonical_json_bytes(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
