#!/usr/bin/env python3
"""U7: review (and, with explicit confirmation, apply) auto-adjustment proposals for the seeded
rubric library, computed from the factory's accumulated graded verdicts.

READ-ONLY by default: it reads ticket verdicts from Praxis, aggregates the signal, and PRINTS
proposals. Nothing changes the seeded library unless you pass BOTH ``--apply`` and ``--confirm``,
and even then a check that is currently in-flight (pinned to an in-progress ticket) is deferred,
never edited under a building ticket.

Usage:
    python -m agent_factory.tools.rubric_adjust_review <project> [--apply --confirm]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _praxis  # noqa: E402
import _ticket_state as ts  # noqa: E402
from _praxis import PraxisUnreachable  # noqa: E402

from agent_factory.rubric_adjust import (  # noqa: E402
    aggregate, apply_proposals, observations_from_tickets, propose_all,
)
from agent_factory.seeded_checks import GRADED, _DEFAULT_PATH, load_seeded_checks  # noqa: E402


def _graded_rubrics(checks):
    return {c.check_id: c.rubric for c in checks if c.kind == GRADED and c.rubric is not None}


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m agent_factory.tools.rubric_adjust_review",
        description="Propose (and optionally apply, human-gated) auto-adjustments to the seeded "
                    "rubric library from the factory's accumulated graded verdicts.")
    p.add_argument("project", help="bare project name (or prd-<project>)")
    p.add_argument("--apply", action="store_true", help="apply numeric proposals to the library")
    p.add_argument("--confirm", action="store_true",
                   help="required alongside --apply to actually mutate the file (no silent writes)")
    args = p.parse_args(argv)

    checks = load_seeded_checks()
    rubrics = _graded_rubrics(checks)
    ref = ts.project_ref(args.project)
    plan_space, plan_snapshot = ref.plan

    try:
        tickets = _praxis.facts_by(state="any", space=plan_space, snapshot=plan_snapshot)
    except PraxisUnreachable as e:
        print(f"error: Praxis unreachable: {e}", file=sys.stderr)
        return 1

    observations, in_flight = observations_from_tickets(tickets)
    signals = aggregate(observations)
    proposals = propose_all(rubrics, signals)

    print(f"project: {ref.plan[0]}   graded observations: {len(observations)}   "
          f"in-flight checks: {sorted(in_flight) or '(none)'}")
    if not proposals:
        print("no adjustment proposals — rubrics look well-calibrated.")
        return 0
    for pr in proposals:
        tgt = f" [{pr.axis}: {pr.from_value} -> {pr.to_value}]" if pr.is_numeric else ""
        print(f"  ({pr.kind}) {pr.check_id}{tgt}\n      {pr.rationale}")

    if not args.apply:
        print("\nread-only. Re-run with --apply --confirm to apply the numeric proposals.")
        return 0

    res = apply_proposals(_DEFAULT_PATH.read_text(encoding="utf-8"), proposals,
                          in_flight=in_flight, confirm=args.confirm)
    for pr, reason in res.skipped:
        print(f"  skipped ({pr.kind}) {pr.check_id}: {reason}")
    if res.applied and args.confirm:
        _DEFAULT_PATH.write_text(res.text, encoding="utf-8")
        print(f"\napplied {len(res.applied)} proposal(s) to {_DEFAULT_PATH}")
    elif not args.confirm:
        print("\n--apply without --confirm: nothing written (add --confirm to mutate the library).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
