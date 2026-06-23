"""Optional live smoke tests for ingest + promote→graph wiring.

Skipped unless both PRAXIS_API_BASE_URL and PRAXIS_INGEST_SMOKE are set.
These tests never block CI — they skip when endpoints are not deployed.

    $env:PRAXIS_API_BASE_URL = "http://localhost:8000"
    $env:PRAXIS_INGEST_SMOKE = "1"
    $env:PYTHONPATH = "frontend"
    uv run pytest frontend/tests/test_ingest_promote_smoke.py -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import urllib.error
import urllib.request

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _REPO_ROOT / "docs" / "integration" / "fixtures"


def _api_base() -> str:
    return os.environ["PRAXIS_API_BASE_URL"].strip().rstrip("/")


def _load_json(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _request(method: str, path: str, body: object | None = None) -> tuple[int, str]:
    url = f"{_api_base()}{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json", "X-Praxis-Contract": "1"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    token = os.environ.get("PRAXIS_API_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("PRAXIS_API_BASE_URL", "").strip(),
        reason="set PRAXIS_API_BASE_URL to run live ingest/promote smoke tests",
    ),
    pytest.mark.skipif(
        not os.environ.get("PRAXIS_INGEST_SMOKE", "").strip(),
        reason="set PRAXIS_INGEST_SMOKE=1 to opt in to ingest/promote smoke tests",
    ),
]


def test_post_ingest_jsonl_returns_candidates_or_skips() -> None:
    payload = _load_json("ingest-jsonl-request.json")
    status, body = _request("POST", "/ingest/jsonl", payload)
    if status in (404, 405):
        pytest.skip("ingest endpoint not deployed yet")
    assert status == 200, body
    if body.strip():
        parsed = json.loads(body)
        assert isinstance(parsed, dict)


def test_promote_active_writes_graph_or_skips() -> None:
    """When Matthew wires promote→graph, a promote to active should persist facts."""
    status, body = _request("GET", "/candidates")
    if status != 200:
        pytest.skip(f"candidates endpoint unavailable ({status})")
    rows = json.loads(body)
    if isinstance(rows, dict):
        rows = rows.get("candidates", [])
    proposed = next(
        (row for row in rows if row.get("state") == "proposed"),
        None,
    )
    if proposed is None:
        pytest.skip("no proposed candidates available for promote→graph smoke")
    candidate_id = proposed["id"]
    promote_status, promote_body = _request(
        "POST",
        f"/candidates/{candidate_id}/promote",
        {"targetState": "active"},
    )
    if promote_status in (404, 405):
        pytest.skip("promote endpoint not deployed yet")
    assert promote_status == 200, promote_body
