#!/usr/bin/env python3
"""Export mock fixtures for Render deploy (API seed + React public JSON).

Canonical source: ``frontend/mock_data.py``

Writes:
  - ``frontend-react/public/mock-candidates.json``
  - ``frontend-react/public/mock-graph.json``
  - ``knowledge/serve/data/pipeline-candidates.json`` (API store seed on first boot)

Run from repo root::

    python scripts/export-render-seed.py
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
_API_SEED = _REPO_ROOT / "knowledge" / "serve" / "data" / "pipeline-candidates.json"


def main() -> int:
    sys.path.insert(0, str(_REPO_ROOT))
    sys.path.insert(0, str(_FRONTEND))
    from mock_data import get_mock_candidate_dicts, get_mock_graph_dict  # noqa: PLC0415

    rows = get_mock_candidate_dicts()
    candidates_payload = json.dumps(rows, indent=2, ensure_ascii=False) + "\n"
    _REACT_CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
    _REACT_CANDIDATES.write_text(candidates_payload, encoding="utf-8")
    print(f"Exported {len(rows)} candidates to {_REACT_CANDIDATES}")

    graph = get_mock_graph_dict()
    graph_payload = json.dumps(graph, indent=2, ensure_ascii=False) + "\n"
    _REACT_GRAPH.write_text(graph_payload, encoding="utf-8")
    print(
        f"Exported graph ({len(graph['nodes'])} nodes, {len(graph['edges'])} edges) "
        f"to {_REACT_GRAPH}"
    )

    _API_SEED.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_REACT_CANDIDATES, _API_SEED)
    print(f"Copied {len(rows)} mock candidates to API seed {_API_SEED}")
    return 0


if __name__ == "__main__":
    main()
