"""Local-venue derived jobs (R45, absorbing R46/R47/R69).

af-build's *local* runs are never persisted as a box-service job row (R44 --
af-build keeps working locally, venue is a projection property, not a
branch in af-build's own instructions). Instead the dashboard PROJECTS a
local job at read time from the whole-set run marker
(``run_owner``/``run_at``, plus each ticket's ``build_state``) af-build
already stamps on every in-scope ticket -- see
``agent_factory/hooks/_ticket_state.py``'s ``stamp_run``/``refresh_run``.

This module is pure decision logic over already-fetched ticket facts -- no
Praxis I/O, no subprocess -- mirroring ``box_service_reconcile.py``'s shape
so the projection is unit-testable without a live backend.

- **R45** -- the derived job's id is deterministic given the run owner, and
  no job row is ever written for venue=local.
- **R46** -- once the run marker passes its recency window (TTL), the job
  stops reporting ``running`` and instead reports a **terminal** state
  (``completed``/``failed``) reconciled against the remaining in-scope
  tickets' ``build_state`` -- never "running forever".
- **R47/R69** -- the projection is *bounded* to the recency window: only a
  fresh (``running``) local job appears in the live job list; a run whose
  marker has aged out is absent from that list (pruned), even though
  ``derive_local_job`` still reports its terminal state. The view marks the
  activity tail, question detection, message delivery and resume as
  ``UNAVAILABLE_BY_DESIGN`` -- a deliberate local-venue limit, distinct from
  a genuine capability failure -- so the dashboard never presents a local
  job as a degraded remote one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: Mirrors agent_factory/hooks/_ticket_state.py's DEFAULT_RUN_TTL_S -- the
#: whole-set run marker's recency window.
DEFAULT_RUN_TTL_S = 3600


class LocalJobState(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class CapabilityStatus(str, Enum):
    """A capability's availability on a derived local job. Local-venue
    omissions are a deliberate design choice, never a runtime failure (R47)
    -- the dashboard must not conflate the two."""

    UNAVAILABLE_BY_DESIGN = "unavailable_by_design"


#: Capabilities a remote job offers that a local derived job deliberately
#: never does (R47): no activity tail, no question detection, no message
#: delivery, no resume -- liveness is TTL staleness only.
LOCAL_UNAVAILABLE_CAPABILITIES = (
    "activity_tail",
    "question_detection",
    "message_delivery",
    "resume",
)


@dataclass(frozen=True)
class LocalJobView:
    """The read-time projection for a local run. Never persisted -- there
    is no job row for venue=local (R45)."""

    id: str
    venue: str
    state: LocalJobState
    run_owner: str
    capabilities: dict[str, CapabilityStatus] = field(
        default_factory=lambda: dict.fromkeys(
            LOCAL_UNAVAILABLE_CAPABILITIES, CapabilityStatus.UNAVAILABLE_BY_DESIGN
        )
    )


def derive_local_job_id(run_owner: str) -> str:
    """Deterministic id for a local derived job (R45) -- the same run owner
    always derives the same id, so re-projecting on every read is stable."""
    return f"local:{run_owner}"


def _latest_run_at(tickets: list[dict[str, Any]]) -> float:
    values = [float((t.get("meta") or {}).get("run_at") or 0) for t in tickets]
    return max(values) if values else 0.0


def derive_local_job(
    tickets: list[dict[str, Any]],
    *,
    now: float | None = None,
    ttl_s: float = DEFAULT_RUN_TTL_S,
) -> LocalJobView | None:
    """Project a local job from a set of already-fetched ticket facts.

    ``tickets`` is the snapshot's in-scope ticket set (each a Praxis fact
    dict carrying a ``meta`` with ``run_owner``/``run_at``/``build_state``).
    Returns ``None`` when no ticket carries a run marker at all -- no local
    run has ever claimed this scope.

    When the freshest marker is within ``ttl_s`` the job is ``RUNNING``.
    Once it ages past the window the job reports a **terminal** state
    reconciled against the remaining in-scope tickets (R46): ``COMPLETED``
    if every member ticket finished, ``FAILED`` otherwise -- so a killed
    local run is never reported as running forever.
    """
    if now is None:
        now = time.time()

    owners: dict[str, list[dict[str, Any]]] = {}
    for ticket in tickets:
        owner = (ticket.get("meta") or {}).get("run_owner")
        if owner:
            owners.setdefault(owner, []).append(ticket)

    if not owners:
        return None

    run_owner = max(owners, key=lambda o: _latest_run_at(owners[o]))
    members = owners[run_owner]

    if (now - _latest_run_at(members)) <= ttl_s:
        state = LocalJobState.RUNNING
    else:
        all_finished = all((t.get("meta") or {}).get("build_state") == "finished" for t in members)
        state = LocalJobState.COMPLETED if all_finished else LocalJobState.FAILED

    return LocalJobView(id=derive_local_job_id(run_owner), venue="local", state=state, run_owner=run_owner)


def list_live_local_jobs(
    tickets: list[dict[str, Any]],
    *,
    now: float | None = None,
    ttl_s: float = DEFAULT_RUN_TTL_S,
) -> list[LocalJobView]:
    """The job-list projection (R47/R69): only a still-fresh local run
    appears here. A run whose marker aged out of the recency window is
    absent -- pruned from the live list -- even though ``derive_local_job``
    still reports its reconciled terminal state.
    """
    job = derive_local_job(tickets, now=now, ttl_s=ttl_s)
    return [job] if job is not None and job.state is LocalJobState.RUNNING else []
