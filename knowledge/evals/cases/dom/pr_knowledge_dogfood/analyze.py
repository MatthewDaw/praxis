"""Trial orchestration, aggregation, and the R8 go-gate for the PR-knowledge dogfood experiment.

Runs N trials/arm of the paired cases through the **real** ``ClaudeCodeRunner``
*in-process* (``run_case_full``), then aggregates tokens/turns and the footgun
outcome per arm and emits a go/no-go verdict.

  uv run python knowledge/evals/cases/dom/pr_knowledge_dogfood/analyze.py --trials 3

Why in-process and not ``python -m knowledge.evals.run <case_id>``: the CLI calls
``write_baseline`` on every invocation, which would clobber the committed
``results/baseline.jsonl`` scoreboard with just these cases. ``run_case_full`` gives
the identical real-agent path (same ``select_runner("claude")`` wiring) without that
side effect.

The pure functions (:func:`aggregate`, :func:`evaluate_gate`) operate on the
``RunTranscript`` JSON shape, so they unit-test offline against committed fixture
transcripts — the live ``results/runs/`` dir is gitignored, so fixtures can't live there.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _ensure_repo_on_path() -> None:
    """Allow ``python <suite>/analyze.py`` (run-by-path) to import the ``knowledge``
    package: the suite dir is not on the package path, so add the repo root (nearest
    ancestor with ``pyproject.toml``) before the package import below."""
    for parent in HERE.parents:
        if (parent / "pyproject.toml").exists():
            if str(parent) not in sys.path:
                sys.path.insert(0, str(parent))
            return


_ensure_repo_on_path()

from knowledge.evals.claude_code import _claude_usage  # noqa: E402

# task name -> is this a footgun task whose control->treatment flip is REQUIRED for GO?
# supersedes_edge is a convention/quantitative task: it carries the token/turn signal
# and reports a (medium-validity) convention flip, but its flip is not gating.
TASKS: dict[str, bool] = {
    "umap_neighbors": True,
    "phoenix_tracing": True,
    "supersedes_edge": False,
}

_BEFORE = "_before"


def control_id(task: str) -> str:
    return f"{task}{_BEFORE}"


def arm_and_task(case_id: str) -> tuple[str, str]:
    """('control', task) for ``<task>_before``; ('treatment', task) otherwise."""
    if case_id.endswith(_BEFORE):
        return "control", case_id[: -len(_BEFORE)]
    return "treatment", case_id


# --------------------------------------------------------------------------- #
# Pure aggregation over the RunTranscript JSON shape
# --------------------------------------------------------------------------- #
def _usage(transcript: dict) -> dict:
    """tokens/turns from a transcript's ``agent.raw_response`` (the claude CLI stdout)."""
    raw = (transcript.get("agent") or {}).get("raw_response")
    return _claude_usage(raw or "")


def total_tokens(transcript: dict) -> int | None:
    u = _usage(transcript)
    i, o = u.get("input_tokens"), u.get("output_tokens")
    if i is None and o is None:
        return None
    return (i or 0) + (o or 0)


def num_turns(transcript: dict) -> int | None:
    return _usage(transcript).get("num_turns")


def footgun_passed(transcript: dict) -> bool | None:
    """Did the footgun/convention check pass? (the lone non-``produced_output`` check).

    For a treatment arm a pass means the agent AVOIDED the footgun / followed the
    convention; for a control arm a pass means the agent EXHIBITED the footgun.
    Returns ``None`` when no such check is present.
    """
    checks = (transcript.get("verdict") or {}).get("checks") or []
    fg = [c for c in checks if c.get("name") != "produced_output"]
    return bool(fg[0]["passed"]) if fg else None


