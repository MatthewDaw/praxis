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

  --target-bpb X      the run reached the metric's best value at or beyond X (direction-aware:
                      "beyond" means "<=" for a minimize metric, ">=" for a maximize one). Use
                      when a specific number matters.
  --min-improvement D the best kept value beat the BASELINE row by at least D, direction-aware.
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

Ledger versioning
------------------
``results.tsv`` has two recognized header shapes:

  version 0 (unversioned, legacy)  commit  val_bpb        memory_gb  status  description
  version 1 (generic metric)       commit  metric_value    memory_gb  status  description

The existing autoresearch loop writes version 0 unchanged: it always names the metric
``val_bpb``, so a version-0 ledger is scored assuming metric ``val_bpb``/direction
``minimize`` unless ``--model-record`` says otherwise. A version-1 ledger names its
metric column generically (``metric_value``) so any registered metric/direction --
including a ``maximize`` one -- can be scored; ``--model-record`` is required to know
which metric and which direction. Any other header is unrecognised and exits 2.

Model-record backstop
----------------------
``--model-record`` points at a JSON export of the model's registered fact (the shape
``praxis_get_fact`` returns: ``{"meta": {...}, "auditTrail": [...]}``). When given, the
check reads ``meta.metric``/``meta.direction`` from it instead of assuming ``val_bpb``/
``minimize``, and acts as the backstop against direct fact edits that bypass the
registry's write path: the write path's own actions never append an ``"edited"`` audit
entry (that action is what a direct ``praxis_edit_fact`` call on the fact leaves behind,
per ``knowledge/serve/facts_candidates.py``). Any ``"edited"`` entry after the record's
first (registration) entry means the frozen judging contract may have been tampered with
outside the write path, so the check FAILS CLOSED (exit 2) rather than trusting it.

Usage
-----
    af_ml_research_target.py --results <path-to-results.tsv> --min-experiments 20 \
        [--target-bpb 1.05 | --min-improvement 0.02] [--max-experiments 200]
        [--budget-exhausted-ok] [--model-record <path-to-model-fact.json>]

Exit codes: 0 = accepted, 1 = not yet met (regress and keep going), 2 = malformed input
or a failed-closed model record.

results.tsv is untracked by design (program.md step 7), so it survives the loop's
discard-reverts. Point --results at the checkout, not at anything inside the praxis repo.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass

MINIMIZE = "minimize"
MAXIMIZE = "maximize"
DIRECTIONS = (MINIMIZE, MAXIMIZE)

LEGACY_METRIC = "val_bpb"
GENERIC_METRIC_COLUMN = "metric_value"

# version -> expected header. Version 0 is the existing, unversioned ledger format --
# kept byte-identical so an in-flight loop's results.tsv keeps parsing unchanged.
HEADER_VERSIONS: dict[int, list[str]] = {
    0: ["commit", LEGACY_METRIC, "memory_gb", "status", "description"],
    1: ["commit", GENERIC_METRIC_COLUMN, "memory_gb", "status", "description"],
}


@dataclass
class Row:
    commit: str
    value: float
    status: str
    description: str


@dataclass
class ModelRecord:
    metric: str
    direction: str


def load_rows(path: str) -> tuple[int, list[Row]]:
    """Parse results.tsv, skipping crash rows (value is 0.000000 there, not a real score).

    Returns the ledger's header version (0 for the existing unversioned format) alongside
    its rows. Raises ``ValueError`` naming the header when it matches no known version.
    """
    with open(path, newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"{path} is empty -- expected at least a header row")
        stripped = [h.strip() for h in header]
        version = next((v for v, expected in HEADER_VERSIONS.items() if stripped == expected), None)
        if version is None:
            raise ValueError(f"{path} header is {header}, not a recognised ledger version")

        rows: list[Row] = []
        for lineno, raw in enumerate(reader, start=2):
            if not raw or all(not c.strip() for c in raw):
                continue
            if len(raw) < 5:
                raise ValueError(f"{path}:{lineno} has {len(raw)} columns, expected 5")
            try:
                value = float(raw[1])
            except ValueError:
                raise ValueError(f"{path}:{lineno} {stripped[1]!r} {raw[1]!r} is not a number")
            rows.append(Row(raw[0].strip(), value, raw[3].strip().lower(), raw[4].strip()))
    return version, rows


