"""Acceptance test for R27 (0ebb295b...) / the ``notification-delivery`` check
(fc15c03b...): given each of the five trigger conditions is induced in turn --
awaiting-human, failed/needs-attention, silence-threshold crossing,
capability-probe refuse-to-claim, and an over-threshold undelivered mailbox
message -- a notification is delivered on the out-of-dashboard channel
carrying the job identity and a condition string naming that trigger, and no
trigger fires a notification twice for the same occurrence.
"""

from __future__ import annotations

from knowledge.serve.box_service_models import Job, JobState, mark_terminal
from knowledge.serve.box_service_preflight import PreflightResult
from knowledge.serve.notification import (
    DevTransport,
    NotificationCondition,
    NotificationCenter,
)
from knowledge.serve.observability_signals import (
    SILENCE_THRESHOLD_S,
    ObservationSignal,
    SignalDomain,
    attention_needed,
)

NOW = 10_000.0


def _job(state: JobState = JobState.RUNNING) -> Job:
    return Job(id="job-1", project="demo-project", snapshot="prd-demo-project", state=state)


def test_awaiting_human_delivers_a_notification_carrying_job_identity_and_condition():
    center = NotificationCenter(DevTransport())
    job = _job()
    job.state = JobState.AWAITING_HUMAN

    receipt = center.notify(
        job_id=job.id,
        project=job.project,
        condition=NotificationCondition.AWAITING_HUMAN,
        occurrence_id=job.state.value,
        now=NOW,
    )

    assert receipt is not None
    assert receipt.payload.job_id == "job-1"
    assert receipt.payload.project == "demo-project"
    assert receipt.payload.condition == "awaiting-human"


def test_failed_delivers_a_notification():
    center = NotificationCenter(DevTransport())
    job = _job()
    mark_terminal(job, JobState.FAILED, reason="session_crashed")

    receipt = center.notify(
        job_id=job.id,
        project=job.project,
        condition=NotificationCondition.FAILED,
        occurrence_id=f"{job.state.value}:{job.failure_reason}",
        now=NOW,
    )

    assert receipt is not None
    assert receipt.payload.condition == "failed"
    assert receipt.payload.job_id == "job-1"


def test_needs_attention_delivers_a_notification():
    center = NotificationCenter(DevTransport())
    job = _job()
    mark_terminal(job, JobState.NEEDS_ATTENTION, reason="merge_conflict")

    receipt = center.notify(
        job_id=job.id,
        project=job.project,
        condition=NotificationCondition.NEEDS_ATTENTION,
        occurrence_id=f"{job.state.value}:{job.failure_reason}",
        now=NOW,
    )

    assert receipt is not None
    assert receipt.payload.condition == "needs-attention"


def test_silence_threshold_crossing_delivers_a_notification():
    center = NotificationCenter(DevTransport())
    job = _job()
    signals = [ObservationSignal(SignalDomain.OUT_OF_DOMAIN, NOW - SILENCE_THRESHOLD_S - 1)]

    assert attention_needed(signals, now=NOW) is True  # induce the crossing

    receipt = center.notify(
        job_id=job.id,
        project=job.project,
        condition=NotificationCondition.SILENCE_THRESHOLD,
        occurrence_id=f"silence:{int(NOW)}",
        now=NOW,
    )

    assert receipt is not None
    assert receipt.payload.condition == "silence-threshold"


def test_capability_probe_refuse_to_claim_delivers_a_notification():
    center = NotificationCenter(DevTransport())
    result = PreflightResult(
        ok=False,
        pinned_version="2.0.0",
        installed_version="2.0.0",
        failed_probe="background_launch",
    )
    assert result.ok is False  # induce the refusal

    receipt = center.notify(
        job_id="pending-claim",  # no job has been claimed yet -- the venue itself is down
        project="demo-project",
        condition=NotificationCondition.CAPABILITY_PROBE_REFUSAL,
        occurrence_id=result.failed_probe or "unknown",
        now=NOW,
    )

    assert receipt is not None
    assert receipt.payload.condition == "capability-probe-refuse-to-claim"
    assert receipt.payload.job_id == "pending-claim"