def _mean_sd(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    return mean, sd


def _arm_summary(transcripts: list[dict]) -> dict:
    """Per-arm rollup: token/turn mean±sd and footgun pass-rate, plus a usable count.

    A transcript with no parseable usage is recorded in ``missing`` (surfaced, never
    silently averaged in) rather than dropped without trace.
    """
    tokens = [t for t in (total_tokens(x) for x in transcripts) if t is not None]
    turns = [t for t in (num_turns(x) for x in transcripts) if t is not None]
    fg = [f for f in (footgun_passed(x) for x in transcripts) if f is not None]
    tok_mean, tok_sd = _mean_sd([float(t) for t in tokens])
    turn_mean, turn_sd = _mean_sd([float(t) for t in turns])
    return {
        "trials": len(transcripts),
        "missing_usage": len(transcripts) - len(tokens),
        "tokens_mean": tok_mean,
        "tokens_sd": tok_sd,
        "turns_mean": turn_mean,
        "turns_sd": turn_sd,
        "footgun_pass_rate": (sum(fg) / len(fg)) if fg else None,
        "footgun_n": len(fg),
    }


def aggregate(transcripts: list[dict]) -> dict:
    """Group transcripts by (task, arm) and compute per-task deltas + the footgun flip.

    Returns ``{tasks: {<task>: {...}}, errors: [...]}``. A task missing an arm or with
    no usable usage in an arm is recorded in ``errors`` (and excluded from the gate),
    never reported as a clean result.
    """
    by_task: dict[str, dict[str, list[dict]]] = {}
    for t in transcripts:
        case_id = t.get("case_id", "")
        arm, task = arm_and_task(case_id)
        by_task.setdefault(task, {}).setdefault(arm, []).append(t)

    tasks: dict[str, dict] = {}
    errors: list[str] = []
    for task, is_footgun in TASKS.items():
        arms = by_task.get(task, {})
        treat = arms.get("treatment", [])
        control = arms.get("control", [])
        if not treat or not control:
            errors.append(f"{task}: missing arm(s) (treatment={len(treat)}, control={len(control)})")
            continue
        ts, cs = _arm_summary(treat), _arm_summary(control)

        def _delta(a: float | None, b: float | None) -> float | None:
            return None if a is None or b is None else a - b  # treatment - control

        token_delta = _delta(ts["tokens_mean"], cs["tokens_mean"])
        turn_delta = _delta(ts["turns_mean"], cs["turns_mean"])
        if token_delta is None or turn_delta is None:
            errors.append(f"{task}: no usable token/turn data in an arm")

        # Flip: treatment mostly avoids the footgun AND control mostly exhibits it.
        t_rate, c_rate = ts["footgun_pass_rate"], cs["footgun_pass_rate"]
        flip = bool(t_rate is not None and c_rate is not None and t_rate >= 0.5 and c_rate >= 0.5)

        tasks[task] = {
            "is_footgun": is_footgun,
            "treatment": ts,
            "control": cs,
            "token_delta": token_delta,  # negative = treatment used fewer tokens
            "turn_delta": turn_delta,
            "token_reduced": (token_delta is not None and token_delta < 0),
            "turn_reduced": (turn_delta is not None and turn_delta < 0),
            "flip": flip,
        }
    return {"tasks": tasks, "errors": errors}


def evaluate_gate(report: dict) -> dict:
    """The R8 go-gate: directional token/turn reduction on MOST tasks AND footgun flip.

    GO iff (every footgun task flips control->treatment) AND (a token-or-turn reduction
    shows on a majority of all tasks). Errors (missing data) force NO-GO with the reason.
    """
    tasks = report["tasks"]
    errors = list(report["errors"])
    footgun_tasks = [n for n, t in tasks.items() if t["is_footgun"]]
    flips = {n: tasks[n]["flip"] for n in footgun_tasks}
    all_flip = bool(footgun_tasks) and all(flips.values())

    reduced = {n: (t["token_reduced"] or t["turn_reduced"]) for n, t in tasks.items()}
    n_reduced = sum(reduced.values())
    most_reduced = bool(tasks) and n_reduced > len(tasks) / 2

    go = all_flip and most_reduced and not errors
    reasons = []
    if not footgun_tasks:
        reasons.append("no footgun task present")
    if footgun_tasks and not all_flip:
        reasons.append("footgun did not flip on: " + ", ".join(n for n, f in flips.items() if not f))
    if not most_reduced:
        reasons.append(f"token/turn reduction on only {n_reduced}/{len(tasks)} tasks (need a majority)")
    if errors:
        reasons.append("data errors: " + "; ".join(errors))
    return {
        "verdict": "GO" if go else "NO-GO",
        "all_footgun_flip": all_flip,
        "flips": flips,
        "tasks_reduced": n_reduced,
        "tasks_total": len(tasks),
        "most_reduced": most_reduced,
        "reasons": reasons if not go else [],
    }


# --------------------------------------------------------------------------- #
# Live orchestration (real Claude Code, in-process)
# --------------------------------------------------------------------------- #
def run_trials(case_ids: list[str], trials: int, workers: int = 1) -> list[dict]:
    """Run each case ``trials`` times through the real ClaudeCodeRunner; return transcript dicts."""
    # Imported lazily so the pure functions above stay importable without a live env.
    from knowledge.evals.run import build_transcript, load_cases, run_case_full, select_runner

    cases_by_id = {c.id: c for c in load_cases()}
    missing = [cid for cid in case_ids if cid not in cases_by_id]
    if missing:
        raise SystemExit(f"unknown case id(s): {missing}")
    runner, judge = select_runner("claude")

    jobs = [(cid, n) for cid in case_ids for n in range(trials)]

    def _one(job: tuple[str, int]) -> dict:
        cid, n = job
        case = cases_by_id[cid]
        ctx, judge_result, verdict = run_case_full(case, runner, judge=judge)
        t = build_transcript(case, ctx, judge_result, verdict, run_id=f"trial-{n}")
        print(f"  ran {cid} trial {n + 1}/{trials}: {'PASS' if verdict.passed else 'FAIL'}", flush=True)
        return t.model_dump()

    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(_one, jobs))
    return [_one(job) for job in jobs]


