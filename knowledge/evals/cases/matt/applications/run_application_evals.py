"""Debuggable entry point: run ONLY Matt's application-filling evals end-to-end.

A thin, step-through-friendly wrapper around the normal harness in
``knowledge.evals.run`` — it reuses the harness's shared run loop
(:func:`execute_cases`) and scoreboard (:func:`report`), and only adds the bits
unique to this suite: selecting the application-filling cases and an offline
``--fake`` toggle. Nothing about running/grading/recording is reimplemented here.

Run it:

    uv run python -m knowledge.evals.cases.matt.applications.run_application_evals
    uv run python knowledge/evals/cases/matt/applications/run_application_evals.py

Filter to specific cases (substring match against the case id):

    uv run python -m knowledge.evals.cases.matt.applications.run_application_evals sekai
    uv run python -m knowledge.evals.cases.matt.applications.run_application_evals complex_ai production_llm

Backends:

    --fake        offline FakeRunner (no agent, no credit). The application
                  cases are full-pipeline file_io cases needing a text-producing
                  agent, so --fake SKIPS them. The intent_gating cases are
                  graph_reader *component* cases that run deterministically with
                  no agent, so --fake DOES grade them (handy for fast iteration
                  on the no-leak checks).
    (default)     real Claude Code (subscription): ingest -> agent fills -> grade.

Good breakpoint spots for stepping through one case:
  * ``_seed_knowledge`` (knowledge.evals.run) — raw docs ingested into the graph
  * ``ClaudeCodeRunner.run`` (knowledge.evals.claude_code) — reader.read + agent
  * ``run_checks`` / ``grade_rubric`` (knowledge.evals.run) — grading
This entry point only *selects* the suite's cases (``applications/`` +
``intent_gating/``) and delegates to ``knowledge.evals.run.main`` for the run
loop; step into its ``run_case_full`` call to walk one case end-to-end.
"""

from __future__ import annotations

import sys
from pathlib import Path

from knowledge.evals.run import load_cases, load_env
from knowledge.evals.run import main as run_main

# Scope to THIS suite by directory, not by id prefix: other (non-application)
# cases also live under cases/matt/ per convention and would share a ``matt_``
# prefix, so a prefix filter would wrongly sweep them in. Selecting by directory
# keeps this entry point to exactly the suites we want.
#
# Two sibling suites run here:
#   - applications/  — resume-filling cases (full pipeline, need a file-producing agent).
#   - intent_gating/ — distractor / no-leak retrieval cases (graph_reader
#     component, no sandbox). They are the acceptance test for intent-aware
#     retrieval, so they belong next to the application evals that motivate them.
APPLICATIONS_DIR = Path(__file__).resolve().parent
SUITE_DIRS = (APPLICATIONS_DIR, APPLICATIONS_DIR.parent / "intent_gating")


def _in_suite(case) -> bool:
    src = getattr(case, "source_dir", None)
    if not src:
        return False
    resolved = Path(src).resolve()
    return any(d == resolved or d in resolved.parents for d in SUITE_DIRS)


def select_cases(filters: list[str]):
    """Load every case in this entry point's suites (``applications/`` +
    ``intent_gating/``).

    Scoped by directory, so unrelated ``cases/matt/`` cases are never included.
    ``filters`` are matched as substrings against the case id (any match keeps the
    case), so ``sekai``, ``complex_ai``, or ``intent`` pick subsets.
    """
    cases = [c for c in load_cases() if _in_suite(c)]
    if filters:
        cases = [c for c in cases if any(f in c.id for f in filters)]
    return sorted(cases, key=lambda c: c.id)


def main(argv: list[str] | None = None) -> int:
    """Select this suite's cases, then delegate to the canonical ``run.main``.

    Backend flags (``--fake`` / ``--openrouter`` / ``--structured``) pass straight
    through. Positional args are substring filters over the case id; we resolve
    them to concrete ids *here* (scoped to our two suite dirs) and hand the
    explicit id list to ``run.main`` so all run/grade/report logic stays in one
    place — no duplicated run loop to drift out of sync with the harness.
    """
    argv = sys.argv[1:] if argv is None else argv
    flags = [a for a in argv if a.startswith("--")]
    filters = [a for a in argv if not a.startswith("--")]

    load_env()

    cases = select_cases(filters)
    if not cases:
        print(f"no suite cases match {filters!r}")
        return 0

    case_ids = [c.id for c in cases]
    return run_main([*case_ids, *flags])


if __name__ == "__main__":
    main()
