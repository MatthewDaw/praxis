"""U6 — candidate-pool lifecycle: keep the shared ``building-validation`` candidate pool bounded.

Two levers keep the pool from growing without limit or bleeding across unrelated tickets:

  * **Tight scoping at authoring** (a skill-guidance concern — af-build/af-intake-build-validation
    author build-discovered candidates scoped to the originating ticket's tags/surface, never a broad
    ``["*"]``). Over-broad predicates are already visible via ``resolve_preview --by-check`` (the
    "TOO BROAD" flag applies to candidate checks too).
  * **Orphan pruning** (this module) — a candidate whose ``applies_to`` resolves onto NO live
    (incomplete) ticket is dead weight: nothing will ever tier it. :func:`orphaned_candidate_ids`
    identifies those deterministically so a prune step can reject them, so the pool tracks the live
    plan rather than accumulating forever.

Pure and deterministic: no Praxis calls, no I/O. The caller supplies the live tickets' tagsets and the
candidate facts; the actual reject is an out-of-band Praxis write the caller performs on the returned ids.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence


def _norm(tag: object) -> str:
    return str(tag).strip().casefold()


def _applies(candidate: Any) -> set[str]:
    meta = (candidate.get("meta") or {}) if isinstance(candidate, dict) else {}
    return {_norm(a) for a in (meta.get("applies_to") or []) if _norm(a)}


def _cid(candidate: Any) -> str:
    meta = (candidate.get("meta") or {}) if isinstance(candidate, dict) else {}
    return str(meta.get("check_id") or candidate.get("id") or "").strip()


def orphaned_candidate_ids(candidates: Sequence[Any],
                           live_tagsets: Iterable[Iterable[str]]) -> list[str]:
    """Return the ``check_id``s of candidates that resolve onto NO live ticket — prunable orphans.

    A candidate is LIVE (kept) if it is a ``"*"`` wildcard (always resolves) or its normalized
    ``applies_to`` intersects at least one live ticket's normalized tagset. Everything else is an
    orphan whose originating ticket/tag is gone from the incomplete set. Deterministic order (sorted).
    A candidate with an empty ``applies_to`` resolves onto nothing and is therefore an orphan.
    """
    live = [{_norm(t) for t in tags if _norm(t)} for tags in live_tagsets]
    orphans: list[str] = []
    for c in candidates:
        applies = _applies(c)
        if "*" in applies:
            continue  # wildcard always resolves — never an orphan
        if applies and any(applies & tagset for tagset in live):
            continue  # resolves onto at least one live ticket
        cid = _cid(c)
        if cid:
            orphans.append(cid)
    return sorted(set(orphans))
