"""Acceptance coverage for R21 — af-build's ML research ticket routing.

Ticket 411e5d7e19ec4e2c9e5648dfd9b4dea9: a ticket carrying `meta.experiment_id` is dispatched to
the supervisor loop and never to a generic build worker; a ticket carrying none is never routed to
the supervisor; routing a ticket whose model already has a live campaign resumes that campaign
rather than starting a second supervisor, and a second concurrent claim on a live campaign is
refused; the research-target check resolves onto every experiment_id-carrying ticket by query and
appears in that ticket's pinned check set; a ticket whose experiment_id names no registered model
is refused at claim naming the missing model rather than silently building.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402 — sys.path bootstrap above must precede this import

SKILL_MD = (
    Path(__file__).resolve().parents[1] / "skills" / "af-build" / "SKILL.md"
).read_text(encoding="utf-8")


def _ticket(rid: str, experiment_id: str | None = None, claim_owner: str | None = None,
            lease_live: bool = False) -> dict[str, Any]:
    meta: dict[str, Any] = {"requirement_id": rid, "build_state": "incomplete"}
    if experiment_id is not None:
        meta["experiment_id"] = experiment_id
    if claim_owner:
        meta["claim_owner"] = claim_owner
        meta["build_state"] = "in_progress"
        if lease_live:
            meta["claim_heartbeat_at"] = time.time()
            meta["claim_lease_ttl"] = 900
        else:
            meta["claim_heartbeat_at"] = 0.0
            meta["claim_lease_ttl"] = 1
    return {"id": rid, "meta": meta}


def _model(model_id: str, experiment_id: str, campaign_status: str | None = "active") -> dict[str, Any]:
    meta: dict[str, Any] = {"experiment_id": experiment_id}
    if campaign_status is not None:
        meta["campaign_status"] = campaign_status
    return {"id": model_id, "meta": meta}


# --------------------------------------------------------------------------- experiment_id identity

def test_ticket_experiment_id_reads_meta_field() -> None:
    assert ts.ticket_experiment_id(_ticket("R1", experiment_id="exp-1")) == "exp-1"
    assert ts.ticket_experiment_id(_ticket("R2")) is None
    # blank/whitespace-only treated as absent, same tolerance ticket_device gives an unset field
    assert ts.ticket_experiment_id({"id": "R3", "meta": {"experiment_id": "   "}}) is None


# --------------------------------------------------------------------------- routing: generic vs supervisor vs refused

def test_ticket_without_experiment_id_routes_generic_never_supervisor() -> None:
    route = ts.resolve_research_route(_ticket("R2"), models=[_model("model-1", "exp-1")])
    assert route["route"] == ts.ROUTE_GENERIC


def test_ticket_with_registered_experiment_id_routes_to_supervisor() -> None:
    models = [_model("model-1", "exp-1")]
    route = ts.resolve_research_route(_ticket("R1", experiment_id="exp-1"), models=models)
    assert route["route"] == ts.ROUTE_SUPERVISOR
    assert route["model_id"] == "model-1"


def test_ticket_naming_unregistered_experiment_id_is_refused() -> None:
    route = ts.resolve_research_route(_ticket("R1", experiment_id="exp-missing"), models=[])
    assert route["route"] == ts.ROUTE_REFUSED
    assert "exp-missing" in route["reason"]
    assert "R1" in route["reason"]


# --------------------------------------------------------------------------- live campaign attach vs second supervisor

def test_campaign_is_live_for_active_and_stalled_statuses() -> None:
    assert ts.campaign_is_live(_model("m1", "exp-1", campaign_status="active")) is True
    assert ts.campaign_is_live(_model("m1", "exp-1", campaign_status="stalled_pending_baseline")) is True
    assert ts.campaign_is_live(_model("m1", "exp-1", campaign_status=None)) is True  # freshly registered


def test_campaign_is_not_live_once_won_or_completed() -> None:
    assert ts.campaign_is_live(_model("m1", "exp-1", campaign_status="won")) is False
    assert ts.campaign_is_live(_model("m1", "exp-1", campaign_status="completed")) is False


def test_route_reports_live_campaign_for_dispatcher_to_attach_rather_than_start_fresh() -> None:
    models = [_model("model-1", "exp-1", campaign_status="active")]
    route = ts.resolve_research_route(_ticket("R1", experiment_id="exp-1"), models=models)
    assert route["route"] == ts.ROUTE_SUPERVISOR
    assert route["live_campaign"] is True
    assert route["model_id"] == "model-1"  # the SAME model — never a second registration


def test_second_concurrent_claim_on_a_live_campaign_is_refused() -> None:
    models = [_model("model-1", "exp-1")]
    live_other = _ticket("R-other", experiment_id="exp-1", claim_owner="worker-1", lease_live=True)
    this_ticket = _ticket("R-new", experiment_id="exp-1")
    reason = ts.research_claim_guard(this_ticket, models, other_claims=[live_other])
    assert reason is not None
    assert "exp-1" in reason
    assert "R-other" in reason


def test_claim_guard_allows_a_fresh_or_unclaimed_campaign() -> None:
    models = [_model("model-1", "exp-1")]
    reason = ts.research_claim_guard(_ticket("R-new", experiment_id="exp-1"), models, other_claims=[])
    assert reason is None


def test_claim_guard_ignores_a_stale_lease_on_the_same_experiment_id() -> None:
    models = [_model("model-1", "exp-1")]
    stale_other = _ticket("R-other", experiment_id="exp-1", claim_owner="worker-dead", lease_live=False)
    reason = ts.research_claim_guard(_ticket("R-new", experiment_id="exp-1"), models, other_claims=[stale_other])
    assert reason is None


def test_claim_guard_ignores_a_live_claim_on_a_different_experiment_id() -> None:
    models = [_model("model-1", "exp-1"), _model("model-2", "exp-2")]
    live_other = _ticket("R-other", experiment_id="exp-2", claim_owner="worker-1", lease_live=True)
    reason = ts.research_claim_guard(_ticket("R-new", experiment_id="exp-1"), models, other_claims=[live_other])
    assert reason is None


def test_claim_guard_ignores_the_tickets_own_live_lease() -> None:
    """A worker's own periodic re-check of a ticket it already holds must never refuse itself."""
    models = [_model("model-1", "exp-1")]
    own = _ticket("R-self", experiment_id="exp-1", claim_owner="worker-1", lease_live=True)
    reason = ts.research_claim_guard(own, models, other_claims=[own])
    assert reason is None


