"""Ancestry-aware, paired-counterfactual evidence for adoption rollback.

A descendant's absolute score cannot identify whether its active adoption or its own
intervention caused a regression.  This service therefore admits ratchet evidence only
when the same intervention was fairly measured on both the active adoption lineage and
that adoption's direct parent lineage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from knowledge.ml_registry.floor import scaled_noise_floor
from knowledge.ml_registry.schema import RegistryValidationError


ACTIVE_LINEAGE_FIELD = "active_adoption_lineage"
LINEAGE_HISTORY_FIELD = "adoption_lineages"
BASE_LINEAGE_FIELD = "base_lineage_id"
BASE_COMMIT_FIELD = "base_commit"
COUNTERFACTUAL_COMMIT_FIELD = "counterfactual_commit"
COUNTERFACTUAL_LINEAGE_FIELD = "counterfactual_lineage_id"
INTERVENTION_DIGEST_FIELD = "intervention_digest"
COUNTERFACTUAL_INTERVENTION_DIGEST_FIELD = "counterfactual_intervention_digest"
RATCHET_EVIDENCE_FIELD = "ratchet_evidence"


@dataclass(frozen=True)
class AdoptionLineage:
    lineage_id: str
    adoption_idea_id: str
    adoption_trial_id: str
    adopted_commit: str
    parent_lineage_id: str
    parent_baseline_commit: str

    def to_mapping(self) -> dict[str, str]:
        return dict(self.__dict__)


def root_lineage_id(model_id: str, baseline_commit: str) -> str:
    return f"root:{model_id}:{baseline_commit}"


def current_lineage(model_id: str, model_meta: Mapping[str, object]) -> str:
    baseline = str(model_meta.get("baseline"))
    return str(model_meta.get(ACTIVE_LINEAGE_FIELD) or root_lineage_id(model_id, baseline))


def stamp_trial_lineage(model_id: str, model_meta: Mapping[str, object], trial_meta: dict[str, object]) -> None:
    """Bind a trial to the baseline ancestry that existed before it ran."""
    expected_commit = str(model_meta.get("baseline"))
    expected_lineage = current_lineage(model_id, model_meta)
    supplied_commit = trial_meta.setdefault(BASE_COMMIT_FIELD, expected_commit)
    supplied_lineage = trial_meta.setdefault(BASE_LINEAGE_FIELD, expected_lineage)
    if str(supplied_commit) != expected_commit:
        raise RegistryValidationError(
            f"trial base_commit {supplied_commit!r} does not match current baseline {expected_commit!r}",
            field=BASE_COMMIT_FIELD,
        )
    if str(supplied_lineage) != expected_lineage:
        raise RegistryValidationError(
            f"trial base_lineage_id {supplied_lineage!r} does not match active lineage {expected_lineage!r}",
            field=BASE_LINEAGE_FIELD,
        )


def record_adoption_lineage(
    model_id: str, model_meta: dict[str, object], *, idea_id: str, trial_id: str,
    adopted_commit: str, parent_baseline_commit: str, parent_lineage_id: str | None = None,
) -> AdoptionLineage:
    parent = parent_lineage_id or current_lineage(model_id, model_meta)
    lineage = AdoptionLineage(
        lineage_id=f"adoption:{trial_id}", adoption_idea_id=idea_id,
        adoption_trial_id=trial_id, adopted_commit=adopted_commit,
        parent_lineage_id=parent, parent_baseline_commit=parent_baseline_commit,
    )
    history = dict(model_meta.get(LINEAGE_HISTORY_FIELD) or {})
    history[lineage.lineage_id] = lineage.to_mapping()
    model_meta[LINEAGE_HISTORY_FIELD] = history
    model_meta[ACTIVE_LINEAGE_FIELD] = lineage.lineage_id
    return lineage


def restore_parent_lineage(model_meta: dict[str, object], lineage: AdoptionLineage) -> None:
    model_meta[ACTIVE_LINEAGE_FIELD] = lineage.parent_lineage_id


def active_adoption_lineage(model_meta: Mapping[str, object]) -> AdoptionLineage | None:
    active = model_meta.get(ACTIVE_LINEAGE_FIELD)
    raw = (model_meta.get(LINEAGE_HISTORY_FIELD) or {}).get(active) if active else None  # type: ignore[union-attr]
    return AdoptionLineage(**raw) if isinstance(raw, dict) else None


def lineage_by_id(model_meta: Mapping[str, object], lineage_id: str) -> AdoptionLineage | None:
    raw = (model_meta.get(LINEAGE_HISTORY_FIELD) or {}).get(lineage_id)  # type: ignore[union-attr]
    return AdoptionLineage(**raw) if isinstance(raw, dict) else None


def counterfactual_harm(
    model_meta: Mapping[str, object], trial_meta: dict[str, object], ledger_rows: Mapping[str, object],
    *, observed_value: float, direction: str,
) -> bool | None:
    """Return True only for causally harmful evidence; None means evidence unavailable."""
    lineage = active_adoption_lineage(model_meta)
    cf_commit = trial_meta.get(COUNTERFACTUAL_COMMIT_FIELD)
    if lineage is None or cf_commit is None:
        trial_meta[RATCHET_EVIDENCE_FIELD] = "counterfactual_unavailable"
        return None
    if str(trial_meta.get(BASE_LINEAGE_FIELD)) != lineage.lineage_id:
        raise RegistryValidationError("trial is not based on the active adoption lineage", field=BASE_LINEAGE_FIELD)
    if str(trial_meta.get(COUNTERFACTUAL_LINEAGE_FIELD)) != lineage.parent_lineage_id:
        raise RegistryValidationError(
            "counterfactual_lineage_id must name the active adoption's direct parent lineage",
            field=COUNTERFACTUAL_LINEAGE_FIELD,
        )
    digest = str(trial_meta.get(INTERVENTION_DIGEST_FIELD) or "").strip()
    counterfactual_digest = str(
        trial_meta.get(COUNTERFACTUAL_INTERVENTION_DIGEST_FIELD) or ""
    ).strip()
    if not digest:
        raise RegistryValidationError("paired counterfactual requires intervention_digest", field=INTERVENTION_DIGEST_FIELD)
    if counterfactual_digest != digest:
        raise RegistryValidationError(
            "counterfactual intervention digest does not match the observed intervention",
            field=COUNTERFACTUAL_INTERVENTION_DIGEST_FIELD,
        )
    row = ledger_rows.get(str(cf_commit))
    if row is None:
        raise RegistryValidationError(
            f"counterfactual commit {cf_commit!r} has no matching ledger row",
            field=COUNTERFACTUAL_COMMIT_FIELD,
        )
    status = str(getattr(row, "status", "")).strip().lower()
    if status not in {"ok", ""}:
        trial_meta[RATCHET_EVIDENCE_FIELD] = "counterfactual_unfair"
        return None
    cf_value = float(getattr(row, "value"))
    benefit = cf_value - observed_value if direction == "minimize" else observed_value - cf_value
    floor = scaled_noise_floor(model_meta, cf_value)
    trial_meta[RATCHET_EVIDENCE_FIELD] = {
        "lineage_id": lineage.lineage_id, "counterfactual_lineage_id": lineage.parent_lineage_id,
        "counterfactual_commit": str(cf_commit), "benefit": benefit, "noise_floor": floor,
    }
    return benefit < -floor
