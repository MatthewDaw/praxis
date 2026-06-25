"""Cost-to-correct orchestration + go-gate for the PR-knowledge dogfood experiment (v2).

v1 gated on first-pass token *volume*, which is biased toward the no-knowledge control:
it credits the control's *wrong* output as free. v2 gates on **cost-to-correct** instead —
knowledge delivered upfront (treatment, one pass) vs the same knowledge delivered late as
review feedback (control's first pass **+** a corrective rework turn when its output is
wrong) — scored in ``cost_usd``, alongside the footgun-flip signal.

  uv run python knowledge/evals/cases/dom/pr_knowledge_dogfood/analyze.py --trials 3

Per task, per trial, it runs the treatment, the control, and (for footgun/convention tasks
whose control came out wrong) a rework turn that mounts the control's wrong file and supplies
the fact as review feedback. The pure functions (:func:`aggregate`, :func:`evaluate_gate`)
operate on plain per-trial records, so they unit-test offline against committed fixtures.

Orchestration runs the real ``ClaudeCodeRunner`` in-process (``run_case_full``) — not the
``python -m knowledge.evals.run`` CLI, which calls ``write_baseline`` and would clobber the
committed ``results/baseline.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _ensure_repo_on_path() -> None:
    """Allow ``python <suite>/analyze.py`` (run-by-path) to import ``knowledge``."""
    for parent in HERE.parents:
        if (parent / "pyproject.toml").exists():
            if str(parent) not in sys.path:
                sys.path.insert(0, str(parent))
            return


_ensure_repo_on_path()

from knowledge.evals.claude_code import _claude_usage  # noqa: E402

# Each task: its kind, the check that PASSES on a CORRECT artifact (footgun-absent /
# convention-present; None for a pure quantitative task), the late "review feedback"
# prompt used to rework a wrong control output, and the graded output file.
TASKS: dict[str, dict] = {
    "umap_neighbors": {
        "kind": "footgun",
        "correct_check": ("knowledge.evals.deterministic_checks.text:regex_absent",
                          {"pattern": r"n_neighbors\s*=\s*(?:min\()?\s*15\b"}),
        "rework_prompt": "Code review feedback: the topic-collapse bug is UMAP's n_neighbors — "
                         "at 15 it over-weights global structure and melts heterogeneous corpora "
                         "into one cluster. Lower it to min(10, n - 1) in clustering.py. Edit only "
                         "clustering.py.",
        "output_file": "clustering.py",
    },
    "yoyo_lazy_import": {
        "kind": "footgun",
        "correct_check": ("knowledge.evals.deterministic_checks.text:regex_absent",
                          {"pattern": r"(?m)^(?:from|import)\s+knowledge\b"}),
        "rework_prompt": "Code review feedback: yoyo execs migration files with the repo root off "
                         "sys.path, so a top-level `from knowledge...` import raises ModuleNotFoundError "
                         "before the step runs. Move the knowledge import inside the step function. "
                         "Edit only 0002_backfill_fact_source.py.",
        "output_file": "0002_backfill_fact_source.py",
    },
    "supersedes_edge": {
        "kind": "convention",
        "correct_check": ("knowledge.evals.deterministic_checks.builds:contains_text",
                          {"text": "supersedes"}),
        "rework_prompt": "Code review feedback: in this codebase the directional replacement edge "
                         "MUST be named `supersedes` (project convention), not whatever name you "
                         "chose. Update supersede_fact.py to use the `supersedes` edge name. Edit "
                         "only supersede_fact.py.",
        "output_file": "supersede_fact.py",
    },
    "repo_mounted_dsn": {
        "kind": "quantitative",  # exploration-savings; no wrong-artifact footgun -> no rework
        "correct_check": None,
        "rework_prompt": None,
        "output_file": "count_active_facts.py",
    },
}


def control_id(task: str) -> str:
    return f"{task}_before"


# A footgun "flips" only if the control reliably EXHIBITS it. The README's validity
# discipline sets that bar at ~2/3 (a control that dodges the footgun blind proves
# nothing). At n=3 this equals "majority"; the constant keeps it honest at n>=4.
_EXHIBIT_BAR = 2 / 3


def _should_rework(correct_check, control_correct) -> bool:
    """Charge a rework turn iff the task has a correctness notion AND the control got it wrong.

    ``is False`` is identity-strict on purpose: a quantitative task (``correct_check is None``)
    and a correct *or* indeterminate control (``True`` / ``None``) all skip the rework.
    """
    return correct_check is not None and control_correct is False


# --------------------------------------------------------------------------- #
# Pure aggregation over per-trial records
# --------------------------------------------------------------------------- #
# A record is: {task, kind,
#   treat:   {cost, turns, tokens, correct|None},
#   control: {cost, turns, tokens, correct|None},
#   rework:  {cost} | None}      # present only when the control was wrong (=> charged)


def _mean_sd(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    return statistics.fmean(values), (statistics.stdev(values) if len(values) > 1 else 0.0)


def _rate(flags: list[bool | None]) -> float | None:
    present = [bool(f) for f in flags if f is not None]
    return (sum(present) / len(present)) if present else None


def aggregate(records: list[dict]) -> dict:
    """Per-task cost-to-correct deltas + footgun flips. Errors force NO-GO downstream."""
    by_task: dict[str, list[dict]] = {}
    for r in records:
        by_task.setdefault(r["task"], []).append(r)

    tasks: dict[str, dict] = {}
    errors: list[str] = []
    for task, cfg in TASKS.items():
        trials = by_task.get(task, [])
        if not trials:
            errors.append(f"{task}: no trials")
            continue

        treat_costs = [t["treat"]["cost"] for t in trials if t["treat"]["cost"] is not None]
        # cost-to-correct: control first-pass + rework (when the control was charged for a fix)
        ctc_costs = [
            t["control"]["cost"] + ((t.get("rework") or {}).get("cost") or 0.0)
            for t in trials if t["control"]["cost"] is not None
        ]
        if not treat_costs or not ctc_costs:
            errors.append(f"{task}: missing cost data in an arm")
            continue

        treat_cost_mean, treat_cost_sd = _mean_sd(treat_costs)
        ctc_cost_mean, ctc_cost_sd = _mean_sd(ctc_costs)
        treat_turns_mean, _ = _mean_sd([float(t["treat"]["turns"]) for t in trials if t["treat"]["turns"] is not None])
        ctrl_turns_mean, _ = _mean_sd([float(t["control"]["turns"]) for t in trials if t["control"]["turns"] is not None])

        kind = cfg["kind"]
        # Footgun tasks always carry a correct_check, so these rates are defined for them;
        # only the quantitative task (no check) yields None, and it is never a footgun.
        treat_correct_rate = _rate([t["treat"]["correct"] for t in trials])
        control_correct_rate = _rate([t["control"]["correct"] for t in trials])
        # "exhibits the footgun" = the control's output is NOT correct
        control_exhibit_rate = None if control_correct_rate is None else 1.0 - control_correct_rate

        is_footgun = kind == "footgun"
        flip = bool(
            is_footgun and treat_correct_rate is not None and control_exhibit_rate is not None
            and treat_correct_rate >= 0.5 and control_exhibit_rate >= _EXHIBIT_BAR
        )

        tasks[task] = {
            "kind": kind,
            "is_footgun": is_footgun,
            "treat_cost_mean": treat_cost_mean,
            "treat_cost_sd": treat_cost_sd,
            "ctc_cost_mean": ctc_cost_mean,
            "ctc_cost_sd": ctc_cost_sd,
            "cost_delta": treat_cost_mean - ctc_cost_mean,  # negative => treatment cheaper to-correct
            "cost_reduced": treat_cost_mean < ctc_cost_mean,
            "treat_turns_mean": treat_turns_mean,
            "control_turns_mean": ctrl_turns_mean,
            "treat_correct_rate": treat_correct_rate,
            "control_exhibit_rate": control_exhibit_rate,
            "flip": flip,
            "trials": len(trials),
            "reworked": sum(1 for t in trials if t.get("rework")),
        }
    return {"tasks": tasks, "errors": errors}


def evaluate_gate(report: dict) -> dict:
    """GO iff every footgun task flips AND cost-to-correct drops on a majority of tasks."""
    tasks, errors = report["tasks"], list(report["errors"])
    footgun_tasks = [n for n, t in tasks.items() if t["is_footgun"]]
    flips = {n: tasks[n]["flip"] for n in footgun_tasks}
    all_flip = bool(footgun_tasks) and all(flips.values())

    # Cost-reduction is scored over ALL tasks, including the quantitative repo-mounted one:
    # it is a task where knowledge was *supposed* to cut cost, so an honest null there counts
    # against the bet rather than being excluded to make the gate easier.
    reduced = {n: t["cost_reduced"] for n, t in tasks.items()}
    n_reduced = sum(reduced.values())
    most_reduced = bool(tasks) and n_reduced > len(tasks) / 2

    go = all_flip and most_reduced and not errors
    reasons = []
    if not footgun_tasks:
        reasons.append("no footgun task present")
    if footgun_tasks and not all_flip:
        reasons.append("footgun did not flip on: " + ", ".join(n for n, f in flips.items() if not f))
    if not most_reduced:
        reasons.append(f"cost-to-correct dropped on only {n_reduced}/{len(tasks)} tasks (need a majority)")
    if errors:
        reasons.append("data errors: " + "; ".join(errors))
    return {
        "verdict": "GO" if go else "NO-GO",
        "all_footgun_flip": all_flip,
        "flips": flips,
        "tasks_reduced": n_reduced,
        "tasks_total": len(tasks),
        "reasons": reasons if not go else [],
    }


# --------------------------------------------------------------------------- #
# Live orchestration (real Claude Code, in-process)
# --------------------------------------------------------------------------- #
def _usage(ctx) -> dict:
    return _claude_usage(getattr(ctx, "raw_response", None) or "")


def _cost(ctx) -> float | None:
    return _usage(ctx).get("cost_usd")


def _turns(ctx) -> int | None:
    return _usage(ctx).get("num_turns")


def _tokens(ctx) -> int | None:
    u = _usage(ctx)
    i, o = u.get("input_tokens"), u.get("output_tokens")
    return None if i is None and o is None else (i or 0) + (o or 0)


def _check_passes(ctx, correct_check) -> bool | None:
    if correct_check is None:
        return None
    from knowledge.evals.eval_def import DeterministicCheckRef
    from knowledge.evals.run import resolve_check
    ref_str, params = correct_check
    ref = DeterministicCheckRef(name="correct", ref=ref_str, params=params)
    return resolve_check(ref)(ctx, **ref.params).passed


def _measure(ctx, cfg) -> dict:
    return {"cost": _cost(ctx), "turns": _turns(ctx), "tokens": _tokens(ctx),
            "correct": _check_passes(ctx, cfg["correct_check"])}


def _rework_case(task: str, cfg: dict, wrong_output: str):
    from knowledge.evals.eval_def import DeterministicCheckRef, EvalCase
    ref_str, params = cfg["correct_check"]
    tmp = Path(tempfile.mkdtemp(prefix=f"rework-{task}-"))
    (tmp / "fixtures").mkdir()
    (tmp / "fixtures" / cfg["output_file"]).write_text(wrong_output, encoding="utf-8")
    return EvalCase(
        id=f"{task}_rework", needs=["sandbox", "file_io"], seed_prompt=cfg["rework_prompt"],
        target_commit="0" * 40, output_file=cfg["output_file"],
        deterministic_checks=[DeterministicCheckRef(name="correct", ref=ref_str, params=params)],
        source_dir=str(tmp),
    )


def run_experiment(trials: int, workers: int = 1, tasks: dict | None = None) -> list[dict]:
    """Run treatment + control (+ rework when the control is wrong) for each task/trial."""
    from knowledge.evals.run import load_cases, run_case_full, select_runner

    tasks = tasks or TASKS
    cases = {c.id: c for c in load_cases()}
    missing = [cid for t in tasks for cid in (t, control_id(t)) if cid not in cases]
    if missing:
        raise SystemExit(f"unknown case id(s): {missing}")
    runner, judge = select_runner("claude")
    jobs = [(t, n) for t in tasks for n in range(trials)]

    def _one(job: tuple[str, int]) -> dict:
        task, n = job
        cfg = tasks[task]
        tctx, _, tverdict = run_case_full(cases[task], runner, judge=judge)
        treat = _measure(tctx, cfg)
        cctx, _, _ = run_case_full(cases[control_id(task)], runner, judge=judge)
        control = _measure(cctx, cfg)
        rework = None
        if _should_rework(cfg["correct_check"], control["correct"]):
            rctx, _, _ = run_case_full(_rework_case(task, cfg, cctx.output), runner, judge=judge)
            rework = {"cost": _cost(rctx), "fixed": _check_passes(rctx, cfg["correct_check"])}
        print(f"  {task} trial {n + 1}/{trials}: treat_ok={treat['correct']} "
              f"ctrl_ok={control['correct']} reworked={bool(rework)}", flush=True)
        return {"task": task, "kind": cfg["kind"], "treat": treat, "control": control, "rework": rework}

    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(_one, jobs))
    return [_one(job) for job in jobs]


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _f(v, money: bool = False) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return str(v)
    return f"${v:.4f}" if money else (f"{v:.2f}" if isinstance(v, float) else str(v))


def format_report(report: dict, gate: dict) -> str:
    lines = ["=== PR-knowledge dogfood v2 - cost-to-correct ===", ""]
    for task, t in report["tasks"].items():
        lines.append(f"[{task}] ({t['kind']}; {t['trials']} trials, {t['reworked']} control reworks)")
        lines.append(
            f"  cost    treat={_f(t['treat_cost_mean'], 1)}+/-{_f(t['treat_cost_sd'], 1)}  "
            f"control_to_correct={_f(t['ctc_cost_mean'], 1)}+/-{_f(t['ctc_cost_sd'], 1)}  "
            f"delta={_f(t['cost_delta'], 1)} ({'cheaper' if t['cost_reduced'] else 'NOT cheaper'})"
        )
        lines.append(f"  turns   treat={_f(t['treat_turns_mean'])}  control={_f(t['control_turns_mean'])}")
        if t["is_footgun"]:
            lines.append(f"  footgun treat_avoid_rate={_f(t['treat_correct_rate'])}  "
                         f"control_exhibit_rate={_f(t['control_exhibit_rate'])}  flip={t['flip']}")
        lines.append("")
    if report["errors"]:
        lines.append("ERRORS: " + "; ".join(report["errors"]) + "\n")
    lines.append(f"VERDICT: {gate['verdict']}")
    lines.append(f"  footgun flips: {gate['flips']}  |  cost-to-correct cheaper on "
                 f"{gate['tasks_reduced']}/{gate['tasks_total']} tasks")
    for r in gate["reasons"]:
        lines.append(f"  - {r}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="analyze", description="dogfood v2 cost-to-correct runner + gate")
    parser.add_argument("--trials", type=int, default=3, help="trials per task (default 3)")
    parser.add_argument("--workers", type=int, default=1, help="concurrent trial jobs (default 1; threads)")
    parser.add_argument("--from-records", type=Path, default=None,
                        help="aggregate a committed records JSON file instead of running the agent")
    parser.add_argument("--out", type=Path, default=HERE / "RESULTS.data.json",
                        help="where to write records + report + verdict")
    args = parser.parse_args(argv)

    if args.from_records is not None:
        records = json.loads(args.from_records.read_text(encoding="utf-8"))
        records = records["records"] if isinstance(records, dict) else records
    else:
        print(f"running {len(TASKS)} tasks x {args.trials} trials (treatment + control + rework) "
              "through real Claude Code...")
        records = run_experiment(args.trials, workers=args.workers)

    report = aggregate(records)
    gate = evaluate_gate(report)
    print("\n" + format_report(report, gate))

    if args.out is not None:
        args.out.write_text(json.dumps({"records": records, "report": report, "gate": gate}, indent=2),
                            encoding="utf-8")
        print(f"\nwrote results -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
