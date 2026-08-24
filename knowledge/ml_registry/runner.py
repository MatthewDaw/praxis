"""Unattended portfolio runner whose only resume state is the canonical registry."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from knowledge.ml_registry.contracts import (
    CampaignOutcome,
    CampaignOutcomeRecord,
    CampaignSpec,
    ContractError,
)
from knowledge.ml_registry.storage.registry import Registry, RegistryError


TERMINAL_OUTCOMES = frozenset({
    CampaignOutcome.PROMOTED,
    CampaignOutcome.MEASURED,
    CampaignOutcome.REFUTED,
    CampaignOutcome.ABANDONED,
})


@dataclass(frozen=True)
class CampaignDispatch:
    """One campaign plus the durable position from which its supervisor resumes."""

    campaign: CampaignSpec
    last_adjudicated_run_id: str | None
    redispatch_run_ids: tuple[str, ...]


@dataclass(frozen=True)
class CampaignRunReport:
    outcomes: tuple[CampaignOutcomeRecord, ...]


CampaignDriver = Callable[[CampaignDispatch], CampaignOutcomeRecord]


def deregister_campaign(
    registry: Registry,
    campaign_id: str,
    *,
    reason: str,
) -> CampaignOutcomeRecord:
    """Close a mistaken registration as ABANDONED without blocking its siblings."""
    campaign_id = campaign_id.strip()
    reason = reason.strip()
    if not campaign_id or not reason:
        raise RegistryError("campaign de-registration requires an id and reason")
    order, entries, outcomes = _portfolio_state(registry)
    if campaign_id not in order or campaign_id not in entries:
        raise RegistryError(f"cannot de-register unknown campaign {campaign_id!r}")
    if campaign_id in outcomes:
        raise RegistryError(f"campaign {campaign_id!r} already has a terminal outcome")
    record = CampaignOutcomeRecord(
        CampaignOutcomeRecord.VERSION,
        campaign_id,
        CampaignOutcome.ABANDONED,
        reason,
        1,
    )
    registry.record_campaign_outcome(record.to_mapping())
    return record


def register_campaign_for_run(
    registry: Registry,
    spec: Mapping[str, Any],
    *,
    scoring_corpora: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    structural_validator: Callable[[Mapping[str, Any]], object] | None = None,
) -> bool:
    """Register one spec, retaining a policy refusal for the eventual run report."""
    campaign_id = spec.get("campaign_id")
    if not isinstance(campaign_id, str) or not campaign_id.strip():
        raise ContractError("campaign_id must be a non-empty string")
    try:
        return registry.register_campaign_spec(
            spec,
            scoring_corpora=scoring_corpora,
            structural_validator=structural_validator,
        )
    except ContractError as exc:
        registry.record_campaign_registration_refusal(campaign_id, str(exc))
        return False


def _portfolio_state(
    registry: Registry,
) -> tuple[list[str], dict[str, tuple[str, Mapping[str, Any]]], dict[str, CampaignOutcomeRecord]]:
    order: list[str] = []
    entries: dict[str, tuple[str, Mapping[str, Any]]] = {}
    outcomes: dict[str, CampaignOutcomeRecord] = {}
    for event in registry.list_events():
        if event.event_type not in {
            "campaign_spec_registered",
            "campaign_registration_refused",
            "campaign_outcome_recorded",
        }:
            continue
        campaign_id = str(event.payload["campaign_id"])
        if campaign_id not in order:
            order.append(campaign_id)
        if event.event_type == "campaign_outcome_recorded":
            outcomes[campaign_id] = CampaignOutcomeRecord.from_mapping(event.payload)
        else:
            entries[campaign_id] = (event.event_type, event.payload)
    return order, entries, outcomes


def _dispatch_from_registry(registry: Registry, campaign: CampaignSpec) -> CampaignDispatch:
    rows = [row for row in registry.list_runs(experiment_id=campaign.campaign_id)]
    adjudicated = [row for row in rows if row["verdict"] is not None]
    last = adjudicated[-1]["run_id"] if adjudicated else None
    unanswered = tuple(row["run_id"] for row in rows
                       if row["status"] == "running" and row["verdict"] is None)
    return CampaignDispatch(campaign, last, unanswered)


def _validate_outcome(campaign_id: str, outcome: CampaignOutcomeRecord) -> None:
    if outcome.campaign_id != campaign_id:
        raise RegistryError(
            f"campaign driver returned outcome for {outcome.campaign_id!r}, expected {campaign_id!r}"
        )
    if outcome.outcome not in TERMINAL_OUTCOMES:
        raise RegistryError(f"campaign driver returned non-terminal outcome {outcome.outcome.value!r}")


def run_registered_campaigns(
    registry: Registry,
    driver: CampaignDriver,
    *,
    max_active: int = 1,
) -> CampaignRunReport:
    """Run every unfinished registration and derive restart position from registry rows.

    No scheduler journal, retry policy, dependency graph, or compatibility predicate is
    maintained here. Completed outcome events are skipped, and running rows with no verdict
    are handed back to the campaign driver for re-dispatch.
    """
    if isinstance(max_active, bool) or not isinstance(max_active, int) or max_active < 1:
        raise ValueError("max_active must be a positive integer")
    order, entries, outcomes = _portfolio_state(registry)
    pending: list[tuple[str, CampaignDispatch]] = []
    for campaign_id in order:
        if campaign_id in outcomes:
            continue
        kind, payload = entries[campaign_id]
        if kind == "campaign_registration_refused":
            outcomes[campaign_id] = CampaignOutcomeRecord(
                CampaignOutcomeRecord.VERSION,
                campaign_id,
                CampaignOutcome.REFUTED,
                str(payload["reason"]),
                1,
            )
            continue
        pending.append((campaign_id, _dispatch_from_registry(
            registry, CampaignSpec.from_mapping(payload),
        )))

    with ThreadPoolExecutor(max_workers=max_active) as pool:
        futures = {pool.submit(driver, dispatch): campaign_id
                   for campaign_id, dispatch in pending}
        for future in as_completed(futures):
            campaign_id = futures[future]
            outcome = future.result()
            _validate_outcome(campaign_id, outcome)
            registry.record_campaign_outcome(outcome.to_mapping())
            outcomes[campaign_id] = outcome

    return CampaignRunReport(tuple(outcomes[campaign_id] for campaign_id in order))
