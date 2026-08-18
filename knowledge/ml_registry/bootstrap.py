"""Bring a project from "has a ledger" to "has a registered campaign", in one call.

Every ML project that wants af-ml-supervise must satisfy the same four preconditions, and before
this module each one re-derived them by hand. That is how the sports_analysis campaign lost an
afternoon: its ledger accumulated 16 rows sharing 2 join keys, which the registry cannot
adjudicate at all, and the mistake only surfaced at registration time -- after the training runs
that produced those rows had been paid for.

What is SYSTEMATIC lives here:

* verifying the ledger is version 2 and that its join keys are unique per arm
* verifying enough baseline rows exist, and MEASURING the noise floor from them
* building a schema-valid model meta and schema-valid idea records
* refusing, with a specific cause, rather than registering something unadjudicable

What is PROJECT-SPECIFIC stays with the project: what the metric means, what trains it, and which
hypotheses are worth trying. Those cannot be generalised and this module does not try.

THE NOISE FLOOR DESERVES ITS OWN NOTE, because it is the single most consequential constant in a
campaign and the cheapest thing to get wrong. The registry's minimum is 4 baseline runs. An SD
estimated from 4 points carries roughly 40% relative uncertainty, and on the sports_analysis
campaign 4 runs suggested SD 0.0164 while 12 gave 0.0115 -- the small sample was inflated by an
artifact that only the larger one exposed. So `measure_noise_floor` reports the sample size and
the uncertainty alongside the value, and `sigmas` defaults to 2 rather than 1: at one sigma a
35-arm backlog is expected to manufacture roughly five winners from optimiser noise alone.
"""

from __future__ import annotations

import csv
import json
import statistics as st
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LEDGER_V2_HEADER = ["commit", "metric_value", "memory_gb", "status", "description",
                    "throughput", "diff_lines"]
REQUIRED_BASELINE_RUN_COUNT = 4
DEFAULT_SIGMAS = 2.0


@dataclass
class Precondition:
    name: str
    ok: bool
    detail: str


@dataclass
class BootstrapReport:
    ready: bool
    preconditions: list[Precondition] = field(default_factory=list)
    model_meta: dict[str, Any] | None = None
    ideas: list[dict[str, Any]] = field(default_factory=list)
    floor: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "preconditions": [p.__dict__ for p in self.preconditions],
            "blocking": [p.name for p in self.preconditions if not p.ok],
            "noise_floor": self.floor,
            "model_meta": self.model_meta,
            "ideas": len(self.ideas),
        }


