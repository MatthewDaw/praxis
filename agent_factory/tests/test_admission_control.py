"""Acceptance coverage for R15 — af-build's explicitly configured concurrency admission control.

Local lanes are unbounded unless an operator or host-scoped launcher configures them; caps remain
overridable per project, and no core-count expression appears anywhere in the dispatch path; a
ticket counts against the concurrency lane named by its ``meta.device``; a campaign still live from
an earlier round counts against its lane in the current round's admission and does not free it by
remaining incomplete; a frontier exceeding either cap dispatches exactly that many from that lane and
logs the remainder by ticket id; a deferred ticket may stay deferred across many rounds without being
treated as wedged; af-build's fan-out contract no longer prescribes Workflow dispatch and names
resource admission as the one sanctioned exception to its no-narrowing rule.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from pytest import MonkeyPatch

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402 — sys.path bootstrap above must precede this import

SKILL_MD = (
    Path(__file__).resolve().parents[1] / "skills" / "af-build" / "SKILL.md"
).read_text(encoding="utf-8")


def _ticket(rid: str, device: str = "", build_state: str = "incomplete",
            claim_owner: str | None = None, lease_live: bool = False) -> dict[str, Any]:
    meta: dict[str, Any] = {"requirement_id": rid, "build_state": build_state}
    if device:
        meta["device"] = device
    if claim_owner:
        meta["claim_owner"] = claim_owner
        meta["build_state"] = "in_progress"
        if lease_live:
            meta["claim_heartbeat_at"] = time.time()
            meta["claim_lease_ttl"] = 900
        else:
            # stale — well outside the ttl window
            meta["claim_heartbeat_at"] = 0.0
            meta["claim_lease_ttl"] = 1
    return {"id": rid, "meta": meta}


# --------------------------------------------------------------------------- defaults + overrides

def test_local_lanes_have_no_praxis_imposed_default_cap(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("AF_MAX_CPU_PARALLEL", raising=False)
    monkeypatch.delenv("AF_MAX_GPU_PARALLEL", raising=False)
    assert ts.lane_cap("cpu") is None
    assert ts.lane_cap("gpu") is None

    ready = [_ticket(f"R{i}", device="cpu") for i in range(20)]
    result = ts.admit_frontier(ready, live=[], project="local-project")
    assert result["admit"] == ready
    assert result["defer"] == []
    assert result["lanes"]["cpu"]["cap"] is None


def test_caps_overridable_per_project(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("AF_MAX_CPU_PARALLEL", raising=False)
    monkeypatch.setenv("AF_MAX_CPU_PARALLEL__AF_ML_RESEARCH", "3")
    assert ts.lane_cap("cpu", project="af-ml-research") == 3
    # a DIFFERENT project is untouched by that override
    assert ts.lane_cap("cpu", project="other-project") is None


def test_global_override_applies_when_no_project_override(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("AF_MAX_GPU_PARALLEL", "4")
    monkeypatch.delenv("AF_MAX_GPU_PARALLEL__AF_ML_RESEARCH", raising=False)
    assert ts.lane_cap("gpu", project="af-ml-research") == 4


# --------------------------------------------------------------------------- lane assignment

def test_ticket_counts_against_lane_named_by_meta_device() -> None:
    assert ts.ticket_device(_ticket("R1", device="gpu")) == "gpu"
    assert ts.ticket_device(_ticket("R2", device="cpu")) == "cpu"
    # absent/unknown device defaults to the cpu lane (mirrors the plan-gate closed-set default, R16)
    assert ts.ticket_device(_ticket("R3")) == "cpu"
    assert ts.ticket_device(_ticket("R4", device="GPU")) == "gpu"


# --------------------------------------------------------------------------- live campaign accounting

def test_live_claim_from_earlier_round_counts_against_its_lane(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("AF_MAX_GPU_PARALLEL", "1")
    monkeypatch.delenv("AF_MAX_GPU_PARALLEL__AF_ML_RESEARCH", raising=False)
    live = [_ticket("R-live", device="gpu", claim_owner="worker-1", lease_live=True)]
    ready = [_ticket("R-new", device="gpu")]
    result = ts.admit_frontier(ready, live=live, project="af-ml-research")
    # The explicitly configured gpu cap is already occupied by the live campaign ticket.
    assert result["admit"] == []
    assert result["defer"] and result["defer"][0]["id"] == "R-new"
    assert result["lanes"]["gpu"]["used"] == 1
    assert result["lanes"]["gpu"]["cap"] == 1


def test_incomplete_alone_does_not_free_the_lane() -> None:
    """A ticket merely staying `incomplete` (never claimed) never occupied a lane in the first
    place, so it must not be confused with a live claim going stale — only ``live_claims`` (a
    live lease) counts, never bare incompleteness."""
    incomplete_untouched = _ticket("R-idle", device="cpu", build_state="incomplete")
    assert ts.live_claims([incomplete_untouched]) == []


def test_stale_lease_does_not_count_as_live() -> None:
    stale = _ticket("R-stale", device="gpu", claim_owner="worker-dead", lease_live=False)
    assert ts.live_claims([stale]) == []


# --------------------------------------------------------------------------- cap enforcement + logging

def test_frontier_exceeding_cap_admits_exactly_that_many_and_defers_the_rest(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("AF_MAX_CPU_PARALLEL", "2")
    monkeypatch.delenv("AF_MAX_CPU_PARALLEL__AF_ML_RESEARCH", raising=False)
    ready = [_ticket(f"R{i}", device="cpu") for i in range(5)]
    result = ts.admit_frontier(ready, live=[], project="af-ml-research")
    assert [t["id"] for t in result["admit"]] == ["R0", "R1"]
    assert result["deferred_ids"] == ["R2", "R3", "R4"]
    assert result["lanes"]["cpu"]["cap"] == 2
    assert result["lanes"]["cpu"]["used"] == 2


def test_lanes_are_independent(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("AF_MAX_CPU_PARALLEL", "1")
    monkeypatch.setenv("AF_MAX_GPU_PARALLEL", "1")
    ready = [_ticket("Rc", device="cpu"), _ticket("Rg", device="gpu")]
    result = ts.admit_frontier(ready, live=[], project="af-ml-research")
    assert {t["id"] for t in result["admit"]} == {"Rc", "Rg"}
    assert result["defer"] == []


# --------------------------------------------------------------------------- deferral never reads as a stall

def test_deferred_ticket_reported_across_many_rounds_is_never_marked_blocked(monkeypatch: MonkeyPatch) -> None:
    """Admission-control deferral is a per-round dispatch decision, not a ticket-state transition —
    `admit_frontier` must never write build_state, so a ticket parked here stays plain
    ``incomplete`` (still claimable, still `ready_tickets`-eligible) no matter how many rounds it
    gets deferred, and the dependency-stall detector never sees it as blocked/waiting."""
    monkeypatch.setenv("AF_MAX_CPU_PARALLEL", "1")
    ticket = _ticket("R-deferred", device="cpu")
    other = _ticket("R-other", device="cpu")
    for _round in range(5):
        result = ts.admit_frontier([other, ticket], live=[], project="af-ml-research")
        assert ticket in result["defer"]
        # never mutated: still incomplete, still dependency-ready, never "blocked"
        assert ticket["meta"]["build_state"] == "incomplete"
        assert ticket in ts.ready_tickets([ticket, other])


# --------------------------------------------------------------------------- no core-count-derived cap

def test_no_core_count_call_in_ticket_state_module() -> None:
    src = Path(ts.__file__).read_text(encoding="utf-8")
    # Built by concatenation (never written as a literal) so this assertion's own text can never
    # trip check_no_core_derived_cap.py's repo-wide scan of it.
    forbidden = ("os." + "cpu_count", "multiprocessing." + "cpu_count", "sched_" + "getaffinity")
    for expr in forbidden:
        assert expr not in src, f"{expr} must never derive the admission cap (R15)"


# --------------------------------------------------------------------------- fan-out contract prose

def test_skill_no_longer_prescribes_workflow_as_the_mandated_dispatch_mechanism() -> None:
    assert "NOT optional, NOT your discretion" not in SKILL_MD
    assert "launching the fan-out Workflow is the default, mandatory behavior" not in SKILL_MD


def test_skill_names_admission_as_the_sanctioned_no_narrowing_exception() -> None:
    assert "no-narrowing rule" in SKILL_MD
    assert "admission" in SKILL_MD.lower()
    assert "max_cpu_parallel" in SKILL_MD
    assert "max_gpu_parallel" in SKILL_MD
