"""``af-retro`` — the operator's detail view onto the failure-learning loop (R23/R24).

FL1 shipped only the foundation this CLI needs to exist and answer for real: the shared
``factory-learnings`` space it reads from is live (see ``agent_factory.ingestion_api``). FL18
completes it into the full per-project record R23 describes (checks activated/suspended/widened,
proof outcomes, check-undraftable rate, gating-vs-demoted ratio) plus R24's push-not-pull FLAGS:
``af-retro <project>`` shows a project's own pending + acked flags inline with its checks/lessons;
``af-retro --flags`` aggregates every project's PENDING flags, newest first;
``af-retro ack <flag_id>`` acknowledges one, recording who/when and dropping it from every later
pending list — including the one the af-build session-start banner and the loop-end notification
(``scripts/af-ticket-loop.sh``) print from the same command.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from typing import Any

from agent_factory import failure_taxonomy
from agent_factory.ingestion_api import ack_flag, read_checks, read_classes, read_flags, read_lessons


def enforcement_counts(checks: list[dict[str, Any]]) -> Counter[str]:
    """``{enforcement_state: count}`` over a project's checks (R23's activated/suspended tally)."""
    return Counter(str((chk.get("meta") or {}).get("enforcement_state") or "unknown") for chk in checks)


def gating_vs_demoted(counts: Counter[str]) -> tuple[int, int]:
    """``(gating, demoted)`` — every non-gating state (report_only/suspended/archived/unknown)
    counts as demoted, so the ratio reflects everything that stopped blocking FINISH. Takes the
    already-computed :func:`enforcement_counts` tally rather than the raw checks, so a caller that
    also prints the per-state breakdown never counts the same list twice."""
    gating = counts.get("gating", 0)
    demoted = sum(n for state, n in counts.items() if state != "gating")
    return gating, demoted


def check_undraftable_rate(checks: list[dict[str, Any]]) -> float:
    """Share of machine-drafted checks whose proof never went ``"proven"`` (R6/R23) — 0.0 with no
    machine-drafted checks yet, never a division error."""
    machine = [c for c in checks if (c.get("meta") or {}).get("channel") == "machine"]
    if not machine:
        return 0.0
    undraftable = sum(1 for c in machine if (c.get("meta") or {}).get("proof_status") != "proven")
    return undraftable / len(machine)


def _flag_line(flag: dict[str, Any], *, show_project: bool) -> str:
    meta = flag.get("meta") or {}
    status = "ACKED" if meta.get("acknowledged") else "PENDING"
    scope = f" {meta.get('project')}" if show_project else ""
    text = flag.get("text") or flag.get("insight") or ""
    return f"  [{status}]{scope} [{meta.get('kind', '')}] {flag.get('id', '')}\t{text}"


def _cmd_report(args: argparse.Namespace) -> int:
    hits = read_lessons(args.project, top_k=args.top_k)
    if not hits:
        print(f"af-retro: no lessons visible for {args.project!r} yet.")
    else:
        print(f"af-retro: lessons visible for {args.project!r} (top {len(hits)}):")
        for hit in hits:
            print(f"  {hit.get('id', '')}\t{hit.get('text', '')}")

    checks = read_checks(args.project)
    counts = enforcement_counts(checks)
    gating, demoted = gating_vs_demoted(counts)
    rate = check_undraftable_rate(checks)
    print(
        f"af-retro: checks for {args.project!r} — gating={counts.get('gating', 0)} "
        f"report_only={counts.get('report_only', 0)} suspended={counts.get('suspended', 0)} "
        f"archived={counts.get('archived', 0)} (gating:demoted={gating}:{demoted}) "
        f"check-undraftable-rate={rate:.0%}"
    )

    flags = read_flags(args.project, pending_only=False)
    if not flags:
        print(f"af-retro: no flags recorded for {args.project!r}.")
    else:
        print(f"af-retro: flags for {args.project!r} (newest first):")
        for flag in flags:
            print(_flag_line(flag, show_project=False))

    if args.calibration:
        _print_calibration()
    return 0


def _cmd_flags(args: argparse.Namespace) -> int:
    flags = read_flags(args.project, pending_only=True)
    scope = f" for {args.project!r}" if args.project else " across every project"
    if not flags:
        print(f"af-retro: no pending flags{scope}.")
        return 0
    print(f"af-retro: pending flags{scope} (newest first):")
    for flag in flags:
        print(_flag_line(flag, show_project=not args.project))
    return 0


def _cmd_ack(args: argparse.Namespace) -> int:
    result = ack_flag(args.flag_id)
    meta = result.get("meta") or result
    print(f"af-retro: acked flag {args.flag_id} (by {meta.get('acknowledged_by')} "
          f"at {meta.get('acknowledged_at')})")
    return 0


def _print_calibration() -> None:
    """Surface the R20b staged-rollout state (FL3): assignments are recorded even while
    taxonomy-dependent automation stays observe-only, so an operator can watch it approach arming.
    Also prints every failure-class ASSIGNMENT (R20/FL15) — recurrence count and merge status — so
    the resurrection path and the near-duplicate sweep both stay spot-auditable, not just the
    aggregate streak counters."""
    state = failure_taxonomy.calibration_state()
    print(
        f"af-retro: taxonomy calibration — armed={state['armed']} "
        f"streak={state['streak']}/{state['required']} "
        f"total_assignments={state['total_assignments']} corrections={state['corrections']}"
    )
    classes = read_classes()
    if not classes:
        print("af-retro: no failure-class assignments recorded yet.")
        return
    print("af-retro: failure-class assignments (recurrence + merge status):")
    for cls in classes:
        print(_class_assignment_line(cls))


def _class_assignment_line(cls: dict[str, Any]) -> str:
    meta = cls.get("meta") or {}
    merged_into = meta.get("merged_into")
    status = f"merged->{merged_into}" if merged_into else "active"
    recurrence = meta.get("recurrence_count", 1)
    return f"  [{status}] {cls.get('id', '')}\trecurrence={recurrence}\t{cls.get('text', '')}"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] == "ack":
        ap = argparse.ArgumentParser(prog="af-retro ack",
                                     description="Acknowledge one pending flag: removes it from "
                                                 "every later pending list and records who/when.")
        ap.add_argument("flag_id")
        args = ap.parse_args(argv[1:])
        return _cmd_ack(args)

    ap = argparse.ArgumentParser(
        prog="af-retro",
        description="Print a project's failure-learning-loop record from Praxis: lessons, checks "
                    "(activated/suspended/widened, proof outcomes, check-undraftable rate, "
                    "gating-vs-demoted ratio), and its flags. `--flags` aggregates pending flags "
                    "across every project (or one, with `project`); `af-retro ack <flag_id>` "
                    "acknowledges one (R23/R24).")
    ap.add_argument("project", nargs="?", default=None, help="the project name to report on")
    ap.add_argument("--top-k", type=int, default=10, dest="top_k")
    ap.add_argument("--calibration", action="store_true",
                    help="also print the R20b taxonomy-calibration staged-rollout state")
    ap.add_argument("--flags", action="store_true",
                    help="aggregate pending flags across every project (or one, if `project` is "
                         "also given), newest first, instead of a project report")
    args = ap.parse_args(argv)

    if args.flags:
        return _cmd_flags(args)
    if not args.project:
        ap.error("project is required unless --flags is given")
    return _cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
