"""Per-dispatch launch configuration (R14): the flags/settings/env a dispatched job
session needs so a brand-new repo on a brand-new box requires **no per-repo or
machine-level configuration step** -- everything ``agent_factory/README.md``'s
"One-time setup" section otherwise asks an operator to do by hand (install the
plugin marketplace, install compound-engineering, edit ``~/.claude/settings.json``
to raise the Stop-hook block cap, wire Praxis MCP) is instead assembled here and
passed as CLI flags / ``--settings`` / subprocess env on every launch.

Permission mode is deliberately NOT assembled here -- it is R19's requirement and
is threaded through separately by whatever calls :func:`build_plugin_flags` et al.

Every function is pure (no I/O, no subprocess) so the whole seam is unit-testable
with no live nested ``claude`` CLI, the same contract ``session_launcher.py`` and
``dispatch.py`` hold themselves to.
"""

from __future__ import annotations

import time

#: compound-engineering is a hard plugin dependency (agent_factory/.claude-plugin/
#: plugin.json + marketplace.json) but ``--plugin-dir`` loads exactly one plugin
#: per flag with no marketplace-dependency resolution of its own, so it is fetched
#: as its own per-session plugin via ``--plugin-url`` -- no persistent local clone
#: (and so no machine-level setup step) is required on the box.
DEFAULT_COMPOUND_ENGINEERING_PLUGIN_URL = (
    "https://github.com/EveryInc/compound-engineering-plugin/archive/refs/heads/main.zip"
)

#: Claude Code's own built-in Stop-hook block cap defaults to 9 consecutive blocks
#: before it force-overrides the hook (agent_factory/README.md, "Raise the Stop-hook
#: block cap"). A real build legitimately blocks far more than 9 times while the
#: model iterates a ticket set, so per-dispatch settings raise it well above that.
DEFAULT_STOP_HOOK_BLOCK_CAP = "250"

#: The observation hook fired on every matched tool call (R14/R22 seam): advances a
#: last-activity timestamp file so external observation has an IN_DOMAIN heartbeat
#: to read (see ``observability_signals.py``'s IN_DOMAIN/OUT_OF_DOMAIN split).
_OBSERVATION_HOOK_COMMAND = (
    '${PRAXIS_HOOK_PYTHON:-python3} "${CLAUDE_PLUGIN_ROOT}/hooks/observation_activity.py"'
)


def build_plugin_flags(
    agent_factory_dir: str,
    *,
    compound_engineering_url: str = DEFAULT_COMPOUND_ENGINEERING_PLUGIN_URL,
) -> list[str]:
    """``--plugin-dir``/``--plugin-url`` flags resolving both the agent_factory
    plugin and its required compound-engineering dependency for this session only
    -- no ``/plugin marketplace add`` / ``/plugin install`` step, on this box or
    any other, is ever needed."""
    return [
        "--plugin-dir", agent_factory_dir,
        "--plugin-url", compound_engineering_url,
    ]


def build_praxis_mcp_config(
    *, org: str, api_key: str, base_url: str | None = None
) -> dict:
    """The ``--mcp-config`` payload wiring the Praxis MCP server (``knowledge.mcp``)
    with the dispatching org's credential -- so the session can complete a Praxis
    MCP tool call with no manual MCP setup on the box."""
    env = {"PRAXIS_ORG": org, "PRAXIS_API_KEY": api_key}
    if base_url:
        env["PRAXIS_API_BASE_URL"] = base_url
    return {
        "mcpServers": {
            "praxis": {
                "command": "uv",
                "args": ["run", "python", "-m", "knowledge.mcp"],
                "env": env,
            }
        }
    }


def build_dispatch_settings(
    *, stop_hook_block_cap: str = DEFAULT_STOP_HOOK_BLOCK_CAP
) -> dict:
    """The ``--settings`` payload: raises the Stop-hook block cap so the
    completeness gate can actually be enforced across a real build (rather than
    being force-overridden after the CLI's own default of 9 blocks), and wires the
    observation hook that advances the last-activity timestamp on every matched
    tool call."""
    return {
        "env": {"CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": stop_hook_block_cap},
        "hooks": {
            "PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": _OBSERVATION_HOOK_COMMAND}]}
            ]
        },
    }


def build_dispatch_env(
    *, project: str, org: str, api_key: str, base_url: str | None = None
) -> dict:
    """The subprocess env accompanying launch. ``FACTORY_PROJECT`` is set
    explicitly because a job's worktree directory name is a generated id, never
    the Praxis project name -- without it the completeness gate resolves the
    wrong ``prd-<project>`` and silently goes inert (factory-state-contract.md).
    ``PRAXIS_*`` mirror the MCP config's credential so the gate hooks (which
    reach Praxis directly, not through MCP) authenticate identically."""
    env = {"FACTORY_PROJECT": project, "PRAXIS_ORG": org, "PRAXIS_API_KEY": api_key}
    if base_url:
        env["PRAXIS_API_BASE_URL"] = base_url
    return env


def fire_observation_activity(activity_file: str, *, now: float | None = None) -> float:
    """Write the current (or injected ``now``) epoch time to ``activity_file``.

    This is what ``_OBSERVATION_HOOK_COMMAND`` runs on every matched harness hook
    event: a last-activity timestamp that advances purely from the harness firing
    the hook, independent of the session's own cooperation (R20/R70: this is an
    IN_DOMAIN, hook-fired signal, advisory rather than authoritative)."""
    ts = now if now is not None else time.time()
    with open(activity_file, "w") as fh:
        fh.write(repr(ts))
    return ts
