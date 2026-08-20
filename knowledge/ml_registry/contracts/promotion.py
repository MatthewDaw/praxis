from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from ._validation import ContractError, exact_keys, integer, number, text


@dataclass(frozen=True)
class PromotionRecord:
    schema_version: int
    promotion_record_id: str
    campaign_id: str
    model_id: str
    adopted_trial_id: str
    lineage_id: str
    convergence_artifact_id: str
    dataset_manifest_hash: str
    split_manifest_hash: str
    preprocessing_hash: str
    code_commit: str
    configuration_hash: str
    metric_name: str
    metric_value: float
    thresholds_hash: str
    upstream_artifact_ids: tuple[str, ...]
    compatibility_test: str
    compatibility_passed: bool

    VERSION = 1

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PromotionRecord":
        exact_keys(value, set(cls.__dataclass_fields__), "promotion record")
        version = integer(value.get("schema_version"), "schema_version", minimum=1)
        if version != cls.VERSION:
            raise ContractError(f"unsupported PromotionRecord schema_version {version}")
        upstream = value.get("upstream_artifact_ids")
        if not isinstance(upstream, (list, tuple)) or not all(isinstance(item, str) and item for item in upstream):
            raise ContractError("upstream_artifact_ids must be a string sequence")
        metric = number(value.get("metric_value"), "metric_value")
        passed = value.get("compatibility_passed")
        if not isinstance(passed, bool):
            raise ContractError("compatibility_passed must be boolean")
        names = ("promotion_record_id", "campaign_id", "model_id", "adopted_trial_id", "lineage_id",
                 "convergence_artifact_id", "dataset_manifest_hash", "split_manifest_hash",
                 "preprocessing_hash", "code_commit", "configuration_hash", "metric_name", "thresholds_hash")
        strings = [text(value.get(name), name) for name in names]
        return cls(version, *strings[:12], metric, strings[12], tuple(upstream),
                   text(value.get("compatibility_test"), "compatibility_test"), passed)

    def to_mapping(self) -> dict[str, Any]:
        value = asdict(self)
        value["upstream_artifact_ids"] = list(self.upstream_artifact_ids)
        return value
