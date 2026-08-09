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
import re
import sys
import time
from collections import Counter
from datetime import datetime
from typing import Any

from agent_factory import failure_taxonomy
from agent_factory._cli import praxis_boundary
from agent_factory.ingestion_api import (
    STATE_ARCHIVED,
    STATE_GATING,
    STATE_REPORT_ONLY,
    STATE_SUSPENDED,
    ack_flag,
    read_checks,
    read_classes,
    read_flags,
    read_lessons,
)

#: The state a check whose fact predates the FL12 enforcement-state machine reports as. It is NOT
#: a demotion — nothing ever demoted it; it simply has no recorded state (see
#: :func:`gating_vs_demoted`).
UNKNOWN_STATE = "unknown"

#: The states that mean "this check stopped blocking FINISH". Every one of them is reached only by
#: an explicit FL12 transition (``ingestion_api.transition_enforcement_state``), which is exactly
#: why ``unknown`` is not among them.
DEMOTED_STATES = (STATE_REPORT_ONLY, STATE_SUSPENDED, STATE_ARCHIVED)


def enforcement_counts(checks: list[dict[str, Any]]) -> Counter[str]:
    """``{enforcement_state: count}`` over a project's checks (R23's activated/suspended tally)."""
    return Counter(str((chk.get("meta") or {}).get("enforcement_state") or UNKNOWN_STATE)
                   for chk in checks)


def gating_vs_demoted(counts: Counter[str]) -> tuple[int, int]:
    """``(gating, demoted)`` — the enforcement-decay ratio, counting as DEMOTED only the states an
    explicit demotion transition produces (:data:`DEMOTED_STATES`).

    A check in state ``unknown`` is NOT demoted: ``unknown`` means the fact carries no
    ``enforcement_state`` at all (a check authored before FL12's state machine, or by a path that
    never stamps one), and nothing demoted it — af-build still resolves and runs it by tag/surface
    query. Counting those as demoted inflated the one number an operator uses to judge whether
    enforcement is decaying, in the alarming direction, on exactly the projects whose checks are
    oldest. They are reported separately by :func:`unclassified_count`.

    Takes the already-computed :func:`enforcement_counts` tally rather than the raw checks, so a
    caller that also prints the per-state breakdown never counts the same list twice."""
    return counts.get(STATE_GATING, 0), sum(counts.get(state, 0) for state in DEMOTED_STATES)


def unclassified_count(counts: Counter[str]) -> int:
    """Checks in neither ``gating`` nor a demoted state — i.e. carrying no recorded enforcement
    state (or one outside FL12's vocabulary). Surfaced beside the ratio instead of being folded
    into either side of it, so an operator can see the tally is incomplete rather than be told a
    number that quietly assumed one answer."""
    known = (STATE_GATING, *DEMOTED_STATES)
    return sum(n for state, n in counts.items() if state not in known)


def check_undraftable_rate(checks: list[dict[str, Any]]) -> float:
    """Share of machine-drafted checks whose proof never went ``"proven"`` (R6/R23) — 0.0 with no
    machine-drafted checks yet, never a division error."""
    machine = [c for c in checks if (c.get("meta") or {}).get("channel") == "machine"]
    if not machine:
        return 0.0
    undraftable = sum(1 for c in machine if (c.get("meta") or {}).get("proof_status") != "proven")
    return undraftable / len(machine)


# --------------------------------------------------------------------------- R23: the run record

#: Each lifecycle event R23's run record asks for, and the meta timestamps that DATE it. These are
#: written by the ``ingestion_api`` verb that performs the transition (``promoted_at``,
#: ``suspended_at``, ``widened_at``, ``check_defeat_at``, ``rolled_back_at``, ``resurrected_at``,
#: ``upgraded_at``), so an event inside the window is a MEASURED event, not an inferred one.
CHECK_EVENT_TIMESTAMPS: dict[str, tuple[str, ...]] = {
    "activated": ("promoted_at", "upgraded_at", "resurrected_at"),
    "suspended": ("suspended_at",),
    "widened": ("widened_at",),
    "demoted": ("check_defeat_at",),
    "archived": ("rolled_back_at",),
}

