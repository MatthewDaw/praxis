"""Acceptance test for R26 (66f29b0be20d4c24af259b2303baa07c::acceptance):

"given a mix of progressing and attention-needing jobs, the listed order places
every attention-needing job above every progressing one, and the same data is
retrievable from an MCP tool."

Two halves, both asserted here: (1) the backend ordering itself
(``box_service_jobs_view.order_jobs_for_view``), and (2) that the MCP tool
(``praxis_list_jobs``) surfaces the identically-ordered data the backend's
``/jobs`` endpoint returns — the same payload shape ``knowledge/serve/app.py``'s
``_job_view`` produces, fed straight through the MCP tool's own HTTP client.
"""

from __future__ import annotations

import json

from knowledge.mcp import identity, server
from knowledge.serve.box_service_jobs_view import order_jobs_for_view
from knowledge.serve.box_service_models import Job, JobState

NOW = 10_000.0


def _job(job_id: str, state: JobState, **kwargs) -> Job:
    return Job(id=job_id, project="p", snapshot="s", state=state, **kwargs)


def _mixed_jobs() -> list[Job]:
    return [
        _job("progressing-1", JobState.RUNNING, claim_heartbeat_at=NOW - 5),
        _job("attention-1", JobState.AWAITING_HUMAN),
        _job("progressing-2", JobState.QUEUED, queued_at=NOW - 1),
        _job("attention-2", JobState.FAILED),
    ]


def test_backend_ordering_places_every_attention_job_above_every_progressing_job():
    rows = order_jobs_for_view(_mixed_jobs(), now=NOW)

    attention_idxs = [i for i, r in enumerate(rows) if r.needs_attention]
    normal_idxs = [i for i, r in enumerate(rows) if not r.needs_attention]
    assert attention_idxs and normal_idxs  # the mix genuinely contains both kinds
    assert max(attention_idxs) < min(normal_idxs)


def test_mcp_tool_retrieves_the_identically_ordered_data(monkeypatch):
    monkeypatch.setattr(identity, "is_logged_in", lambda: True)
    monkeypatch.setattr(identity, "token", lambda: "id-tok")
    monkeypatch.setattr(identity, "active_org", lambda: "acme")
    monkeypatch.setattr(identity, "api_base", lambda: "http://api.test")

    rows = order_jobs_for_view(_mixed_jobs(), now=NOW)
    backend_payload = {
        "jobs": [
            {"id": r.job.id, "state": r.job.state.value, "needsAttention": r.needs_attention}
            for r in rows
        ]
    }

    class _Resp:
        def json(self):
            return backend_payload

        def raise_for_status(self):
            pass

    monkeypatch.setattr(server.httpx, "get", lambda url, headers, timeout=None: _Resp())

    out = server.praxis_list_jobs()
    mcp_jobs = json.loads(out.split("```json", 1)[1].split("```", 1)[0])["jobs"]

    # The MCP tool's data is exactly the backend's ordering — same ids, same order.
    assert [j["id"] for j in mcp_jobs] == [j["id"] for j in backend_payload["jobs"]]
    attention_idxs = [i for i, j in enumerate(mcp_jobs) if j["needsAttention"]]
    normal_idxs = [i for i, j in enumerate(mcp_jobs) if not j["needsAttention"]]
    assert attention_idxs and normal_idxs
    assert max(attention_idxs) < min(normal_idxs)
