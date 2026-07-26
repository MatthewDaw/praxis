"""R29 acceptance / the ``control``-tagged ``resume-arms-completeness-gate`` check:

"A resumed job relaunches under the job-scoped owner id recorded on the job row, takes over the
prior run's ticket claims and run marker, and its completeness gate ARMS rather than going inert —
the single most expensive silent failure in this plan."

Two things are asserted together, because the requirement is precisely their combination:

1. ``box_service_resume.resume_job`` — the pure job-row decision logic (refuses a completed job or
   one with no recorded owner; otherwise relaunches and returns the job to ``running`` without ever
   reassigning ``run_owner``).
2. The relaunch identity actually arms ``agent_factory/hooks/build_completeness_gate.py``'s Stop-hook
   gate: a session started under the job's persisted owner id sees its prior ticket claim and run
   marker as its OWN (the gate's ``in_run`` condition), where a session that fell back to its own
   fresh CLI ``session_id`` — the failure mode this ticket exists to prevent — would not.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

_AGENT_FACTORY = Path(__file__).resolve().parents[3] / "agent_factory"
_SRC = str(_AGENT_FACTORY / "src")
_HOOKS = str(_AGENT_FACTORY / "hooks")
# ``_ticket_state`` imports ``agent_factory.resumability`` from the sibling ``src/`` tree, with its
# own same-directory fallback insert — but that fallback can lose a race against an already-cached
# namespace-package ``agent_factory`` bound only to the bare top-level dir (no ``resumability``
# module there) when a DIFFERENT test collected earlier in this same pytest session imported
# ``agent_factory.*`` first. Insert ``src`` ourselves, before ``_ticket_state`` ever imports, so the
# regular (``__init__.py``-bearing) package at ``agent_factory/src/agent_factory`` wins regardless of
# what else pytest has already collected.
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402
import build_completeness_gate as gate  # noqa: E402

from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.box_service_resume import (
    FACTORY_TICKET_OWNER_ENV,
    ResumeError,
    can_resume,
    resume_job,
)

JOB_OWNER = "af-build-remote-jobs:job-42"


def make_job(**overrides) -> Job:
    defaults = dict(
        id="job-42",
        project="af-build-remote-jobs",
        snapshot="prd-af-build-remote-jobs",
        state=JobState.FAILED,
        session_id="old-session-id",
        run_owner=JOB_OWNER,
    )
    defaults.update(overrides)
    return Job(**defaults)


# --------------------------------------------------------------------------- resume_job decision logic

@pytest.mark.parametrize(
    "state",
    [
        JobState.QUEUED,
        JobState.CLAIMED,
        JobState.RUNNING,
        JobState.AWAITING_HUMAN,
        JobState.NEEDS_ATTENTION,
        JobState.FAILED,
    ],
)
def test_every_non_completed_state_is_resumable(state):
    job = make_job(state=state)
    assert can_resume(job) is True

    resume_job(job, launch=lambda j: "new-session-id")

    assert job.state is JobState.RUNNING
    assert job.session_id == "new-session-id"
    assert job.resumable is False
    assert job.failure_reason is None


def test_completed_job_is_not_resumable():
    job = make_job(state=JobState.COMPLETED)
    assert can_resume(job) is False

    with pytest.raises(ResumeError):
        resume_job(job, launch=lambda j: pytest.fail("must not launch a completed job"))


def test_resume_refuses_a_job_with_no_recorded_owner():
    job = make_job(run_owner=None)

    with pytest.raises(ResumeError):
        resume_job(job, launch=lambda j: pytest.fail("must not launch with no owner to resume under"))


def test_resume_never_reassigns_the_job_scoped_owner_id():
    """The owner id is what stays fixed across a relaunch (R31) — resume must not mint a new one."""
    job = make_job()

    resume_job(job, launch=lambda j: "some-new-session-id")

    assert job.run_owner == JOB_OWNER


def test_launch_receives_the_job_so_the_caller_can_read_run_owner_off_it():
    """The real launcher (not this pure module) is the one that sets ``FACTORY_TICKET_OWNER`` in the
    relaunched process's environment; it must be handed the job row to read ``run_owner`` from."""
    job = make_job()
    seen = {}

    def launch(j):
        seen["run_owner"] = j.run_owner
        return "sid"

    resume_job(job, launch=launch)
    assert seen["run_owner"] == JOB_OWNER


# --------------------------------------------------------------------------- the gate actually arms

PLAN = ("af-build-remote-jobs", "prd-af-build-remote-jobs")