def load_model_record(path: str) -> ModelRecord:
    """Read a registered model's frozen judging contract, failing closed on tampering.

    ``path`` names a JSON export of the model's fact (``{"meta": {...}, "auditTrail": [...]}``,
    the shape ``praxis_get_fact`` returns). Raises ``ValueError`` when the record is
    malformed, missing ``metric``/``direction``, or its audit trail shows a mutation
    entry (``action == "edited"``) AFTER the first (registration) entry -- evidence the
    record was changed by a direct fact edit rather than through the registry's write path.
    """
    with open(path) as fh:
        record = json.load(fh)
    if not isinstance(record, dict):
        raise ValueError(f"{path} is not a JSON object")

    meta = record.get("meta")
    if not isinstance(meta, dict):
        raise ValueError(f"{path} has no 'meta' object")

    metric = meta.get("metric")
    direction = meta.get("direction")
    if not metric or direction not in DIRECTIONS:
        raise ValueError(
            f"{path} meta must carry 'metric' and a 'direction' in {DIRECTIONS}, got "
            f"metric={metric!r} direction={direction!r}"
        )

    trail = record.get("auditTrail")
    if isinstance(trail, list):
        mutations = [
            entry for entry in trail[1:]
            if isinstance(entry, dict) and entry.get("action") == "edited"
        ]
        if mutations:
            raise ValueError(
                f"{path} audit trail shows {len(mutations)} direct fact edit(s) after "
                f"registration -- refusing to trust a judging contract that bypassed the "
                f"write path"
            )

    return ModelRecord(metric=str(metric), direction=str(direction))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True, help="path to the run's results.tsv")
    ap.add_argument("--min-experiments", type=int, default=10,
                    help="rows required before any pass is possible (default 10)")
    ap.add_argument("--target-bpb", type=float, help="accept when the best value reaches this")
    ap.add_argument("--min-improvement", type=float,
                    help="accept when the best value beats the baseline row by at least this")
    ap.add_argument("--max-experiments", type=int,
                    help="experiment budget; see --budget-exhausted-ok")
    ap.add_argument("--budget-exhausted-ok", action="store_true",
                    help="accept a miss once --max-experiments rows exist, recording the best result")
    ap.add_argument("--model-record", help="path to the model's registered fact JSON (metric/direction)")
    args = ap.parse_args(argv)

    if (args.target_bpb is None) == (args.min_improvement is None):
        print("FAIL: pass exactly one of --target-bpb or --min-improvement", file=sys.stderr)
        return 2

    try:
        version, rows = load_rows(args.results)
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    if args.model_record is not None:
        try:
            model = load_model_record(args.model_record)
        except (OSError, ValueError) as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 2
        metric_name, direction = model.metric, model.direction
    else:
        metric_name, direction = LEGACY_METRIC, MINIMIZE

    if version == 0 and metric_name != LEGACY_METRIC:
        print(
            f"FAIL: {args.results} is the unversioned (version 0) ledger, which is always "
            f"{LEGACY_METRIC!r}, but the model record names metric {metric_name!r}",
            file=sys.stderr,
        )
        return 2

    scored = [r for r in rows if r.status != "crash"]
    if not scored:
        print(f"FAIL: {args.results} has {len(rows)} row(s), none of them scored (all crashes)")
        return 1

    baseline = scored[0]
    minimizing = direction == MINIMIZE
    best = min(scored, key=lambda r: r.value) if minimizing else max(scored, key=lambda r: r.value)
    improvement = (baseline.value - best.value) if minimizing else (best.value - baseline.value)

    print(f"ledger:      version {version}, metric {metric_name!r}, direction {direction!r}")
    print(f"experiments: {len(rows)} logged, {len(scored)} scored, {len(rows) - len(scored)} crashed")
    print(f"baseline:    {baseline.value:.6f}  ({baseline.commit} {baseline.description})")
    print(f"best:        {best.value:.6f}  ({best.commit} {best.description})")
    print(f"improvement: {improvement:+.6f}")

    if len(rows) < args.min_experiments:
        print(f"FAIL: {len(rows)} experiments < --min-experiments {args.min_experiments}")
        return 1

    if args.target_bpb is not None:
        met = best.value <= args.target_bpb if minimizing else best.value >= args.target_bpb
        goal = f"best {'<=' if minimizing else '>='} {args.target_bpb}"
    else:
        met, goal = improvement >= args.min_improvement, f"improvement >= {args.min_improvement}"

    if met:
        print(f"PASS: {goal} satisfied")
        return 0

    if args.max_experiments is not None and len(rows) >= args.max_experiments:
        if args.budget_exhausted_ok:
            print(f"PASS: budget of {args.max_experiments} experiments exhausted without {goal}; "
                  f"best recorded result is {best.value:.6f} ({best.commit})")
            return 0
        print(f"FAIL: budget of {args.max_experiments} experiments exhausted and {goal} not met "
              f"(pass --budget-exhausted-ok if a miss should still close the ticket)")
        return 1

    print(f"FAIL: {goal} not met yet -- keep running")
    return 1


if __name__ == "__main__":
    sys.exit(main())
