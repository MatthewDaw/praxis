"""End-to-end plan-reproduction eval: PRD -> reproduced plan -> coverage vs the golden.

Wires the three pieces together:
  planner.produce_candidate  (PRD [+ checklist] -> candidate plan)
  -> coverage.run_coverage    (per-part sweep, evidence-required, targeted adversarial)
     with llm_evaluator        (semantic judge + refuter)

Needs a real model backend (the Anthropic SDK + an API key, or edit to inject your own
``Complete``). Run the baseline vs. the checklist treatment and compare derived-hole counts::

    python -m evals.plan_repro.run_eval --out team-app/candidate-baseline.yaml
    python -m evals.plan_repro.run_eval --checklist --out team-app/candidate-treatment.yaml

A candidate is saved so scoring can be re-run deterministically without re-planning.
"""

from __future__ import annotations

from pathlib import Path


def _main(argv: list[str] | None = None) -> int:  # pragma: no cover - exercises the network
    import argparse

    from evals.plan_repro.coverage import (
        lexical_related_query,
        load_candidate,
        load_golden,
        run_coverage,
    )
    from evals.plan_repro.llm_evaluator import (
        DEFAULT_JUDGE_MODEL,
        make_anthropic_complete,
        make_llm_evaluator,
        make_llm_refuter,
    )
    from evals.plan_repro.planner import (
        DEFAULT_GOLDEN,
        load_prd,
        produce_candidate,
        save_candidate,
    )
    from evals.plan_repro.praxis_source import (
        provision_and_load_checklist,
        teardown_eval_space,
    )

    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Run the plan-reproduction coverage eval.")
    p.add_argument("--golden", default=str(DEFAULT_GOLDEN))
    p.add_argument("--out", default=None, help="candidate plan output path (under this dir)")
    p.add_argument("--checklist", action="store_true", help="apply the planning checklist (treatment)")
    p.add_argument("--planner-model", default=DEFAULT_JUDGE_MODEL)
    p.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    p.add_argument("--candidate", default=None, help="score an existing candidate; skip planning")
    args = p.parse_args(argv)

    judge = make_anthropic_complete(model=args.judge_model)

    provisioned = False
    try:
        if args.candidate:
            candidate = load_candidate(args.candidate)
            print(f"scoring existing candidate: {args.candidate} ({len(candidate)} features)")
        else:
            planner = make_anthropic_complete(model=args.planner_model)
            checklist = None
            if args.checklist:
                # Provision the eval's OWN Praxis space, seed the checklist into it, read it
                # back — the eval relies on Praxis at runtime, not a copy in code.
                checklist = provision_and_load_checklist()
                provisioned = True
                print(f"provisioned eval space + loaded {len(checklist)} planning check(s) "
                      f"from Praxis")
            candidate = produce_candidate(planner, load_prd(), checklist=checklist)
            out = args.out or f"team-app/candidate-{'treatment' if args.checklist else 'baseline'}.yaml"
            out_path = out if Path(out).is_absolute() else str(here / out)
            save_candidate(candidate, out_path, project="team-app")
            print(f"planned {len(candidate)} features "
                  f"({'with' if args.checklist else 'without'} checklist) -> {out_path}")

        golden = load_golden(args.golden)
        report = run_coverage(
            golden, candidate, lexical_related_query,
            make_llm_evaluator(judge), refuter=make_llm_refuter(judge),
        )
        print(report.format())
        return 0 if report.passed else 1
    finally:
        if provisioned:
            teardown_eval_space()
            print("torn down eval space (cleared)")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
