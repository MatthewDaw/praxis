#!/usr/bin/env python3
"""Export frontend/mock_data.py to React and integration JSON fixtures.

Canonical source: frontend/mock_data.py
Outputs:
  - frontend-react/public/mock-candidates.json (React mock mode)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FRONTEND = _REPO_ROOT / "frontend"
_REACT_FIXTURE = _REPO_ROOT / "frontend-react" / "public" / "mock-candidates.json"


def main() -> int:
    sys.path.insert(0, str(_FRONTEND))
    from mock_data import get_mock_candidate_dicts  # noqa: PLC0415

    rows = get_mock_candidate_dicts()
    payload = json.dumps(rows, indent=2, ensure_ascii=False) + "\n"
    _REACT_FIXTURE.write_text(payload, encoding="utf-8")
    print(f"Exported {len(rows)} candidates to {_REACT_FIXTURE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
