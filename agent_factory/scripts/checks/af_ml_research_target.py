#!/usr/bin/env python3
"""External-signal check for an af-build ticket whose work is an autoresearch run.

An ML research ticket cannot be verified the way a normal ticket is. There is no
"the feature works" condition -- the loop is open-ended by construction, and program.md
explicitly runs until a human stops it. But af-build requires a BINARY acceptance
condition decided by an external signal, never by the agent's own judgment
(af-build/SKILL.md, step 6). This check is that signal: it reads ``results.tsv``, which
the loop writes and which no agent can fake into passing without actually having run
experiments that improved the metric.

The metric is read from the ledger, not from the agent's report. That is the whole point.

Doneness has two accepting forms, and which one applies is the ticket author's choice:

  --target-bpb X      the run reached val_bpb <= X. Use when a specific number matters.
  --min-improvement D the best kept val_bpb beat the BASELINE row by at least D.
                      Use when the goal is "make progress", which is the honest framing
                      for most research tickets.

Both are additionally gated on ``--min-experiments`` so a ticket cannot pass on a lucky
first row, and both REQUIRE a baseline row (the first row, per program.md's "your very
first run should always be to establish the baseline").

Exhausting ``--max-experiments`` without hitting the target is a PASS only with
``--budget-exhausted-ok``. That flag exists because a research ticket that can only ever
pass will otherwise wedge the whole build set behind af-build's completeness gate --
the loop is not guaranteed to find an improvement, and "we ran N experiments and the
best result was X" is a legitimate research outcome. Without the flag, a miss fails and
the ticket is regressed for another run. The author picks which risk they want; there is
no safe default, so neither is silent.

Usage
-----
    af_ml_research_target.py --results <path-to-results.tsv> --min-experiments 20 \
        [--target-bpb 1.05 | --min-improvement 0.02] [--max-experiments 200]
        [--budget-exhausted-ok]

Exit codes: 0 = accepted, 1 = not yet met (regress and keep going), 2 = malformed input.

results.tsv is untracked by design (program.md step 7), so it survives the loop's
discard-reverts. Point --results at the checkout, not at anything inside the praxis repo.
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass

EXPECTED_HEADER = ["commit", "val_bpb", "memory_gb", "status", "description"]


@dataclass
class Row:
    commit: str
    val_bpb: float
    status: str
    description: str


def load_rows(path: str) -> list[Row]:
    """Parse results.tsv, skipping crash rows (val_bpb is 0.000000 there, not a real score)."""
    with open(path, newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"{path} is empty -- expected at least a header row")
        if [h.strip() for h in header] != EXPECTED_HEADER:
            raise ValueError(f"{path} header is {header}, expected {EXPECTED_HEADER}")

        rows: list[Row] = []
        for lineno, raw in enumerate(reader, start=2):
            if not raw or all(not c.strip() for c in raw):
                continue
            if len(raw) < 5:
                raise ValueError(f"{path}:{lineno} has {len(raw)} columns, expected 5")
            try:
                bpb = float(raw[1])
            except ValueError:
                raise ValueError(f"{path}:{lineno} val_bpb {raw[1]!r} is not a number")
            rows.append(Row(raw[0].strip(), bpb, raw[3].strip().lower(), raw[4].strip()))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True, help="path to the run's results.tsv")
    ap.add_argument("--min-experiments", type=int, default=10,
                    help="rows required before any pass is possible (default 10)")
    ap.add_argument("--target-bpb", type=float, help="accept when best val_bpb <= this")
    ap.add_argument("--min-improvement", type=float,
                    help="accept when best val_bpb beats the baseline row by at least this")
    ap.add_argument("--max-experiments", type=int,
                    help="experiment budget; see --budget-exhausted-ok")
    ap.add_argument("--budget-exhausted-ok", action="store_true",
                    help="accept a miss once --max-experiments rows exist, recording the best result")
    args = ap.parse_args(argv)

    if (args.target_bpb is None) == (args.min_improvement is None):
        print("FAIL: pass exactly one of --target-bpb or --min-improvement", file=sys.stderr)
        return 2

    try:
        rows = load_rows(args.results)
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    scored = [r for r in rows if r.status != "crash"]
    if not scored:
        print(f"FAIL: {args.results} has {len(rows)} row(s), none of them scored (all crashes)")
        return 1

    baseline = scored[0]
    best = min(scored, key=lambda r: r.val_bpb)
    improvement = baseline.val_bpb - best.val_bpb

    print(f"experiments: {len(rows)} logged, {len(scored)} scored, {len(rows) - len(scored)} crashed")
    print(f"baseline:    {baseline.val_bpb:.6f}  ({baseline.commit} {baseline.description})")
    print(f"best:        {best.val_bpb:.6f}  ({best.commit} {best.description})")
    print(f"improvement: {improvement:+.6f}")

    if len(rows) < args.min_experiments:
        print(f"FAIL: {len(rows)} experiments < --min-experiments {args.min_experiments}")
        return 1

    if args.target_bpb is not None:
        met, goal = best.val_bpb <= args.target_bpb, f"val_bpb <= {args.target_bpb}"
    else:
        met, goal = improvement >= args.min_improvement, f"improvement >= {args.min_improvement}"

    if met:
        print(f"PASS: {goal} satisfied")
        return 0

    if args.max_experiments is not None and len(rows) >= args.max_experiments:
        if args.budget_exhausted_ok:
            print(f"PASS: budget of {args.max_experiments} experiments exhausted without {goal}; "
                  f"best recorded result is {best.val_bpb:.6f} ({best.commit})")
            return 0
        print(f"FAIL: budget of {args.max_experiments} experiments exhausted and {goal} not met "
              f"(pass --budget-exhausted-ok if a miss should still close the ticket)")
        return 1

    print(f"FAIL: {goal} not met yet -- keep running")
    return 1


if __name__ == "__main__":
    sys.exit(main())
