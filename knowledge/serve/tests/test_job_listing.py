"""Pure unit tests for job_listing.order_by_attention / list_jobs_for_operator (R26).

No Postgres, no HTTP — the same invariant the app/MCP-facing tests re-check
end to end: attention-needing jobs (awaiting-human, failed, needs-attention,
or a claimed/running job whose heartbeat has gone silent past the threshold)
sort above progressing ones.
"""

from __future__ import annotations

from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.job_listing import (
    job_needs_attention,
    list_jobs_for_operator,
    order_by_attention,
)

NOW = 1_000_000.0


def _job(job_id, state, **extra) -> Job:
    return Job(id=job_id, project="p", snapshot="prd-p", state=state, **extra)


def test_always_attention_states():
    for state in (JobState.AWAITING_HUMAN, JobState.FAILED, JobState.NEEDS_ATTENTION):
        assert job_needs_attention(_job("j", state), now=NOW) is True


def test_progressing_states_without_stale_heartbeat():
    fresh = _job("j", JobState.RUNNING, claim_heartbeat_at=NOW - 10)
    assert job_needs_attention(fresh, now=NOW) is False
    assert job_needs_attention(_job("q", JobState.QUEUED), now=NOW) is False


def test_running_job_past_silence_threshold_needs_attention():
    stale = _job("j", JobState.RUNNING, claim_heartbeat_at=NOW - 1000)
    assert job_needs_attention(stale, now=NOW, silence_threshold_s=900) is True


def test_running_job_with_no_heartbeat_at_all_needs_attention():
    # No trustworthy (out-of-domain) signal at all -> treat as needing attention.
    assert job_needs_attention(_job("j", JobState.RUNNING), now=NOW) is True


def test_order_by_attention_puts_every_attention_job_above_every_progressing_one():
    jobs = [
        _job("progressing-1", JobState.RUNNING, claim_heartbeat_at=NOW),
        _job("attention-1", JobState.AWAITING_HUMAN),
        _job("progressing-2", JobState.QUEUED),
        _job("attention-2", JobState.FAILED),
        _job("progressing-3", JobState.CLAIMED, claim_heartbeat_at=NOW - 5),
        _job("attention-3", JobState.RUNNING, claim_heartbeat_at=NOW - 2000),
    ]
    ordered = order_by_attention(jobs, now=NOW, silence_threshold_s=900)
    ids = [j.id for j in ordered]

    attention_ids = {"attention-1", "attention-2", "attention-3"}
    progressing_ids = {"progressing-1", "progressing-2", "progressing-3"}
    last_attention_idx = max(ids.index(i) for i in attention_ids)
    first_progressing_idx = min(ids.index(i) for i in progressing_ids)
    assert last_attention_idx < first_progressing_idx
    assert set(ids) == attention_ids | progressing_ids


def test_list_jobs_for_operator_summary_shape_matches_order():
    jobs = [
        _job("running", JobState.RUNNING, claim_heartbeat_at=NOW),
        _job("failed", JobState.FAILED, failure_reason="boom"),
    ]
    summaries = list_jobs_for_operator(jobs, now=NOW)
    assert [s["id"] for s in summaries] == ["failed", "running"]
    assert summaries[0]["attentionNeeded"] is True
    assert summaries[0]["failureReason"] == "boom"
    assert summaries[1]["attentionNeeded"] is False
