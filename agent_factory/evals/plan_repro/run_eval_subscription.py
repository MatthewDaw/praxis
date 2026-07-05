"""Run the plan-reproduction eval on the logged-in Claude SUBSCRIPTION (no API key).

Same wiring as run_eval._main but with the `claude` CLI backend (claude_cli.make_claude_cli_complete)
for BOTH the planner-under-test and the coverage judge/refuter. Loads the repo .env so the eval's
Praxis space lifecycle (praxis_source) authenticates with PRAXIS_BASE_URL/PRAXIS_API_KEY.

If a previously-planned candidate (team-app/candidate-subscription.yaml) exists, it is SCORED
directly (no re-provision / no re-plan). Delete that file to force a fresh plan.

    python -m evals.plan_repro.run_eval_subscription
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _load_dotenv(root: Path) -> None:
    env = root / ".env"
    if not env.is_file():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def main() -> int:
    # Force UTF-8 stdout/stderr so feature text with chars like U+2265 ('≥') never crashes a
    # Windows cp1252 console.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    root = Path(__file__).resolve().parents[2]  # agent_factory/
    _load_dotenv(root)
    praxis_repo = root.parent / "praxis"
    if praxis_repo.is_dir():
        sys.path.insert(0, str(praxis_repo))

    from evals.plan_repro.claude_cli import make_claude_cli_complete
    from evals.plan_repro.coverage import (
        lexical_related_query, load_candidate, load_golden, run_coverage,
    )
    from evals.plan_repro.llm_evaluator import make_llm_evaluator, make_llm_refuter
    from evals.plan_repro.planner import DEFAULT_GOLDEN, load_prd, produce_candidate, save_candidate

    here = Path(__file__).resolve().parent
    out_path = here / "team-app" / "candidate-subscription.yaml"

    # Resilient subscription backend: a hung `claude -p` call must not kill the whole scoring pass
    # (round 3 died on a single 600s hang). Use a shorter per-call timeout and retry a few times.
    _base = make_claude_cli_complete(timeout=150)

    def complete(prompt: str) -> str:
        last = None
        for attempt in range(3):
            try:
                return _base(prompt)
            except Exception as exc:  # TimeoutExpired or transient CLI error
                last = exc
                print(f"  [judge retry {attempt + 1}/3 after {type(exc).__name__}]", flush=True)
        raise last  # type: ignore[misc]

    print("backend: claude CLI (subscription, timeout=150 x3 retry)", flush=True)

    from evals.plan_repro.depth import run_depth
    from evals.plan_repro.praxis_source import load_seed_checklist

    # The LENSES = the Praxis planning checks (round-tripped through Praxis when we plan; the
    # version-controlled seed otherwise). They are the SINGLE source of truth shared with af-intake,
    # and they GENERATE the implied set the depth question scores against.
    if out_path.is_file():
        candidate = load_candidate(str(out_path))
        lenses = load_seed_checklist()
        print(f"SCORING existing candidate ({len(candidate)} features) -> {out_path}", flush=True)
    else:
        from evals.plan_repro.praxis_source import (
            provision_and_load_checklist, teardown_eval_space,
        )
        provisioned = False
        try:
            lenses = provision_and_load_checklist()
            provisioned = True
            print(f"provisioned eval space + loaded {len(lenses)} planning check(s)", flush=True)
            candidate = produce_candidate(complete, load_prd(), checklist=lenses)
            save_candidate(candidate, str(out_path), project="team-app")
            print(f"PLANNED {len(candidate)} features -> {out_path}", flush=True)
        finally:
            if provisioned:
                teardown_eval_space()
                print("torn down eval space", flush=True)

    for i, feat in enumerate(candidate, 1):
        t = getattr(feat, "text", None) or (feat.get("title") if isinstance(feat, dict) else str(feat))
        print(f"  [{i:02d}] {t}", flush=True)

    golden = load_golden(str(DEFAULT_GOLDEN))
    # Q1 BREADTH — explicit coverage. Ground truth is what the raw PRD EXPLICITLY asked for
    # (golden features with derived==False), NOT the full refined plan. Non-circular.
    explicit = [f for f in golden if not getattr(f, "derived", False)]
    print(f"\n[Q1] explicit coverage: {len(candidate)} candidate vs {len(explicit)} EXPLICIT "
          f"features ...", flush=True)
    explicit_report = run_coverage(
        explicit, candidate, lexical_related_query,
        make_llm_evaluator(complete), refuter=make_llm_refuter(complete),
    )
    print(explicit_report.format(), flush=True)

    # Q2 DEPTH — implied-need surfacing. The implied set is GENERATED by the lenses applied to the plan
    # (not matched to a golden); a lens that applies but whose need is neither built nor flagged = hole.
    print(f"\n[Q2] implied-need surfacing: applying {len(lenses)} lens(es) to the plan ...", flush=True)
    depth_report = run_depth(complete, lenses, candidate)
    print(depth_report.format(), flush=True)

    e_holes, d_holes = len(explicit_report.holes), len(depth_report.holes)
    print(f"\n=== SIGNAL: {e_holes} explicit-coverage hole(s), {d_holes} depth hole(s). "
          "(Directional — read the persistent holes across runs, not a single pass/fail.) ===",
          flush=True)
    print(f"PASSED: {explicit_report.passed and depth_report.passed}", flush=True)
    return 0 if (explicit_report.passed and depth_report.passed) else 1


if __name__ == "__main__":
    raise SystemExit(main())
