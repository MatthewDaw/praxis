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

# The CHANGE CLASS of a finding: what KIND of edit it proposes. This exists because the blind
# verifier asks a different question of each class (see :mod:`.verifier`), and a finding that does
# not say which class it belongs to would be verified against whichever question happened to be the
# default -- which is how a cleaner starts approving changes its verifier never actually checked.
CLASS_DELETION = "deletion"
CLASS_CONSOLIDATION = "consolidation"
CLASS_ANNOTATION = "annotation"
CLASS_LINT_FIX = "lint-fix"
CLASS_JS_TO_TS = "js-to-ts"
#: A finding that proposes NO edit at all -- a posture report a human actions. It still must be
#: located, because "somewhere in this repo the type gate is unenforced" is not actionable.
CLASS_REPORT_ONLY = "report-only"

_VALID_CHANGE_CLASSES = frozenset({
    CLASS_DELETION, CLASS_CONSOLIDATION, CLASS_ANNOTATION,
    CLASS_LINT_FIX, CLASS_JS_TO_TS, CLASS_REPORT_ONLY,
})

# The pole is the SLOP axis, and only the slop classes sit on it: removing bloat and un-fragmenting
# are opposite remedies, so a deletion or a consolidation must say which one it is. An annotation, a
# lint fix, a JS->TS conversion, or a posture report is on neither pole -- demanding one there would
# force every such finding to lie about itself to get admitted.
_POLED_CLASSES = frozenset({CLASS_DELETION, CLASS_CONSOLIDATION})

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
    ``change_class`` is which KIND of edit this proposes, and therefore which question the blind
    verifier must answer about it; it defaults to ``"deletion"``, the only class that existed before
    the verifier's question was split.
    """

    rule: str
    tier: str
    location: Location | None = None
    pole: str | None = None
    change_class: str = CLASS_DELETION
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
    a known change class, chunk enumeration when judgment-tier (B13), a declared pole on a slop
    class (B14), a DRY observable when the finding is a DRY finding (B15), the inline-refusal guard
    (RK3), and the failed-centralization guard (B27).
    """
    loc = finding.location
    if loc is None or not loc.file or loc.line is None:
        return _dropped("no located file:line instance")

    if finding.change_class not in _VALID_CHANGE_CLASSES:
        return _dropped(f"unknown change class {finding.change_class!r}: no verifier question exists for it")

    if finding.tier == "judgment" and not finding.chunks:
        return _dropped("judgment-tier finding has no enumerated chunks")

    if finding.change_class in _POLED_CLASSES and finding.pole not in _VALID_POLES:
        return _dropped("finding declares no bloat/fragmentation pole")

    if finding.pole is not None and finding.pole not in _VALID_POLES:
        return _dropped(f"finding declares an unknown pole {finding.pole!r}")

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
