from __future__ import annotations

import re
from pathlib import Path
import subprocess
import sys

from knowledge.ml_registry.storage import Registry


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / "agent_factory/skills/af-ml-supervise/SKILL.md"
FIXTURE_MODULE = "knowledge.ml_registry.testing.standard_campaign_fixture"


def test_supervisor_skill_uses_canonical_registry_vocabulary_and_authorities() -> None:
    text = SKILL.read_text()
    required = (
        "experiments",
        "runs",
        "artifacts",
        "registered_models",
        "model_versions",
        "lineage",
        "aliases",
        "events",
        "champion",
        "production",
        "finalize",
        "code_ref",
        "runs/<run_id>",
    )
    assert [term for term in required if term not in text] == []
    assert "Praxis adjudication services are the only" in text
    assert "`finalize` is the only writer of `production`" in text


def test_supervisor_skill_has_no_project_paths_dead_links_or_retired_vocabulary() -> None:
    text = SKILL.read_text()
    forbidden = (
        "sports_analysis",
        "stroke_lab",
        "results.tsv",
        "PromotionRecord",
        "CampaignArtifact",
        "convergence_run",
        "supervise-campaign",
        "dispatch-script",
        "af-ml-campaign-loop.sh",
    )
    assert [term for term in forbidden if term in text] == []
    refs = set(re.findall(r"`((?:docs|agent_factory)/[^`]+\.md)`", text))
    missing = [ref for ref in refs if not (REPO_ROOT / ref).is_file()]
    assert missing == []


def test_supervisor_skill_preserves_idea_dependencies_and_commit_lifecycle() -> None:
    text = SKILL.read_text()
    assert "`depends_on` remains an IDEA-level scientific dependency" in text
    assert "`depends_on` unlocks only when each named IDEA was adopted" in text
    assert "arm/<experiment>/<idea>" in text
    assert "tag `runs/<run_id>`" in text
    assert "remove its toggle" in text
    assert "keep the branch only until" in text


def test_supervisor_skill_links_to_executable_generic_fixture() -> None:
    text = SKILL.read_text()
    assert FIXTURE_MODULE in text
    fixture = REPO_ROOT / "knowledge/ml_registry/testing/standard_campaign_fixture.py"
    assert fixture.is_file()
    assert "--registry-root /tmp/standard-campaign-fixture" in text


def test_documented_registry_commands_and_schema_are_real(tmp_path: Path) -> None:
    help_result = subprocess.run(
        [sys.executable, "-m", "knowledge.ml_registry.cli", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    commands = (
        "create-run",
        "complete-run",
        "adjudicate-run",
        "registry-status",
        "finalize",
        "claim-idea",
        "heartbeat-idea-claim",
        "export-runs",
    )
    assert [command for command in commands if command not in help_result.stdout] == []
    assert Registry(tmp_path / "registry").table_names() == (
        "aliases",
        "artifacts",
        "events",
        "experiments",
        "lineage",
        "model_versions",
        "registered_models",
        "runs",
    )
