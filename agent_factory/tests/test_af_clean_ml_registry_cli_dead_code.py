from agent_factory.af_clean.findings import CLASS_CODE_DELETION, admit_finding
from agent_factory.af_clean.ml_registry_cli_dead_code import ALLOWLIST, LOCATIONS, WITNESSES, finding


def test_cli_dead_code_finding_is_located_and_admitted():
    candidate = finding()
    assert admit_finding(candidate).admitted
    assert candidate.change_class == CLASS_CODE_DELETION
    assert {(candidate.location.file, candidate.location.line)} == LOCATIONS


def test_cli_dead_code_driver_is_exactly_bounded_and_witnessed():
    assert ALLOWLIST == {
        "knowledge/ml_registry/cli/registry.py",
        "knowledge/ml_registry/tests/test_registry_cli_golden.py",
    }
    assert len(WITNESSES) == 2
    assert any(
        "knowledge/ml_registry/tests/test_registry_cli_golden.py" in witness.argv
        for witness in WITNESSES
    )
    assert any("knowledge/ml_registry/tests" in witness.argv for witness in WITNESSES)
