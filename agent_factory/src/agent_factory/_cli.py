"""The shared error boundary for the operator-facing console scripts (``af-retro``, ``af-ingest``,
``af-taxonomy``).

These CLIs talk to Praxis, so every one of them can fail in two ways that look identical at the
transport layer and demand opposite responses from the operator:

* **configuration** — the space/org does not exist, or this key is not scoped to it. Nothing is
  broken; the command was pointed somewhere it does not belong. The near-universal cause is running
  a CLI from the wrong directory: each factory project pins its own ``PRAXIS_ORG`` in
  ``.claude/settings.local.json``, so ``af-retro sports_analysis`` run from ``praxis/`` asks the
  ``praxis`` org about a space that only exists under ``sports-analysis``.
* **availability** — Praxis is down, unreachable, or the credentials expired. The truth exists and
  cannot be read.

Before this module both arrived as an unhandled ``PraxisUnreachable`` and printed a 25-line
traceback ending in ``HTTP Error 404: Not Found`` — which reads as "the tool is broken" for what is
usually a one-word fix (``cd`` to the project). The predicate that separates them is
``_gate_common.not_a_factory_project``, already shipped and already tested for the Stop-hook gates;
this reuses it rather than growing a second copy that can drift.

Exit codes follow the convention ``failure_taxonomy.main`` established: ``0`` success, ``2`` the
command could not run. ``2`` deliberately covers BOTH failures above — a caller scripting these
only needs "did it produce a report", and splitting the code would silently change the meaning of
the ``2`` that is already documented and shipped. The *message* carries the distinction, because
that is what the human reads.

A non-Praxis exception is NEVER swallowed here: a genuine bug in this package must still raise with
its traceback intact, or this boundary would convert every programming error into a tidy
"could not run" and hide it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

from agent_factory._hooks import _praxis, not_a_factory_project

#: The command could not run. Matches ``failure_taxonomy.main``'s documented contract.
EXIT_CANNOT_RUN = 2


def active_org() -> str:
    """The org these commands are talking to, for the error message.

    Read the same way ``_praxis`` reads it, but never through a call that can itself fail: this
    runs on the error path, where a second exception would replace the diagnosis with a traceback
    from the diagnostic code.
    """
    return os.environ.get("PRAXIS_ORG", "").strip() or _praxis.DEFAULT_ORG


def praxis_boundary(prog: str, fn: Callable[[], int]) -> int:
    """Run ``fn``, converting a Praxis failure into a one-line diagnosis on stderr.

    Returns ``fn``'s status, or :data:`EXIT_CANNOT_RUN` when Praxis refused. Anything that is not a
    Praxis transport failure propagates untouched.
    """
    try:
        return fn()
    except _praxis.PraxisUnreachable as exc:
        if not_a_factory_project(exc):
            print(
                f"{prog}: nothing to read in org {active_org()!r} — {exc}\n"
                f"{prog}: this is a scoping answer, not an outage. Each factory project pins its "
                f"own PRAXIS_ORG in .claude/settings.local.json, so run this from that project's "
                f"directory (or set PRAXIS_ORG explicitly).",
                file=sys.stderr,
            )
        else:
            print(f"{prog}: Praxis is unreachable — {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN
