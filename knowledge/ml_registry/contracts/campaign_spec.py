from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
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
    #: The scalar judge, or ``None`` when the campaign declares a vector judge in ``metrics``.
    #: Exactly one of the two is present; a single-entry ``metrics`` list is normalised into
    #: ``metric`` at parse time, so downstream code sees one canonical scalar shape.
    metric: Mapping[str, Any] | None
    stages: tuple[Mapping[str, Any], ...]
    corpora: tuple[Mapping[str, Any], ...]
    requires: tuple[Mapping[str, Any], ...]
    produces: tuple[Mapping[str, Any], ...]
    supervision: Mapping[str, Any]
    resources: Mapping[str, Any]
    isolation: Mapping[str, Any]
    production: Mapping[str, Any]
    #: What a campaign is FED, per pipeline position: a contract plus a source, which is either
    #: ground-truth labels or a promoted upstream model. Opaque to Praxis like the rest of a spec --
    #: it is carried and stored, never interpreted. A campaign may declare none.
    inputs: tuple[Mapping[str, Any], ...] = ()
    extends: tuple[Mapping[str, Any], ...] = ()
    deterministic_incumbent: Mapping[str, Any] | None = None
    learned_escalation: bool = False
    rope: Mapping[str, Any] | None = None
    #: The VECTOR judge: every output the product consumes, each entry the same shape as
    #: ``metric`` (name, direction, adoption_floor, aggregation, scoring corpus,
    #: adjudication). Adoption over a vector is Pareto -- at least one judged metric wins
    #: and none regresses beyond its rope. Empty whenever ``metric`` is present.
    metrics: tuple[Mapping[str, Any], ...] = ()
    #: Within-corpus split: every dataset trains AND scores; held-out evidence comes from
    #: splitting inside the corpus at its own split unit, group-pure and frozen. Opaque to
    #: Praxis like the rest of a spec. ``None`` when a campaign has not declared one yet.
    split_policy: Mapping[str, Any] | None = None
    #: How far ahead of the predicted frame the model may read PIXELS (never labels).
    #: Offline campaigns declare a whole-sequence window. Opaque; ``None`` when undeclared.
    lookahead_window: Mapping[str, Any] | None = None
    #: Downstream work this model exists to enable, and the quality that work needs of it.
    #: Opaque to Praxis; ``None`` when undeclared. Written at seeding so "good enough" is a
    #: claim against a stated purpose, not a feeling reached at the end.
    sufficiency: Mapping[str, Any] | None = None
    #: Optional, immutable staged-measurement control.  It makes a campaign's cheap screen
    #: reproducible and prevents that screen from being mistaken for an alias-moving verdict.
    measurement_protocol: Mapping[str, Any] | None = None

    VERSION = 1

    @property
    def judged_metrics(self) -> tuple[Mapping[str, Any], ...]:
        """Every judged metric object, whichever of ``metric``/``metrics`` declared it."""
        if self.metric is not None:
            return (self.metric,)
        return self.metrics

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
        for name in ("stages", "corpora", "requires", "produces", "extends", "inputs"):
            raw = value.get(name, ())
            if not isinstance(raw, (list, tuple)) or not all(isinstance(item, Mapping) for item in raw):
                raise ContractError(f"{name} must be a sequence of objects")
            sequences[name] = tuple(dict(item) for item in raw)
        mappings = {}
        for name in ("supervision", "resources", "isolation", "production"):
            raw = value.get(name)
            if not isinstance(raw, Mapping):
                raise ContractError(f"{name} must be an object")
            mappings[name] = dict(raw)
        metric, vector_metrics = cls._judge(value)
        deterministic = value.get("deterministic_incumbent")
        if deterministic is not None and not isinstance(deterministic, Mapping):
            raise ContractError("deterministic_incumbent must be an object or null")
        escalation = value.get("learned_escalation", False)
        if not isinstance(escalation, bool):
            raise ContractError("learned_escalation must be boolean")
        rope = value.get("rope")
        if rope is not None and not isinstance(rope, Mapping):
            raise ContractError("rope must be an object or null")
        split_policy = value.get("split_policy")
        if split_policy is not None and not isinstance(split_policy, Mapping):
            raise ContractError("split_policy must be an object or null")
        lookahead_window = value.get("lookahead_window")
        if lookahead_window is not None and not isinstance(lookahead_window, Mapping):
            raise ContractError("lookahead_window must be an object or null")
        sufficiency = value.get("sufficiency")
        if sufficiency is not None and not isinstance(sufficiency, Mapping):
            raise ContractError("sufficiency must be an object or null")
        measurement_protocol = value.get("measurement_protocol")
        if measurement_protocol is not None:
            cls._measurement_protocol(measurement_protocol)
        if not sequences["produces"] and deterministic is None:
            raise ContractError("a learned campaign must declare at least one produced artifact")
        return cls(
            version, text(value.get("campaign_id"), "campaign_id"),
            text(value.get("model_id_policy"), "model_id_policy"), text(value.get("axis"), "axis"),
            scope, text(value.get("target_ontology"), "target_ontology"), metric,
            sequences["stages"], sequences["corpora"], sequences["requires"], sequences["produces"],
            mappings["supervision"], mappings["resources"], mappings["isolation"], mappings["production"],
            sequences["inputs"], sequences["extends"],
            None if deterministic is None else dict(deterministic), escalation,
            None if rope is None else dict(rope),
            vector_metrics,
            None if split_policy is None else dict(split_policy),
            None if lookahead_window is None else dict(lookahead_window),
            None if sufficiency is None else dict(sufficiency),
            None if measurement_protocol is None else dict(measurement_protocol),
        )

    @staticmethod
    def _measurement_protocol(value: object) -> None:
        """Validate the small immutable control plane for screen-then-confirm campaigns.

        This deliberately validates only the facts the registry can enforce without knowing a
        campaign's science: a screen's units and their fingerprint are frozen before it runs,
        its guards are named, and only a confirmation may reach external adjudication or an alias.
        """
        if not isinstance(value, Mapping):
            raise ContractError("measurement_protocol must be an object or null")
        screen, confirm = value.get("screen"), value.get("confirm")
        if not isinstance(screen, Mapping) or not isinstance(confirm, Mapping):
            raise ContractError("measurement_protocol requires screen and confirm objects")
        if screen.get("kind") != "screen":
            raise ContractError("measurement_protocol.screen.kind must be 'screen'")
        if confirm.get("kind") != "confirm":
            raise ContractError("measurement_protocol.confirm.kind must be 'confirm'")
        units = screen.get("units")
        if (not isinstance(units, (list, tuple)) or not units
                or not all(isinstance(unit, str) and unit.strip() for unit in units)):
            raise ContractError("measurement_protocol.screen.units must be a non-empty string sequence")
        canonical_units = tuple(sorted(str(unit) for unit in units))
        if len(set(canonical_units)) != len(canonical_units):
            raise ContractError("measurement_protocol.screen.units must not contain duplicates")
        fingerprint = screen.get("unit_fingerprint")
        expected = sha256("\n".join(canonical_units).encode()).hexdigest()
        if fingerprint != expected:
            raise ContractError("measurement_protocol.screen.unit_fingerprint does not match units")
        if screen.get("minimum_units") != len(canonical_units):
            raise ContractError("measurement_protocol.screen.minimum_units must equal frozen unit count")
        if not isinstance(screen.get("target"), str) or not screen["target"].strip():
            raise ContractError("measurement_protocol.screen.target must be a non-empty string")
        sentinels = screen.get("sentinels")
        if (not isinstance(sentinels, (list, tuple)) or not sentinels
                or not all(isinstance(item, str) and item.strip() for item in sentinels)):
            raise ContractError("measurement_protocol.screen.sentinels must be a non-empty string sequence")
        required_for = confirm.get("required_for")
        if not isinstance(required_for, (list, tuple)) or not {
            "external_adjudication", "alias_move"
        }.issubset(required_for):
            raise ContractError(
                "measurement_protocol.confirm.required_for must include external_adjudication and alias_move"
            )

    @classmethod
    def _judge(
        cls, value: Mapping[str, Any],
    ) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], ...]]:
        """Parse the judge: exactly one of ``metric`` (scalar) or ``metrics`` (vector).

        Both present and neither present are refused BY NAME. A single-entry ``metrics``
        list is the same declaration as ``metric`` and is normalised into it, so the two
        spellings cannot behave differently anywhere downstream.
        """
        raw_metric = value.get("metric")
        raw_metrics = value.get("metrics")
        if raw_metric is not None and raw_metrics is not None:
            raise ContractError(
                "campaign spec must declare exactly one of 'metric' (scalar judge) or "
                "'metrics' (vector judge); got both"
            )
        if raw_metric is None and raw_metrics is None:
            raise ContractError(
                "campaign spec must declare exactly one of 'metric' (scalar judge) or "
                "'metrics' (vector judge); got neither"
            )
        if raw_metric is not None:
            if not isinstance(raw_metric, Mapping):
                raise ContractError("metric must be an object")
            return dict(raw_metric), ()
        if (not isinstance(raw_metrics, (list, tuple)) or not raw_metrics
                or not all(isinstance(item, Mapping) for item in raw_metrics)):
            raise ContractError("metrics must be a non-empty sequence of judged metric objects")
        entries = tuple(dict(item) for item in raw_metrics)
        names = [text(item.get("name"), f"metrics[{index}].name")
                 for index, item in enumerate(entries)]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ContractError(
                f"metrics names each judged metric once; duplicated: {', '.join(duplicates)}"
            )
        if len(entries) == 1:
            return entries[0], ()
        return None, entries

    def to_mapping(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "campaign_id": self.campaign_id,
            "model_id_policy": self.model_id_policy,
            "axis": self.axis,
            "sport_scope": list(self.sport_scope),
            "target_ontology": self.target_ontology,
            # exactly one of the two judge spellings survives, at the position `metric`
            # has always held so a scalar spec round-trips byte-for-byte
            **({"metric": dict(self.metric)} if self.metric is not None
               else {"metrics": [dict(item) for item in self.metrics]}),
            "stages": [dict(item) for item in self.stages],
            "corpora": [dict(item) for item in self.corpora],
            "requires": [dict(item) for item in self.requires],
            "produces": [dict(item) for item in self.produces],
            "supervision": dict(self.supervision),
            "resources": dict(self.resources),
            "isolation": dict(self.isolation),
            "production": dict(self.production),
            "inputs": [dict(item) for item in self.inputs],
            "extends": [dict(item) for item in self.extends],
            "deterministic_incumbent": (None if self.deterministic_incumbent is None
                                          else dict(self.deterministic_incumbent)),
            "learned_escalation": self.learned_escalation,
        }
        if self.rope is not None:
            result["rope"] = dict(self.rope)
        if self.split_policy is not None:
            result["split_policy"] = dict(self.split_policy)
        if self.lookahead_window is not None:
            result["lookahead_window"] = dict(self.lookahead_window)
        if self.sufficiency is not None:
            result["sufficiency"] = dict(self.sufficiency)
        if self.measurement_protocol is not None:
            result["measurement_protocol"] = dict(self.measurement_protocol)
        return result
