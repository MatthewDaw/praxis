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
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

BASIS_EXTERNAL = "external"
BASIS_DIRECT = "direct"
BASIS_REASONED = "reasoned"
IDEA_BASES: tuple[str, ...] = (BASIS_EXTERNAL, BASIS_DIRECT, BASIS_REASONED)

RESOLUTION_RESOLVED = "resolved"
RESOLUTION_NON_EXISTENT = "non-existent"
RESOLUTION_UNREACHABLE = "unreachable"

MAX_CONSECUTIVE_UNREACHABLE = 3

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


def resolve_citation(reference: str, meta: dict[str, object], resolver: Resolver) -> dict[str, object]:
    """Resolve one idea's ``reference`` and return a meta PATCH (never mutates ``meta``).

    ``meta`` supplies this reference's prior ``unreachable_streak`` (0 if absent, e.g. on
    the reference's first ideation pass). See the module docstring for the three-valued
    resolution contract.
    """
    kind = reference_kind(reference)
    if kind == OTHER:
        return {
            "basis": BASIS_REASONED,
            "resolution": None,
            "reference_kind": kind,
            "unreachable_streak": 0,
        }

    streak = int(meta.get("unreachable_streak", 0) or 0)
    try:
        resolved = resolver(reference)
    except ResolverUnreachable:
        streak += 1
        patch: dict[str, object] = {
            "resolution": RESOLUTION_UNREACHABLE,
            "reference_kind": kind,
            "unreachable_streak": streak,
        }
        if streak >= MAX_CONSECUTIVE_UNREACHABLE:
            patch["basis"] = BASIS_REASONED
            patch["downgrade_note"] = (
                f"reference {reference!r} unreachable on {streak} consecutive ideation "
                "passes; downgraded to reasoned"
            )
            patch["unreachable_streak"] = 0
        return patch

    if resolved is None:
        return {
            "basis": BASIS_REASONED,
            "resolution": RESOLUTION_NON_EXISTENT,
            "reference_kind": kind,
            "downgrade_note": f"reference {reference!r} resolved as non-existent; downgraded to reasoned",
            "unreachable_streak": 0,
        }

    return {
        "basis": BASIS_EXTERNAL,
        "resolution": RESOLUTION_RESOLVED,
        "reference_kind": kind,
        "title": resolved.title,
        "authors": list(resolved.authors),
        "unreachable_streak": 0,
    }