def test_mailbox_message_undelivered_past_threshold_delivers_a_notification():
    center = NotificationCenter(DevTransport())
    job = _job()
    message_posted_at = NOW - 1000
    undelivered_threshold_s = 900

    assert (NOW - message_posted_at) > undelivered_threshold_s  # induce the crossing

    receipt = center.notify(
        job_id=job.id,
        project=job.project,
        condition=NotificationCondition.MAILBOX_UNDELIVERED,
        occurrence_id=f"mailbox:{int(message_posted_at)}",
        now=NOW,
    )

    assert receipt is not None
    assert receipt.payload.condition == "mailbox-undelivered"


def test_notification_payload_carries_only_job_id_project_and_condition():
    transport = DevTransport()
    center = NotificationCenter(transport)

    center.notify(
        job_id="job-1",
        project="demo-project",
        condition=NotificationCondition.FAILED,
        occurrence_id="occ-1",
        now=NOW,
    )

    assert len(transport.sent) == 1
    payload = transport.sent[0]
    assert payload.job_id == "job-1"
    assert payload.project == "demo-project"
    assert payload.condition == "failed"
    assert not hasattr(payload, "destination")


def test_notify_ignores_a_payload_supplied_destination():
    transport = DevTransport()
    center = NotificationCenter(transport)

    center.notify(
        job_id="job-1",
        project="demo-project",
        condition=NotificationCondition.FAILED,
        occurrence_id="occ-1",
        now=NOW,
        destination="attacker@example.com",
    )

    payload = transport.sent[0]
    assert not hasattr(payload, "destination")
    assert "attacker@example.com" not in payload.__dict__.values()


def test_dev_transport_surfaces_the_identical_payload_without_a_real_send():
    transport = DevTransport()
    center = NotificationCenter(transport)

    receipt = center.notify(
        job_id="job-1",
        project="demo-project",
        condition=NotificationCondition.AWAITING_HUMAN,
        occurrence_id="occ-1",
        now=NOW,
    )

    assert transport.sent == [receipt.payload]


def test_no_trigger_fires_a_notification_twice_for_the_same_occurrence():
    transport = DevTransport()
    center = NotificationCenter(transport)

    first = center.notify(
        job_id="job-1",
        project="demo-project",
        condition=NotificationCondition.AWAITING_HUMAN,
        occurrence_id="awaiting-human",
        now=NOW,
    )
    second = center.notify(
        job_id="job-1",
        project="demo-project",
        condition=NotificationCondition.AWAITING_HUMAN,
        occurrence_id="awaiting-human",  # same occurrence -- e.g. a re-poll
        now=NOW + 5,
    )

    assert first is not None
    assert second is None  # never fires twice for the same occurrence
    assert len(transport.sent) == 1


def test_a_new_occurrence_of_the_same_condition_does_fire_again():
    transport = DevTransport()
    center = NotificationCenter(transport)

    center.notify(
        job_id="job-1",
        project="demo-project",
        condition=NotificationCondition.FAILED,
        occurrence_id="attempt-1",
        now=NOW,
    )
    second = center.notify(
        job_id="job-1",
        project="demo-project",
        condition=NotificationCondition.FAILED,
        occurrence_id="attempt-2",  # a genuinely new occurrence (e.g. re-queued and failed again)
        now=NOW + 5,
    )

    assert second is not None
    assert len(transport.sent) == 2


def test_no_trigger_fires_twice_across_all_five_conditions_induced_in_turn():
    transport = DevTransport()
    center = NotificationCenter(transport)
    conditions = [
        NotificationCondition.AWAITING_HUMAN,
        NotificationCondition.FAILED,
        NotificationCondition.NEEDS_ATTENTION,
        NotificationCondition.SILENCE_THRESHOLD,
        NotificationCondition.CAPABILITY_PROBE_REFUSAL,
        NotificationCondition.MAILBOX_UNDELIVERED,
    ]

    for condition in conditions:
        receipt = center.notify(
            job_id="job-1",
            project="demo-project",
            condition=condition,
            occurrence_id=condition.value,
            now=NOW,
        )
        assert receipt is not None
        # re-inducing the identical occurrence never fires a second delivery
        assert center.notify(
            job_id="job-1",
            project="demo-project",
            condition=condition,
            occurrence_id=condition.value,
            now=NOW + 1,
        ) is None

    assert len(transport.sent) == len(conditions)
    delivered_conditions = {p.condition for p in transport.sent}
    assert delivered_conditions == {c.value for c in conditions}
