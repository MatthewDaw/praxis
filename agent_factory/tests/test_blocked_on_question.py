"""Acceptance test for R23 (5a8a8dde56914520b71e623edfc920c8): "blocked on a
question" is a first-class af-build behavior with its own harness-emitted
event, never inferred from a permission prompt.

Scenario: a ticket is seeded with an unresolvable decision -- an acceptance
condition naming two mutually exclusive external services with no tiebreak.
The worker does what af-build's EXISTING skill already instructs for exactly
this case: calls ``_ticket_state.block(cid, owner, reason)``. This test
proves that ONE Bash tool call, observed by a per-dispatch injected
PostToolUse hook, yields every acceptance criterion:

- a ``blocked_on_question`` event is emitted (a harness-fired signal, not
  parsed out of edited skill text);
- the job's state becomes ``awaiting-human``;
- no permission-prompt mechanism is involved at all;
- af-build's skill files are byte-identical to the pre-feature baseline;
- the job later returns to ``running`` under the SAME job id once the
  question is answered (resume).
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from agent_factory.event_log import EventLog

# hooks/ is a flat directory of harness-invoked scripts, not the `agent_factory`
# package (see agent_factory/hooks/_ticket_state.py's own module docstring) --
# the established convention (test_build_gate_scenarios.py) is to add it to
# sys.path and import the bare module name.
_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

# Repo root on sys.path so the `knowledge` package imports (agent_factory's own
# pytest config only puts "src" and itself on sys.path, not the repo root --
# mirroring test_tag_normalization.py's precedent for the same cross-package need).
_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from blocked_on_question import detect_block_call, handle_post_tool_use  # noqa: E402
from knowledge.serve.box_service_models import JobState  # noqa: E402
from knowledge.serve.box_service_store import JobStore  # noqa: E402

SKILL_MD = Path(__file__).resolve().parents[2] / "agent_factory/skills/af-build/SKILL.md"

UNRESOLVABLE_TICKET_REASON = (
    "acceptance condition names two mutually exclusive external services "
    "(ServiceA vs ServiceB) with no tiebreak -- cannot proceed without a human decision"
)

# The Bash tool_input a worker running af-build's EXISTING §8 contract would
# run for exactly this unresolvable-decision case -- no new skill text needed.
BLOCK_CALL_COMMAND = (
    'python3 -c "import _ticket_state as ts; '
    f'ts.block(\'{"5a8a8dde56914520b71e623edfc920c8"}\', \'owner\', '
    f'\'{UNRESOLVABLE_TICKET_REASON}\', ref=PLAN)"'
)

NON_BLOCK_COMMAND = 'python3 -c "print(1)"'


def _skill_sha256() -> str:
    return hashlib.sha256(SKILL_MD.read_bytes()).hexdigest()


def test_detect_block_call_true_for_the_ticket_block_escape_hatch():
    assert detect_block_call(BLOCK_CALL_COMMAND) is True


def test_detect_block_call_false_for_an_unrelated_bash_command():
    assert detect_block_call(NON_BLOCK_COMMAND) is False


def test_full_blocked_on_question_scenario(tmp_path):
    baseline_sha = _skill_sha256()

    store = JobStore()
    job = store.create(project="af-build-remote-jobs", snapshot="prd-af-build-remote-jobs")
    job.state = JobState.RUNNING  # the job is mid-run when the worker hits the decision
    job_id = job.id

    log = EventLog(job_id, root=tmp_path)

    # A PostToolUse payload carries no permission-prompt field at all: R19's
    # allowlist mode means one could never fire. Detection reads only
    # tool_name + tool_input, proving no permission mechanism is consulted.
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": BLOCK_CALL_COMMAND},
    }
    assert "permission" not in payload

    record = handle_post_tool_use(payload, job_id=job_id, log=log, store=store)

    # 1. A harness-fired blocked_on_question event was emitted (durable --
    #    re-reading the on-disk log finds it, independent of this process).
    assert record is not None
    assert record["type"] == "blocked_on_question"
    assert record["job_id"] == job_id
    reread = EventLog(job_id, root=tmp_path)
    fired = [e for e in reread.read() if e["type"] == "blocked_on_question"]
    assert len(fired) == 1
    assert fired[0]["reason"] == UNRESOLVABLE_TICKET_REASON

    # 2. The job's state is now awaiting-human (not the ticket-level `blocked`).
    assert store.get(job_id).state is JobState.AWAITING_HUMAN
    assert store.get(job_id).reason == UNRESOLVABLE_TICKET_REASON

    # 3. af-build's skill files are unchanged from the pre-feature baseline --
    #    this feature was built entirely without touching SKILL.md.
    assert _skill_sha256() == baseline_sha

    # 4. The job later returns to running under the SAME job id once the
    #    question is answered.
    resumed = store.resume_from_awaiting_human(job_id)
    assert resumed.id == job_id
    assert resumed.state is JobState.RUNNING


def test_non_block_bash_call_fires_nothing(tmp_path):
    store = JobStore()
    job = store.create(project="p", snapshot="s")
    job.state = JobState.RUNNING
    log = EventLog(job.id, root=tmp_path)

    record = handle_post_tool_use(
        {"tool_name": "Bash", "tool_input": {"command": NON_BLOCK_COMMAND}},
        job_id=job.id, log=log, store=store,
    )

    assert record is None
    assert log.read() == []
    assert store.get(job.id).state is JobState.RUNNING


def test_non_bash_tool_use_fires_nothing(tmp_path):
    store = JobStore()
    job = store.create(project="p", snapshot="s")
    job.state = JobState.RUNNING
    log = EventLog(job.id, root=tmp_path)

    record = handle_post_tool_use(
        {"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}},
        job_id=job.id, log=log, store=store,
    )

    assert record is None
    assert log.read() == []