def read_ledger(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        header = [c.strip() for c in next(reader)]
        rows = [dict(zip(header, r)) for r in reader if r and any(c.strip() for c in r)]
    return header, rows


def check_ledger(path: Path, baseline_prefix: str) -> tuple[list[Precondition], list[dict]]:
    """Every ledger precondition, each failing with the specific cause and its fix."""
    out: list[Precondition] = []
    if not path.is_file():
        return [Precondition("ledger_exists", False, f"no ledger at {path}")], []

    header, rows = read_ledger(path)
    out.append(Precondition(
        "ledger_is_v2", header == LEDGER_V2_HEADER,
        "header matches version 2" if header == LEDGER_V2_HEADER else
        f"header is {header}, expected {LEDGER_V2_HEADER}. Versions 0/1 lack throughput and "
        "diff_lines, and synthesising them turns two of adjudicate_verdict's four verdicts into "
        "dead code -- add the columns to the loop that WRITES the ledger, never by hand."))

    keys = [r.get("commit", "") for r in rows]
    unique = len(set(keys))
    out.append(Precondition(
        "join_keys_unique", unique == len(keys) and bool(keys),
        f"{unique} distinct keys over {len(keys)} rows" + ("" if unique == len(keys) else
        " -- the registry joins trials to rows BY THIS COLUMN, so a campaign that varies arms by "
        "CONFIG rather than by code collapses the join. Write '{sha}:{arm_tag}' instead of a bare "
        "SHA.")))

    baselines = [r for r in rows if r.get("description", "").startswith(baseline_prefix)]
    out.append(Precondition(
        "enough_baseline_rows", len(baselines) >= REQUIRED_BASELINE_RUN_COUNT,
        f"{len(baselines)} baseline rows (need >= {REQUIRED_BASELINE_RUN_COUNT}); "
        f"matched on description prefix {baseline_prefix!r}"))

    tputs = {round(float(b["throughput"]), 2) for b in baselines if b.get("throughput")}
    out.append(Precondition(
        "baseline_throughput_homogeneous", len(tputs) <= 1 or (max(tputs) / min(tputs) < 1.5),
        f"baseline throughputs {sorted(tputs)}" + ("" if len(tputs) <= 1 else
        " -- baseline_throughput gates the VOIDED verdict, so baselines measured under different "
        "settings make that gate meaningless."), ))
    return out, baselines


def measure_noise_floor(baselines: list[dict], sigmas: float = DEFAULT_SIGMAS) -> dict[str, Any]:
    values = [float(b["metric_value"]) for b in baselines]
    sd = st.stdev(values) if len(values) > 1 else 0.0
    # Relative uncertainty of an SD estimated from n points, ~1/sqrt(2(n-1)).
    rel_unc = (1.0 / (2 * (len(values) - 1))) ** 0.5 if len(values) > 1 else float("inf")
    return {
        "n_baseline_runs": len(values),
        "mean": round(st.mean(values), 6) if values else None,
        "sd": round(sd, 6),
        "sd_relative_uncertainty": round(rel_unc, 3),
        "sigmas": sigmas,
        "noise_floor": round(sigmas * sd, 6),
        "note": ("SD from few runs is itself uncertain; prefer >= 12 runs when a run is cheap. "
                 "sigmas defaults to 2 because at one sigma a large backlog manufactures winners "
                 "from optimiser noise."),
    }


def build_model_meta(*, metric: str, direction: str, baseline_commit: str, noise_floor: float,
                     baseline_throughput: float, diff_size_limit: int,
                     win_condition: str = "beats baseline by noise_floor",
                     notes: str | None = None) -> dict[str, Any]:
    meta = {
        "metric": metric, "direction": direction, "win_condition": win_condition,
        "baseline": baseline_commit, "noise_floor": noise_floor,
        "baseline_throughput": baseline_throughput, "diff_size_limit": diff_size_limit,
    }
    if notes:
        meta["notes"] = notes
    return meta


def build_ideas(backlog: list[dict[str, Any]], *, model_id: str,
                skip_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """Backlog records -> schema-valid idea records.

    Convention over configuration: a backlog entry needs `id` and `axis`, and either a
    `description` or a `hypothesis` (optionally with `basis`). The description keeps the hypothesis
    AND its basis together on purpose -- a rejected idea is only useful later if the reason it was
    worth trying survives next to the verdict, otherwise the rejection memory degrades into a list
    of names nobody can re-evaluate.
    """
    skip = skip_ids or set()
    out = []
    for r in backlog:
        if r.get("id") in skip:
            continue
        desc = r.get("description")
        if not desc:
            desc = r.get("hypothesis", "")
            if r.get("basis"):
                desc = f"{desc} BASIS: {r['basis']}"
        out.append({
            "model_id": model_id,
            "origin": r.get("origin", "seeded"),
            "axis": r["axis"],
            "description": f"{r['id']}: {desc}" if r.get("id") else desc,
            **{k: v for k, v in r.items() if k not in {"axis", "description", "hypothesis",
                                                       "basis", "origin"}},
        })
    return out


def bootstrap(*, ledger: Path, backlog: list[dict[str, Any]], model_id: str, metric: str,
              direction: str, diff_size_limit: int, baseline_prefix: str = "baseline",
              sigmas: float = DEFAULT_SIGMAS, noise_floor_override: float | None = None,
              skip_ids: set[str] | None = None, notes: str | None = None) -> BootstrapReport:
    checks, baselines = check_ledger(ledger, baseline_prefix)
    if not all(c.ok for c in checks):
        return BootstrapReport(ready=False, preconditions=checks)

    floor = measure_noise_floor(baselines, sigmas=sigmas)
    if noise_floor_override is not None:
        floor["noise_floor_measured_here"] = floor["noise_floor"]
        floor["noise_floor"] = noise_floor_override
        floor["override_reason"] = "caller supplied a floor measured over more runs than the ledger holds"

    # The baseline is the row CLOSEST TO THE MEAN, not the best one, and the distinction is the
    # difference between two paradigms that share this file's vocabulary.
    #
    # In autoresearch every ledger row is a DIFFERENT code state, so "best so far" is exactly the
    # right baseline -- it is the incumbent. Here the rows are REPEATS OF ONE CONFIGURATION; that
    # is the entire reason this function demands at least four of them and measures an SD across
    # them. Taking the max of repeats selects on noise: for four normal draws E[max] is about
    # mu + 1.03*sigma, so the registered incumbent is systematically better than the system it
    # claims to describe, and the whole campaign is measured against a bar the baseline config
    # cannot itself reliably clear.
    #
    # Two costs, both real. Statistical: an arm with a true effect of exactly one noise floor now
    # has to clear roughly 1.6 floors, so genuine winners get rejected. Reportorial: "our baseline
    # is X" overstates, because X was chosen for being the luckiest of four.
    #
    # This was internally inconsistent besides -- baseline_throughput below already takes the
    # MEDIAN over these same rows, on the same reasoning. Observed on the first campaign to run
    # this path: 4 rows at 0.6700-0.6811 registered the 0.6811 one, 0.6 sigma above their mean.
    #
    # The mean itself cannot be the answer because meta.baseline names a COMMIT that must join to
    # a real ledger row, so this picks the row that best represents the central tendency. Ties
    # break on the first row, which is deterministic given a stable ledger order.
    mean_value = st.fmean(float(b["metric_value"]) for b in baselines)
    best = min(baselines, key=lambda b: abs(float(b["metric_value"]) - mean_value))
    meta = build_model_meta(
        metric=metric, direction=direction, baseline_commit=best["commit"],
        noise_floor=floor["noise_floor"],
        baseline_throughput=round(st.median(float(b["throughput"]) for b in baselines), 4),
        diff_size_limit=diff_size_limit, notes=notes)
    return BootstrapReport(ready=True, preconditions=checks, model_meta=meta,
                           ideas=build_ideas(backlog, model_id=model_id, skip_ids=skip_ids),
                           floor=floor)
