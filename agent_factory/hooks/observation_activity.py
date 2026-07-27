#!/usr/bin/env python3
"""Observation hook (R14): wired via ``dispatch_launch.build_dispatch_settings``'s
``PreToolUse`` entry so it fires on every matched harness tool call. Advances a
last-activity timestamp file (path from ``AF_ACTIVITY_FILE``, default
``~/.claude/af-activity``) so external job observation has an IN_DOMAIN,
hook-fired heartbeat to read (see ``knowledge/serve/observability_signals.py``'s
IN_DOMAIN/OUT_OF_DOMAIN split -- this signal is advisory, never authoritative on
its own).

Writes ONLY that one timestamp file. Never touches Praxis, never blocks, never
raises: an observation hook that could stall or fail the session's turn would
defeat its own purpose.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Reuse the same pure timestamp-write the build seam is unit-tested against
# (knowledge/serve/dispatch_launch.fire_observation_activity) -- this script is
# just that function's harness-invoked entry point.
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def main() -> int:
    try:
        from knowledge.serve.dispatch_launch import fire_observation_activity
    except Exception:
        return 0  # never block the session's turn over an observation signal
    path = os.environ.get("AF_ACTIVITY_FILE") or os.path.expanduser("~/.claude/af-activity")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fire_observation_activity(path)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
