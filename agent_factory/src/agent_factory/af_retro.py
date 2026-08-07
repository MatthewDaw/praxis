"""``af-retro`` — the operator's detail view onto the failure-learning loop (R23).

FL1 ships only the foundation this CLI needs to exist and answer for real: the shared
``factory-learnings`` space it reads from is live (see ``agent_factory.ingestion_api``), so this
first cut prints what is CURRENTLY knowable — the lessons a project can see through the read-only
mount. The full per-run record (regressions, checks activated/suspended/widened, proof outcomes,
check-undraftable rate, gating-vs-demoted ratio, push-notification flags) is R23/R24's job and
lands with FL18, which extends this module rather than replacing it.
"""

from __future__ import annotations

import argparse
import sys

from agent_factory.ingestion_api import read_lessons


def _cmd_lessons(args: argparse.Namespace) -> int:
    hits = read_lessons(args.project, top_k=args.top_k)
    if not hits:
        print(f"af-retro: no lessons visible for {args.project!r} yet.")
        return 0
    print(f"af-retro: lessons visible for {args.project!r} (top {len(hits)}):")
    for hit in hits:
        print(f"  {hit.get('id', '')}\t{hit.get('text', '')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="af-retro",
        description="Print a project's failure-learning-loop record from Praxis. "
                    "Today: lessons visible through the shared factory-learnings space (FL1). "
                    "The full ingestion/lifecycle record (R23) lands with FL18.")
    ap.add_argument("project", help="the project name to report on")
    ap.add_argument("--top-k", type=int, default=10, dest="top_k")
    ap.set_defaults(func=_cmd_lessons)
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
