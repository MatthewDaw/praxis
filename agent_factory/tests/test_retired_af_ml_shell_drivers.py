"""Regression guards for the retired pre-registry campaign control plane."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_ROOT = REPO_ROOT / "agent_factory" / "scripts"
RETIRED = (
    "af-ml-campaign-loop.sh",
    "af-ml-campaign-queue.sh",
    "af-ml-agent-queue.sh",
    "af-ml-supervise-keepalive.sh",
)
REMOVED_CONTROL_PLANE = ("campaign-status", "campaign-complete")


def test_retired_drivers_are_loud_exit_two_shims() -> None:
    for name in RETIRED:
        path = SCRIPT_ROOT / name
        assert os.access(path, os.X_OK), f"{name} must remain executable so stale callers fail loud"
        result = subprocess.run(
            [str(path)], capture_output=True, text=True, check=False, timeout=5,
        )
        assert result.returncode == 2
        assert "RETIRED" in result.stderr
        assert "knowledge.ml_registry.runtime.campaign_job" in result.stderr
        assert "af-ml-portfolio-launch.sh" in result.stderr
        assert result.stdout == ""


def test_no_shipped_executable_invokes_removed_campaign_control_plane() -> None:
    offenders: list[str] = []
    for path in SCRIPT_ROOT.iterdir():
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        executable_lines = "\n".join(
            line for line in path.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        found = [term for term in REMOVED_CONTROL_PLANE if term in executable_lines]
        if found:
            offenders.append(f"{path.name}: {', '.join(found)}")
    assert offenders == []


def test_seed_skill_refuses_legacy_launchers_and_requires_observed_campaign_job() -> None:
    skill = (REPO_ROOT / "agent_factory/skills/af-seed-ml-supervise/SKILL.md").read_text()
    for name in RETIRED:
        assert name in skill
    assert "retired refusal shims" in skill
    assert "observed spawning the configured canonical campaign job" in skill
