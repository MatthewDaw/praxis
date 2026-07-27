"""Mailbox (R28): the operator posts a message to a running remote job; delivery is by a
per-dispatch injected Stop hook (``agent_factory/hooks/mailbox_relay.py``) that surfaces every
still-pending message at the session's next ticket boundary, so af-build's own instructions
(``agent_factory/skills/af-build/SKILL.md``) and its always-on hook set
(``agent_factory/hooks/hooks.json``) are never touched.

``post_message`` is the one function a website handler and an MCP tool are both thin callers of
(mirrors ``box_service_resume.resume_job``'s single-function-of-truth shape — see that module's
docstring). ``dispatch_wiring`` is what ``box_service_session.launch_job_session`` passes to
``SessionLauncher.launch`` — ONLY when launching a job's session — so a local ``claude``
invocation, which never goes through that path, never receives the hook: the mailbox capability
is absent by construction, not gated by a conditional inside a hook that runs on every session.
"""

from __future__ import annotations

import json
from pathlib import Path

from knowledge.serve.box_service_models import Job

#: The mailbox file's fixed name inside a job's own worktree — R11 already gives every launched
#: job a dedicated ``worktree_path``; this rides on it rather than inventing a second storage path.
MAILBOX_FILENAME = ".af-build-mailbox.json"

#: The env var the injected hook (``mailbox_relay.py``) reads to find its job's mailbox file.
#: Absent entirely on a local run — which is what makes "no mailbox exists locally" true by
#: construction rather than by an in-hook venue check.
MAILBOX_ENV_VAR = "AF_JOB_MAILBOX_PATH"

#: The injected hook script's path — the single source of truth ``dispatch_wiring`` wires in.
HOOK_SCRIPT = str(Path(__file__).resolve().parents[2] / "agent_factory" / "hooks" / "mailbox_relay.py")


def mailbox_path(job: Job) -> Path:
    """The mailbox file address for ``job``. Raises if the job has no worktree yet — nothing
    dispatched has nowhere to deliver a message into."""
    if not job.worktree_path:
        raise ValueError(f"job {job.id!r} has no worktree_path — cannot address its mailbox")
    return Path(job.worktree_path) / MAILBOX_FILENAME


def post_message(job: Job, text: str) -> Job:
    """Post ``text`` to ``job``'s mailbox (the operator-facing action — callable identically from
    a website handler and an MCP tool, R28). Refuses an empty message; never touches SKILL.md or
    hooks.json."""
    if not text.strip():
        raise ValueError("cannot post an empty mailbox message")
    path = mailbox_path(job)
    pending = json.loads(path.read_text()) if path.exists() else []
    pending.append(text)
    path.write_text(json.dumps(pending))
    return job


def dispatch_wiring(job: Job) -> tuple[list[str], dict[str, str]]:
    """The per-dispatch ``(extra_args, env)`` pair ``box_service_session.launch_job_session``
    passes to ``SessionLauncher.launch`` when launching ``job``'s session — wires
    ``mailbox_relay.py`` as an additional Stop hook for THIS session only, via a ``--settings``
    payload distinct from (and never written into) ``agent_factory/hooks/hooks.json``'s shared,
    always-on hook set. A local invocation never calls this function, so it never receives either
    the flag or the env var — the capability is absent by construction on local runs.
    """
    settings = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f'${{PRAXIS_HOOK_PYTHON:-python3}} "{HOOK_SCRIPT}"',
                        }
                    ]
                }
            ]
        }
    }
    extra_args = ["--settings", json.dumps(settings)]
    env = {MAILBOX_ENV_VAR: str(mailbox_path(job))}
    return extra_args, env
