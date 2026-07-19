#!/usr/bin/env python3
"""Dry-run resolution inspector — show, per incomplete ticket, WHICH validation requirements
af-build would resolve onto it and by WHICH lane, WITHOUT touching Praxis state.

This is strictly READ-ONLY: it never claims, pins, patches meta, or stamps a run. It calls the
EXACT resolution functions the real build uses (``_ticket_state.resolve_validation_requirements`` +
``contract_with_floor``), so the preview and the live run can never drift apart.

Usage:
    python -m agent_factory.tools.resolve_preview <project> [--checks-space=space[:snapshot]]

``--checks-space`` mirrors the af-build seam: ``space:snapshot`` overrides the check-read reference;
given only ``space``, the snapshot defaults to ``building-validation``. A live run needs Praxis
reachable; ``--help`` works fully offline.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Share the real build's hook code — mirror the test files' sys.path insert so we import the SAME
# `_ticket_state` / `_praxis` af-build runs, never a re-implementation.
_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _praxis  # noqa: E402
import _ticket_state as ts  # noqa: E402
from _praxis import PraxisUnreachable  # noqa: E402

_FLOOR_SUFFIX = "::acceptance"


def parse_checks_space(raw):
    """Parse ``--checks-space=<space[:snapshot]>`` into an ``(space, snapshot)`` override tuple,
    exactly like the af-build seam: a bare ``space`` defaults the snapshot to ``building-validation``.
    Returns ``None`` when unset (use the per-scope default)."""
    if not raw:
        return None
    space, sep, snapshot = raw.partition(":")
    space = space.strip()
    snapshot = snapshot.strip() if sep else ""
    if not space:
        raise ValueError("--checks-space needs a space (space[:snapshot])")
    return (space, snapshot or ts.DEFAULT_VALIDATION_CHECKS_SNAPSHOT)


def _lane_of(check, ticket_tags):
    """Derive the LANE label for one already-resolved check from the resolved set itself (never
    re-resolves): floor -> tag/surface fallthrough, matching af-build's own resolution model."""
    cid = (check or {}).get("id") or ""
    if str(cid).endswith(_FLOOR_SUFFIX) or (check.get("meta") or {}).get("synthetic") == "acceptance-floor":
        return "floor"
    applies = [ts.normalize_tag(a) for a in ((check.get("meta") or {}).get("applies_to") or []) if a]
    if "*" in applies:
        return "wildcard"
    if set(applies) & ticket_tags:
        return "tag"
    return "surface"


def _ticket_tagset(meta):
    """The ticket's normalized IDENTITY tags (meta.tags ∪ meta.applies_to), same union the resolver
    matches against."""
    raw = ts._as_list(meta.get("tags")) + ts._as_list(meta.get("applies_to"))
    return {ts.normalize_tag(t) for t in raw if t and ts.normalize_tag(t)}


def _resolve_ticket(ticket, bare, override):
    """Resolve ONE incomplete ticket ONCE and return everything downstream needs:
    ``(lines, info)`` where ``lines`` is the printable per-ticket preview and ``info`` is
    ``{requirement_id, verify, floor_only_automated}``.

    ``floor_only_automated`` is the coverage-gap signal: the ticket is verify==automated AND its
    DECLARED (resolved) checks are ZERO — the acceptance floor is the only thing it would prove.
    The floor is added ONLY by :func:`contract_with_floor`, so an empty ``resolved`` is exactly the
    floor-only condition; MANUAL tickets are exempt (their floor is a human sign-off, not a gap).
    """
    meta = ticket.get("meta") or {}
    cid = ticket.get("id") or ticket.get("factId") or "?"
    req_id = meta.get("requirement_id") or cid
    tags = ts._as_list(meta.get("tags"))
    tagset = _ticket_tagset(meta)
    # Normalize the verify mode the same way start_ticket / plan_gate do (strip+casefold), so a
    # mis-cased "Automated" cannot silently escape the coverage gate by failing an exact-string compare.
    verify = str(meta.get("verify") or "automated").strip().casefold()

    resolved = ts.resolve_validation_requirements(
        ticket, project=bare, scope="validation", override=override)
    contract = ts.contract_with_floor(
        cid, meta.get("acceptance"), resolved, verify=verify)

    floor_only_automated = (verify == "automated") and (len(resolved) == 0)
    info = {"requirement_id": req_id, "verify": verify,
            "floor_only_automated": floor_only_automated}

    lanes: dict[str, list[str]] = {"floor": [], "wildcard": [], "tag": [], "surface": []}
    for chk in contract:
        lanes[_lane_of(chk, tagset)].append((chk or {}).get("id") or "?")

    non_floor = [c for c in contract if _lane_of(c, tagset) != "floor"]
    lines = [f"  requirement_id: {req_id}   (fact {cid})",
             f"  tags: {tags or '(none)'}"]
    if not non_floor and lanes["floor"]:
        lines.append("  ** ONLY acceptance-floor (no declared check) **")
    for lane in ("floor", "wildcard", "tag", "surface"):
        if lanes[lane]:
            lines.append(f"    [{lane}] {', '.join(lanes[lane])}")
    if not contract:
        lines.append("    (empty contract — no checks AND no acceptance; a planning defect)")
    return lines, info


def _preview_ticket(ticket, bare, override):
    """Resolve + describe ONE incomplete ticket. Returns printable lines. Read-only."""
    return _resolve_ticket(ticket, bare, override)[0]


