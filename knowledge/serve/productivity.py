"""Server-side kill switch for the Productivity feature.

The Productivity tab/route call out to a backend-held GitHub token (see
``docs/brainstorms/2026-07-24-productivity-page-requirements.md``, D23 et al.).
A leaked or revoked token must be containable **without a redeploy**: setting
``PRODUCTIVITY_KILL_SWITCH=1`` flips ``GET /productivity`` to a disabled status
and the dashboard tab to hidden/disabled, with no outbound GitHub call ever
attempted on that path.

This module owns ONLY the switch + the disabled-status short-circuit. The
enabled path (actual GitHub-backed metrics) is out of scope here and is a
placeholder for the sibling ticket(s) that build the real data fetch.
"""

from __future__ import annotations

import os
from typing import Any

KILL_SWITCH_ENV = "PRODUCTIVITY_KILL_SWITCH"

_TRUTHY = {"1", "true", "yes", "on"}


def productivity_enabled() -> bool:
    """Read the kill switch fresh from the environment (no redeploy needed)."""
    return os.environ.get(KILL_SWITCH_ENV, "").strip().lower() not in _TRUTHY


def productivity_status() -> dict[str, Any]:
    """Body for ``GET /productivity``.

    When the kill switch is set, returns a disabled status immediately —
    no GitHub client is imported or called on this path.
    """
    if not productivity_enabled():
        return {"status": "disabled"}
    return {"status": "enabled"}
