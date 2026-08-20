"""The portfolio CLI exposes lifecycle and lineage as stable JSON operations."""

from __future__ import annotations

import json

from knowledge.ml_registry.portfolio import CampaignStatus, Portfolio
from knowledge.ml_registry.portfolio_cli import (
    EXIT_MALFORMED_INPUT,
    EXIT_VALIDATION_ERROR,
    main,
)


def _invoke(capsys, *arguments):
    code = main(list(arguments))
    output = json.loads(capsys.readouterr().out)
    return code, output


def _add_artifact(capsys, path, artifact_id, model_id):
    return _invoke(
        capsys,
        "--file", str(path), "add-artifact",
        "--artifact-id", artifact_id,
        "--model-id", model_id,
        "--verdict", "adopted",
        "--dataset-hash", f"data-{artifact_id}",
        "--split-hash", f"split-{artifact_id}",
        "--prediction-hash", f"pred-{artifact_id}",
        "--coverage", "0.99",
    )


def _dependency(artifact_id, model_id):
    return json.dumps({
        "upstream_model_id": model_id,
        "artifact_id": artifact_id,
        "required_verdict": "adopted",
        "dataset_manifest_hash": f"data-{artifact_id}",
        "split_manifest_hash": f"split-{artifact_id}",
        "prediction_manifest_hash": f"pred-{artifact_id}",
        "minimum_coverage": 0.95,
    })


def test_init_and_show_emit_json_and_create_a_valid_file(tmp_path, capsys):
    path = tmp_path / "portfolio.json"

    code, initialized = _invoke(capsys, "--file", str(path), "init")
    show_code, shown = _invoke(capsys, "--file", str(path), "show")

    assert code == show_code == 0
    assert initialized == {"ok": True, "file": str(path), "schema_version": 2}
    assert shown["portfolio"]["campaigns"] == []
    assert shown["portfolio"]["artifacts"] == []
    assert Portfolio.load(path).path == path


