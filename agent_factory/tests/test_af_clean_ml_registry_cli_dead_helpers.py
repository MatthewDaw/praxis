from agent_factory.af_clean.findings import CLASS_CODE_DELETION, admit_finding
from agent_factory.af_clean.ml_registry_cli_dead_helpers import (
    ALLOWLIST,
    LOCATIONS,
    WITNESSES,
    findings,
)


def test_dead_helper_findings_are_located_and_admitted():
    candidates = findings()
    assert len(candidates) == len(LOCATIONS) == 2
    assert all(admit_finding(candidate).admitted for candidate in candidates)
    assert all(candidate.change_class == CLASS_CODE_DELETION for candidate in candidates)
    assert {(candidate.location.file, candidate.location.line) for candidate in candidates} == LOCATIONS


def test_dead_helper_driver_is_source_only_and_has_cli_full_suite_and_ruff_witnesses():
    assert ALLOWLIST == {"knowledge/ml_registry/cli/registry.py"}
    assert len(WITNESSES) == 3
    joined = [" ".join(witness.argv) for witness in WITNESSES]
    assert any("test_registry_cli_golden.py" in command for command in joined)
    assert any("knowledge/ml_registry/tests -q" in command for command in joined)
    assert any("ruff check knowledge/ml_registry/cli/registry.py" in command for command in joined)
