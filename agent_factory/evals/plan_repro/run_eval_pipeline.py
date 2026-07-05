"""End-to-end PLANNING eval — scores the REAL af-intake forcing, not a proxy planner.

What it does (the rewrite per the "two questions, real pipeline" design):
  1. Provision an isolated Praxis space and seed the planning lenses (scope="planning" checks) into it.
  2. Drive the REAL af-intake (headless `claude` CLI, subscription, tools enabled) in UNATTENDED mode on
     the fixed PRD (docs/inspiration/): push in every explicitly-stated feature as a requirement, sweep
     every active planning check, and for every implied need / forced decision the audit raises, choose
     the low-regret DEFAULT and record it as an episode (never ask) — then leave the admitted
     requirements + decision episodes in the space (no snapshot).
  3. Read the produced plan back from the space (requirements + decision episodes).
  4. Score it on the two questions:
       Q1 explicit coverage  — every EXPLICIT golden feature (derived==False) is present as a requirement.
       Q2 implied surfacing  — for every planning lens, its implied need is surfaced (a requirement OR a
                               recorded forced-decision/default); an unsurfaced applicable need = a hole.
  5. Tear down the space.

"Working" = both ~complete: the real pipeline ingested every stated feature AND let every planning
insight expose its implied need. A hole is a genuine af-intake defect, not a weak proxy's luck.

    python -m evals.plan_repro.run_eval_pipeline

Heavy: this runs a full real planning session (slow, lots of subscription). Needs the af-intake skill
loaded (plugin reloaded after the rename) and Praxis reachable.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

EVAL_PROJECT = "team-app-eval"   # the project name af-intake plans under, inside the isolated space


def _load_dotenv(root: Path) -> None:
    env = root / ".env"
    if not env.is_file():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _drive_af_intake(space_id: str, prd_dir: Path, timeout: int) -> int:
    """Run the REAL af-intake unattended into the eval space via the headless claude CLI."""
    from evals.plan_repro.claude_cli import _claude_path
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)      # bill the subscription, not an API key
    env["PRAXIS_SPACE"] = space_id          # so the agent's Praxis writes land in the isolated space
    prompt = (
        f"Run /agent-factory:af-intake in FULL INTAKE mode for project '{EVAL_PROJECT}', sourcing the PRD "
        f"from {prd_dir.as_posix()} (read every doc fully). UNATTENDED MODE — the owner is asleep, NEVER "
        "ask a question: for every forced architecture/external-service decision or open fork the audit "
        "raises, choose the low-regret DEFAULT and record it as a Praxis episode, then proceed. Push in "
        "EVERY feature the PRD explicitly states as a requirement fact. Sweep ALL active scope='planning' "
        "checks and, for each that applies, admit the implied requirement(s) or record the forced-decision "
        "default it raises. Write requirements + episodes to the ACTIVE Praxis space only. Do NOT save a "
        "snapshot and do NOT clear the graph — just leave the admitted requirements + decision episodes."
    )
    print(f"  driving REAL af-intake (unattended) into space '{space_id}' ...", flush=True)
    proc = subprocess.run(
        [_claude_path(), "-p", prompt, "--permission-mode", "bypassPermissions"],
        cwd=str(Path(__file__).resolve().parents[2]), env=env, capture_output=True, text=True,
        timeout=timeout, encoding="utf-8",
    )
    print(f"  af-intake agent exited {proc.returncode}", flush=True)
    if proc.returncode != 0:
        print("  (stderr tail)\n" + (proc.stderr or "")[-600:], flush=True)
    return proc.returncode


def _read_plan(space_id: str):
    """Read the produced plan from the eval space via the factory's own exhaustive Praxis client
    (hooks/_praxis), scoped to the space. Requirements + decision episodes are scorable items."""
    import _praxis as px  # hooks/_praxis — added to sys.path in main()
    from evals.plan_repro.coverage import Feature

    os.environ["PRAXIS_SPACE"] = space_id  # scope reads to the eval space (x-praxis-space header)

    def _facts(category):
        try:
            return px.facts_by(category=category)
        except Exception as exc:  # noqa: BLE001
            print(f"  facts_by(category={category!r}) failed: {exc}", flush=True)
            return []

    feats: list = []
    for r in _facts("requirement"):
        feats.append(Feature(id=str(r.get("id", "?")),
                             text=str(r.get("text") or r.get("content") or "").strip()))
    # decision defaults / forced-decision records count as "surfaced" for the depth question.
    for e in _facts("episodic"):
        t = str(e.get("text") or e.get("content") or "").strip()
        if t:
            feats.append(Feature(id=str(e.get("id", "?")), text="[decision] " + t))
    return [f for f in feats if f.text]


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    root = Path(__file__).resolve().parents[2]   # agent_factory/
    _load_dotenv(root)
    praxis_repo = root.parent / "praxis"
    if praxis_repo.is_dir():
        sys.path.insert(0, str(praxis_repo))
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "hooks"))  # for _praxis (the exhaustive read-back client)

    from evals.plan_repro import praxis_source as ps
    from evals.plan_repro.claude_cli import make_claude_cli_complete
    from evals.plan_repro.coverage import (
        lexical_related_query, load_golden, run_coverage,
    )
    from evals.plan_repro.depth import run_depth
    from evals.plan_repro.llm_evaluator import make_llm_evaluator, make_llm_refuter
    from evals.plan_repro.planner import DEFAULT_GOLDEN, DEFAULT_PRD_DIR

    space_id = ps.EVAL_SPACE_ID
    agent_timeout = int(os.environ.get("PIPELINE_AGENT_TIMEOUT", "3600"))

    # 1. provision the isolated space + seed the planning lenses (round-tripped through Praxis).
    lenses = ps.provision_and_load_checklist(space_id=space_id)
    print(f"provisioned eval space '{space_id}' + {len(lenses)} planning lens(es)", flush=True)

    try:
        # 2. drive the REAL af-intake (unattended) on the PRD into the space.
        _drive_af_intake(space_id, DEFAULT_PRD_DIR, agent_timeout)

        # 3. read the produced plan back.
        candidate = _read_plan(space_id)
        print(f"af-intake produced {len(candidate)} scorable item(s) "
              f"(requirements + decision episodes)", flush=True)
        if not candidate:
            print("NO requirements produced — af-intake did not write to the space (check skill loaded / "
                  "PRAXIS_SPACE targeting / Praxis auth). Cannot score.", flush=True)
            return 2

        complete = _retrying(make_claude_cli_complete(timeout=150))

        # 4a. Q1 — explicit coverage vs the EXPLICIT golden (raw-PRD features, derived==False).
        golden = load_golden(str(DEFAULT_GOLDEN))
        explicit = [f for f in golden if not getattr(f, "derived", False)]
        print(f"\n[Q1] explicit coverage: {len(candidate)} produced vs {len(explicit)} EXPLICIT "
              f"features ...", flush=True)
        explicit_report = run_coverage(
            explicit, candidate, lexical_related_query,
            make_llm_evaluator(complete), refuter=make_llm_refuter(complete),
        )
        print(explicit_report.format(), flush=True)

        # 4b. Q2 — implied-need surfacing (lenses applied to the REAL produced plan).
        print(f"\n[Q2] implied-need surfacing: applying {len(lenses)} lens(es) to the produced plan ...",
              flush=True)
        depth_report = run_depth(complete, lenses, candidate)
        print(depth_report.format(), flush=True)

        e_holes, d_holes = len(explicit_report.holes), len(depth_report.holes)
        print(f"\n=== SIGNAL (real af-intake pipeline): {e_holes} explicit-coverage hole(s), "
              f"{d_holes} depth hole(s). ===", flush=True)
        print(f"PASSED: {explicit_report.passed and depth_report.passed}", flush=True)
        return 0 if (explicit_report.passed and depth_report.passed) else 1
    finally:
        ps.teardown_eval_space(space_id=space_id)
        print("torn down eval space", flush=True)


def _retrying(base):
    def complete(prompt: str) -> str:
        last = None
        for attempt in range(3):
            try:
                return base(prompt)
            except Exception as exc:  # noqa: BLE001
                last = exc
                print(f"  [judge retry {attempt + 1}/3 after {type(exc).__name__}]", flush=True)
        raise last  # type: ignore[misc]
    return complete


if __name__ == "__main__":
    raise SystemExit(main())
