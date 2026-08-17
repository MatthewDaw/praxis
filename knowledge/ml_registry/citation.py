"""Idea citation resolution for the af-ml-research registry (R7).

Every registry idea carries a ``basis`` of ``external``, ``direct`` or ``reasoned``.
When an idea names a ``reference``, that reference is resolved only through a CLOSED
allowlist of arXiv ids and DOIs (:func:`reference_kind`); any other URL form lands
``reasoned`` immediately and never triggers an outbound fetch (:data:`OTHER`, "no
fetch" branch of :func:`resolve_citation`).

Resolution against an allow-listed reference is three-valued, decided by the injected
``resolver`` callable (network-free by construction -- the real network resolver, if
any, lives at the CLI boundary in :mod:`knowledge.ml_registry.cli`):

* it resolves               -> ``basis=external``, ``resolution=resolved``, the
  resolved title and authors are recorded.
* it resolves as non-existent (resolver returns ``None``) -> ``basis`` downgrades to
  ``reasoned`` with a ``downgrade_note``, ``resolution=non-existent``.
* it cannot be reached (resolver raises :class:`ResolverUnreachable`) ->
  ``resolution=unreachable``; ``basis`` is left UNTOUCHED (neither downgraded nor
  treated as verified); the consecutive-unreachable streak increments, and only the
  3rd CONSECUTIVE failure downgrades ``basis`` to ``reasoned`` (with its own
  ``downgrade_note``), after which the streak resets to 0. Reaching the reference
  (resolved or non-existent) also resets the streak to 0.

The streak is counted PER REFERENCE, not per idea: it lives in
``meta["unreachable_streaks"]`` as ``{reference: consecutive_failures}``. An idea whose
author CORRECTS its citation starts that new reference's count at 0, so the downgrade
always reflects three consecutive failures against the SAME reference and the
``downgrade_note``'s attempt count is always true of the reference it names.
``meta["unreachable_streak"]`` is still written as the scalar streak of the reference
just attempted (what callers/tests read for the current attempt), but it is never the
source of truth for a DIFFERENT reference's history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

BASIS_EXTERNAL = "external"
BASIS_DIRECT = "direct"
BASIS_REASONED = "reasoned"

RESOLUTION_RESOLVED = "resolved"
RESOLUTION_NON_EXISTENT = "non-existent"
RESOLUTION_UNREACHABLE = "unreachable"

MAX_CONSECUTIVE_UNREACHABLE = 3

# meta key holding {reference: consecutive_unreachable_attempts} -- the streak is a property
# of the REFERENCE, not of the idea that happens to cite it right now.
UNREACHABLE_STREAKS = "unreachable_streaks"

ARXIV = "arxiv"
DOI = "doi"
OTHER = "other"

_ARXIV_RE = re.compile(
    r"^(?:arxiv:|https?://arxiv\.org/abs/)?(\d{4}\.\d{4,5})(v\d+)?$", re.IGNORECASE
)
_DOI_RE = re.compile(r"^(?:doi:|https?://doi\.org/)?(10\.\d{4,9}/\S+)$", re.IGNORECASE)


class ResolverUnreachable(Exception):
    """Raised by a ``resolver`` callable when a reference could not be reached this attempt."""


@dataclass(frozen=True)
class ResolvedCitation:
    """What a ``resolver`` returns when a reference genuinely resolves."""

    title: str
    authors: tuple[str, ...]


Resolver = Callable[[str], Optional[ResolvedCitation]]


def reference_kind(reference: str) -> str:
    """Classify ``reference`` against the closed allowlist: ``"arxiv"``, ``"doi"``, or ``"other"``."""
    ref = reference.strip()
    if _ARXIV_RE.match(ref):
        return ARXIV
    if _DOI_RE.match(ref):
        return DOI
    return OTHER


def _streaks(meta: dict[str, object]) -> dict[str, int]:
    """The per-reference streak map carried on ``meta`` (a fresh copy; ``{}`` if absent)."""
    raw = meta.get(UNREACHABLE_STREAKS)
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, int) and not isinstance(v, bool)}


def _prior_streak(meta: dict[str, object], reference: str) -> int:
    """THIS reference's consecutive-unreachable count so far -- 0 for a reference never
    attempted before, even on an idea whose PREVIOUS reference had a running streak."""
    streaks = _streaks(meta)
    if reference in streaks:
        return streaks[reference]
    # Legacy per-idea scalar: only ever trusted when the idea's recorded reference is the
    # very one being attempted, so a corrected citation never inherits the old streak.
    legacy = meta.get("unreachable_streak", 0)
    if isinstance(legacy, int) and not isinstance(legacy, bool):
        if str(meta.get("reference") or "").strip() == reference:
            return legacy
    return 0


def _streak_patch(meta: dict[str, object], reference: str, streak: int) -> dict[str, object]:
    """The streak half of a patch: this reference's new count, both in the per-reference map
    and in the scalar every caller reads for the attempt just made."""
    streaks = _streaks(meta)
    if reference:
        streaks[reference] = streak
    return {"unreachable_streak": streak, UNREACHABLE_STREAKS: streaks}


def resolve_citation(reference: str, meta: dict[str, object], resolver: Resolver) -> dict[str, object]:
    """Resolve one idea's ``reference`` and return a meta PATCH (never mutates ``meta``).

    ``meta`` supplies THIS reference's prior streak from ``meta["unreachable_streaks"]``
    (0 if this reference has never been attempted, e.g. on its first ideation pass or
    immediately after the author corrected the citation). See the module docstring for the
    three-valued resolution contract.

    A blank ``reference`` (the idea author made a direct claim, citing nothing) lands
    ``basis=direct`` -- distinct from ``reasoned`` (a reference was given but could not be
    used) and never calls ``resolver``.
    """
    ref = reference.strip()
    if not ref:
        return {
            "basis": BASIS_DIRECT,
            "resolution": None,
            "reference_kind": None,
            **_streak_patch(meta, ref, 0),
        }

    kind = reference_kind(reference)
    if kind == OTHER:
        return {
            "basis": BASIS_REASONED,
            "resolution": None,
            "reference_kind": kind,
            **_streak_patch(meta, ref, 0),
        }

    streak = _prior_streak(meta, ref)
    try:
        resolved = resolver(reference)
    except ResolverUnreachable:
        streak += 1
        patch: dict[str, object] = {
            "resolution": RESOLUTION_UNREACHABLE,
            "reference_kind": kind,
            **_streak_patch(meta, ref, streak),
        }
        if streak >= MAX_CONSECUTIVE_UNREACHABLE:
            patch["basis"] = BASIS_REASONED
            patch["downgrade_note"] = (
                f"reference {reference!r} unreachable on {streak} consecutive ideation "
                "passes; downgraded to reasoned"
            )
            patch.update(_streak_patch(meta, ref, 0))
        return patch

    if resolved is None:
        return {
            "basis": BASIS_REASONED,
            "resolution": RESOLUTION_NON_EXISTENT,
            "reference_kind": kind,
            "downgrade_note": f"reference {reference!r} resolved as non-existent; downgraded to reasoned",
            **_streak_patch(meta, ref, 0),
        }

    return {
        "basis": BASIS_EXTERNAL,
        "resolution": RESOLUTION_RESOLVED,
        "reference_kind": kind,
        "title": resolved.title,
        "authors": list(resolved.authors),
        **_streak_patch(meta, ref, 0),
    }
