"""Agent factory — local helpers for the Praxis-backed Claude Code plugin.

The factory's intelligence lives in skills (markdown policy) and in Praxis (the
knowledge graph). This package holds only the small, deterministic pieces that are
better as code than as prose:

- ``event_log``  — the append-only structured run log (the compounding spine).
- ``tabular``    — the deterministic table linearizer (the H6 ingestion shim).
"""

from agent_factory.event_log import EventLog
from agent_factory.tabular import LinearizeResult, linearize

__all__ = ["EventLog", "LinearizeResult", "linearize"]
