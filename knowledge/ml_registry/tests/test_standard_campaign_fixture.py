from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE = "knowledge.ml_registry.testing.standard_campaign_fixture"


def test_generic_fixture_runs_standard_registry_lifecycle_end_to_end(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    result = subprocess.run(
        [sys.executable, "-m", MODULE, "--registry-root", str(root)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    summary = json.loads(result.stdout)
    assert summary["verdict"] == "adopted"
    assert summary["aliases"] == {"champion": 2, "production": 2}
    assert summary["model_version"] == 2
    assert summary["table_names"] == [
        "aliases",
        "artifacts",
        "events",
        "experiments",
        "lineage",
        "model_versions",
        "registered_models",
        "runs",
    ]
    assert summary["event_types"][-1] == "registry_finalized"
    assert not (root / "results.tsv").exists()


def test_generic_fixture_contains_no_project_or_legacy_registry_vocabulary() -> None:
    source = (REPO_ROOT / "knowledge/ml_registry/testing/standard_campaign_fixture.py").read_text()
    forbidden = (
        "sports_analysis",
        "stroke_lab",
        "results.tsv",
        "Promotion" + "Record",
        "Campaign" + "Artifact",
        "convergence" + "_run",
    )
    assert [term for term in forbidden if term in source] == []
