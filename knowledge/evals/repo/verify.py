"""Agent-free verification that a RepoTask is a sound behavioral oracle.

Materializes the base + target tests, grades (expect RED), applies the gold fix,
grades again (expect GREEN) — the harness-level discrimination proof, no agent.
Network + venv install happen here, so this is NOT part of the offline suite;
run it on demand:

    uv run python -m knowledge.evals.repo.verify [<case_id>]   # default: exactly_n_negative
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from knowledge.evals.repo.behavioral import grade
from knowledge.evals.repo.checkout import apply_gold, materialize_base
from knowledge.evals.repo.repo_task_def import RepoTask
from knowledge.evals.repo.venv import ensure_venv


def verify_instance(task: RepoTask) -> dict:
    """Return whether the instance flips RED (base) -> GREEN (gold) as expected."""
    with tempfile.TemporaryDirectory(prefix="praxis-repoverify-") as tmp:
        dest = Path(tmp) / "repo"
        materialize_base(task, dest)
        python = ensure_venv(dest)
        red_pass, red_ev = grade(python, dest, task.fail_to_pass, task.pass_to_pass)
        apply_gold(task, dest)
        green_pass, green_ev = grade(python, dest, task.fail_to_pass, task.pass_to_pass)
    return {
        "red_failed_as_expected": not red_pass,
        "green_passed_as_expected": green_pass,
        "red_evidence": red_ev,
        "green_evidence": green_ev,
    }


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    case_id = argv[0] if argv else "exactly_n_negative"
    from knowledge.evals.run import CASES_DIR, load_case

    case = load_case(CASES_DIR / case_id)
    if case.code_task is None:
        print(f"case {case_id!r} has no code_task")
        return 2

    print(f"verifying behavioral oracle for {case_id} ({case.code_task.repo})...")
    result = verify_instance(case.code_task)
    print(f"  RED  (base, buggy source): {'failed as expected' if result['red_failed_as_expected'] else 'UNEXPECTEDLY PASSED'} — {result['red_evidence']}")
    print(f"  GREEN (gold fix applied):  {'passed as expected' if result['green_passed_as_expected'] else 'UNEXPECTEDLY FAILED'} — {result['green_evidence']}")
    ok = result["red_failed_as_expected"] and result["green_passed_as_expected"]
    print("\nORACLE SOUND" if ok else "\nORACLE MISMATCH — pick a different instance")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
