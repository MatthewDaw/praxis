"""Reinterpret a same-slot value clash as temporal supersession, not contradiction.

The structural :class:`ClaimConflictDetector` flags two facts that fill the same
functional slot with incompatible values. But a value that legitimately *changed
over time* — "HQ in Denver in 2019" vs "HQ in Austin in 2024" — is not a standing
contradiction; it is supersession (the newer fact replaced the older). The live
audit caught exactly this as a false-positive contradiction.

The discriminator is in the claims, not the prose. A dated value change shows up as
TWO clashing functional slots on the same subject: a **non-temporal** slot whose
value changed (``location``: Denver vs Austin) AND a **temporal** slot that dates
each assertion (``year``: 2019 vs 2024). A genuine year contradiction
("invented in 1799" vs "1800") clashes on the temporal slot ALONE — the year *is*
the disputed value, with no accompanying non-temporal change.

So this step (run LAST, only reinterpreting flags the detectors already raised — it
never invents a clash, so it can't lower contradiction precision) converts a
``contradiction:<id>`` to supersession only when the incoming write and the flagged
fact clash on **both** a non-temporal slot and a temporal slot. The newer fact (by
the temporal slot's year) supersedes the older via Graphiti's invalidate-and-keep;
the older is retired but retained for point-in-time recall. Slot-less (semantic)
flags carry no claim evidence and are always left as contradictions.
"""

from __future__ import annotations

import re

from knowledge.knowledge_graph.knowledge_graph_def import Claim
from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_policy_def import (
    WriteDecision,
    contradiction_ids,
)

_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_TEMPORAL_ATTRS = ("year", "date", "time", "as of", "as-of", "since", "effective")


def _is_year_value(value: str) -> bool:
    """True if the value IS itself a year (so the year is the disputed value)."""
    return _YEAR.fullmatch((value or "").strip()) is not None


def _year_in(value: str) -> int | None:
    years = {int(m.group(0)) for m in _YEAR.finditer(value or "")}
    return max(years) if years else None


def _is_temporal_slot(attribute: str, old_val: str, new_val: str) -> bool:
    """A slot is temporal if its attribute names time, or both values are years."""
    attr = attribute.lower()
    if any(t in attr for t in _TEMPORAL_ATTRS):
        return True
    return _is_year_value(old_val) and _is_year_value(new_val)


class TemporalSupersessionDetector(WriteStep):
    """Convert dated same-slot value changes from contradiction to supersession."""

    consumes_claim_candidates = True

    def apply(self, decision: WriteDecision) -> None:
        if decision.dropped or decision.action == "update":
            return
        flagged_ids = contradiction_ids(decision.flags)
        if not flagged_ids:
            return
        incoming = {c.slot: c.value for c in decision.claims if c.functional}
        if not incoming:
            return
        # Per flagged candidate, gather the slots it actually clashes with the
        # incoming write on: (attribute, old_value, new_value).
        clashes: dict[str, list[tuple[str, str, str]]] = {}
        for ch in decision.claim_candidates:
            cid = ch.fact.fact.id
            if cid not in flagged_ids:
                continue
            slot = (Claim.norm(ch.subject), Claim.norm(ch.attribute))
            new_val = incoming.get(slot)
            if new_val is None or Claim.norm(new_val) == Claim.norm(ch.value):
                continue  # same value, or a slot the incoming write doesn't fill
            clashes.setdefault(cid, []).append((slot[1], ch.value, new_val))

        for cid, slot_clashes in clashes.items():
            temporal = [c for c in slot_clashes if _is_temporal_slot(*c)]
            non_temporal = [c for c in slot_clashes if not _is_temporal_slot(*c)]
            # Supersession requires BOTH a changed non-temporal value AND a temporal
            # slot dating the two assertions. Year-only clash -> genuine contradiction.
            if not (temporal and non_temporal):
                continue
            old_year = min((_year_in(o) for _a, o, _n in temporal if _year_in(o)), default=None)
            new_year = max((_year_in(n) for _a, _o, n in temporal if _year_in(n)), default=None)
            if old_year is None or new_year is None or old_year == new_year:
                continue
            decision.flags.remove(f"contradiction:{cid}")
            if new_year > old_year:
                decision.flags.append(f"supersede:{cid}")  # incoming newer -> retire old
            else:
                decision.flags.append(f"supersede_self:{cid}")  # incoming is historical
