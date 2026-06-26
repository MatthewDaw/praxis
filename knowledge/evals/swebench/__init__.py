"""SWE-rebench cost-to-correct A/B pilot harness (Praxis PR-knowledge eval).

The public entry point is :func:`knowledge.evals.swebench.run.main`
(``uv run python -m knowledge.evals.swebench.run``). The per-unit modules
(``instances``, ``grader``, ``ingest``, ``relevance``, ``runner``, ``experiment``,
``analyze``) are imported directly where needed; this package marker re-exports only
the analysis surface most callers reach for offline.
"""

from knowledge.evals.swebench.analyze import aggregate, evaluate_gate, format_report

__all__ = ["aggregate", "evaluate_gate", "format_report"]
