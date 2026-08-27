"""`create_run` refuses an unresolvable code_ref by NAMING what it could not resolve.

On 2026-08-27 a campaign lost a measured 13-minute vector baseline here: it passed the
logical name "sports_analysis" as `code_ref.repo`, `git -C sports_analysis` resolved that
against the caller's cwd and failed, and the refusal read only "code_ref sha does not exist
as a commit in its declared repo" -- naming neither the repo, the sha, nor the fact that the
path did not exist. Diagnosis is automatic; a refusal that does not name its cause costs a
whole cycle to re-derive.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from knowledge.ml_registry import Registry
from knowledge.ml_registry.storage import RegistryError


REPO = Path(__file__).resolve().parents[3]
SHA = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], check=True,
                     capture_output=True, text=True).stdout.strip()


def _create(registry: Registry, repo: str, sha: str) -> None:
    registry.create_experiment(
        experiment_id="campaign", spec_digest="d" * 64, stages=["representation"],
        metric="score", direction="maximize", win_condition={"metric_at_least": 0.9},
        rope=0.01, baseline_throughput=1.0,
    )
    registry.create_run(
        run_id="run-1", experiment_id="campaign", idea_id="idea-1", stage="representation",
        family="linear", params={}, metrics={},
        code_ref={"schema_version": 1, "repo": repo, "sha": sha, "base_sha": sha,
                  "diff_hash": "c" * 64, "diff_lines": 3},
        device_fingerprint="cpu:test", status="running", verdict=None, started_at=1.0,
        finished_at=None, claim_owner="worker", heartbeat_at=1.0,
    )


def test_a_repo_that_is_not_a_directory_says_so_and_names_the_path(tmp_path):
    registry = Registry(tmp_path)
    with pytest.raises(RegistryError) as excinfo:
        _create(registry, "sports_analysis", SHA)
    message = str(excinfo.value)
    assert "sports_analysis" in message
    assert SHA in message
    assert "not an existing directory" in message
    assert "not a repository name" in message


def test_a_real_repo_missing_the_commit_names_the_repo_and_the_sha(tmp_path):
    registry = Registry(tmp_path)
    missing = "b" * 40
    with pytest.raises(RegistryError) as excinfo:
        _create(registry, str(REPO), missing)
    message = str(excinfo.value)
    assert missing in message
    assert str(REPO) in message
    assert "no such commit" in message
