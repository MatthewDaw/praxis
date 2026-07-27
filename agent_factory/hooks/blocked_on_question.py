"""Per-dispatch injected hook (R23): "blocked on a question" is a first-class
af-build behavior with its own harness-emitted event, never inferred from a
permission prompt.

R19's allowlist permission mode means no permission prompt can ever occur, and
an agent stuck on a genuinely unresolvable decision produces *text* -- which
fires no permission hook at all. Without a purpose-built signal, the
awaiting-human state collapses into elapsed silence, indistinguishable from a
hard-blocked ticket.

This hook never edits af-build's skill text to teach it some new phrase to
emit. It observes the ONE thing a worker already does today for exactly this
situation -- call the existing ``_ticket_state.block(cid, owner, reason)``
escape hatch (see ``agent_factory/hooks/_ticket_state.py`` and the af-build
SKILL.md worker contract) -- via the Bash ``tool_input`` a PostToolUse hook
already receives, and turns that ONE observation into two harness-fired
signals:

1. a ``blocked_on_question`` event, appended to the job's on-disk
   :class:`~agent_factory.event_log.EventLog` (durable across the hook's own
   process boundary) -- an IN-DOMAIN, forgeable, advisory signal per
   ``docs/observation-signal-domains.md``, never itself the sole basis for a
   terminal decision;
2. (when a live :class:`~knowledge.serve.box_service_store.JobStore` is
   supplied, e.g. by the box-service process reconciling the trail) the JOB
   entering ``awaiting-human`` via ``JobStore.enter_awaiting_human`` -- a
   mid-run pause the SAME job id later returns from via
   ``JobStore.resume_from_awaiting_human`` when the question is answered.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

from agent_factory.event_log import EventLog
from knowledge.serve.box_service_store import JobStore

#: Matches the worker invoking the ticket-level ``block()`` escape hatch --
#: the ONLY detection surface. No skill text is read or matched here, only
#: the Bash command the worker actually ran.
_BLOCK_CALL_RE = re.compile(r"(?:_ticket_state|ts)\.block\(")

#: Best-effort extraction of block()'s ``reason`` positional/string argument.
_REASON_RE = re.compile(r"""block\([^,]+,[^,]+,\s*["']([^"']+)["']""")

_FALLBACK_REASON = "blocked on an unresolvable decision"


def detect_block_call(command: str) -> bool:
    """True iff ``command`` (a Bash ``tool_input`` string) invokes the
    ticket-level ``block()`` escape hatch af-build's skill already instructs
    a worker to call for an unresolvable decision."""
    return bool(_BLOCK_CALL_RE.search(command or ""))


def _extract_reason(command: str) -> str:
    m = _REASON_RE.search(command or "")
    return m.group(1) if m else _FALLBACK_REASON


def handle_post_tool_use(
    payload: dict[str, Any], *, job_id: str, log: EventLog, store: JobStore | None = None,
) -> dict | None:
    """Given one PostToolUse hook payload, detect a blocked-on-question
    signal. Returns the appended event record, or ``None`` when this tool
    call was not a ``block()`` call -- a hook that never fires here is
    byte-identical to no hook at all (no event, no job-state write).
    """
    if str(payload.get("tool_name") or "") != "Bash":
        return None
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if not detect_block_call(command):
        return None
    reason = _extract_reason(command)
    if store is not None:
        store.enter_awaiting_human(job_id, reason)
    return log.append("blocked_on_question", job_id=job_id, reason=reason)


def main() -> None:
    """Hook entry point: read one PostToolUse payload from stdin and record
    a blocked-on-question signal if this call reveals one. Always allows --
    this hook only observes, never blocks the tool call it fires on."""
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    job_id = os.environ.get("FACTORY_JOB_ID", "")
    if job_id:
        handle_post_tool_use(payload, job_id=job_id, log=EventLog(job_id))
    print(json.dumps({}))
    sys.exit(0)


if __name__ == "__main__":
    main()
