"""Executable read-only CLI for constrained promotion gates."""

from __future__ import annotations

import json

from knowledge.ml_registry.constraints_cli import main


def _run(capsys, contract: object, metrics: object) -> tuple[int, dict[str, object]]:
    code = main(
        [
            "evaluate-constraints",
            "--contract-json",
            json.dumps(contract),
            "--metrics-json",
            json.dumps(metrics),
        ]
    )
    return code, json.loads(capsys.readouterr().out)


def test_cli_passes_a_primary_metric_and_all_hard_gates(capsys):
    code, output = _run(
        capsys,
        {
            "primary_metric": "idf1",
            "primary_direction": "maximize",
            "constraints": [
                {"metric": "latency_ms", "direction": "minimize", "threshold": 20},
                {
                    "metric": "idf1",
                    "direction": "maximize",
                    "threshold": 0.7,
                    "scope": "worst_slice",
                    "slices": ["soccer", "basketball"],
                },
            ],
        },
        {
            "idf1": 0.84,
            "latency_ms": 18,
            "slices": {"soccer": {"idf1": 0.81}, "basketball": {"idf1": 0.75}},
        },
    )
    assert code == 0
    assert output["status"] == "passed"
    assert output["evidence"][1]["observed"] == [["basketball", 0.75]]


def test_cli_returns_one_for_a_real_measured_failure(capsys):
    code, output = _run(
        capsys,
        {
            "primary_metric": "f1",
            "primary_direction": "maximize",
            "constraints": [
                {"metric": "ece", "direction": "minimize", "threshold": 0.05}
            ],
        },
        {"f1": 0.82, "ece": 0.08},
    )
    assert code == 1
    assert output["status"] == "failed"
    assert output["evidence"][0]["passed"] is False


def test_cli_returns_two_when_required_slice_evidence_is_missing(capsys):
    code, output = _run(
        capsys,
        {
            "primary_metric": "f1",
            "primary_direction": "maximize",
            "constraints": [
                {
                    "metric": "f1",
                    "direction": "maximize",
                    "threshold": 0.7,
                    "scope": "each_slice",
                    "slices": ["soccer", "hockey"],
                }
            ],
        },
        {"f1": 0.82, "slices": {"soccer": {"f1": 0.8}}},
    )
    assert code == 2
    assert output["status"] == "refused"
    assert "hockey" in output["reasons"][0]


def test_cli_returns_json_refusal_for_invalid_contract(capsys):
    code, output = _run(
        capsys,
        {"primary_metric": "f1", "primary_direction": "sideways"},
        {"f1": 0.8},
    )
    assert code == 2
    assert output["status"] == "refused"
    assert output["field"] == "primary_direction"


def test_cli_accepts_json_files(tmp_path, capsys):
    contract = tmp_path / "contract.json"
    contract.write_text(
        json.dumps({"primary_metric": "f1", "primary_direction": "maximize"})
    )
    metrics = tmp_path / "metrics.json"
    metrics.write_text(json.dumps({"f1": 0.8}))
    code = main(
        [
            "evaluate-constraints",
            "--contract-json",
            str(contract),
            "--metrics-json",
            str(metrics),
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["primary_value"] == 0.8


def test_cli_help_documents_exit_codes(capsys):
    try:
        main(["evaluate-constraints", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    assert "exits 0 passed, 1 failed, or 2 refused/invalid" in capsys.readouterr().out
