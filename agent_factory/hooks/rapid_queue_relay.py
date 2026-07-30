#!/usr/bin/env python3
"""rapid_queue_relay.py — the Stop hook that makes a captured request impossible to forget.

WHY A HOOK AT ALL. ``af-rapid-queue`` captures a drive-by request to the local spool and then
promotes it to a Praxis ticket in the same turn. Once the ticket exists, the existing machinery owns
it end-to-end: af-build finds it in ``incomplete_requirements`` and ``build_completeness_gate``
blocks the session from stopping while it is unfinished. The ONLY hole is the window before that
promotion lands — Praxis was unreachable, the write errored, or the session died between the capture
and the write. This hook closes exactly that hole: at every Stop boundary it reports any entry still
owed a ticket, so the next thing that would otherwise be "session over, request forgotten" becomes
"file these, then stop".

ADVICE, NEVER A BLOCK. This hook always ALLOWS. The two factory gates fail closed because they guard
*completion* — letting a run declare itself done with unbuilt tickets is a correctness failure. This
one guards *intake*, and blocking here would derail the very session af-rapid-queue exists to keep
undisturbed (and could wedge a session whose Praxis is simply down). A pending entry is not lost by
being unblocked — it is in the spool, and it comes back at the NEXT boundary too, because only a real
ticket id retires it (``rapid_queue.mark_filed``). The nag repeats until the work is real.

INERT WHEN THERE IS NOTHING TO SAY. With an empty spool this prints nothing and exits 0 —
byte-identical to no hook being wired at all, so ordinary sessions in any repo are unaffected. Unlike
``mailbox_relay.py`` (whose capability is absent locally by construction, keyed off a per-dispatch
env var), this one IS wired into the shared always-on ``hooks.json`` set, because a drive-by request
is typed in a LOCAL debugging session — that is the whole point — and it stays silent by having an
empty spool rather than by not existing.

FAILS OPEN, LOUDLY ENOUGH. Any unexpected error reading the spool degrades to a plain allow: a
diagnostic problem in an advisory hook must not be able to wedge a session. The entries survive on
disk, so the next boundary tries again.
"""

from __future__ import annotations

import json
import os
import sys

# The helper modules (_gate_common, rapid_queue) live next to this file. A bare hook subprocess may
# be launched with an arbitrary cwd, so make sure our own directory is importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _gate_common import allow as _allow  # noqa: E402


def decide(pending: list[dict], *, project: str = "") -> str:
    """Pure decision: render ``pending`` into the ``additionalContext`` advice, or ``""`` when
    nothing is owed a ticket (kept separate from I/O so it is unit-testable without a real spool).

    The advice names each entry's ``qid`` and text, and states the two-step close-out, because the
    session that reads it may be a fresh one with no memory of the capture.
    """
    if not pending:
        return ""
    lines = "\n".join(f"- [{entry.get('qid')}] {entry.get('text', '')}" for entry in pending)
    scope = f" for project {project!r}" if project else ""
    return (
        f"af-rapid-queue: {len(pending)} captured request(s){scope} are NOT yet Praxis tickets, so "
        "nothing is tracking them:\n"
        f"{lines}\n"
        "File each one now, per the af-rapid-queue skill: write it into "
        "(space=<project>, snapshot=prd-<project>) as a requirement with "
        'meta.build_state="incomplete", then retire the spool entry with '
        "`python3 <plugin>/hooks/rapid_queue.py filed <qid> <ticket-id>`. Do NOT start building "
        "them here — filing is the whole job; af-build drains them. If Praxis is unreachable, say "
        "so and leave them queued; they will be offered again at the next boundary."
    )


def main() -> None:
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001 — no readable hook payload => nothing to scope to
        _allow()
        return
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        data = {}
    cwd = data.get("cwd") or os.getcwd()

    try:
        import rapid_queue
        project = rapid_queue.bare_project(cwd=cwd)
        _allow(decide(rapid_queue.pending(cwd=cwd), project=project))
    except Exception as exc:  # noqa: BLE001 — an advisory hook must never wedge a session
        sys.stderr.write(f"[af-rapid-queue] spool unreadable, allowing stop: {exc}\n")
        _allow()


if __name__ == "__main__":
    main()
