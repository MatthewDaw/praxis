"""Acceptance test for ticket R28 (feb075399a6b49379102221c76e8010b):

given a posted message on a running remote job, the session surfaces it at its next ticket
boundary; given a local run, no mailbox exists and af-build's instruction text is byte-identical
to the unmodified skill.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from knowledge.serve.box_service_mailbox import (
    MAILBOX_ENV_VAR,
    HOOK_SCRIPT,
    dispatch_wiring,
    post_message,
)
from knowledge.serve.box_service_models import Job, JobState

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HOOKS_JSON = _REPO_ROOT / "agent_factory" / "hooks" / "hooks.json"
_SKILL_DIR = _REPO_ROOT / "agent_factory" / "skills" / "af-build"


def _make_job(worktree_path: str) -> Job:
    return Job(
        id="job-mbx-1",
        project="af-build-remote-jobs",
        snapshot="prd-af-build-remote-jobs",
        state=JobState.RUNNING,
        worktree_path=worktree_path,
    )


def test_posted_message_surfaces_at_the_next_ticket_boundary(tmp_path):
    job = _make_job(str(tmp_path))
    post_message(job, "please pause after this ticket")

    extra_args, env = dispatch_wiring(job)
    assert HOOK_SCRIPT in json.dumps(extra_args)  # the injected hook is wired into THIS launch

    proc = subprocess.run(
        [sys.executable, HOOK_SCRIPT],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0  # surfaces, never a hard block on the operator's own message
    assert "please pause after this ticket" in proc.stdout

    # delivered at most once — the mailbox drains on read
    proc2 = subprocess.run(
        [sys.executable, HOOK_SCRIPT], env={**os.environ, **env}, capture_output=True, text=True
    )
    assert proc2.returncode == 0
    assert proc2.stdout.strip() == ""


def test_local_run_has_no_mailbox_and_af_builds_instructions_are_untouched():
    # A local run never sets the per-dispatch env var this hook is wired against — invoked bare,
    # it allows the Stop immediately and prints nothing: no mailbox exists, by construction.
    env = {k: v for k, v in os.environ.items() if k != MAILBOX_ENV_VAR}
    proc = subprocess.run([sys.executable, HOOK_SCRIPT], env=env, capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "" and proc.stderr.strip() == ""

    # the shared hook set every session (local or remote) loads never references this hook — its
    # wiring is per-dispatch only (box_service_session.launch_job_session), never global
    assert "mailbox_relay" not in _HOOKS_JSON.read_text()

    # af-build's own instruction text is byte-identical to the unmodified skill
    baseline = (_SKILL_DIR / ".skill-baseline-sha256").read_text().strip()
    actual = hashlib.sha256((_SKILL_DIR / "SKILL.md").read_bytes()).hexdigest()
    # This guard fires on ANY edit to af-build's SKILL.md, including a perfectly legitimate one, and
    # it has now caught two of those and zero tampering attempts. That is the design — it cannot
    # tell intent — so the failure has to say what to do about it, or the next person meets a bare
    # hash mismatch in an unrelated test file and has to reverse-engineer the remedy.
    assert actual == baseline, (
        "af-build/SKILL.md changed but its baseline did not.\n"
        "If the edit was deliberate, re-stamp it:\n"
        "    shasum -a 256 agent_factory/skills/af-build/SKILL.md | cut -d' ' -f1 \\\n"
        "      > agent_factory/skills/af-build/.skill-baseline-sha256\n"
        "If you did NOT edit af-build's instructions, something else rewrote them — investigate."
    )
