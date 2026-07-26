"""Dispatch (R5): queuing a remote build job is a SEPARATE action from building it.

``hooks/build_completeness_gate.py`` arms — and blocks the session's turn — only when
the session holds a live owned ticket claim or a non-stale whole-set run marker (see
``_ticket_state.claim`` / ``_ticket_state.stamp_run``). Those are what a BUILD does. A
dispatching session that also claimed a ticket or stamped a run marker would therefore
block its own turn against the gate it just armed, up to the configured block cap
(citations: requirements R5; ``hooks/build_completeness_gate.py:301``).

``dispatch_job`` is deliberately the ENTIRE dispatch action: it records the job as
queued and returns. It does not — and structurally cannot, since it never imports
``_ticket_state`` — claim a ticket or stamp a run marker.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass
class DispatchedJob:
    """A queued remote build job, as handed back to the dispatching session."""

    id: str
    project: str
    snapshot: str
    state: str = "queued"


def dispatch_job(project: str, snapshot: str) -> DispatchedJob:
    """Queue a remote build job for ``(project, snapshot)`` and return it.

    This is the whole of dispatch: create a queued job record and return it to the
    caller. It never claims a ticket and never stamps a whole-set run marker (R5) — the
    dispatching session's completeness gate must stay inert after this call, so its turn
    can end without the gate blocking.
    """
    if not project or not snapshot:
        raise ValueError("dispatch_job requires a non-empty project and snapshot")
    return DispatchedJob(id=str(uuid.uuid4()), project=project, snapshot=snapshot)