def _compact(transcripts: list[dict]) -> list[dict]:
    """Per-trial rows for the committed results artifact (no bulky raw_response/output)."""
    rows = []
    for t in transcripts:
        arm, task = arm_and_task(t.get("case_id", ""))
        rows.append({
            "task": task,
            "arm": arm,
            "case_id": t.get("case_id"),
            "tokens": total_tokens(t),
            "turns": num_turns(t),
            "footgun_passed": footgun_passed(t),
            "verdict_passed": (t.get("verdict") or {}).get("passed"),
        })
    return rows


def format_report(report: dict, gate: dict) -> str:
    lines = ["=== PR-knowledge dogfood — aggregation ===", ""]
    for task, t in report["tasks"].items():
        tag = "footgun" if t["is_footgun"] else "convention"
        tr, ct = t["treatment"], t["control"]
        lines.append(f"[{task}] ({tag})")
        lines.append(
            f"  tokens  treat={_fmt(tr['tokens_mean'])}±{_fmt(tr['tokens_sd'])}  "
            f"control={_fmt(ct['tokens_mean'])}±{_fmt(ct['tokens_sd'])}  "
            f"delta={_fmt(t['token_delta'])} ({'reduced' if t['token_reduced'] else 'no reduction'})"
        )
        lines.append(
            f"  turns   treat={_fmt(tr['turns_mean'])}±{_fmt(tr['turns_sd'])}  "
            f"control={_fmt(ct['turns_mean'])}±{_fmt(ct['turns_sd'])}  "
            f"delta={_fmt(t['turn_delta'])} ({'reduced' if t['turn_reduced'] else 'no reduction'})"
        )
        lines.append(
            f"  footgun treat_avoid_rate={_fmt(tr['footgun_pass_rate'])}  "
            f"control_exhibit_rate={_fmt(ct['footgun_pass_rate'])}  flip={t['flip']}"
        )
        lines.append("")
    if report["errors"]:
        lines.append("ERRORS: " + "; ".join(report["errors"]))
        lines.append("")
    lines.append(f"VERDICT: {gate['verdict']}")
    lines.append(
        f"  footgun flips: {gate['flips']}  |  token/turn reduced on "
        f"{gate['tasks_reduced']}/{gate['tasks_total']} tasks"
    )
    for r in gate["reasons"]:
        lines.append(f"  - {r}")
    return "\n".join(lines)


def _fmt(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return str(v)
    return f"{v:.2f}" if isinstance(v, float) else str(v)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="analyze", description="dogfood experiment trial runner + gate")
    parser.add_argument("--trials", type=int, default=3, help="trials per arm (default 3)")
    parser.add_argument("--workers", type=int, default=1, help="concurrent runs (default 1; threads)")
    parser.add_argument(
        "--from-transcripts",
        type=Path,
        default=None,
        help="aggregate committed transcript JSON files in this dir instead of running the agent",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=HERE / "RESULTS.data.json",
        help="where to write the compact per-trial results + verdict",
    )
    args = parser.parse_args(argv)

    if args.from_transcripts is not None:
        transcripts = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(args.from_transcripts.glob("*.json"))]
    else:
        case_ids = [cid for task in TASKS for cid in (task, control_id(task))]
        print(f"running {len(case_ids)} cases x {args.trials} trials through real Claude Code...")
        transcripts = run_trials(case_ids, args.trials, workers=args.workers)

    report = aggregate(transcripts)
    gate = evaluate_gate(report)
    text = format_report(report, gate)
    print("\n" + text)

    if args.out is not None:
        args.out.write_text(
            json.dumps({"rows": _compact(transcripts), "report": report, "gate": gate}, indent=2),
            encoding="utf-8",
        )
        print(f"\nwrote results -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