def _ticket_meta_from_prior_run(owner: str, *, run_at: float) -> dict:
    """A ticket left mid-build by the prior (now-dead) session: still ``in_progress`` under the
    job-scoped owner, its heartbeat and run marker stamped by that same owner."""
    return {
        ts.M_BUILD_STATE: "in_progress",
        ts.M_CLAIM_OWNER: owner,
        ts.M_CLAIM_AT: run_at,
        ts.M_CLAIM_HEARTBEAT_AT: run_at,
        ts.M_CLAIM_LEASE_TTL: ts.DEFAULT_LEASE_TTL_S,
        ts.M_RUN_OWNER: owner,
        ts.M_RUN_AT: run_at,
        ts.M_RUN_SCOPE: "ALL",
    }


@pytest.fixture(autouse=True)
def _clean_env():
    prior = os.environ.pop(FACTORY_TICKET_OWNER_ENV, None)
    yield
    if prior is None:
        os.environ.pop(FACTORY_TICKET_OWNER_ENV, None)
    else:
        os.environ[FACTORY_TICKET_OWNER_ENV] = prior


def test_relaunch_under_the_job_scoped_owner_arms_the_gate():
    """The fix: launching the resumed session with FACTORY_TICKET_OWNER=<job.run_owner> makes the
    gate's owner resolution return the SAME id the prior run's ticket claim and run marker carry,
    even though the CLI handed this new process an entirely different session_id."""
    job = make_job()
    stale_heartbeat = time.time() - (ts.DEFAULT_LEASE_TTL_S * 5)  # long stale — the prior session died
    ticket_meta = _ticket_meta_from_prior_run(job.run_owner, run_at=stale_heartbeat)

    resume_job(job, launch=lambda j: "brand-new-cli-session-id")
    os.environ[FACTORY_TICKET_OWNER_ENV] = job.run_owner

    hook_payload = {"session_id": "brand-new-cli-session-id"}
    owner = gate._session_owner(hook_payload)

    assert owner == job.run_owner
    assert owner != hook_payload["session_id"]
    # The gate's own arming predicate (build_completeness_gate.py's ARMING section): the ticket's
    # run marker belongs to this resolved owner. Staleness of the LEASE doesn't matter for arming —
    # only the run marker's own TTL does, and re-stamping/heartbeat on resume refreshes it; asserting
    # against a fresh run_at here isolates "does the identity match" from "is the marker fresh".
    fresh_ticket_meta = dict(ticket_meta, **{ts.M_RUN_AT: time.time()})
    assert fresh_ticket_meta[ts.M_RUN_OWNER] == owner
    assert ts.run_live(fresh_ticket_meta) is True


def test_without_the_owner_override_a_fresh_session_id_would_go_inert():
    """The regression this ticket closes: absent the FACTORY_TICKET_OWNER injection, a relaunched
    session's own fresh CLI session_id does NOT match the prior run's stamped owner, so neither the
    gate's live-claim nor its run-marker arming signal fires — it judges no build active and stands
    down, exactly the "ends immediately having built nothing" failure R29/R31 describe."""
    job = make_job()
    ticket_meta = _ticket_meta_from_prior_run(job.run_owner, run_at=time.time())

    resume_job(job, launch=lambda j: "brand-new-cli-session-id")
    # No FACTORY_TICKET_OWNER set — the gate falls back to the raw hook-supplied session_id.
    hook_payload = {"session_id": job.session_id}
    owner = gate._session_owner(hook_payload)

    assert owner != ticket_meta[ts.M_RUN_OWNER]
    in_run = bool(owner) and ticket_meta.get(ts.M_RUN_OWNER) == owner and ts.run_live(ticket_meta)
    assert in_run is False


def test_idempotent_claim_reclaims_the_stale_lease_once_the_owner_matches(monkeypatch):
    """Beyond the run marker, the prior ticket CLAIM itself must resolve back to this owner. Once the
    resumed session presents the same owner id, ``_ticket_state.claim``'s existing stale-lease-reclaim
    path (hooks/_ticket_state.py::claim) picks it up on its own — no bespoke "takeover" write needed."""

    class FakePraxis:
        def __init__(self, meta):
            self._meta = dict(meta)

        def get_fact(self, cid, *, space=None, snapshot=None):
            return {"id": cid, "meta": dict(self._meta)}

        def patch_meta(self, cid, meta_dict, *, space=None, snapshot=None):
            self._meta.update(meta_dict)
            return {"id": cid, "meta": dict(self._meta)}

    stale_heartbeat = time.time() - (ts.DEFAULT_LEASE_TTL_S * 5)
    fake = FakePraxis(_ticket_meta_from_prior_run(JOB_OWNER, run_at=stale_heartbeat))
    monkeypatch.setattr(ts, "_praxis", fake)

    assert ts.claim("R-ticket", JOB_OWNER, ref=PLAN) is True
    assert fake.get_fact("R-ticket")["meta"][ts.M_BUILD_STATE] == "in_progress"
    assert fake.get_fact("R-ticket")["meta"][ts.M_CLAIM_OWNER] == JOB_OWNER
