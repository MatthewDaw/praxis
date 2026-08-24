from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ._validation import ContractError, exact_keys, integer, text


@dataclass(frozen=True)
class CampaignSpec:
    """Version-1 structural contract owned by Praxis; project semantics stay opaque."""

    schema_version: int
    campaign_id: str
    model_id_policy: str
    axis: str
    sport_scope: tuple[str, ...]
    target_ontology: str
    metric: Mapping[str, Any]
    stages: tuple[Mapping[str, Any], ...]
    corpora: tuple[Mapping[str, Any], ...]
    requires: tuple[Mapping[str, Any], ...]
    produces: tuple[Mapping[str, Any], ...]
    supervision: Mapping[str, Any]
    resources: Mapping[str, Any]
    isolation: Mapping[str, Any]
    production: Mapping[str, Any]
    extends: tuple[Mapping[str, Any], ...] = ()
    deterministic_incumbent: Mapping[str, Any] | None = None
    learned_escalation: bool = False
    rope: Mapping[str, Any] | None = None

    VERSION = 1

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CampaignSpec":
        exact_keys(value, set(cls.__dataclass_fields__) - {"VERSION"}, "campaign spec")
        version = integer(value.get("schema_version"), "schema_version", minimum=1)
        if version != cls.VERSION:
            raise ContractError(f"unsupported CampaignSpec schema_version {version}")
        scope = value.get("sport_scope")
        if isinstance(scope, str):
            scope = (text(scope, "sport_scope"),)
        elif isinstance(scope, (list, tuple)) and scope and all(isinstance(item, str) and item for item in scope):
            scope = tuple(scope)
        else:
            raise ContractError("sport_scope must be a string or non-empty string sequence")
        sequences = {}
        for name in ("stages", "corpora", "requires", "produces", "extends"):
            raw = value.get(name, ())
            if not isinstance(raw, (list, tuple)) or not all(isinstance(item, Mapping) for item in raw):
                raise ContractError(f"{name} must be a sequence of objects")
            sequences[name] = tuple(dict(item) for item in raw)
        mappings = {}
        for name in ("metric", "supervision", "resources", "isolation", "production"):
            raw = value.get(name)
            if not isinstance(raw, Mapping):
                raise ContractError(f"{name} must be an object")
            mappings[name] = dict(raw)
        deterministic = value.get("deterministic_incumbent")
        if deterministic is not None and not isinstance(deterministic, Mapping):
            raise ContractError("deterministic_incumbent must be an object or null")
        escalation = value.get("learned_escalation", False)
        if not isinstance(escalation, bool):
            raise ContractError("learned_escalation must be boolean")
        rope = value.get("rope")
        if rope is not None and not isinstance(rope, Mapping):
            raise ContractError("rope must be an object or null")
        if not sequences["produces"] and deterministic is None:
            raise ContractError("a learned campaign must declare at least one produced artifact")
        return cls(
            version, text(value.get("campaign_id"), "campaign_id"),
            text(value.get("model_id_policy"), "model_id_policy"), text(value.get("axis"), "axis"),
            scope, text(value.get("target_ontology"), "target_ontology"), mappings["metric"],
            sequences["stages"], sequences["corpora"], sequences["requires"], sequences["produces"],
            mappings["supervision"], mappings["resources"], mappings["isolation"], mappings["production"],
            sequences["extends"], None if deterministic is None else dict(deterministic), escalation,
            None if rope is None else dict(rope),
        )

    def to_mapping(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "campaign_id": self.campaign_id,
            "model_id_policy": self.model_id_policy,
            "axis": self.axis,
            "sport_scope": list(self.sport_scope),
            "target_ontology": self.target_ontology,
            "metric": dict(self.metric),
            "stages": [dict(item) for item in self.stages],
            "corpora": [dict(item) for item in self.corpora],
            "requires": [dict(item) for item in self.requires],
            "produces": [dict(item) for item in self.produces],
            "supervision": dict(self.supervision),
            "resources": dict(self.resources),
            "isolation": dict(self.isolation),
            "production": dict(self.production),
            "extends": [dict(item) for item in self.extends],
            "deterministic_incumbent": (None if self.deterministic_incumbent is None
                                          else dict(self.deterministic_incumbent)),
            "learned_escalation": self.learned_escalation,
        }
        if self.rope is not None:
            result["rope"] = dict(self.rope)
        return result