def _bleeds_across_concerns(landed_tagsets, applies):
    """Heuristic flag for an over-broad ``applies_to``: a NON-wildcard check bleeds across unrelated
    concerns when it lands on 2+ tickets that share NO identity tag in common *beyond the check's own
    ``applies_to``*. If the intersection of those residual tagsets is empty, the check straddles
    otherwise-unrelated tickets — the author should eyeball whether the predicate is too generic."""
    if len(landed_tagsets) < 2:
        return False
    applied = set(applies)
    residuals = [tagset - applied for tagset in landed_tagsets]
    return not set.intersection(*residuals)


def _by_check_view(tickets, checks_ref):
    """INVERTED preview: for each building-validation CHECK, the set of incomplete tickets it pins
    onto — so an over-broad ``applies_to`` that bleeds a check across unrelated concerns is visible.

    Matching reuses the SAME primitives the live resolver uses (:func:`_ticket_tagset` +
    ``ts.normalize_tag``): a check lands on a ticket iff its normalized ``meta.applies_to`` intersects
    the ticket's normalized identity tagset, and the ``"*"`` wildcard lands on EVERY ticket. Returns
    printable lines (read-only)."""
    space, snapshot = checks_ref
    checks = _praxis.facts_by(category="check", space=space, snapshot=snapshot)

    # Pre-compute each incomplete ticket's printable requirement_id + normalized identity tagset once.
    entries = []
    for t in tickets:
        meta = t.get("meta") or {}
        rid = meta.get("requirement_id") or t.get("id") or t.get("factId") or "?"
        entries.append((rid, _ticket_tagset(meta)))

    lines = [f"by-check view: {len(checks)} building-validation check(s) over "
             f"{len(entries)} incomplete ticket(s)\n"]
    for chk in checks:
        cid = (chk or {}).get("id") or "?"
        applies_raw = (chk.get("meta") or {}).get("applies_to") or []
        applies = [ts.normalize_tag(a) for a in applies_raw if a]
        wildcard = "*" in applies
        if wildcard:
            landed = [(rid, tagset) for rid, tagset in entries]
        else:
            want = set(applies)
            landed = [(rid, tagset) for rid, tagset in entries if want & tagset]
        landed_ids = [rid for rid, _ in landed]

        lines.append(f"  check: {cid}")
        lines.append(f"    applies_to: {applies_raw or '(none)'}")
        lines.append(f"    fan-out {len(landed_ids)} ticket(s): "
                     f"{', '.join(landed_ids) or '(none)'}")
        if wildcard:
            lines.append("    [wildcard '*' — pins onto EVERY incomplete ticket by design]")
        elif _bleeds_across_concerns([tagset for _, tagset in landed], applies):
            lines.append("    ** POTENTIALLY TOO BROAD — lands on tickets across unrelated tags; "
                         "eyeball this applies_to **")
        lines.append("")
    return lines


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m agent_factory.tools.resolve_preview",
        description="READ-ONLY dry-run: show which validation requirements af-build would resolve "
                    "onto each incomplete ticket, grouped by lane. Never writes to Praxis.")
    p.add_argument("project", help="bare project name (or prd-<project>); the plan is prd-<project>")
    p.add_argument("--checks-space", metavar="space[:snapshot]", default=None,
                   help="override the check-read reference (snapshot defaults to building-validation)")
    p.add_argument("--require-coverage", "--assert-covered", dest="require_coverage",
                   action="store_true",
                   help="exit non-zero if any incomplete automated ticket has ZERO declared checks "
                        "(only the acceptance floor). Manual tickets are exempt. Opt-in.")
    p.add_argument("--by-check", dest="by_check", action="store_true",
                   help="INVERT the view: for each building-validation check, list the incomplete "
                        "tickets its applies_to lands on (plus fan-out count), so an over-broad "
                        "applies_to that bleeds a check across unrelated concerns is visible. "
                        "Prints the by-check view instead of the default per-ticket view.")
    args = p.parse_args(argv)

    try:
        override = parse_checks_space(args.checks_space)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    ref = ts.project_ref(args.project)
    bare = ref.plan[0]
    plan_space, plan_snapshot = ref.plan

    try:
        tickets = _praxis.incomplete_requirements(bare, space=plan_space, snapshot=plan_snapshot)
    except PraxisUnreachable as e:
        print(f"error: Praxis unreachable — cannot list incomplete tickets: {e}", file=sys.stderr)
        return 1

    checks_ref = override or ref.validation
    print(f"project: {bare}   plan: {plan_space}:{plan_snapshot}   "
          f"checks: {checks_ref[0]}:{checks_ref[1]}")
    print(f"incomplete tickets: {len(tickets)}\n")

    if args.by_check:
        try:
            for line in _by_check_view(tickets, checks_ref):
                print(line)
        except PraxisUnreachable as e:
            print(f"error: Praxis unreachable reading checks: {e}", file=sys.stderr)
            return 1
        return 0

    floor_only_automated: list[str] = []
    try:
        for ticket in tickets:
            lines, info = _resolve_ticket(ticket, bare, override)
            for line in lines:
                print(line)
            print()
            if info["floor_only_automated"]:
                floor_only_automated.append(info["requirement_id"])
    except PraxisUnreachable as e:
        print(f"error: Praxis unreachable during resolution: {e}", file=sys.stderr)
        return 1

    if args.require_coverage and floor_only_automated:
        print("error: --require-coverage: the following automated tickets resolve ZERO declared "
              "checks (only the acceptance floor — a coverage gap):", file=sys.stderr)
        for rid in floor_only_automated:
            print(f"  - {rid}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
