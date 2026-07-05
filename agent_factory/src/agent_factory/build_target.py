"""Deterministic build-target selector — bounds what the forced build gate must finish.

The autonomous build runs under a forced completeness gate (CONSTITUTION /
af-build): it keeps working until every *targeted* requirement reaches a
"succeeded" outcome. But the Praxis completeness query ``incomplete_requirements(project)``
returns ALL active requirements, which is the wrong target for an automated gate:

- **post-MVP** requirements would make the gate chase scope forever (it can never
  "finish" a project whose target includes everything ever planned), and
- **manual-verify** requirements can never earn an automated "succeeded" outcome, so a
  gate that waits on them is structurally trapped.

The plan ALREADY tags each requirement with the two facts that resolve this — the tier
(``meta.scope`` ∈ {"mvp", "post-mvp"}) and the verification mode (``meta.verify`` ∈
{"automated", "manual"}). This module is the pure consumer of those tags: it partitions
a requirement list into disjoint groups so the gate targets ONLY ``mvp`` + ``automated``.

Selection rules (each requirement lands in exactly one group):

- **build** — tier == "mvp" AND verify == "automated". The gate's completion set: the
  only requirements the autonomous build is forced to finish.
- **deferred_manual** — tier == "mvp" AND verify == "manual". In-scope for the MVP but
  parked: surfaced to humans, never blocks the autonomous gate (no automated success
  signal is possible).
- **excluded_post_mvp** — tier == "post-mvp" (any verify). Out of the build entirely.
- **needs_triage** — the fail-SAFE bucket. Any requirement whose ``tier`` or ``verify``
  is missing or unrecognized lands here, NEVER in ``build``. A mis-tagged requirement
  must never be silently auto-built; routing it to triage forces a human to fix the tag.

This module is pure: no I/O, no Praxis calls. Callers extract the raw requirement facts
(``category == "requirement"``) and hand them here; :func:`requirement_from_fact` is a
tolerant adapter from the raw fact-dict shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Recognized tag values. Anything outside these sets is treated as unknown and routed to
# ``needs_triage`` (fail-safe) rather than guessed into the build set.
_TIER_MVP = "mvp"
_TIER_POST_MVP = "post-mvp"
_VERIFY_AUTOMATED = "automated"
_VERIFY_MANUAL = "manual"


@dataclass(frozen=True)
class Requirement:
    """One requirement reduced to the fields the selector needs.

    ``tier`` comes from ``meta.scope`` and ``verify`` from ``meta.verify`` in the raw
    Praxis fact. Both default to ``None`` so a missing tag is representable (and routed to
    triage) rather than coerced into a misleading value.
    """

    id: str
    tier: str | None = None
    verify: str | None = None


@dataclass
class BuildTarget:
    """The disjoint partition of a requirement list. Each requirement appears once.

    ``build`` is the gate's completion set (mvp + automated). The other three groups are
    surfaced but never block the autonomous gate. See module docstring for the rules.
    """

    build: list[Requirement] = field(default_factory=list)
    deferred_manual: list[Requirement] = field(default_factory=list)
    excluded_post_mvp: list[Requirement] = field(default_factory=list)
    needs_triage: list[Requirement] = field(default_factory=list)


def _norm(value: Any) -> str | None:
    """Lower-case/strip a tag value; map missing or non-string values to ``None``."""
    if not isinstance(value, str):
        return None
    stripped = value.strip().lower()
    return stripped or None


def requirement_from_fact(fact: dict) -> Requirement:
    """Adapt a raw Praxis requirement fact into a :class:`Requirement`.

    Tolerant of shape drift: a missing ``meta``, ``meta.scope`` or ``meta.verify`` yields a
    ``None`` tag (which the selector routes to ``needs_triage``), never a crash. The id is
    pulled from ``meta.requirement_id`` and falls back to a top-level ``id``.
    """
    meta = fact.get("meta") or {}
    req_id = meta.get("requirement_id") or fact.get("id") or ""
    return Requirement(
        id=str(req_id),
        tier=_norm(meta.get("scope")),
        verify=_norm(meta.get("verify")),
    )


def _coerce(requirement: Requirement | dict) -> Requirement:
    """Accept either a :class:`Requirement` or a raw fact dict; return a Requirement."""
    if isinstance(requirement, Requirement):
        return requirement
    return requirement_from_fact(requirement)


def select_build_target(requirements: list[Requirement | dict]) -> BuildTarget:
    """Partition ``requirements`` into the four disjoint build-target groups.

    Accepts either :class:`Requirement` instances or raw Praxis fact dicts (mixed lists are
    fine). Returns a :class:`BuildTarget`. The decision is fail-SAFE: a requirement whose
    ``tier`` or ``verify`` is missing or unrecognized is routed to ``needs_triage`` and is
    NEVER placed in ``build`` — so the forced completeness gate can never auto-build a
    mis-tagged requirement. An empty input yields four empty groups.
    """
    target = BuildTarget()
    for raw in requirements:
        req = _coerce(raw)

        if req.tier == _TIER_POST_MVP:
            target.excluded_post_mvp.append(req)
        elif req.tier == _TIER_MVP and req.verify == _VERIFY_AUTOMATED:
            target.build.append(req)
        elif req.tier == _TIER_MVP and req.verify == _VERIFY_MANUAL:
            target.deferred_manual.append(req)
        else:
            # Unknown/missing tier or verify, or mvp with an unrecognized verify mode:
            # fail safe to triage rather than risk an unintended auto-build.
            target.needs_triage.append(req)

    return target
