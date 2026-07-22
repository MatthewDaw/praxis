"""Falsifiable fresh-worker resumability probe — a pure, offline structural predicate.

The factory bets its whole parallel-worktree model on one invariant: a ticket's "done" is
reconstructable from Praxis state ALONE, so a cold worker that never saw the planning conversation can
still know what to build and how to prove it. This module makes that bet *testable per-ticket* instead
of an untested belief: :func:`resumability_report` inspects a ticket's Praxis meta (plus its
already-resolved required-validation set) and reports whether a fresh worker could reconstruct "done"
from state — and, if not, exactly which structural piece is missing.

It is deliberately PURE and OFFLINE: no Praxis calls, no model call, no I/O. The caller passes the
resolved required set (from ``_ticket_state.resolve_validation_requirements``) so the probe adds no
Praxis round-trip. This is the STRUCTURAL probe (deterministic, CI-safe); the cold-worker LLM "deep"
probe is deferred behind a separate seam.

Resumable iff ALL of:
  * COVERABLE-FROM-STATE — ``(non-empty acceptance) OR (non-empty resolved required_validations)``.
    This MIRRORS ``_ticket_state.contract_with_floor``'s own coverability rule (acceptance floor OR
    resolved checks). Mirroring it is load-bearing: a ticket covered purely by declared checks (no
    acceptance text) is a legitimate, buildable state today, so requiring acceptance unconditionally
    would starve exactly the terminal/backend tickets the acceptance floor already makes coverable —
    the false-positive the adversarial review caught.
  * ``verify`` MODE SET — the ticket declares how it is proven ("automated" / "manual"). An absent
    mode means the plan never decided how "done" is observed.
  * NO DANGLING DEPENDENCY — every ``depends_on`` id names a real plan requirement. This is checked
    ONLY when the caller supplies ``known_requirement_ids`` (the universe of valid ids); without a
    universe the dimension is a no-op, so a caller working from the live *incomplete* set — where a
    FINISHED prerequisite has already dropped out — never false-flags a satisfied dependency.

The returned ``missing`` list names exactly the failed dimensions, in a stable order
(:data:`MISSING_ORDER`), so a router can surface them to intake verbatim.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

# Stable, human-facing labels for the structural dimensions a ticket can be missing. Order is fixed so
# the router's surfaced ``under_specified`` list reads the same way every time.
MISS_CONTRACT = "contract"      # neither an acceptance condition NOR any resolved required check
MISS_VERIFY = "verify"          # no verify mode declared
MISS_DEPENDS_ON = "depends_on"  # a depends_on id names no known plan requirement (dangling)

MISSING_ORDER = (MISS_CONTRACT, MISS_VERIFY, MISS_DEPENDS_ON)


def _as_list(v: Any) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def resumability_report(ticket_meta: Optional[dict],
                        resolved_required: Optional[Iterable[Any]],
                        known_requirement_ids: Optional[Iterable[Any]] = None) -> dict:
    """Structural resumability of ONE ticket from Praxis state alone.

    :param ticket_meta: the ticket/requirement node's ``meta`` dict (``acceptance``, ``verify``,
        ``depends_on``). ``None`` is treated as an empty meta.
    :param resolved_required: this pass's resolved required-validation set (the check facts/ids from
        ``resolve_validation_requirements``). Its non-emptiness is half of the coverability rule, so a
        check-covered but acceptance-less ticket is still resumable.
    :param known_requirement_ids: the universe of valid plan requirement ids. When provided, every
        ``depends_on`` id is checked against it and a dangling id fails the probe. When ``None`` the
        dependency dimension is skipped entirely (see module docstring).
    :returns: ``{"resumable": bool, "missing": [dimension, ...]}`` — ``missing`` is empty iff resumable
        and otherwise names the failed dimensions in :data:`MISSING_ORDER`.
    """
    meta = dict(ticket_meta or {})
    missing: set[str] = set()

    # Coverable-from-state: acceptance floor OR a non-empty resolved required set (mirrors
    # contract_with_floor). Either one gives a cold worker a concrete thing to prove.
    has_acceptance = bool(str(meta.get("acceptance") or "").strip())
    has_resolved = any(r for r in (resolved_required or []))
    if not has_acceptance and not has_resolved:
        missing.add(MISS_CONTRACT)

    # A verify mode must be declared — how "done" is observed.
    if not str(meta.get("verify") or "").strip():
        missing.add(MISS_VERIFY)

    # Dangling-dependency check — only when the caller can vouch for the id universe.
    if known_requirement_ids is not None:
        known = {str(x) for x in known_requirement_ids if x}
        deps = [str(d) for d in _as_list(meta.get("depends_on")) if d]
        if any(d not in known for d in deps):
            missing.add(MISS_DEPENDS_ON)

    ordered = [m for m in MISSING_ORDER if m in missing]
    return {"resumable": not ordered, "missing": ordered}
