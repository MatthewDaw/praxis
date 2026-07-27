"""The unsolicited-notification payload shape (R27/R79).

R27 delivers the operator a notification when a job enters awaiting-human or
failed, or crosses the silence threshold, carrying the job id, project, and
which condition fired -- and nothing else. R79 adds a ``question`` field to
the job (see ``box_service_models.Job``) for the blocked-on-question event's
text; :func:`build_notification_payload` is the single place that payload is
assembled, so the question staying out of it is a structural guarantee (an
explicit allowlist of three fields) rather than a filter someone could forget
to apply as the job model grows more fields later.
"""

from __future__ import annotations

from knowledge.serve.box_service_models import Job


def build_notification_payload(job: Job, condition: str) -> dict[str, str]:
    """The exact (and only) fields R27's outbound notification carries."""
    return {"job_id": job.id, "project": job.project, "condition": condition}
