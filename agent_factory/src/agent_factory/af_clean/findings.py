"""R12: the located-finding admission gate.

Every af-clean finding must clear a small set of mechanical rules before it is ever reported or
applied. A finding failing any rule is DISCARDED, never reported with a caveat — but the
discard always carries a machine-readable ``reason`` so the run is auditable rather than silently
lossy. See docs/brainstorms/2026-07-29-af-clean-requirements.md B11-B15, B27 and
docs/ideation/2026-07-29-af-clean-ideation.md idea 8, idea 10, idea 11, RK4 for the rubric this
module operationalizes.

This module is PURE: no I/O, no LLM calls. It only classifies candidate findings a detector or
judge has already produced.
"""

from __future__ import annotations

from dataclasses import dataclass

# The signed slop axis (B14/idea 11): every finding names which pole it sits at because the two
# remedies are opposite directions. A finding declaring anything else -- including no pole at all
# -- is not well-formed.
_VALID_POLES = frozenset({"bloat", "fragmentation"})

# The DRY conflict resolves only by an observable discriminator (B15/idea 10) -- never a guess.
_VALID_DRY_OBSERVABLES = frozenset({"co-change", "parameter-accretion"})

# "Three callers earns the helper" (RK3) read in reverse: inlining a helper that already has 3+
# live callers is refused, not merely discouraged.
_MIN_LIVE_CALLERS_FOR_INLINE_REFUSAL = 3


@dataclass(frozen=True)
class Location:
    """A located ``file:line`` instance. Both fields must be present and non-empty to count."""

    file: str
    line: int | None


@dataclass(frozen=True)
class Finding:
    """A candidate af-clean finding, prior to admission.

    ``tier`` is the evidence tier (B12): ``"enforce"``, ``"advise"``, or ``"judgment"`` (a
    judgment-tier finding additionally must enumerate its cognitive-load chunks, B13).
    ``pole`` is the signed slop axis (B14): ``"bloat"`` or ``"fragmentation"``.
    ``is_dry`` + ``observable`` cover the DRY discriminator rule (B15/idea 10).
    ``proposal`` + ``live_caller_count`` cover the inline-refusal rule (RK3/idea 10).
    ``consolidation_requires_flag_per_caller`` covers failed centralization (B27/RK4).
    """

    rule: str
    tier: str
    location: Location | None = None
    pole: str | None = None
    chunks: tuple[str, ...] = ()
    is_dry: bool = False
    observable: str | None = None
    proposal: str | None = None
    live_caller_count: int | None = None
    consolidation_requires_flag_per_caller: bool = False


@dataclass(frozen=True)
class Verdict:
    """Whether a finding is admitted; ``reason`` is populated iff ``admitted`` is False."""

    admitted: bool
    reason: str | None = None


def _admitted() -> Verdict:
    return Verdict(admitted=True, reason=None)


def _dropped(reason: str) -> Verdict:
    return Verdict(admitted=False, reason=reason)


def admit_finding(finding: Finding) -> Verdict:
    """Apply every mechanical admission rule in order; the first violation drops the finding.

    Returns an admitted verdict only if the finding clears all of: located instance (B11),
    chunk enumeration when judgment-tier (B13), a declared pole (B14), a DRY observable when the
    finding is a DRY finding (B15), the inline-refusal guard (RK3), and the failed-centralization
    guard (B27).
    """
    loc = finding.location
    if loc is None or not loc.file or loc.line is None:
        return _dropped("no located file:line instance")

    if finding.tier == "judgment" and not finding.chunks:
        return _dropped("judgment-tier finding has no enumerated chunks")

    if finding.pole not in _VALID_POLES:
        return _dropped("finding declares no bloat/fragmentation pole")

    if finding.is_dry and finding.observable not in _VALID_DRY_OBSERVABLES:
        return _dropped("DRY finding has no co-change or parameter-accretion observable")

    if (
        finding.proposal == "inline"
        and (finding.live_caller_count or 0) >= _MIN_LIVE_CALLERS_FOR_INLINE_REFUSAL
    ):
        return _dropped(
            f"inline proposal refused: helper has {finding.live_caller_count} live callers (>= "
            f"{_MIN_LIVE_CALLERS_FOR_INLINE_REFUSAL})"
        )

    if finding.consolidation_requires_flag_per_caller:
        return _dropped(
            "failed centralization: consolidation would require a flag/branch per caller"
        )

    return _admitted()
