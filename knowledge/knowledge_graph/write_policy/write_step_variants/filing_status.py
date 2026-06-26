"""Filing-status identity guard for the write policy.

Tax facts distilled per filing status collide hard on their *numbers*: Single,
MFS, and HoH share whole bracket ranges (Single 22% and MFS 22% are both
$48,475-$103,350) and two statuses share a standard-deduction amount (Single ==
MFS == $15,750). The dedup/merge + claim/semantic conflict steps key on
``(subject, attribute, value)``, which is status-blind, so two facts that differ
ONLY by filing status look like a duplicate (same value -> silent merge) or a
clash (same rate, different range -> false contradiction, loser rejected). Either
way a whole filing status's ladder silently disappears.

The fix is to treat the **filing status as part of a fact's identity**: a fact
about Single and a fact about MFS are distinct facts even when their numbers
coincide, so they must never be merged into one another and never flagged as
contradicting one another. ``dominant_filing_status`` extracts that identity from
the fact text; the dedup/conflict steps consult it and, when two facts carry
*different* statuses, hold them apart.

Distilled facts are whole-ladder blocks ("...for Single filers...10%...12%...")
rather than one bracket per fact, and a block may name another status in passing
(the MFS block notes "MFS thresholds match the Single filing status"). So the
status is the **dominant** (most-mentioned) one, not merely "a status appears":
a clear plurality wins, a tie or no mention yields ``None`` (guard does not
engage — unchanged behavior for non-status facts and for multi-status summaries
like the combined standard-deduction table).
"""

from __future__ import annotations

import re

# Canonical filing statuses, each with the surface forms that count as a mention.
# Ordered most-specific-first only matters for readability; counting is independent.
# "filing jointly"/"filing separately" rather than the strict "married filing ..."
# so distiller paraphrases ("married couples filing jointly") still resolve.
_STATUS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("mfj", re.compile(r"filing jointly|\bmfj\b", re.I)),
    ("mfs", re.compile(r"filing separately|\bmfs\b", re.I)),
    ("hoh", re.compile(r"head of household|\bhoh\b", re.I)),
    ("single", re.compile(r"\bsingle\b", re.I)),
]


def dominant_filing_status(text: str) -> str | None:
    """The filing status this fact is primarily about, or ``None`` if ambiguous.

    Counts mentions of each canonical status and returns the strict plurality
    winner. ``None`` when no status is mentioned or the top is tied (e.g. a
    summary table listing every status once) — in those cases the caller leaves
    its existing dedup/conflict behavior unchanged.
    """
    counts = {status: len(pat.findall(text)) for status, pat in _STATUS_PATTERNS}
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    top_status, top_count = ranked[0]
    if top_count == 0:
        return None
    if len(ranked) > 1 and ranked[1][1] == top_count:
        return None  # tie at the top -> ambiguous, don't engage the guard
    return top_status


def different_status(a: str, b: str) -> bool:
    """True when both texts have a clear, *different* dominant filing status.

    The predicate the dedup/conflict steps use to hold two facts apart: distinct
    statuses -> distinct facts (never merge, never a contradiction). A ``None`` on
    either side (ambiguous / not a status fact) returns False, so the guard only
    fires when both sides are unambiguously about different statuses.
    """
    sa, sb = dominant_filing_status(a), dominant_filing_status(b)
    return sa is not None and sb is not None and sa != sb


# A marginal-rate mention ("22%", "22 percent"). Bracket facts each name exactly
# one rate; whole-ladder summaries name several (and so resolve to no single rate).
_RATE = re.compile(r"(\d{1,2})\s*(?:%|percent)", re.I)


def bracket_rate(text: str) -> str | None:
    """The single marginal rate this fact is about, or ``None`` if not a one-rate fact.

    A tax-bracket fact ("Single filers are taxed at 22% on $48,475-$103,350") names
    exactly one rate; a whole-ladder block or a non-bracket fact names zero or several,
    so the bracket identity does not apply and the caller falls back to filing status.
    """
    rates = {m.group(1) for m in _RATE.finditer(text)}
    return next(iter(rates)) if len(rates) == 1 else None


def distinct_tax_facts(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` are distinct tax facts that must be held apart.

    Generalizes :func:`different_status` to the full bracket identity ``(filing
    status, rate)``: a tax-bracket schedule is a *ladder* of coexisting facts, never a
    set of mutual contradictions, and the same range recurs across statuses. So two
    facts are distinct — never merged into one another, never flagged as a
    contradiction — when:

    * both are single-rate **bracket** facts with a known filing status and their
      ``(status, rate)`` identity differs — this covers BOTH the same-status adjacent
      rungs (HoH 10% vs HoH 12%, which only *look* like a numeric clash) AND the
      same-rate cross-status twins (Single 22% vs MFS 22%, identical $48,475-$103,350);
    * otherwise, both have a clear, different dominant filing status (the standard
      deduction case: Single $15,750 vs MFS $15,750).

    Returns False whenever the identity is ambiguous on either side, so non-tax facts
    and same-bracket restatements keep their ordinary dedup/conflict behavior.
    """
    sa, sb = dominant_filing_status(a), dominant_filing_status(b)
    ra, rb = bracket_rate(a), bracket_rate(b)
    if sa is not None and sb is not None and ra is not None and rb is not None:
        return (sa, ra) != (sb, rb)  # different bracket rung -> distinct facts
    return sa is not None and sb is not None and sa != sb
