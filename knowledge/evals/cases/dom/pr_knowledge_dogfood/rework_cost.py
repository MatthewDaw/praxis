"""Cost-to-correct probe: knowledge upfront vs. knowledge as late review feedback.

The main experiment (``analyze.py``) measures only FIRST-PASS tokens. But a control arm
that produces a *wrong* artifact isn't done — the error is caught and fixed, and that
rework is a real cost the first-pass number hides (and bills to nobody). This probe
charges it honestly: it runs a control, and if its output is wrong it runs a corrective
"review feedback" turn that delivers the SAME fact LATE on the control's wrong file, then
compares the treatment's single upfront pass against ``control_first_pass + rework``.

  uv run python knowledge/evals/cases/dom/pr_knowledge_dogfood/rework_cost.py [--trials 2]

Same knowledge, only the *timing* differs — so it is a fair like-for-like. Backs the
"Cost-to-correct" section of RESULTS.md. Uses real Claude Code (subscription); costs money.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _ensure_repo_on_path() -> None:
    for parent in HERE.parents:
        if (parent / "pyproject.toml").exists():
            if str(parent) not in sys.path:
                sys.path.insert(0, str(parent))
            return


_ensure_repo_on_path()

from knowledge.evals.claude_code import _claude_usage  # noqa: E402
from knowledge.evals.eval_def import DeterministicCheckRef, EvalCase  # noqa: E402
from knowledge.evals.run import load_cases, resolve_check, run_case_full, select_runner  # noqa: E402

# task -> (treatment-style check the fix must satisfy, late "review feedback" prompt, output file)
TASKS = {
    "supersedes_edge": (
        DeterministicCheckRef(
            name="c", ref="knowledge.evals.deterministic_checks.builds:contains_text",
            params={"text": "supersedes"},
        ),
        "Code review feedback: in this codebase the directional replacement edge MUST be named "
        "`supersedes` (project convention from the contradiction/supersession model), not whatever "
        "name you chose. Update supersede_fact.py to use the `supersedes` edge name. Edit only "
        "supersede_fact.py.",
        "supersede_fact.py",
    ),
    "umap_neighbors": (
        DeterministicCheckRef(
            name="c", ref="knowledge.evals.deterministic_checks.text:regex_absent",
            params={"pattern": r"n_neighbors\s*=\s*(?:min\()?\s*15\b"},
        ),
        "Code review feedback: the topic-collapse bug is UMAP's n_neighbors — at 15 it over-weights "
        "global structure and melts heterogeneous corpora into one cluster. Lower it to min(10, n - 1) "
        "in clustering.py. Edit only clustering.py.",
        "clustering.py",
    ),
}


def _tokens(ctx) -> int:
    u = _claude_usage(ctx.raw_response or "")
    return (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)


def _passes(ctx, ref: DeterministicCheckRef) -> bool:
    return resolve_check(ref)(ctx, **ref.params).passed


def _rework_case(task: str, wrong_output: str, prompt: str, output_file: str, ref: DeterministicCheckRef) -> EvalCase:
    """An ad-hoc case that mounts the control's wrong file and asks for the late fix."""
    tmp = Path(tempfile.mkdtemp(prefix=f"rework-{task}-"))
    (tmp / "fixtures").mkdir()
    (tmp / "fixtures" / output_file).write_text(wrong_output, encoding="utf-8")
    return EvalCase(
        id=f"{task}_rework", needs=["sandbox", "file_io"], seed_prompt=prompt,
        target_commit="0" * 40, output_file=output_file,
        deterministic_checks=[ref], source_dir=str(tmp),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rework_cost")
    parser.add_argument("--trials", type=int, default=2, help="control/rework trials per task (default 2)")
    args = parser.parse_args(argv)

    runner, judge = select_runner("claude")
    cases = {c.id: c for c in load_cases()}

    header = f"{'task':18} {'treat_fp':>9} {'ctrl_fp':>9} {'ctrl_wrong':>10} {'rework':>8} {'ctrl_total':>10} {'fixed?':>7}"
    print(header)
    print("-" * len(header))

    for task, (ref, fix_prompt, out_file) in TASKS.items():
        treat, control = cases[task], cases[f"{task}_before"]
        treat_fp = [_tokens(run_case_full(treat, runner, judge=judge)[0]) for _ in range(args.trials)]
        treat_mean = sum(treat_fp) / len(treat_fp)

        for _ in range(args.trials):
            cctx, _, _ = run_case_full(control, runner, judge=judge)
            c_fp = _tokens(cctx)
            if _passes(cctx, ref):  # control got it right blind -> no rework to charge
                print(f"{task:18} {treat_mean:9.0f} {c_fp:9d} {'no(luck)':>10} {'-':>8} {'-':>10} {'-':>7}")
                continue
            rctx, _, _ = run_case_full(_rework_case(task, cctx.output, fix_prompt, out_file, ref), runner, judge=judge)
            rework = _tokens(rctx)
            fixed = _passes(rctx, ref)
            print(f"{task:18} {treat_mean:9.0f} {c_fp:9d} {'yes':>10} {rework:8d} {c_fp + rework:10d} {str(fixed):>7}")

    print("\ntreat_fp = treatment first-pass (knowledge upfront); "
          "ctrl_total = ctrl_fp + rework (same knowledge delivered late as review feedback)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