def test_claim_guard_refuses_missing_model_even_with_no_other_claims() -> None:
    reason = ts.research_claim_guard(_ticket("R1", experiment_id="exp-missing"), models=[], other_claims=[])
    assert reason is not None
    assert "exp-missing" in reason


# --------------------------------------------------------------------------- research-target check by query

def test_research_target_requirement_id_and_script() -> None:
    req = ts.research_target_requirement("R1", "exp-1")
    assert req["id"] == "R1::research-target"
    assert req["meta"]["experiment_id"] == "exp-1"
    assert ts.RESEARCH_TARGET_CHECK_SCRIPT in req["text"]


def test_contract_with_floor_includes_research_target_requirement_for_experiment_id_ticket() -> None:
    reqs = ts.contract_with_floor(
        "R1", "acceptance text", resolved=[],
        ticket_meta={"experiment_id": "exp-1", "verify": "automated"},
    )
    ids = {r["id"] for r in reqs}
    assert "R1::research-target" in ids
    assert "R1::acceptance" in ids


def test_contract_with_floor_omits_research_target_requirement_for_ordinary_ticket() -> None:
    reqs = ts.contract_with_floor(
        "R2", "acceptance text", resolved=[],
        ticket_meta={"verify": "automated"},
    )
    ids = {r["id"] for r in reqs}
    assert "R2::research-target" not in ids


def test_contract_with_floor_research_target_requirement_deduped() -> None:
    existing = ts.research_target_requirement("R1", "exp-1")
    reqs = ts.contract_with_floor(
        "R1", "acceptance text", resolved=[existing],
        ticket_meta={"experiment_id": "exp-1", "verify": "automated"},
    )
    ids = [r["id"] for r in reqs]
    assert ids.count("R1::research-target") == 1


# --------------------------------------------------------------------------- SKILL.md prose (mirrors test_admission_control.py)

def test_skill_documents_ml_research_routing() -> None:
    assert "R21" in SKILL_MD
    assert "resolve_research_route" in SKILL_MD
    assert "research_claim_guard" in SKILL_MD
    assert "supervise_campaign" in SKILL_MD


def test_skill_names_the_refusal_and_attach_semantics() -> None:
    assert "refusing a second concurrent claim" in SKILL_MD or "refuses this claim" in SKILL_MD
    assert "ATTACHING" in SKILL_MD or "attaching" in SKILL_MD.lower()


# --------------------------------------------------------------------------- lockstep with knowledge/ml_registry
#
# _ticket_state.py duplicates the campaign-close vocabulary as bare string literals (rather than
# importing knowledge/ml_registry, keeping this stdlib-only hook module's import surface intact for
# a bare-subprocess call). This test is the enforcement half of that duplication's "must stay in
# lockstep" comment: a drift in either module's literal values fails HERE, immediately, rather than
# silently mis-routing a closed campaign as live.

def test_campaign_status_vocabulary_matches_knowledge_ml_registry() -> None:
    from knowledge.ml_registry.floor import CAMPAIGN_STATUS_FIELD
    from knowledge.ml_registry.supervisor import CAMPAIGN_COMPLETED, CLOSE_WON

    assert ts._CAMPAIGN_STATUS_FIELD == CAMPAIGN_STATUS_FIELD
    assert ts._CAMPAIGN_CLOSED_STATUSES == frozenset({CLOSE_WON, CAMPAIGN_COMPLETED})