#: The state each event family leaves a check in — used only to count checks that ARE in that state
#: but carry no timestamp for how they got there ("undated"). Those are reported, never silently
#: dropped into or out of the window.
CHECK_EVENT_STATES: dict[str, str] = {
    "activated": STATE_GATING,
    "suspended": STATE_SUSPENDED,
    "demoted": STATE_REPORT_ONLY,
    "archived": STATE_ARCHIVED,
}

#: Where a fact records when it was created, in preference order.
CREATION_TIMESTAMP_KEYS = ("createdAt", "created_at", "recorded_at", "at", "pinned_at")

#: What R23's "full run record" asks for that the corpus af-retro reads CANNOT supply. Printed
#: verbatim with every run record rather than being approximated: an operator must never read a
#: run record and believe a missing dimension was measured and came back zero.
RUN_RECORD_GAPS = (
    "run identity: no fact in the learnings corpus carries a run/session id, so `--since` scopes a "
    "TIME WINDOW, not a run. Two runs inside the window are reported as one.",
    "regressions: they live in each ticket's `regression_detail` in the project's own prd-<project> "
    "snapshot, which this CLI does not read (ingestion_api exposes no requirement reader).",
    "lessons are corpus-wide: a lesson fact carries no project attribution, so `lessons_ingested` "
    "counts the shared factory-learnings corpus, not this project alone.",
)

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smhdw])$")
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_since(raw: str) -> float:
    """Parse a ``--since`` value into an epoch-seconds floor: a duration back from now (``90m``,
    ``24h``, ``7d``, ``2w``), an epoch-seconds value, or an ISO-8601 timestamp. Raises
    ``ValueError`` on anything else rather than guessing a window."""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("empty --since value")
    match = _DURATION.match(text)
    if match:
        return time.time() - float(match.group(1)) * _DURATION_SECONDS[match.group(2)]
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise ValueError(f"unparseable --since value {raw!r}: expected a duration (24h, 7d), "
                         f"epoch seconds, or an ISO-8601 timestamp") from exc


