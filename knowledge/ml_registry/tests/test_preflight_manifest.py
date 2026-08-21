from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge.ml_registry.preflight import REFUSED, load_manifest, preflight


def _manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "campaigns": [
            {
                "name": "fixture-alpha",
                "space": "state/fixture-alpha/space.json",
                "model_id": "fixture-model",
                "ledger": "state/fixture-alpha/results.tsv",
                "corpus_probe": "print('OK fixture')",
                "arms_probe": "print('ARMS candidate')",
                "composing_module": "fixture_project.campaign",
                "dispatch": "python -m fixture_project.campaign",
                "known_nonmove_arms": ["incumbent"],
            }
        ],
        "refused": {"fixture-retired": "retired by project policy"},
    }


def test_loads_versioned_project_manifest(tmp_path: Path) -> None:
    path = tmp_path / "preflight.json"
    path.write_text(json.dumps(_manifest()))

    manifest = load_manifest(path)

    assert manifest.schema_version == 1
    assert tuple(manifest.campaigns) == ("fixture-alpha",)
    assert manifest.campaigns["fixture-alpha"].known_nonmove_arms == ("incumbent",)
    assert manifest.refused == {"fixture-retired": "retired by project policy"}


def test_rejects_unknown_manifest_version(tmp_path: Path) -> None:
    payload = _manifest()
    payload["schema_version"] = 2
    path = tmp_path / "preflight.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="schema_version must be 1"):
        load_manifest(path)


def test_project_refusal_is_injected(tmp_path: Path) -> None:
    path = tmp_path / "preflight.json"
    path.write_text(json.dumps(_manifest()))
    manifest = load_manifest(path)

    code, report = preflight(
        "fixture-retired",
        tmp_path,
        tmp_path,
        campaigns=manifest.campaigns,
        refused=manifest.refused,
    )

    assert code == REFUSED
    assert report.lines == [
        "PREFLIGHT fixture-retired REFUSAL FAIL campaign=fixture-retired "
        "detail=retired by project policy"
    ]