def test_cli_drives_complete_campaign_lifecycle(tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    assert _add_artifact(capsys, path, "tracking-v1", "tracking")[0] == 0
    code, added = _invoke(
        capsys,
        "--file", str(path), "add-campaign",
        "--campaign-id", "possession-campaign",
        "--model-id", "possession",
        "--dependency", _dependency("tracking-v1", "tracking"),
    )
    assert code == 0
    assert added["campaign"]["status"] == "PLANNED"

    code, ready = _invoke(
        capsys, "--file", str(path), "readiness", "--campaign-id", "possession-campaign"
    )
    assert code == 0
    assert ready["readiness"] == {"activatable": True, "reasons": []}
    assert ready["campaign"]["status"] == "ACTIVATABLE"

    for command, expected in (("start-seeding", "SEEDING"), ("mark-ready", "READY")):
        code, result = _invoke(
            capsys, "--file", str(path), command, "--campaign-id", "possession-campaign"
        )
        assert code == 0
        assert result["campaign"]["status"] == expected
    assert Portfolio.load(path).campaigns["possession-campaign"].status == CampaignStatus.READY


def test_supersession_returns_affected_campaigns_and_persists_staleness(tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _add_artifact(capsys, path, "tracking-v1", "tracking")
    _add_artifact(capsys, path, "tracking-v2", "tracking")
    _invoke(
        capsys,
        "--file", str(path), "add-campaign",
        "--campaign-id", "possession-campaign",
        "--model-id", "possession",
        "--dependency", _dependency("tracking-v1", "tracking"),
    )

    code, result = _invoke(
        capsys,
        "--file", str(path), "supersede",
        "--artifact-id", "tracking-v1",
        "--replacement-id", "tracking-v2",
    )

    campaign = Portfolio.load(path).campaigns["possession-campaign"]
    assert code == 0
    assert result["affected_campaigns"] == ["possession-campaign"]
    assert campaign.stale
    assert campaign.status == CampaignStatus.BLOCKED


def test_validation_errors_have_distinct_exit_and_do_not_corrupt_file(tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    _add_artifact(capsys, path, "tracking-v1", "tracking")
    before = path.read_bytes()

    code, result = _add_artifact(capsys, path, "tracking-v1", "tracking")

    assert code == EXIT_VALIDATION_ERROR
    assert result["error"] == "validation"
    assert path.read_bytes() == before
    assert set(Portfolio.load(path).artifacts) == {"tracking-v1"}


def test_malformed_dependency_has_input_exit_and_json_error(tmp_path, capsys):
    path = tmp_path / "portfolio.json"

    code, result = _invoke(
        capsys,
        "--file", str(path), "add-campaign",
        "--campaign-id", "bad",
        "--model-id", "bad-model",
        "--dependency", "not-json",
    )

    assert code == EXIT_MALFORMED_INPUT
    assert result["error"] == "malformed_input"
    assert not path.exists()


def test_missing_file_is_a_validation_error_and_validate_reports_counts(tmp_path, capsys):
    path = tmp_path / "portfolio.json"
    code, missing = _invoke(capsys, "--file", str(path), "validate")
    assert code == EXIT_VALIDATION_ERROR
    assert missing["error"] == "validation"

    _add_artifact(capsys, path, "tracking-v1", "tracking")
    code, valid = _invoke(capsys, "--file", str(path), "validate")
    assert code == 0
    assert valid == {"ok": True, "campaign_count": 0, "artifact_count": 1}


def _dependency_json(artifact_id, model_id, **overrides):
    values = {
        "upstream_model_id": model_id,
        "artifact_id": artifact_id,
        "required_verdict": "adopted",
        "dataset_manifest_hash": f"data-{artifact_id}",
        "split_manifest_hash": f"split-{artifact_id}",
        "prediction_manifest_hash": f"pred-{artifact_id}",
        "minimum_coverage": 0.95,
    }
    values.update(overrides)
    return json.dumps(values)


def test_add_artifact_records_repeatable_input_artifact_lineage(capsys, tmp_path):
    path = tmp_path / "portfolio.json"
    _add_artifact(capsys, path, "a-fit", "a")
    _add_artifact(capsys, path, "a2-fit", "a2")

    code, output = _invoke(
        capsys,
        "--file", str(path), "add-artifact",
        "--artifact-id", "b-fit", "--model-id", "b",
        "--verdict", "adopted",
        "--dataset-hash", "data-b-fit", "--split-hash", "split-b-fit",
        "--prediction-hash", "pred-b-fit", "--coverage", "0.99",
        "--input-artifact", "a-fit", "--input-artifact", "a2-fit",
    )

    assert code == 0
    assert output["artifact"]["input_artifact_ids"] == ["a-fit", "a2-fit"]


def test_add_artifact_refuses_lineage_to_a_superseded_artifact(capsys, tmp_path):
    path = tmp_path / "portfolio.json"
    _add_artifact(capsys, path, "a-fit", "a")
    _add_artifact(capsys, path, "a-fit-2", "a")
    _invoke(capsys, "--file", str(path), "supersede",
            "--artifact-id", "a-fit", "--replacement-id", "a-fit-2")

    code, output = _invoke(
        capsys,
        "--file", str(path), "add-artifact",
        "--artifact-id", "b-fit", "--model-id", "b",
        "--verdict", "adopted",
        "--dataset-hash", "d", "--split-hash", "s",
        "--prediction-hash", "p", "--coverage", "0.99",
        "--input-artifact", "a-fit",
    )

    assert code == EXIT_VALIDATION_ERROR
    assert "superseded artifact 'a-fit'" in output["message"]


def test_repin_clears_stale_from_the_cli(capsys, tmp_path):
    path = tmp_path / "portfolio.json"
    _add_artifact(capsys, path, "a-fit", "a")
    _add_artifact(capsys, path, "a-fit-2", "a")
    _invoke(capsys, "--file", str(path), "add-campaign",
            "--campaign-id", "consumer", "--model-id", "c",
            "--dependency", _dependency_json("a-fit", "a"))
    _invoke(capsys, "--file", str(path), "supersede",
            "--artifact-id", "a-fit", "--replacement-id", "a-fit-2")

    stale_code, stale = _invoke(capsys, "--file", str(path), "show", "--campaign-id", "consumer")
    assert stale_code == 0 and stale["campaign"]["stale"] is True

    code, output = _invoke(capsys, "--file", str(path), "repin",
                           "--campaign-id", "consumer",
                           "--dependency", _dependency_json("a-fit-2", "a"))

    assert code == 0
    assert output["campaign"]["stale"] is False
    assert output["campaign"]["status"] == CampaignStatus.ACTIVATABLE.value
    assert Portfolio.load(path).campaigns["consumer"].stale is False


def test_repin_onto_a_still_superseded_dependency_is_refused(capsys, tmp_path):
    path = tmp_path / "portfolio.json"
    _add_artifact(capsys, path, "a-fit", "a")
    _add_artifact(capsys, path, "a-fit-2", "a")
    _invoke(capsys, "--file", str(path), "add-campaign",
            "--campaign-id", "consumer", "--model-id", "c",
            "--dependency", _dependency_json("a-fit", "a"))
    _invoke(capsys, "--file", str(path), "supersede",
            "--artifact-id", "a-fit", "--replacement-id", "a-fit-2")

    code, output = _invoke(capsys, "--file", str(path), "repin",
                           "--campaign-id", "consumer",
                           "--dependency", _dependency_json("a-fit", "a"))

    assert code == EXIT_VALIDATION_ERROR
    assert "cannot repin" in output["message"]
    assert Portfolio.load(path).campaigns["consumer"].stale is True


def test_malformed_persisted_portfolio_refuses_without_a_traceback(capsys, tmp_path):
    path = tmp_path / "portfolio.json"
    _add_artifact(capsys, path, "a-fit", "a")
    document = json.loads(path.read_text())
    document["artifacts"][0]["coverage"] = "abc"
    path.write_text(json.dumps(document))

    code, output = _invoke(capsys, "--file", str(path), "show")

    assert code == EXIT_VALIDATION_ERROR
    assert output["ok"] is False
    assert "coverage" in output["message"]


def test_pre_v2_portfolio_document_refuses_with_a_migration_message(capsys, tmp_path):
    path = tmp_path / "portfolio.json"
    _add_artifact(capsys, path, "a-fit", "a")
    document = json.loads(path.read_text())
    document["schema_version"] = 1
    path.write_text(json.dumps(document))

    code, output = _invoke(capsys, "--file", str(path), "show")

    assert code == EXIT_VALIDATION_ERROR
    assert "predates version 2" in output["message"]
    assert EXIT_MALFORMED_INPUT != code