def _event_time(meta: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """The most recent of ``keys`` recorded on ``meta``, or ``None`` when it records none of them."""
    stamps = []
    for key in keys:
        value = meta.get(key)
        if isinstance(value, (int, float)) and value:
            stamps.append(float(value))
    return max(stamps) if stamps else None


def run_record(checks: list[dict[str, Any]], flags: list[dict[str, Any]],
               lessons: list[dict[str, Any]], *, since: float | None = None) -> dict[str, Any]:
    """R23's run record over one project's corpus, scoped to events at/after ``since`` (``None`` =
    the project's whole history).

    Every number here is counted off a MEASURED timestamp the transition itself wrote. An event
    whose fact carries no timestamp cannot be placed in or out of the window, so it is counted in
    ``undated`` instead of being assumed either way — and :data:`RUN_RECORD_GAPS` names the two
    dimensions R23 asks for that this corpus cannot answer at all."""
    events: dict[str, int] = {}
    undated: dict[str, int] = {}
    for name, keys in CHECK_EVENT_TIMESTAMPS.items():
        in_window = 0
        no_date = 0
        state = CHECK_EVENT_STATES.get(name)
        for chk in checks:
            meta = chk.get("meta") or {}
            stamp = _event_time(meta, keys)
            if stamp is None:
                if state is not None and str(meta.get("enforcement_state") or "") == state:
                    no_date += 1
                continue
            if since is None or stamp >= since:
                in_window += 1
        events[name] = in_window
        undated[name] = no_date

    proof: Counter[str] = Counter()
    proof_undated = 0
    for chk in checks:
        meta = chk.get("meta") or {}
        stamp = _event_time(meta, CREATION_TIMESTAMP_KEYS)
        if stamp is None:
            proof_undated += 1
            continue
        if since is None or stamp >= since:
            proof[str(meta.get("proof_status") or "unrecorded")] += 1

    lessons_in_window = 0
    lessons_undated = 0
    for lesson in lessons:
        stamp = _event_time(lesson.get("meta") or {}, CREATION_TIMESTAMP_KEYS)
        if stamp is None:
            lessons_undated += 1
        elif since is None or stamp >= since:
            lessons_in_window += 1

    flags_in_window = sum(
        1 for f in flags
        if (lambda s: s is not None and (since is None or s >= since))(
            _event_time(f.get("meta") or {}, CREATION_TIMESTAMP_KEYS))
    )

    return {
        "since": since,
        "events": events,
        "undated": undated,
        "proof_outcomes": proof,
        "proof_outcomes_undated": proof_undated,
        "lessons_ingested": lessons_in_window,
        "lessons_undated": lessons_undated,
        "flags_raised": flags_in_window,
        "gaps": list(RUN_RECORD_GAPS),
    }


def _print_run_record(record: dict[str, Any]) -> None:
    since = record["since"]
    window = "all history" if since is None else f"since {datetime.fromtimestamp(since).isoformat(timespec='seconds')}"
    events = record["events"]
    undated = record["undated"]
    print(f"af-retro: run record ({window}) — "
          + " ".join(f"{name}={events[name]}" for name in CHECK_EVENT_TIMESTAMPS)
          + f" lessons_ingested={record['lessons_ingested']} flags_raised={record['flags_raised']}")
    proof = record["proof_outcomes"]
    print("af-retro: run record proof outcomes — "
          + (" ".join(f"{status}={n}" for status, n in sorted(proof.items())) or "none in window")
          + f" (undated checks excluded: {record['proof_outcomes_undated']})")
    stale = {k: v for k, v in undated.items() if v} | (
        {"lessons": record["lessons_undated"]} if record["lessons_undated"] else {})
    if stale:
        print("af-retro: run record UNDATED (carry no timestamp, so counted in NO window): "
              + " ".join(f"{k}={v}" for k, v in sorted(stale.items())))
    for gap in record["gaps"]:
        print(f"af-retro: run record GAP — {gap}")


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
        f"af-retro: checks for {args.project!r} — gating={counts.get(STATE_GATING, 0)} "
        f"report_only={counts.get(STATE_REPORT_ONLY, 0)} "
        f"suspended={counts.get(STATE_SUSPENDED, 0)} "
        f"archived={counts.get(STATE_ARCHIVED, 0)} (gating:demoted={gating}:{demoted}, "
        f"unclassified={unclassified_count(counts)}) check-undraftable-rate={rate:.0%}"
    )

    flags = read_flags(args.project, pending_only=False)
    # The exhaustive lesson enumeration, not the ranked top-k printed above: a run record counts
    # what was ingested, so a similarity cut-off would silently under-report it.
    _print_run_record(run_record(checks, flags, read_lessons(), since=args.since))

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
        return praxis_boundary("af-retro", lambda: _cmd_ack(args))

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
    ap.add_argument("--since", default=None,
                    help="scope the run record to events at/after this point: a duration back from "
                         "now (24h, 7d, 90m, 2w), epoch seconds, or an ISO-8601 timestamp. "
                         "Default: the project's whole history.")
    args = ap.parse_args(argv)

    if args.since is not None:
        try:
            args.since = parse_since(args.since)
        except ValueError as exc:
            ap.error(str(exc))

    if args.flags:
        return praxis_boundary("af-retro", lambda: _cmd_flags(args))
    if not args.project:
        ap.error("project is required unless --flags is given")
    return praxis_boundary("af-retro", lambda: _cmd_report(args))


if __name__ == "__main__":
    sys.exit(main())
