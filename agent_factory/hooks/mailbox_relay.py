#!/usr/bin/env python3
"""mailbox_relay.py — the per-dispatch injected Stop hook (R28).

Wired ONLY by ``knowledge.serve.box_service_mailbox.dispatch_wiring``, which
``knowledge.serve.box_service_session.launch_job_session`` passes to ``SessionLauncher.launch``
when it starts a job's background session — this script is never referenced by
``agent_factory/hooks/hooks.json``, the shared Stop-hook set every session (local or remote)
loads. af-build's own instructions (``agent_factory/skills/af-build/SKILL.md``) are therefore
unmodified, and a local ``claude`` invocation — which never goes through the box-service launch
path — never receives this hook at all: the mailbox capability is absent by construction, not by
a check inside a hook that runs everywhere.

Reads ``AF_JOB_MAILBOX_PATH`` from the environment; unset (a local run, or this script invoked
bare) means there is no job to have a mailbox for, so it allows the Stop exactly as if no hook
were wired at all (``_gate_common.allow()`` with no advice prints nothing). Otherwise it drains
every still-pending message from that file and, if any exist, allows the Stop WITH those messages
attached as ``additionalContext`` (never a hard block — an operator message informs the next
ticket boundary, it does not refuse to let the run continue) so it is surfaced exactly once; the
file reads empty again once drained.
"""

from __future__ import annotations

import json
import os
import sys

# The helper modules (_gate_common) live next to this file. A bare hook subprocess may be
# launched with an arbitrary cwd, so make sure our own directory is importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _gate_common import allow as _allow  # noqa: E402

#: The env var this hook reads — the single source of truth shared with
#: ``knowledge.serve.box_service_mailbox.MAILBOX_ENV_VAR``.
MAILBOX_ENV_VAR = "AF_JOB_MAILBOX_PATH"


def _drain(path: str) -> list[str]:
    """Read-and-clear every still-pending message at ``path``. A missing file (nothing ever
    posted, or already drained) reads as no messages, never an error."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    try:
        pending = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError:
        pending = []
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("[]")
    return pending


def decide(pending: list[str]) -> str:
    """Pure decision: render ``pending`` into the ``additionalContext`` advice string, or ``""``
    when nothing is pending (kept separate from the file I/O above so it is unit-testable without
    a real mailbox file)."""
    if not pending:
        return ""
    lines = "\n".join(f"- {message}" for message in pending)
    return f"Message(s) posted by the operator on this job's mailbox:\n{lines}"


def main() -> None:
    mailbox_file = os.environ.get(MAILBOX_ENV_VAR)
    if not mailbox_file:
        _allow()
        return
    _allow(decide(_drain(mailbox_file)))


if __name__ == "__main__":
    main()
