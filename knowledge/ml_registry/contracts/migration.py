"""Pure, offline schema migrations for serialized campaign contracts.

Migrations never read or write campaign state. Callers provide a detached mapping
or ledger string and receive a new value suitable for validation by the current
contract type.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from ._validation import ContractError
from .campaign_spec import CampaignSpec
from .launch_intent import LaunchIntent
from .lease import CampaignLease
from .ledger_v2 import LEDGER_V2_HEADER, LedgerV2
from .outcome import CampaignOutcomeRecord


def migrate_legacy_trial_state(status: str, verdict: str | None = None) -> tuple[str, str | None]:
    """Losslessly translate the two pre-standard trial statuses.

    ``stagnant`` carried the parked adjudication inline; the standard registry
    separates it into execution status ``succeeded`` and verdict ``parked``.
    ``errored`` carried no adjudication and becomes the retryable execution status
    ``failed``. Current values pass through after strict validation.
    """
    from knowledge.ml_registry.domain.status import TrialStatus, Verdict

    if status == "stagnant":
        if verdict not in (None, Verdict.PARKED.value):
            raise ContractError("legacy stagnant status conflicts with its verdict")
        return TrialStatus.SUCCEEDED.value, Verdict.PARKED.value
    if status == "errored":
        if verdict is not None:
            raise ContractError("legacy errored status cannot carry an adjudication verdict")
        return TrialStatus.FAILED.value, None
    parsed = TrialStatus(status).value
    if verdict is not None:
        Verdict(verdict)
    return parsed, verdict


def migrate_registry_ratchet_lineage(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Offline-add ancestry markers without inventing counterfactual measurements.

    Historical trials cannot be made causally attributable after the fact. They are
    therefore bound to the best recoverable baseline lineage and explicitly marked
    ``counterfactual_unknown``; callers must not interpret migration as ratchet proof.
    """
    if not isinstance(payload, Mapping) or not isinstance(payload.get("facts"), list):
        raise ContractError("registry migration payload must contain a facts list")
    result = deepcopy(dict(payload))
    facts = result["facts"]
    models = {
        str(fact.get("id")): fact for fact in facts
        if isinstance(fact, dict) and fact.get("category") == "model"
    }
    by_id = {
        str(fact.get("id")): fact for fact in facts if isinstance(fact, dict)
    }
    for model_id, model in models.items():
        meta = model.setdefault("meta", {})
        baseline = str(meta.get("baseline"))
        previous = meta.get("previous_baseline")
        root_commit = str(previous if previous is not None else baseline)
        root = f"root:{model_id}:{root_commit}"
        history = meta.setdefault("adoption_lineages", {})
        adopted = next((
            fact for fact in facts
            if isinstance(fact, dict) and fact.get("category") == "idea"
            and fact.get("meta", {}).get("model_id") == model_id
            and fact.get("meta", {}).get("status") == "adopted"
        ), None)
        if adopted is not None and previous is not None:
            adopted_trial_id = str(adopted.get("meta", {}).get("adopted_trial_id"))
            adopted_trial = by_id.get(adopted_trial_id, {})
            lineage_id = f"adoption:{adopted_trial_id}"
            history.setdefault(lineage_id, {
                "lineage_id": lineage_id,
                "adoption_idea_id": str(adopted.get("id")),
                "adoption_trial_id": adopted_trial_id,
                "adopted_commit": str(adopted_trial.get("meta", {}).get("commit", baseline)),
                "parent_lineage_id": root,
                "parent_baseline_commit": root_commit,
            })
            meta.setdefault("active_adoption_lineage", lineage_id)
        else:
            meta.setdefault("active_adoption_lineage", root)
        meta["ratchet_lineage_migration"] = "counterfactual_unknown"
    for fact in facts:
        if not isinstance(fact, dict) or fact.get("category") != "trial":
            continue
        meta = fact.setdefault("meta", {})
        model = models.get(str(meta.get("model_id")))
        if model is None:
            continue
        meta.setdefault("base_lineage_id", f"legacy-unknown:{meta.get('model_id')}:{fact.get('id')}")
        meta.setdefault("base_commit", "legacy-unknown")
        meta.setdefault("ratchet_evidence", "counterfactual_unknown")
    return result


LATEST_SCHEMA_VERSIONS = {
    "campaign_spec": CampaignSpec.VERSION,
    "campaign_outcome": CampaignOutcomeRecord.VERSION,
    "launch_intent": LaunchIntent.VERSION,
    "campaign_lease": CampaignLease.VERSION,
    "ledger": 2,
}

_VALIDATORS = {
    "campaign_spec": CampaignSpec.from_mapping,
    "campaign_outcome": CampaignOutcomeRecord.from_mapping,
    "launch_intent": LaunchIntent.from_mapping,
    "campaign_lease": CampaignLease.from_mapping,
}


def migrate_mapping(kind: str, payload: Mapping[str, Any], *, source_version: int | None = None) -> dict[str, Any]:
    """Return a current, validated copy of one object contract.

    Version 0 denotes the pre-contract fixture shape: the current fields were
    already present but ``schema_version`` was absent. This migration adds only
    that discriminator. It deliberately provides no aliases or inferred data.
    """
    if kind in {"campaign_artifact", "promotion_record"}:
        raise ContractError(
            f"retired contract kind {kind!r} has no lossless standard-registry mapping; "
            "authenticate its frozen event log with read_retired_event_log and import the "
            "underlying run/artifact evidence explicitly"
        )
    if kind not in _VALIDATORS:
        raise ContractError(f"unknown contract kind {kind!r}")
    if not isinstance(payload, Mapping):
        raise ContractError("migration payload must be an object")
    result = deepcopy(dict(payload))
    embedded = result.get("schema_version")
    if source_version is None:
        source_version = 0 if embedded is None else embedded
    if isinstance(source_version, bool) or not isinstance(source_version, int) or source_version < 0:
        raise ContractError("source_version must be a non-negative integer")
    if embedded is not None and embedded != source_version:
        raise ContractError(
            f"declared source_version {source_version} does not match embedded schema_version {embedded}"
        )
    target = LATEST_SCHEMA_VERSIONS[kind]
    if source_version > target:
        raise ContractError(f"cannot migrate future {kind} schema_version {source_version}")
    if source_version == 0:
        result["schema_version"] = target
        source_version = target
    if kind == "campaign_outcome" and source_version == 1 and target == 2:
        raise ContractError(
            "campaign outcome v1 cannot be migrated losslessly: promotion_record_id does not identify a model version"
        )
    if source_version != target:
        raise ContractError(f"no {kind} migration path from schema_version {source_version} to {target}")
    return _VALIDATORS[kind](result).to_mapping()


def migrate_ledger(content: str) -> str:
    """Validate LedgerV2 or refuse a lossy legacy-ledger conversion.

    V0/V1 omit throughput and diff_lines, two adjudication inputs that cannot be
    reconstructed. Their writer must be upgraded and new measurements emitted.
    """
    first_line = content.splitlines()[0] if content.splitlines() else ""
    header = tuple(first_line.split("\t"))
    if header != LEDGER_V2_HEADER:
        raise ContractError(
            "legacy ledger cannot be migrated offline: throughput and diff_lines must be emitted by the writer"
        )
    return LedgerV2.parse(content).serialize()
