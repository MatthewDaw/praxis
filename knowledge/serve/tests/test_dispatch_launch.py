"""Acceptance test for ticket R14 (004d37569e1742678f9f0f2a98decafa):

per-dispatch flags supply the agent_factory plugin directory and its required
compound-engineering plugin, the Praxis MCP configuration, the settings needed for
the completeness gate to be enforceable, and the observation hooks -- so a brand-new
repo on a brand-new box needs no per-repo or machine-level configuration step
(permission mode is R19's, and is not asserted here).

Covers, at the unit-testable seam (no live nested CLI, mirroring
``test_session_launcher_seam.py``'s own no-live-CLI contract):

  (a) ``--plugin-dir`` names the agent_factory plugin directory, and a second
      per-session plugin load resolves the compound-engineering dependency the
      plugin manifest declares -- so the af-build skill and its cold-eyes panel
      both resolve with zero pre-installed marketplace.
  (b) the Praxis MCP server is present in the ``--mcp-config`` payload, so the
      session can complete a Praxis MCP tool call with no manual MCP setup.
  (c) the assembled settings/env raise ``CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`` above
      the CLI's own documented default of 9 (see agent_factory/README.md "Raise
      the Stop-hook block cap"), so the completeness gate can actually block a
      real multi-ticket build without hitting the CLI's force-override.
  (d) the observation-hook wiring is present in settings, and firing it (the
      harness-invoked script itself) advances a last-activity timestamp.

``launch_job_session`` threads an optional dispatch config through to
``SessionLauncher.launch`` without changing its default (no-config) call shape --
``test_box_service_session.py`` and ``test_session_launcher_seam.py`` assert the
plain call is untouched.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.box_service_session import launch_job_session
from knowledge.serve.dispatch_launch import (
    DEFAULT_STOP_HOOK_BLOCK_CAP,
    build_dispatch_env,
    build_dispatch_settings,
    build_plugin_flags,
    build_praxis_mcp_config,
    fire_observation_activity,
)
from knowledge.serve.session_launcher import SessionLauncher


# ---------------------------------------------------------------------------
# (a) plugin directory + compound-engineering dependency resolution
# ---------------------------------------------------------------------------
def test_plugin_flags_name_agent_factory_dir_and_compound_engineering_url():
    args = build_plugin_flags(
        "/box/agent_factory", compound_engineering_url="https://example.test/ce.zip"
    )

    assert args == [
        "--plugin-dir", "/box/agent_factory",
        "--plugin-url", "https://example.test/ce.zip",
    ]


def test_plugin_flags_default_compound_engineering_url_is_the_real_dependency():
    args = build_plugin_flags("/box/agent_factory")

    assert "--plugin-url" in args
    url = args[args.index("--plugin-url") + 1]
    assert "compound-engineering-plugin" in url


# ---------------------------------------------------------------------------
# (b) Praxis MCP configuration
# ---------------------------------------------------------------------------
def test_praxis_mcp_config_carries_org_and_api_key_with_no_manual_setup():
    config = build_praxis_mcp_config(org="acme", api_key="key-123")

    server = config["mcpServers"]["praxis"]
    assert server["args"] == ["run", "python", "-m", "knowledge.mcp"]
    assert server["env"]["PRAXIS_ORG"] == "acme"
    assert server["env"]["PRAXIS_API_KEY"] == "key-123"


def test_praxis_mcp_config_includes_base_url_only_when_given():
    without = build_praxis_mcp_config(org="acme", api_key="key-123")
    assert "PRAXIS_API_BASE_URL" not in without["mcpServers"]["praxis"]["env"]

    with_url = build_praxis_mcp_config(org="acme", api_key="key-123", base_url="https://x.test")
    assert with_url["mcpServers"]["praxis"]["env"]["PRAXIS_API_BASE_URL"] == "https://x.test"


# ---------------------------------------------------------------------------
# (c) settings needed for the completeness gate to be enforceable
# ---------------------------------------------------------------------------
def test_default_stop_hook_block_cap_is_above_the_cli_default_of_nine():
    assert int(DEFAULT_STOP_HOOK_BLOCK_CAP) > 9


def test_dispatch_settings_raise_the_stop_hook_block_cap():
    settings = build_dispatch_settings()

    assert int(settings["env"]["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"]) > 9


def test_dispatch_env_carries_factory_project_and_praxis_credentials():
    env = build_dispatch_env(
        project="some-project", org="acme", api_key="key-123", base_url="https://x.test"
    )

    # FACTORY_PROJECT must be set explicitly: the job worktree's directory name is a
    # generated id, not the Praxis project name, so the gate would resolve the wrong
    # prd-<project> (and silently go inert) without it (factory-state-contract.md).
    assert env["FACTORY_PROJECT"] == "some-project"
    assert env["PRAXIS_ORG"] == "acme"
    assert env["PRAXIS_API_KEY"] == "key-123"
    assert env["PRAXIS_API_BASE_URL"] == "https://x.test"


# ---------------------------------------------------------------------------
# (d) observation hooks: wired in settings, and firing one advances activity
# ---------------------------------------------------------------------------
def test_dispatch_settings_wire_an_observation_hook():
    settings = build_dispatch_settings()

    hook_commands = [
        h["command"]
        for group in settings["hooks"]["PreToolUse"]
        for h in group["hooks"]
    ]
    assert any("observation_activity.py" in cmd for cmd in hook_commands)


def test_firing_the_observation_hook_advances_last_activity(tmp_path):
    activity_file = tmp_path / "activity"

    first = fire_observation_activity(str(activity_file), now=100.0)
    second = fire_observation_activity(str(activity_file), now=200.0)

    assert first == 100.0
    assert second == 200.0
    assert float(activity_file.read_text()) == 200.0


# ---------------------------------------------------------------------------
# launch_job_session threads dispatch flags/env through without disturbing the
# existing no-config call shape.
# ---------------------------------------------------------------------------
@dataclass
class FakeRunner:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    calls: list[dict] = field(default_factory=list)

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(
            args=args, returncode=self.returncode, stdout=self.stdout, stderr=self.stderr
        )


def test_launch_job_session_with_no_dispatch_config_is_unchanged():
    runner = FakeRunner(stdout="sess-1\n")
    launcher = SessionLauncher(runner=runner, cli="claude")
    job = Job(
        id="job-1", project="p", snapshot="s", state=JobState.CLAIMED,
        worktree_path="/repo/jobs/job-1",
    )

    launch_job_session(job, launcher)

    assert runner.calls == [
        {
            "args": ["claude", "--bg", "/af-build", "--name", "job-1"],
            "cwd": "/repo/jobs/job-1",
            "capture_output": True,
            "text": True,
            "check": False,
        }
    ]


def test_launch_job_session_threads_extra_args_and_env_when_given():
    runner = FakeRunner(stdout="sess-1\n")
    launcher = SessionLauncher(runner=runner, cli="claude")
    job = Job(
        id="job-1", project="p", snapshot="s", state=JobState.CLAIMED,
        worktree_path="/repo/jobs/job-1",
    )

    launch_job_session(
        job,
        launcher,
        extra_args=["--plugin-dir", "/box/agent_factory"],
        env={"FACTORY_PROJECT": "p"},
    )

    call = runner.calls[0]
    assert "--plugin-dir" in call["args"] and "/box/agent_factory" in call["args"]
    assert call["env"] == {"FACTORY_PROJECT": "p"}
