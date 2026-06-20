#!/usr/bin/env python3
"""Export frontend/mock_data.py to React and integration JSON fixtures.

Canonical source: frontend/mock_data.py
Outputs:
  - frontend-react/public/mock-candidates.json (React mock mode)
  - frontend-react/public/mock-graph.json (React graph view fixtures)
  - frontend-react/public/mock-eval-metrics.json (React eval metrics mock)
  - frontend/fixtures/eval-metrics.json (Streamlit eval metrics mock)
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FRONTEND = _REPO_ROOT / "frontend"
_REACT_CANDIDATES = _REPO_ROOT / "frontend-react" / "public" / "mock-candidates.json"
_REACT_GRAPH = _REPO_ROOT / "frontend-react" / "public" / "mock-graph.json"
_REACT_EVAL_METRICS = _REPO_ROOT / "frontend-react" / "public" / "mock-eval-metrics.json"
_STREAMLIT_EVAL_METRICS = _FRONTEND / "fixtures" / "eval-metrics.json"
_EVAL_METRICS_SOURCE = _REPO_ROOT / "docs" / "integration" / "fixtures" / "eval-metrics.json"


def _export_eval_metrics() -> None:
    if not _EVAL_METRICS_SOURCE.is_file():
        raise FileNotFoundError(f"Missing eval metrics source: {_EVAL_METRICS_SOURCE}")
    payload = _EVAL_METRICS_SOURCE.read_text(encoding="utf-8")
    _REACT_EVAL_METRICS.write_text(payload, encoding="utf-8")
    _STREAMLIT_EVAL_METRICS.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_EVAL_METRICS_SOURCE, _STREAMLIT_EVAL_METRICS)
    print(f"Exported eval metrics to {_REACT_EVAL_METRICS} and {_STREAMLIT_EVAL_METRICS}")


def main() -> int:
    sys.path.insert(0, str(_REPO_ROOT))
    sys.path.insert(0, str(_FRONTEND))
    from mock_data import get_mock_candidate_dicts, get_mock_graph_dict  # noqa: PLC0415

    rows = get_mock_candidate_dicts()
    candidates_payload = json.dumps(rows, indent=2, ensure_ascii=False) + "\n"
    _REACT_CANDIDATES.write_text(candidates_payload, encoding="utf-8")
    print(f"Exported {len(rows)} candidates to {_REACT_CANDIDATES}")

    graph = get_mock_graph_dict()
    graph_payload = json.dumps(graph, indent=2, ensure_ascii=False) + "\n"
    _REACT_GRAPH.write_text(graph_payload, encoding="utf-8")
    print(
        f"Exported graph ({len(graph['nodes'])} nodes, {len(graph['edges'])} edges) "
        f"to {_REACT_GRAPH}"
    )

    _export_eval_metrics()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
