#!/usr/bin/env python3
"""Export distillation/scoring output into candidate-api JSON for knowledge.serve.

Run from repo root:

    uv run python scripts/export-pipeline-candidates.py

Writes ``knowledge/serve/data/pipeline-candidates.json``, which ``CandidateStore``
uses as its seed when no persisted store exists yet.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from knowledge.serve.pipeline_adapter import DEFAULT_EXPORT, DEFAULT_INSIGHTS, export_pipeline_candidates


def main() -> int:
    candidates = export_pipeline_candidates(
        insights_path=DEFAULT_INSIGHTS,
        output_path=DEFAULT_EXPORT,
    )
    print(f"Wrote {len(candidates)} pipeline candidates -> {DEFAULT_EXPORT}")
    return 0


if __name__ == "__main__":
    main()
